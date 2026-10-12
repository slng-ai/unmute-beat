"""The timings' routes: the overview, one studio run, one trace, and the limits; a 503 that says what to do when Jaeger is down."""

import urllib.error
from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException

from studio.metrics import Limits, Metrics

if TYPE_CHECKING:
    from studio.api.app import Studio


class MetricsRoutes:
    """Everything under `/api/metrics`."""

    DOWN = (urllib.error.URLError, TimeoutError, ConnectionError, ValueError)

    def __init__(self, studio: Studio) -> None:
        self.studio = studio

    def router(self) -> APIRouter:
        """The routes, bound to this studio."""
        router = APIRouter(prefix='/api/metrics')
        router.add_api_route('/limits', self.limits, methods=['GET'])
        router.add_api_route('', self.recent, methods=['GET'])
        router.add_api_route('/runs/{trace}', self.trace, methods=['GET'])
        router.add_api_route('/run/{run}', self.run, methods=['GET'])
        return router

    @staticmethod
    def limits() -> Limits:
        """What counts as slow."""
        return Limits()

    def asked(self, question: Callable[[], Metrics]) -> Metrics:
        """Ask Jaeger; when it does not answer, a 503 that says how to get timings."""
        try:
            return question()
        except self.DOWN as error:
            raise HTTPException(
                503,
                f'Jaeger did not answer at {self.studio.settings.jaeger_url} ({error}). Start the studio with its '
                'services, then start a run from the Agents page, or a bot with `slng studio bot <use case>`.',
            ) from error

    def recent(self, lookback: str = '1h', agent: str = '') -> Metrics:
        """The overview, for every agent or the one named: the agent filter lives here and only here."""
        return self.asked(lambda: self.studio.metrics.collect(agent=agent, lookback=lookback))

    def trace(self, trace: str) -> Metrics:
        """One run's view: its own trace, so it names its agent and needs no filter."""
        try:
            return self.asked(lambda: self.studio.metrics.collect_run(trace))
        except LookupError as error:
            raise HTTPException(404, str(error)) from error

    def run(self, run: str) -> Metrics:
        """A studio run's timings, found in Jaeger by its run id (`slng.run.id`): every trace it wrote, as one."""
        found = self.asked(lambda: self.studio.metrics.collect(run=run, lookback='168h'))
        if not found.runs:
            raise HTTPException(
                404, f'Jaeger has no trace for {run} yet: spans arrive a few seconds after a turn'
            )
        return found
