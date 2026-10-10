"""Where the studio runs and what it reads: every value from the environment, with a local default.

Nothing here reads `.env`. The studio needs no model key to start: agents are read with
`core.load`, which builds no client. A run that calls a model is the launcher's business.
"""

import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
"""`studio/src/studio`: schema.sql lives here."""
STUDIO = PACKAGE.parents[1]
"""`studio/`: compose.yaml and the web app live here."""


@dataclass(frozen=True)
class Settings:
    """One studio's ports, paths and service URLs."""

    root: Path
    """The repository root: agent paths are shown relative to it."""
    agents: str = 'use-cases/*/*/agent.yaml'
    """The glob, from `root`, that finds every manifest."""
    host: str = '127.0.0.1'
    port: int = 7800
    """The engine: API under /api, the built web app at /."""
    web_dev_port: int = 7802
    """Vite's dev server, when `up --dev` runs it beside the engine; it sends /api on to `port`."""
    pg_port: int = 5432
    redis_port: int = 6379
    otlp_port: int = 4318
    """Where bots and the engine send their spans: OTLP over HTTP, into Jaeger."""
    jaeger_port: int = 16686
    """Jaeger's UI and the query API the studio reads traces from."""
    runner_port: int = 7810
    """The runner `up` starts; more runners take the next ports (`slng studio runner --port`)."""
    project: str = 'slng-studio'
    """The compose project name, so `down` finds what `up` started."""

    @property
    def compose_file(self) -> Path:
        return STUDIO / 'compose.yaml'

    @property
    def web_dist(self) -> Path:
        return STUDIO / 'web' / 'dist'

    @property
    def database_url(self) -> str:
        return f'postgresql://studio:studio@127.0.0.1:{self.pg_port}/studio'

    @property
    def redis_url(self) -> str:
        return f'redis://127.0.0.1:{self.redis_port}/0'

    @property
    def otlp_endpoint(self) -> str:
        """The value a bot sets as `OTEL_EXPORTER_OTLP_ENDPOINT` to send its spans here."""
        return f'http://127.0.0.1:{self.otlp_port}'

    @property
    def jaeger_url(self) -> str:
        return f'http://127.0.0.1:{self.jaeger_port}'

    @property
    def data(self) -> Path:
        """Where the studio's runners keep their logs and the use cases their business data: `data/studio`, ignored by git."""
        return self.root / 'data' / 'studio'

    @property
    def compose_env(self) -> dict[str, str]:
        """What compose.yaml reads, so a changed port reaches the containers too."""
        return {
            **os.environ,
            'STUDIO_PG_PORT': str(self.pg_port),
            'STUDIO_REDIS_PORT': str(self.redis_port),
            'STUDIO_OTLP_PORT': str(self.otlp_port),
            'STUDIO_JAEGER_PORT': str(self.jaeger_port),
        }

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        return cls(
            root=Path(env('STUDIO_ROOT') or STUDIO.parent).resolve(),
            agents=env('STUDIO_AGENTS', cls.agents),
            host=env('STUDIO_HOST', cls.host),
            port=int(env('STUDIO_PORT', str(cls.port))),
            web_dev_port=int(env('STUDIO_WEB_DEV_PORT', str(cls.web_dev_port))),
            pg_port=int(env('STUDIO_PG_PORT', str(cls.pg_port))),
            redis_port=int(env('STUDIO_REDIS_PORT', str(cls.redis_port))),
            otlp_port=int(env('STUDIO_OTLP_PORT', str(cls.otlp_port))),
            jaeger_port=int(env('STUDIO_JAEGER_PORT', str(cls.jaeger_port))),
            runner_port=int(env('STUDIO_RUNNER_PORT', str(cls.runner_port))),
        )
