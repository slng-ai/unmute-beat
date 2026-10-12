"""The engine's HTTP face: the API under /api, the runners' link, and the built web app at /.

Agents are read from disk on every request, so a manifest saved in an editor shows at once. Runs are
placed on runners and followed live (`studio.runs.hub`), kept in the store (`studio.runs.store`), and
gone back to from any turn's snapshot. Timings come from the local Jaeger (`studio.metrics`); when it
does not answer, `/api/metrics` says so with a 503 rather than showing no slow turns.

The browser's voice page is core's own client (`core/client`: Pipecat's SDK, with core's playback
reports), served at `/voice-sdk/`, so a call from the studio reports playback like a call from a
`bot.py`, and a read-back can be agreed to.
"""

import asyncio
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger

from studio.agents.catalog import Agent, Catalog
from studio.api.agents import AgentRoutes
from studio.api.metrics import MetricsRoutes
from studio.api.runs import RunRoutes
from studio.boot import Boot, BootState, Status
from studio.events import EVENT
from studio.metrics import Fetch, Jaeger, MetricsReader
from studio.runs.hub import Hub
from studio.runs.process import RunnerProcess
from studio.runs.store import MemoryStore, ServiceStore, Store
from studio.settings import Settings


class Studio:
    """One studio: its store, its hub of runners, its start-up, its own runner, and the FastAPI app over them.

    A `store` given (a test's `MemoryStore`) is used as is; otherwise the boot opens Redis and Postgres,
    or keeps runs in memory when they do not answer. `runner=True` starts a runner of the studio's own
    once the agents are read (`slng studio up`). `RUNNER_WAIT` is how long the boot waits for it to link
    and finish warming. A runner imports an agent's code once, so after a fact is saved the studio renews
    its own runners (`renew_runners`): a new one first, then the old one drains its calls for at most
    `DRAIN_SECONDS` (the runner's own bound) and stops. `renewal` says how that stands, '' when idle.
    """

    RUNNER_WAIT = 180.0
    DRAIN_SECONDS = 300.0

    def __init__(
        self,
        settings: Settings,
        boot: Boot | None = None,
        fetch: Fetch | None = None,
        *,
        store: Store | None = None,
        runner: bool = False,
        hub: Hub | None = None,
    ) -> None:
        self.settings = settings
        self.given_store = store
        self.store: Store = store or MemoryStore()
        self.catalog = Catalog(settings.root, settings.agents)
        self.processes: list[RunnerProcess] = []
        self.renewing: asyncio.Task[None] | None = None
        self.renew_again = False
        self.renewal = ''
        self.hub = hub or Hub(settings, self.store, self.agents, stale=self.renew_runners)
        self.metrics = MetricsReader(Jaeger(settings.jaeger_url, fetch))
        self.boot = boot or Boot(settings)
        if self.boot.store is None and store is None:
            self.boot.store = self.open_store
        if runner and self.boot.runners is None:
            self.boot.runners = self.start_runner
        self.tasks: set[asyncio.Task[BootState]] = set()

    def agents(self) -> list[Agent]:
        """Every agent, read from disk now."""
        return self.catalog.agents()

    async def open_store(self) -> tuple[Status, str]:
        """Redis and Postgres, once the services are up; memory when they do not answer."""
        if self.given_store is not None:
            return 'healthy', self.given_store.name
        found = ServiceStore(self.settings.redis_url, self.settings.database_url)
        try:
            await found.open()
        except Exception as error:
            logger.warning('the store did not open: {}', error)
            return (
                'skipped',
                f'Redis or Postgres did not answer ({error}): runs are kept in memory until the studio stops',
            )
        self.store = self.hub.store = found
        closed = await found.close_unfinished()
        left = f' · {closed} run(s) left live by the last studio marked stopped' if closed else ''
        return 'healthy', f'Redis for 7 days, Postgres behind it{left}'

    async def start_runner(self) -> tuple[Status, str]:
        """The studio's own runner, started and waited for until it links and is ready."""
        if not RunnerProcess.available():
            return 'skipped', 'the runner is not installed: uv sync --all-packages'
        port = self.settings.runner_port
        started = RunnerProcess(self.settings, port, self.settings.data / f'runner-{port}.log')
        started.start()
        self.processes.append(started)
        deadline = time.monotonic() + self.RUNNER_WAIT
        while time.monotonic() < deadline:
            if (code := started.exited) is not None:
                return 'failed', f'the runner exited ({code}): see {started.log}'
            if ready := next((r for r in self.hub.listed() if r.connected and r.ready), None):
                return (
                    'healthy',
                    f'{ready.runner} · {len(ready.agents)} agents · up to {ready.max_calls} calls',
                )
            await asyncio.sleep(0.5)
        return 'failed', f'the runner did not get ready in {self.RUNNER_WAIT:.0f} s: see {started.log}'

    def renew_runners(self) -> str:
        """Start runners that carry the code on disk now, then let the old ones finish their calls and stop.

        Only the runners this studio started can be renewed; for one started by hand it says what to do. A
        save while a renewal is under way renews once more after it, since the new runner may have imported
        the code before that save.
        Returns what it is doing, in words the page shows.
        """
        owned = [process for process in self.processes if process.exited is None]
        if not owned:
            return 'no runner the studio started is running: restart yours (`slng studio runner`) to carry the new code'
        if self.renewing is None or self.renewing.done():
            self.renewing = asyncio.get_running_loop().create_task(self._renew(owned))
        else:
            self.renew_again = True
        return 'the studio is starting a runner with the new code; runs already going keep the old one until they end'

    async def _renew(self, old: list[RunnerProcess]) -> None:
        """Each old runner replaced: the new one ready first, then the old one drained and stopped."""
        for each in old:
            port = self._free_port()
            fresh = RunnerProcess(
                self.settings, port, self.settings.data / f'runner-{port}.log', max_calls=each.max_calls
            )
            fresh.start()
            self.processes.append(fresh)
            self.renewal = f'starting a runner on port {port} with the new code'
            if not await self._ready(fresh):
                self.renewal = f'the new runner did not get ready: see {fresh.log}'
                logger.warning('runner on port {} did not get ready; the old one keeps running', port)
                return
            self.renewal = (
                f'the runner on port {port} carries the new code; the old one is finishing its calls'
            )
            logger.info(
                'runner on port {} carries the new code; draining the one on port {}', port, each.port
            )
            await asyncio.to_thread(each.stop, self.DRAIN_SECONDS)
            self.processes.remove(each)
        if self.renew_again:
            self.renew_again = False
            await self._renew([process for process in self.processes if process.exited is None])
            return
        self.renewal = ''

    async def _ready(self, process: RunnerProcess) -> bool:
        """Whether a runner started here links and gets ready within `RUNNER_WAIT`."""
        deadline = time.monotonic() + self.RUNNER_WAIT
        while time.monotonic() < deadline and process.exited is None:
            pid = process.process.pid if process.process is not None else -1
            if any(r.pid == pid and r.connected and r.ready for r in self.hub.listed()):
                return True
            await asyncio.sleep(0.5)
        return False

    def _free_port(self) -> int:
        """The first port from the runner port up that no runner of this studio holds and nothing else listens on."""
        held = {process.port for process in self.processes if process.exited is None}
        port = self.settings.runner_port
        while True:
            if port not in held:
                with socket.socket() as probe:
                    try:
                        probe.bind((self.settings.host, port))
                        return port
                    except OSError:
                        pass
            port += 1

    @asynccontextmanager
    async def lifespan(self, _: FastAPI) -> AsyncIterator[None]:
        """Start the boot in the background; on the way out, stop the runner, the hub and the store."""
        task = asyncio.create_task(self.boot.start())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        try:
            yield
        finally:
            for started in self.processes:
                await asyncio.to_thread(started.stop)
            await self.hub.close()
            await self.store.close()

    def app(self) -> FastAPI:
        """The FastAPI app: health and boot, the agents, the runs, the timings, the voice page and the web app."""
        app = FastAPI(title='slng studio', lifespan=self.lifespan)
        app.state.hub = self.hub
        app.state.studio = self
        app.add_api_route('/api/health', self.health, methods=['GET'])
        app.add_api_route('/api/boot', self.boot.state, methods=['GET'])
        app.add_api_route('/api/events/schema', self.event_schema, methods=['GET'])
        app.include_router(AgentRoutes(self).router())
        app.include_router(RunRoutes(self).router())
        app.include_router(MetricsRoutes(self).router())
        app.mount(
            '/voice-sdk', StaticFiles(directory=str(files('core').joinpath('client'))), name='voice-sdk'
        )
        if (self.settings.web_dist / 'index.html').exists():
            app.mount('/', StaticFiles(directory=self.settings.web_dist, html=True), name='web')
        return app

    def health(self) -> dict[str, str]:
        """The engine is up, and which store it keeps runs in."""
        return {'engine': 'ok', 'store': self.store.name}

    @staticmethod
    def event_schema() -> dict[str, Any]:
        """The run format, as JSON schema."""
        return EVENT.json_schema()
