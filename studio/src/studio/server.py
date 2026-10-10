"""The engine's HTTP face: the API under /api, the runners' link, and the built web app at /.

Agents are read from disk on every request, so a manifest saved in an editor shows at once. Runs are
placed on runners and followed live (`studio.runs`), kept in the store (`studio.store`), and gone back
to from any turn's snapshot. Timings come from the local Jaeger (`studio.metrics`); when it does not
answer, `/api/metrics` says so with a 503 rather than showing no slow turns.

The browser's voice page is core's own client (`core/client`: Pipecat's SDK, with core's playback
reports), served at `/voice-sdk/`, so a call from the studio reports playback like a call from a
`bot.py`, and a read-back can be agreed to.
"""

import asyncio
import time
import urllib.error
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel

from studio import discovery, editor, metrics
from studio.boot import Boot, BootState, Status
from studio.events import EVENT
from studio.runs import Hub, RunnerInfo, RunnerProcess, StartRequest
from studio.settings import Settings
from studio.store import ConfigRow, MemoryStore, RunRow, ServiceStore, Store

RUNNER_WAIT = 180.0
"""How long the studio waits for its own runner to link and finish warming before the boot step fails."""


class TurnText(BaseModel):
    text: str


def create_app(
    settings: Settings,
    boot: Boot | None = None,
    fetch: metrics.Fetch = metrics.fetch_json,
    *,
    store: Store | None = None,
    runner: bool = False,
    hub: Hub | None = None,
) -> FastAPI:
    """The studio. `store` given (a test's `MemoryStore`) is used as is; otherwise the boot opens one.

    `runner=True` starts a runner of the studio's own once the agents are read (`slng studio up`).
    """
    holder: dict[str, Any] = {'store': store or MemoryStore()}
    process: list[RunnerProcess] = []

    def agents() -> list[discovery.Agent]:
        return discovery.find(settings.root, settings.agents)

    hub = hub or Hub(settings, holder['store'], agents)

    async def open_store() -> tuple[Status, str]:
        if store is not None:
            return 'healthy', f'{store.name}'
        found = ServiceStore(settings.redis_url, settings.database_url)
        try:
            await found.open()
        except Exception as error:
            logger.warning('the store did not open: {}', error)
            return (
                'skipped',
                f'Redis or Postgres did not answer ({error}): runs are kept in memory until the studio stops',
            )
        holder['store'] = hub.store = found
        closed = await found.close_unfinished()
        return 'healthy', 'Redis for 7 days, Postgres behind it' + (
            f' · {closed} run(s) left live by the last studio marked stopped' if closed else ''
        )

    async def start_runner() -> tuple[Status, str]:
        if not RunnerProcess.available():
            return 'skipped', 'the runner is not installed: uv sync --all-packages'
        started = RunnerProcess(
            settings, settings.runner_port, settings.data / f'runner-{settings.runner_port}.log'
        )
        started.start()
        process.append(started)
        deadline = time.monotonic() + RUNNER_WAIT
        while time.monotonic() < deadline:
            if (code := started.exited) is not None:
                return 'failed', f'the runner exited ({code}): see {started.log}'
            linked = [r for r in hub.listed() if r.connected]
            if any(r.ready for r in linked):
                ready = next(r for r in linked if r.ready)
                return (
                    'healthy',
                    f'{ready.runner} · {len(ready.agents)} agents · up to {ready.max_calls} calls',
                )
            await asyncio.sleep(0.5)
        return 'failed', f'the runner did not get ready in {RUNNER_WAIT:.0f} s: see {started.log}'

    boot = boot or Boot(settings)
    if boot.store is None and store is None:
        boot.store = open_store
    if runner and boot.runners is None:
        boot.runners = start_runner
    tasks: set[asyncio.Task[BootState]] = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(boot.start())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        try:
            yield
        finally:
            for started in process:
                await asyncio.to_thread(started.stop)
            await hub.close()
            await holder['store'].close()

    app = FastAPI(title='slng studio', lifespan=lifespan)
    app.state.hub = hub

    @app.get('/api/health')
    def health() -> dict[str, str]:
        return {'engine': 'ok', 'store': holder['store'].name}

    @app.get('/api/boot')
    def boot_state() -> BootState:
        return boot.state()

    @app.get('/api/agents', response_model_exclude={'__all__': {'graph'}})
    def list_agents() -> list[discovery.Agent]:
        return agents()

    def agent_or_404(agent_id: str) -> discovery.Agent:
        for found in agents():
            if found.id == agent_id:
                return found
        raise HTTPException(404, f'no agent {agent_id!r} under {settings.agents}')

    @app.get('/api/agents/{agent_id}')
    def agent(agent_id: str) -> discovery.Agent:
        return agent_or_404(agent_id)

    @app.get('/api/agents/{agent_id}/schema')
    async def state_schema(agent_id: str) -> dict[str, Any]:
        """The agent's state type as JSON schema, from a runner that carries it: the go-back form is built from it."""
        return await hub.state_schema(agent_id)

    # the editor (studio.editor): the config as the editor shows it, checked before it is saved

    @app.get('/api/agents/{agent_id}/config')
    def config(agent_id: str) -> editor.Config:
        found = agent_or_404(agent_id)
        return editor.read(settings.root / found.path, found.id, settings.root)

    @app.post('/api/agents/{agent_id}/config/check')
    def check_config(agent_id: str, edit: editor.Edit) -> editor.Checked:
        found = agent_or_404(agent_id)
        return editor.check(settings.root / found.path, edit)

    @app.put('/api/agents/{agent_id}/config')
    async def save_config(agent_id: str, edit: editor.Edit) -> editor.Saved:
        found = agent_or_404(agent_id)
        saved = editor.save(settings.root / found.path, edit, found.id, settings.root)
        if saved.ok:
            await holder['store'].save_config(saved.config)
        return saved

    @app.get('/api/agents/{agent_id}/receipts')
    async def last_receipts(agent_id: str) -> dict[str, dict[str, Any]]:
        """Each operation's latest successful receipt in this agent's recent runs: what the editor fills a closing line from."""
        found: dict[str, dict[str, Any]] = {}
        for row in (await hub.runs(agent_id))[:30]:
            for event in reversed(await holder['store'].events(row.id)):
                if (
                    event.get('type') == 'receipt'
                    and event.get('succeeded')
                    and event['operation'] not in found
                ):
                    found[event['operation']] = event['receipt']
        return found

    @app.get('/api/agents/{agent_id}/configs')
    async def versions(agent_id: str) -> list[ConfigRow]:
        return await holder['store'].configs(agent_id)

    # runners and runs (studio.runs)

    @app.websocket('/api/runners/link')
    async def runner_link(socket: WebSocket) -> None:
        await hub.link(socket)

    @app.get('/api/runners')
    def runners() -> list[RunnerInfo]:
        return hub.listed()

    @app.post('/api/runs')
    async def start_run(ask: StartRequest) -> dict[str, Any]:
        return await hub.start(ask)

    @app.get('/api/runs')
    async def runs(agent: str = '') -> list[RunRow]:
        return await hub.runs(agent)

    async def row_or_404(run: str) -> RunRow:
        if (live := hub.live.get(run)) is not None:
            return live.row
        if (row := await holder['store'].run(run)) is None:
            raise HTTPException(404, f'no run {run!r}')
        return row

    @app.get('/api/runs/{run}')
    async def one_run(run: str) -> dict[str, Any]:
        row = await row_or_404(run)
        live = hub.live.get(run)
        return {
            'run': row,
            'live': live is not None and row.outcome is None and bool(live.runner_url),
            'turns': await holder['store'].snapshot_turns(run),
        }

    @app.get('/api/runs/{run}/events')
    async def run_events(run: str) -> list[dict[str, Any]]:
        await row_or_404(run)
        return await holder['store'].events(run)

    @app.websocket('/api/runs/{run}/live')
    async def live(run: str, socket: WebSocket) -> None:
        await hub.watch(run, socket)

    @app.post('/api/runs/{run}/offer')
    async def offer(run: str, request: Request) -> Any:
        return await hub.offer(run, 'POST', await request.json())

    @app.patch('/api/runs/{run}/offer')
    async def candidates(run: str, request: Request) -> Any:
        return await hub.offer(run, 'PATCH', await request.json())

    @app.post('/api/runs/{run}/turns')
    async def text_turn(run: str, words: TurnText) -> dict[str, Any]:
        return await hub.turn(run, words.text)

    @app.delete('/api/runs/{run}')
    async def hang_up(run: str) -> dict[str, Any]:
        return await hub.hang_up(run)

    @app.get('/api/runs/{run}/turns/{turn}')
    async def go_back(run: str, turn: int) -> dict[str, Any]:
        """What going back to the end of this turn means: the state, what was said, and what stays done after it."""
        row = await row_or_404(run)
        snapshot = await holder['store'].snapshot(run, turn)
        if snapshot is None:
            raise HTTPException(404, f'no snapshot of {run} at turn {turn}')
        from core.snapshot import TurnSnapshot

        saved = TurnSnapshot.model_validate(snapshot)
        events = await holder['store'].events(run)
        after = [
            {'turn': e['turn'], 'operation': e['operation'], 'receipt': e['receipt']}
            for e in events
            if e.get('type') == 'receipt' and e['turn'] > turn and e.get('succeeded')
        ]
        return {
            'run': row,
            'turn': turn,
            'step': saved.agent,
            'pending': saved.pending,
            'consulting': saved.consulting,
            'facts': saved.facts,
            'said': saved.said(),
            'intents': [i.model_dump(mode='json') for i in saved.intents],
            'receipts': [e.receipt | {'operation': e.operation} for e in saved.evidence if e.succeeded],
            'done_after': after,
            'turns': await holder['store'].snapshot_turns(run),
        }

    # timings (studio.metrics)

    @app.get('/api/metrics/limits')
    def limits() -> metrics.Limits:
        return metrics.Limits()

    def jaeger_down(error: Exception) -> HTTPException:
        return HTTPException(
            503,
            f'Jaeger did not answer at {settings.jaeger_url} ({error}). Start the studio with its services, '
            f'then start a run from the Agents page, or a bot with `slng studio bot <use case>`.',
        )

    @app.get('/api/metrics')
    def recent(lookback: str = '1h', agent: str = '') -> metrics.Metrics:
        """The overview, for every agent or the one named: the agent filter lives here and only here."""
        try:
            return metrics.collect(settings.jaeger_url, agent=agent, lookback=lookback, fetch=fetch)
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as error:
            raise jaeger_down(error) from error

    @app.get('/api/metrics/runs/{trace}')
    def one_trace(trace: str) -> metrics.Metrics:
        """One run's view: its own trace, so it names its agent and needs no filter."""
        try:
            return metrics.collect_run(settings.jaeger_url, trace, fetch=fetch)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as error:
            raise jaeger_down(error) from error

    @app.get('/api/metrics/run/{run}')
    def by_run(run: str) -> metrics.Metrics:
        """A studio run's timings, found in Jaeger by its run id (`slng.run.id`)."""
        try:
            trace = metrics.trace_of_run(settings.jaeger_url, run, fetch=fetch)
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as error:
            raise jaeger_down(error) from error
        if not trace:
            raise HTTPException(
                404, f'Jaeger has no trace for {run} yet: spans arrive a few seconds after a turn'
            )
        return one_trace(trace)

    @app.get('/api/events/schema')
    def event_schema() -> dict[str, Any]:
        return EVENT.json_schema()

    app.mount('/voice-sdk', StaticFiles(directory=str(files('core').joinpath('client'))), name='voice-sdk')
    if (settings.web_dist / 'index.html').exists():
        app.mount('/', StaticFiles(directory=settings.web_dist, html=True), name='web')
    return app
