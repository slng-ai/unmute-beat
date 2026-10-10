"""The start-up sequence: what the loading screen follows while the studio comes up.

The engine starts first, so the web page can show the rest: Docker, then Postgres and Redis
(compose's own health checks, `up --wait`), then every agent read with `core.load`. Each step
is waiting, starting, healthy, failed or skipped, and the page polls `/api/boot` until the
sequence is ready or a step failed. A failed step says what to do, in words a person can act on.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Literal

from pydantic import BaseModel

from studio import discovery
from studio.settings import Settings

type Status = Literal['waiting', 'starting', 'healthy', 'failed', 'skipped']
type Runner = Callable[[Sequence[str], dict[str, str]], Awaitable[tuple[int, str]]]


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


class Boot:
    """One start-up, run once. `services=False` leaves Docker alone (`up --no-services`)."""

    def __init__(self, settings: Settings, *, services: bool = True, runner: Runner = run) -> None:
        self.settings = settings
        self.services = services
        self.runner = runner
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
            for step in ('docker', 'postgres', 'redis'):
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
        self._set('postgres', 'starting')
        self._set('redis', 'starting')
        compose = ['docker', 'compose', '-f', str(self.settings.compose_file), '-p', self.settings.project]
        code, out = await self.runner([*compose, 'up', '--detach', '--wait', 'postgres', 'redis'], env)
        if code != 0:
            last = out.splitlines()[-1] if out else f'exit {code}'
            for step in ('postgres', 'redis'):
                self._set(step, 'failed', last)
            return False
        self._set('postgres', 'healthy', 'accepting connections')
        self._set('redis', 'healthy', 'ready')
        return True

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
