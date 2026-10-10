"""The start-up sequence: what the loading screen follows while the studio comes up.

The engine starts first, so the web page can show the rest: Docker, then Postgres and Redis
(compose's own health checks, `up --wait`) and Jaeger (asked over HTTP: its image has no shell for
a health check), then every agent read with `core.load`. Jaeger not answering does not stop the
studio: the step is skipped with what is missing (timings), since everything else still works. Each step
is waiting, starting, healthy, failed or skipped, and the page polls `/api/boot` until the
sequence is ready or a step failed. A failed step says what to do, in words a person can act on.
"""

import asyncio
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Sequence
from typing import Literal

from pydantic import BaseModel

from studio import discovery
from studio.settings import Settings

type Status = Literal['waiting', 'starting', 'healthy', 'failed', 'skipped']
type Runner = Callable[[Sequence[str], dict[str, str]], Awaitable[tuple[int, str]]]
type Probe = Callable[[str], bool]


class BootStep(BaseModel):
    id: str
    label: str
    detail: str
    status: Status = 'waiting'
    message: str = ''


class BootState(BaseModel):
    steps: list[BootStep]
    ready: bool
    failed: bool
    agents: list[str] = []


async def run(argv: Sequence[str], env: dict[str, str]) -> tuple[int, str]:
    """One command, its exit code and its combined output. A missing program is exit 127, as a shell says."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except FileNotFoundError:
        return 127, f'{argv[0]}: not found'
    out, _ = await process.communicate()
    return process.returncode or 0, out.decode(errors='replace').strip()


def answers(url: str) -> bool:
    """Whether anything answers 2xx or 3xx at `url` within a second."""
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status < 400
    except urllib.error.URLError, TimeoutError, ConnectionError:
        return False


class Boot:
    """One start-up, run once. `services=False` leaves Docker alone (`up --no-services`).

    `probe` asks Jaeger whether it is up, `patience` times with half a second between; tests pass both.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        services: bool = True,
        runner: Runner = run,
        probe: Probe = answers,
        patience: int = 40,
    ) -> None:
        self.settings = settings
        self.services = services
        self.runner = runner
        self.probe = probe
        self.patience = patience
        pg, redis = settings.pg_port, settings.redis_port
        self.steps = {
            step.id: step
            for step in (
                BootStep(id='docker', label='docker', detail='Docker with compose v2'),
                BootStep(
                    id='postgres', label='postgres:16', detail=f':{pg} · runs, turns, snapshots, configs'
                ),
                BootStep(id='redis', label='redis:7', detail=f':{redis} · live event stream per run'),
                BootStep(
                    id='traces',
                    label='jaeger',
                    detail=f':{settings.otlp_port} OTLP in · :{settings.jaeger_port} traces, timings per turn',
                ),
                BootStep(
                    id='engine',
                    label='engine',
                    detail=f':{settings.port}/api · runs turns on slng-voice-core',
                ),
                BootStep(id='agents', label='agents', detail=f'{settings.agents} · core.load, no model'),
                BootStep(id='web', label='web', detail=f':{settings.port} · this page'),
            )
        }
        self.agents: list[discovery.Agent] = []

    def state(self) -> BootState:
        steps = list(self.steps.values())
        return BootState(
            steps=steps,
            ready=all(step.status in ('healthy', 'skipped') for step in steps),
            failed=any(step.status == 'failed' for step in steps),
            agents=[agent.id for agent in self.agents],
        )

    def _set(self, step: str, status: Status, message: str = '') -> None:
        self.steps[step] = self.steps[step].model_copy(update={'status': status, 'message': message})

    async def start(self) -> BootState:
        self._set('engine', 'healthy', 'serving')
        self._set('web', *self._web())
        if self.services and not await self._containers():
            return self.state()
        if not self.services:
            for step in ('docker', 'postgres', 'redis', 'traces'):
                self._set(step, 'skipped', 'started with --no-services')
        await self._agents()
        return self.state()

    def _web(self) -> tuple[Status, str]:
        if (self.settings.web_dist / 'index.html').exists():
            return 'healthy', 'built'
        return 'skipped', 'not built: npm --prefix studio/web run build, or slng studio up --dev'

    async def _containers(self) -> bool:
        self._set('docker', 'starting')
        env = self.settings.compose_env
        code, out = await self.runner(['docker', 'compose', 'version', '--short'], env)
        if code != 0:
            self._set(
                'docker',
                'failed',
                'Docker with compose v2 is needed: install Docker Desktop or the compose plugin',
            )
            return False
        self._set('docker', 'healthy', f'compose {out.splitlines()[-1] if out else ""}'.strip())
        for step in ('postgres', 'redis', 'traces'):
            self._set(step, 'starting')
        compose = ['docker', 'compose', '-f', str(self.settings.compose_file), '-p', self.settings.project]
        code, out = await self.runner(
            [*compose, 'up', '--detach', '--wait', 'postgres', 'redis', 'jaeger'], env
        )
        if code != 0:
            last = out.splitlines()[-1] if out else f'exit {code}'
            for step in ('postgres', 'redis', 'traces'):
                self._set(step, 'failed', last)
            return False
        self._set('postgres', 'healthy', 'accepting connections')
        self._set('redis', 'healthy', 'ready')
        await self._traces()
        return True

    async def _traces(self) -> None:
        url = self.settings.jaeger_url
        for _ in range(self.patience):
            if await asyncio.to_thread(self.probe, url):
                self._set(
                    'traces',
                    'healthy',
                    f'collecting at {self.settings.otlp_endpoint}: start a bot with `slng studio bot <use case>`',
                )
                return
            await asyncio.sleep(0.5)
        self._set(
            'traces', 'skipped', f'Jaeger did not answer at {url}: turn and request timings will be missing'
        )

    async def _agents(self) -> None:
        self._set('agents', 'starting')
        self.agents = await asyncio.to_thread(discovery.find, self.settings.root, self.settings.agents)
        broken = [agent.id for agent in self.agents if agent.status == 'error']
        compiled = len(self.agents) - len(broken)
        message = f'{compiled} compiled' + (
            f', {len(broken)} with errors: {", ".join(broken)}' if broken else ''
        )
        self._set(
            'agents',
            'healthy' if self.agents else 'failed',
            message if self.agents else 'no agent.yaml found',
        )
