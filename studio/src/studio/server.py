"""The engine's HTTP face: the API under /api, and the built web app at /.

Agents are read from disk on every request, so a manifest saved in an editor shows at once.
Runs are not recorded yet: `/api/runs` says so with a 501 the page shows, rather than an empty
list that would read as "no calls happened". Timings come from the local Jaeger (`studio.metrics`);
when it does not answer, `/api/metrics` says so with a 503 rather than showing no slow turns.
"""

import asyncio
import urllib.error
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from studio import discovery, metrics
from studio.boot import Boot, BootState
from studio.events import EVENT
from studio.settings import Settings


def create_app(
    settings: Settings, boot: Boot | None = None, fetch: metrics.Fetch = metrics.fetch_json
) -> FastAPI:
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

    @app.get('/api/metrics/limits')
    def limits() -> metrics.Limits:
        return metrics.Limits()

    def jaeger_down(error: Exception) -> HTTPException:
        return HTTPException(
            503,
            f'Jaeger did not answer at {settings.jaeger_url} ({error}). Start the studio with its services, '
            f'then start a bot with `slng studio bot <use case>`, which sends its spans to {settings.otlp_endpoint}.',
        )

    @app.get('/api/metrics')
    def recent(lookback: str = '1h', agent: str = '') -> metrics.Metrics:
        """The overview, for every agent or the one named: the agent filter lives here and only here."""
        try:
            return metrics.collect(settings.jaeger_url, agent=agent, lookback=lookback, fetch=fetch)
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as error:
            raise jaeger_down(error) from error

    @app.get('/api/metrics/runs/{trace}')
    def one_run(trace: str) -> metrics.Metrics:
        """One run's view: its own trace, so it names its agent and needs no filter."""
        try:
            return metrics.collect_run(settings.jaeger_url, trace, fetch=fetch)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise HTTPException(404, f'Jaeger has no trace {trace!r}') from error
            raise jaeger_down(error) from error
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as error:
            raise jaeger_down(error) from error

    @app.get('/api/events/schema')
    def event_schema() -> dict[str, Any]:
        return EVENT.json_schema()

    if (settings.web_dist / 'index.html').exists():
        app.mount('/', StaticFiles(directory=settings.web_dist, html=True), name='web')
    return app
