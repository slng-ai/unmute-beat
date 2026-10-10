"""Run: uv run python -m tests.test_runner

The studio and two runners, all in this process on real sockets, the salon's flow on a scripted model:

* both runners link to the studio and say they are ready; calls are spread over them;
* a text run from the studio greets, takes turns, and every event reaches the studio's store in order,
  with a snapshot per turn; the Runs list filters by agent;
* going back to a turn carries on from its state, with an edit the salon's types accept; an edit they
  refuse opens nothing, and no run is listed for it;
* a runner told to stop takes no new calls (the studio places them on the other one) and finishes the
  call it has.

Scripted models prove runtime properties, never language understanding.
"""

import asyncio
import contextlib
import json
import os
import socket
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault('GOOGLE_API_KEY', 'offline-test-placeholder')
sys.path.insert(0, str(ROOT / 'use-cases' / 'salon'))

import core  # noqa: E402
from studio.boot import Boot  # noqa: E402
from studio.server import create_app as studio_app  # noqa: E402
from studio.settings import Settings  # noqa: E402
from studio.store import MemoryStore  # noqa: E402
from voice_runner.agents import Found  # noqa: E402
from voice_runner.app import Settings as RunnerSettings  # noqa: E402
from voice_runner.app import create_app as runner_app  # noqa: E402

MOVES: list[dict[str, Any]] = []
"""What the scripted model answers next, one reply each; 'Sure.' once they run out."""


async def scripted(messages: list[Any], info: AgentInfo):
    move = MOVES.pop(0) if MOVES else {'say': 'Sure.'}
    yield {0: DeltaToolCall(name='reply', json_args=json.dumps(move))}


def salon_entry() -> core.Entry:
    from salon import app  # ty: ignore[unresolved-import]
    from salon.entry import ENTRY  # ty: ignore[unresolved-import]

    flow = app.SPEC.compile(model=FunctionModel(stream_function=scripted))
    return replace(ENTRY, flow=lambda: flow)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


async def serve(app: Any, port: int) -> tuple[uvicorn.Server, asyncio.Task]:
    server = uvicorn.Server(
        uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning', lifespan='on')
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    return server, task


async def until(check, what: str, seconds: float = 20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if found := await check():
            return found
        await asyncio.sleep(0.1)
    raise AssertionError(f'never: {what}')


async def main() -> None:
    studio_port = free_port()
    settings = replace(
        Settings.from_env(), root=ROOT, agents='use-cases/salon/salon/agent.yaml', port=studio_port
    )
    studio = studio_app(settings, Boot(settings, services=False), store=MemoryStore())
    servers = [await serve(studio, studio_port)]
    entry = salon_entry()
    runners = []
    for name in ('runner-a', 'runner-b'):
        port = free_port()
        found = [Found(ROOT / 'use-cases' / 'salon', 'salon', entry)]
        rs = RunnerSettings(
            port=port,
            studio=f'ws://127.0.0.1:{studio_port}/api/runners/link',
            max_calls=2,
            warm=False,
            runner_id=name,
        )
        app = runner_app(rs, found, signals=False)
        runners.append(app.state.runner)
        servers.append(await serve(app, port))
    api = httpx.AsyncClient(base_url=f'http://127.0.0.1:{studio_port}', timeout=30)
    try:
        await scenario(api, runners)
    finally:
        await api.aclose()
        for server, task in reversed(servers):
            server.should_exit = True
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def scenario(api: httpx.AsyncClient, runners: list[Any]) -> None:
    async def both_ready():
        listed = (await api.get('/api/runners')).json()
        return len([r for r in listed if r['ready']]) == 2 and listed

    listed = await until(both_ready, 'both runners linked and ready')
    assert {r['runner'] for r in listed} == {'runner-a', 'runner-b'} and all(
        r['agents'] == ['sage-and-stone'] for r in listed
    )

    started = (await api.post('/api/runs', json={'agent': 'sage-and-stone', 'channel': 'text'})).json()
    run = started['run']
    assert started['said'] == ["Hi, you've reached Sage and Stone. Robin speaking, what can I do for you?"], (
        started
    )
    assert started['config'] and started['step'] == 'concierge', started

    MOVES.append({'say': 'A haircut, lovely.', 'update': {'service': 'haircut'}})
    first = (await api.post(f'/api/runs/{run}/turns', json={'text': 'A haircut, please.'})).json()
    assert first['said'] == ['A haircut, lovely.'] and first['turn'] == 1, first
    second = (await api.post(f'/api/runs/{run}/turns', json={'text': 'Is it far?'})).json()
    assert second['said'] == ['Sure.'] and second['turn'] == 2, second

    async def two_snapshots():
        found = (await api.get(f'/api/runs/{run}')).json()
        return found['turns'] == [1, 2] and found

    held = await until(two_snapshots, 'a snapshot per turn in the store')
    assert held['live'] and held['run']['agent'] == 'sage-and-stone' and held['run']['turns'] == 2
    events = (await api.get(f'/api/runs/{run}/events')).json()
    seqs = [e['seq'] for e in events]
    assert seqs == sorted(set(seqs)), 'in order, each once (snapshots are numbered too, and kept apart)'
    types = [e['type'] for e in events]
    assert types[0] == 'run.started' and events[0]['config'] == started['config']
    for kind in (
        'caller.speech',
        'model.request',
        'model.decision',
        'model.ended',
        'state.committed',
        'bot.speech',
        'turn.finished',
    ):
        assert kind in types, (kind, types)
    assert 'turn.snapshot' not in types, 'snapshots are kept apart from the event stream'
    assert [r['id'] for r in (await api.get('/api/runs?agent=sage-and-stone')).json()] == [run]
    assert (await api.get('/api/runs?agent=faro')).json() == [], 'filtered by agent'

    back = (await api.get(f'/api/runs/{run}/turns/1')).json()
    assert back['facts']['service'] == 'haircut' and back['step'] == 'concierge', back
    assert [line['who'] for line in back['said']] == ['bot', 'caller', 'bot'], back['said']

    edited = back['facts'] | {'service': 'blowout'}
    resumed = (
        await api.post(
            '/api/runs',
            json={
                'agent': 'sage-and-stone',
                'channel': 'text',
                'resume_from': {'run': run, 'turn': 1},
                'facts': edited,
            },
        )
    ).json()
    assert resumed['said'] == [] and resumed['turn'] == 1, (
        'a resumed call says nothing first: it is under way'
    )
    again = (await api.post(f'/api/runs/{resumed["run"]}/turns', json={'text': 'And the price?'})).json()
    assert again['turn'] == 2, again

    async def resumed_snapshot():
        found = (await api.get(f'/api/runs/{resumed["run"]}')).json()
        return 2 in found['turns'] and found

    row = (await until(resumed_snapshot, 'the resumed run snapshotted'))['run']
    assert (row['forked_from'], row['forked_turn']) == (run, 1), row
    assert (await api.get(f'/api/runs/{resumed["run"]}/turns/2')).json()['facts']['service'] == 'blowout'

    refused = await api.post(
        '/api/runs',
        json={
            'agent': 'sage-and-stone',
            'channel': 'text',
            'resume_from': {'run': run, 'turn': 1},
            'facts': back['facts'] | {'booking_day': '2020-01-01'},
        },
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()['detail']['refused'][0]['loc'] == ['booking_day'], refused.json()
    assert len((await api.get('/api/runs?agent=sage-and-stone')).json()) == 2, (
        'nothing opened for a refused edit'
    )

    holder = next(r for r in runners if run in r.calls)
    other = next(r for r in runners if r is not holder)
    draining = asyncio.create_task(holder.drain())

    async def stopping_seen():
        listed = (await api.get('/api/runners')).json()
        return any(r['runner'] == holder.settings.runner_id and r['stopping'] for r in listed)

    await until(stopping_seen, 'the studio heard the runner is stopping')
    placed = (await api.post('/api/runs', json={'agent': 'sage-and-stone', 'channel': 'text'})).json()
    assert placed['runner'] == other.settings.runner_id, 'a stopping runner takes no new calls'
    still = (await api.post(f'/api/runs/{run}/turns', json={'text': 'Still there?'})).json()
    assert still['turn'] == 3, 'the call it has goes on while it stops'
    assert not draining.done()
    for open_run in (run, resumed['run'], placed['run']):
        await api.delete(f'/api/runs/{open_run}')
    await asyncio.wait_for(draining, 5)

    async def ended():
        found = (await api.get(f'/api/runs/{run}')).json()
        return found['run']['outcome'] == 'hangup' and found

    await until(ended, 'the run ended as a hang-up')


async def a_saved_config_reaches_the_next_call():
    """A run naming a new config version is carried on the manifest as saved; one already running keeps its flow.

    A save between a runner's start and its first call counts too: the files, not the version heard, say so.
    """
    import shutil
    import tempfile

    from voice_runner.app import Runner

    folder = Path(tempfile.mkdtemp()) / 'salon'
    shutil.copytree(
        ROOT / 'use-cases' / 'salon' / 'salon', folder, ignore=shutil.ignore_patterns('__pycache__')
    )
    model = FunctionModel(stream_function=scripted)
    entry = replace(
        salon_entry(), manifest=folder / 'agent.yaml', compile=lambda spec: spec.compile(model=model)
    )
    runner = Runner(RunnerSettings(warm=False), [Found(folder.parent, 'salon', entry)])
    runner.flows[entry.id] = first = entry.recompile()
    assert await runner.flow_for(entry.id, 'v1') is first, (
        'the first version heard of is the one compiled at start'
    )
    manifest = folder / 'agent.yaml'
    manifest.write_text(
        manifest.read_text().replace(
            '  book_appointment: operations.py:BookAppointment\n',
            '  book_appointment: {class: "operations.py:BookAppointment", say: "Booked: {spoken}."}\n',
        )
    )
    assert await runner.flow_for(entry.id, 'v1') is first, 'the same version is not compiled again'
    second = await runner.flow_for(entry.id, 'v2')
    book = next(op for op in second.operations() if op.name == 'book_appointment')
    assert second is not first and book.say == 'Booked: {spoken}.', book.say
    assert next(op for op in first.operations() if op.name == 'book_appointment').say != book.say

    late = Runner(RunnerSettings(warm=False), [Found(folder.parent, 'salon', entry)])
    late.flows[entry.id] = started = entry.recompile()
    manifest.write_text(manifest.read_text().replace('say: "Booked: {spoken}."', 'say: "All set: {spoken}."'))
    heard = await late.flow_for(entry.id, 'v3')
    book = next(op for op in heard.operations() if op.name == 'book_appointment')
    assert heard is not started and book.say == 'All set: {spoken}.', (
        'saved after the runner started, before its first call: the first version it hears is new too'
    )


if __name__ == '__main__':
    asyncio.run(main())
    asyncio.run(a_saved_config_reaches_the_next_call())
    print(
        'runner ok: two runners linked, runs placed and stored, gone back with an edit, a runner stopping, a saved config'
    )
