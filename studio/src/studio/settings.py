"""Where the studio runs and what it reads: every value from the environment, with a local default.

Nothing here reads `.env`. The studio needs no model key to start: agents are read with
`core.load`, which builds no client. A run that calls a model is the launcher's business.
`PACKAGE` is `studio/src/studio` (schema.sql lives there) and `STUDIO` is `studio/` (compose.yaml and
the web app).
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Self

PACKAGE = Path(__file__).resolve().parent
STUDIO = PACKAGE.parents[1]


@dataclass(frozen=True)
class Settings:
    """One studio's ports, paths and service URLs.

    `root` is the repository, which agent paths are shown relative to, and `agents` the glob from it that
    finds every manifest. `port` serves the API under /api and the built web app at /; `web_dev_port` is
    Vite's dev server under `up --dev`, which sends /api on to `port`. Bots and the engine send their
    spans to `otlp_port` (OTLP over HTTP, into Jaeger), and the studio reads traces from Jaeger's query
    API on `jaeger_port`. `runner_port` is the runner `up` starts; more take the next ports. `project` is
    the compose project name, so `down` finds what `up` started.
    """

    root: Path
    agents: str = 'use-cases/*/*/agent.yaml'
    host: str = '127.0.0.1'
    port: int = 7800
    web_dev_port: int = 7802
    pg_port: int = 5432
    redis_port: int = 6379
    otlp_port: int = 4318
    jaeger_port: int = 16686
    runner_port: int = 7810
    project: str = 'slng-studio'

    @property
    def compose_file(self) -> Path:
        """The compose file of the studio's services."""
        return STUDIO / 'compose.yaml'

    @property
    def web_dist(self) -> Path:
        """The built web app."""
        return STUDIO / 'web' / 'dist'

    @property
    def database_url(self) -> str:
        """Postgres, as asyncpg takes it."""
        return f'postgresql://studio:studio@127.0.0.1:{self.pg_port}/studio'

    @property
    def redis_url(self) -> str:
        """Redis, as redis-py takes it."""
        return f'redis://127.0.0.1:{self.redis_port}/0'

    @property
    def otlp_endpoint(self) -> str:
        """The value a bot sets as `OTEL_EXPORTER_OTLP_ENDPOINT` to send its spans here."""
        return f'http://127.0.0.1:{self.otlp_port}'

    @property
    def jaeger_url(self) -> str:
        """Jaeger's UI and query API."""
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
    def from_env(cls) -> Self:
        """Every setting from its `STUDIO_*` variable, or its default."""
        env = os.environ.get

        def number(name: str) -> int:
            """A port from `STUDIO_<NAME>`, or its default."""
            return int(env(f'STUDIO_{name.upper()}', str(getattr(cls, name))))

        return cls(
            root=Path(env('STUDIO_ROOT') or STUDIO.parent).resolve(),
            agents=env('STUDIO_AGENTS', cls.agents),
            host=env('STUDIO_HOST', cls.host),
            port=number('port'),
            web_dev_port=number('web_dev_port'),
            pg_port=number('pg_port'),
            redis_port=number('redis_port'),
            otlp_port=number('otlp_port'),
            jaeger_port=number('jaeger_port'),
            runner_port=number('runner_port'),
        )
