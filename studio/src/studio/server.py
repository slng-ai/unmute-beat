"""The engine's HTTP face: the API under /api, and the built web app at /.

Agents are read from disk on every request, so a manifest saved in an editor shows at once.
Runs are not recorded yet: `/api/runs` says so with a 501 the page shows, rather than an empty
list that would read as "no calls happened".
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from studio import discovery
from studio.boot import Boot, BootState
from studio.events import EVENT
from studio.settings import Settings


def create_app(settings: Settings, boot: Boot | None = None) -> FastAPI:
    boot = boot or Boot(settings)
    tasks: set[asyncio.Task[BootState]] = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(boot.start())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        yield

    app = FastAPI(title='slng studio', lifespan=lifespan)

    @app.get('/api/health')
    def health() -> dict[str, str]:
        return {'engine': 'ok'}

    @app.get('/api/boot')
    def boot_state() -> BootState:
        return boot.state()

    @app.get('/api/agents', response_model_exclude={'__all__': {'graph'}})
    def agents() -> list[discovery.Agent]:
        return discovery.find(settings.root, settings.agents)

    @app.get('/api/agents/{agent_id}')
    def agent(agent_id: str) -> discovery.Agent:
        for found in discovery.find(settings.root, settings.agents):
            if found.id == agent_id:
                return found
        raise HTTPException(404, f'no agent {agent_id!r} under {settings.agents}')

    @app.get('/api/runs')
    def runs() -> None:
        raise HTTPException(501, 'Runs are not recorded yet: the engine does not run calls in this build.')

    @app.get('/api/events/schema')
    def event_schema() -> dict[str, Any]:
        return EVENT.json_schema()

    if (settings.web_dist / 'index.html').exists():
        app.mount('/', StaticFiles(directory=settings.web_dist, html=True), name='web')
    return app
