"""`uv run slng-runner` (or `python -m voice_runner`): one runner process, linked to the studio.

    uv run slng-runner --port 7810 --studio ws://127.0.0.1:7800/api/runners/link

Reads the repository's `.env` first, as every `bot.py` does, since the agents build their clients
at import. `slng studio up` starts one for you; start more on other ports to carry more calls.
"""

import argparse
import json
import os
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

ROOT = Path(__file__).resolve().parents[3]
"""The repository: `runner/src/voice_runner/__main__.py` is three folders below it."""


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog='slng-runner', description=__doc__.splitlines()[0])
    parser.add_argument('--host', default=os.getenv('RUNNER_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.getenv('RUNNER_PORT', '7810')))
    parser.add_argument('--public-url', default=os.getenv('RUNNER_PUBLIC_URL', ''))
    parser.add_argument(
        '--studio', default=os.getenv('RUNNER_STUDIO', 'ws://127.0.0.1:7800/api/runners/link')
    )
    parser.add_argument('--max-calls', type=int, default=int(os.getenv('RUNNER_MAX_CALLS', '4')))
    parser.add_argument(
        '--no-warm', action='store_true', help='compile every flow, but send no warm-up request'
    )
    parser.add_argument('--root', type=Path, default=ROOT, help='the repository whose use cases to carry')
    args = parser.parse_args(argv)

    load_dotenv(find_dotenv(str(args.root / '.env')) or None, override=False)

    import uvicorn

    from voice_runner import agents
    from voice_runner.app import Settings, create_app, data_defaults

    data_defaults(args.root)
    found = agents.find(args.root)
    ice = tuple(json.loads(os.environ['PIPECAT_ICE_SERVERS'])) if os.getenv('PIPECAT_ICE_SERVERS') else ()
    settings = Settings(
        port=args.port,
        host=args.host,
        public_url=args.public_url,
        studio=args.studio,
        max_calls=args.max_calls,
        warm=not args.no_warm,
        ice_servers=ice,
    )
    uvicorn.run(create_app(settings, found), host=args.host, port=args.port, log_level='info')


if __name__ == '__main__':
    main()
