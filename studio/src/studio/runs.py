"""Runs: the runners that carry them, where each new one goes, and every event on its way to the store and the page.

Runners link in over a websocket (`voice_runner.link`): a hello (id, URL, capacity, agents), a stats
line every two seconds (ready, warming, stopping, calls), and each call's events. The studio is the
only writer of the store: each event goes to Redis (and Postgres behind it) and to every page
watching that run, in the order it came.

Starting a run (`Hub.start`): the agent's config on disk is versioned and kept (`studio.configs`),
the run is placed on the least busy runner that is ready and carries the agent, and the runner is
asked for the call. A run that carries on from a turn of an earlier one takes that turn's snapshot
from the store, with the state edited on the go-back page when it was. The browser's WebRTC offer and
a text run's turns pass through the studio to the runner that holds the call: the audio itself goes
straight from the browser to that runner.

The studio can also start a runner of its own (`RunnerProcess`), which `slng studio up` does.
"""

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import httpx
from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from loguru import logger
from pydantic import BaseModel

from studio import configs, discovery
from studio.settings import Settings
from studio.store import RunRow, Store

SUBSCRIBER_QUEUE = 5000
GONE_SECONDS = 10.0
"""A runner not heard from for this long is taken out of placement."""


class RunnerInfo(BaseModel):
    """One runner as the studio sees it: what it said in its hello, and its latest stats."""

    runner: str
    url: str
    pid: int = 0
    max_calls: int = 0
    agents: list[str] = []
    frameworks: list[str] = ['pipecat']
    ready: bool = False
    warming: bool = True
    stopping: bool = False
    active: int = 0
    calls: list[dict[str, Any]] = []
    lag_p50_ms: float | None = None
    lag_max_ms: float | None = None
    connected: bool = True
    seen: float = 0.0


class ResumeFrom(BaseModel):
    run: str
    turn: int


class StartRequest(BaseModel):
    """What the Agents page asks for: an agent, a channel, and where to start from."""

    agent: str
    channel: Literal['voice', 'text'] = 'text'
    caller_id: str = ''
    resume_from: ResumeFrom | None = None
    facts: dict[str, Any] | None = None
    """The state to carry on with, as edited on the go-back page."""
    delivery: Literal['marks', 'simulated'] = 'marks'


class Live:
    """A run this studio placed or heard from, while it lasts: its row, its runner, and who is watching."""

    def __init__(self, row: RunRow, runner_url: str) -> None:
        self.row = row
        self.runner_url = runner_url
        self.offer_url = ''
        self.watchers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.last_seq = -1


class Hub:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        agents: Callable[[], list[discovery.Agent]],
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.agents = agents
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5))
        self.runners: dict[str, RunnerInfo] = {}
        self.live: dict[str, Live] = {}
        self.events_in = 0

    # runners

    async def link(self, socket: WebSocket) -> None:
        """One runner's link, for as long as it stays: its hello, its stats, and its calls' events."""
        await socket.accept()
        name = ''
        try:
            while True:
                message = await socket.receive_json()
                if hello := message.get('hello'):
                    name = hello['runner']
                    self.runners[name] = RunnerInfo.model_validate(hello | {'seen': time.monotonic()})
                    logger.info('runner {} linked from {}', name, hello.get('url'))
                elif (stats := message.get('stats')) and name in self.runners:
                    known = self.runners[name].model_dump()
                    fields = {k: v for k, v in stats.items() if k in RunnerInfo.model_fields}
                    self.runners[name] = RunnerInfo.model_validate(
                        known | fields | {'seen': time.monotonic(), 'connected': True}
                    )
                elif event := message.get('event'):
                    await self.ingest(event)
        except WebSocketDisconnect:
            pass
        finally:
            if name in self.runners:
                self.runners[name] = self.runners[name].model_copy(
                    update={'connected': False, 'ready': False}
                )
                logger.warning('runner {} unlinked', name)

    def listed(self) -> list[RunnerInfo]:
        now = time.monotonic()
        return [
            r.model_copy(update={'connected': r.connected and now - r.seen < GONE_SECONDS})
            for r in sorted(self.runners.values(), key=lambda r: r.runner)
        ]

    def place(self, agent: str) -> RunnerInfo:
        """The least busy runner that is ready and carries this agent, or a 503 that says what to do.

        The call placed counts against its runner at once: runners report their load every 2 s, so
        calls started faster than that would otherwise all go to the one that looked idle. The next
        report replaces the count with the runner's own.
        """
        now = time.monotonic()
        ready = [
            r
            for r in self.runners.values()
            if r.connected and r.ready and now - r.seen < GONE_SECONDS and agent in r.agents
        ]
        if not ready:
            linked = [r for r in self.runners.values() if r.connected and now - r.seen < GONE_SECONDS]
            why = (
                'no runner is linked: `slng studio up` starts one, `slng studio runner` adds more'
                if not linked
                else f'no runner is ready for {agent!r}: '
                + ', '.join(
                    f'{r.runner} is {"warming" if r.warming else "stopping" if r.stopping else "full" if r.active >= r.max_calls else "without it"}'
                    for r in linked
                )
            )
            raise HTTPException(503, why)
        chosen = min(ready, key=lambda r: (r.active / max(r.max_calls, 1), r.runner))
        chosen.active += 1
        return chosen

    # runs

    def _agent(self, agent: str) -> discovery.Agent:
        for found in self.agents():
            if found.id == agent:
                if found.status != 'compiled':
                    raise HTTPException(409, f'{agent} does not compile: {found.error}')
                return found
        raise HTTPException(404, f'no agent {agent!r}')

    async def start(self, ask: StartRequest) -> dict[str, Any]:
        """Place a new run, keep its config version, and ask its runner for the call."""
        agent = self._agent(ask.agent)
        config = configs.current(agent.id, self.settings.root / agent.path, self.settings.root)
        await self.store.save_config(config)
        snapshot = None
        if ask.resume_from is not None:
            snapshot = await self.store.snapshot(ask.resume_from.run, ask.resume_from.turn)
            if snapshot is None:
                raise HTTPException(
                    404, f'no snapshot of {ask.resume_from.run} at turn {ask.resume_from.turn}'
                )
        runner = self.place(agent.id)
        run = f'run_{uuid4().hex[:10]}'
        row = RunRow(
            id=run,
            agent=agent.id,
            config=config.version,
            channel=ask.channel,
            started_at=datetime.now(UTC),
            forked_from=ask.resume_from.run if ask.resume_from else None,
            forked_turn=ask.resume_from.turn if ask.resume_from else None,
            runner=runner.runner,
        )
        live = Live(row, runner.url)
        self.live[run] = live
        body = {
            'agent': agent.id,
            'channel': ask.channel,
            'run': run,
            'config': config.version,
            'caller_id': ask.caller_id,
            'resume': snapshot,
            'facts': ask.facts,
            'delivery': ask.delivery,
        }
        try:
            answer = await self.http.post(f'{runner.url}/calls', json=body)
        except httpx.HTTPError as error:
            del self.live[run]
            raise HTTPException(502, f'runner {runner.runner} did not answer: {error}') from error
        if answer.status_code != 200:
            del self.live[run]
            detail = _detail(answer)
            raise HTTPException(answer.status_code, detail)
        await self.store.save_run(live.row)
        found = answer.json()
        live.offer_url = found.get('offer_url', '')
        reply: dict[str, Any] = {
            'run': run,
            'agent': agent.id,
            'channel': ask.channel,
            'config': config.version,
            'runner': runner.runner,
            'said': found.get('said', []),
            'step': found.get('step', ''),
            'turn': found.get('turn', 0),
        }
        if ask.channel == 'voice':
            reply |= {'offer': f'/api/runs/{run}/offer', 'ice_servers': found.get('ice_servers', [])}
        return reply

    def _live(self, run: str) -> Live:
        if (live := self.live.get(run)) is None:
            raise HTTPException(404, f'run {run} is not live on any runner this studio knows')
        return live

    async def offer(self, run: str, method: Literal['POST', 'PATCH'], body: dict[str, Any]) -> Any:
        live = self._live(run)
        if not live.offer_url:
            raise HTTPException(409, f'run {run} is not a voice call')
        answer = await self.http.request(method, live.offer_url, json=body)
        if answer.status_code != 200:
            raise HTTPException(answer.status_code, _detail(answer))
        return answer.json()

    async def turn(self, run: str, text: str) -> dict[str, Any]:
        live = self._live(run)
        answer = await self.http.post(f'{live.runner_url}/calls/{run}/turns', json={'text': text})
        if answer.status_code != 200:
            raise HTTPException(answer.status_code, _detail(answer))
        return answer.json()

    async def hang_up(self, run: str) -> dict[str, Any]:
        live = self._live(run)
        answer = await self.http.delete(f'{live.runner_url}/calls/{run}')
        if answer.status_code != 200:
            raise HTTPException(answer.status_code, _detail(answer))
        return answer.json()

    async def state_schema(self, agent: str) -> dict[str, Any]:
        """The agent's state type as JSON schema, from a runner that carries it (the studio imports no use case)."""
        runner = self.place(agent)
        answer = await self.http.get(f'{runner.url}/agents/{agent}/schema')
        if answer.status_code != 200:
            raise HTTPException(answer.status_code, _detail(answer))
        return answer.json()

    # events

    async def ingest(self, event: dict[str, Any]) -> None:
        """One event from a runner: into the store, onto its run's row, and out to every page watching the run."""
        self.events_in += 1
        run = str(event.get('run', ''))
        live = self.live.get(run)
        if live is None:
            known = await self.store.run(run)
            if known is not None and event.get('type') != 'run.ended':
                known = known.model_copy(update={'outcome': None, 'ended_at': None})
            live = self.live[run] = Live(known or self._row_from(event), '')
            await self.store.save_run(live.row)
        kind = event.get('type')
        if kind == 'turn.snapshot':
            await self.store.save_snapshot(run, int(event['turn']), event['snapshot'])
            shown = {k: v for k, v in event.items() if k != 'snapshot'}
        else:
            await self.store.add_event(run, event)
            shown = event
        live.last_seq = max(live.last_seq, int(event.get('seq', -1)))
        row = live.row
        changed = False
        if int(event.get('turn', 0)) > row.turns:
            row.turns, changed = int(event['turn']), True
        if kind == 'step.entered':
            row.step = str(event.get('step', ''))
        elif kind == 'run.started' and not row.step:
            row.step = str(event.get('entry', ''))
        elif kind == 'run.ended':
            row.ended_at, row.outcome, changed = datetime.now(UTC), str(event.get('outcome')), True
        if changed:
            await self.store.save_run(row)
        for queue in list(live.watchers):
            if queue.full():
                live.watchers.discard(queue)
            else:
                queue.put_nowait(shown)

    @staticmethod
    def _row_from(event: dict[str, Any]) -> RunRow:
        """A row for a run this studio did not place (it restarted while the run went on), from what the event says."""
        return RunRow(
            id=str(event.get('run', '')),
            agent=str(event.get('agent', '?')),
            config=str(event.get('config', '')),
            channel=str(event.get('channel', 'voice')),
            started_at=datetime.now(UTC),
            forked_from=event.get('forked_from'),
            forked_turn=event.get('forked_turn'),
        )

    async def watch(self, run: str, socket: WebSocket) -> None:
        """A page follows one run: every event so far, then each new one, until the run ends or the page goes."""
        await socket.accept()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE)
        live = self.live.get(run)
        if live is not None:
            live.watchers.add(queue)
        try:
            sent = -1
            for event in await self.store.events(run):
                await socket.send_json(event)
                sent = max(sent, int(event.get('seq', -1)))
            row = live.row if live is not None else await self.store.run(run)
            if row is None:
                await socket.send_json({'type': 'studio.missing', 'run': run})
                return
            if live is None or row.outcome is not None:
                await socket.send_json({'type': 'studio.replayed', 'run': run})
                if live is None:
                    return
            while True:
                event = await queue.get()
                if int(event.get('seq', -1)) <= sent and event.get('type') != 'turn.snapshot':
                    continue
                await socket.send_json(event)
                sent = max(sent, int(event.get('seq', -1)))
                if event.get('type') == 'run.ended':
                    await socket.send_json({'type': 'studio.replayed', 'run': run})
        except WebSocketDisconnect:
            pass
        finally:
            if live is not None:
                live.watchers.discard(queue)
            with contextlib.suppress(RuntimeError):
                await socket.close()

    async def runs(self, agent: str = '') -> list[RunRow]:
        rows = {r.id: r for r in await self.store.runs(agent)}
        for live in self.live.values():
            if not agent or live.row.agent == agent:
                rows[live.row.id] = live.row
        return sorted(rows.values(), key=lambda r: r.started_at, reverse=True)

    async def close(self) -> None:
        await self.http.aclose()


def _detail(answer: httpx.Response) -> Any:
    try:
        return answer.json().get('detail', answer.text)
    except ValueError:
        return answer.text


class RunnerProcess:
    """A runner the studio starts itself (`python -m voice_runner`), stopped with the studio, its calls let finish first."""

    def __init__(self, settings: Settings, port: int, log: Path) -> None:
        self.settings = settings
        self.port = port
        self.log = log
        self.process: subprocess.Popen[bytes] | None = None

    @staticmethod
    def available() -> bool:
        try:
            import voice_runner  # noqa: F401
        except ImportError:
            return False
        return True

    def start(self) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            'OTEL_EXPORTER_OTLP_ENDPOINT': self.settings.otlp_endpoint,
            'PYDANTIC_AI_NO_BANNER': '1',
        }
        argv = [
            sys.executable,
            '-m',
            'voice_runner',
            '--port',
            str(self.port),
            '--studio',
            f'ws://{self.settings.host}:{self.settings.port}/api/runners/link',
            '--root',
            str(self.settings.root),
        ]
        with self.log.open('ab') as out:
            self.process = subprocess.Popen(
                argv, env=env, stdout=out, stderr=subprocess.STDOUT, cwd=self.settings.root
            )

    @property
    def exited(self) -> int | None:
        return None if self.process is None else self.process.poll()

    def stop(self, wait: float = 20.0) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(wait)
        except subprocess.TimeoutExpired:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
