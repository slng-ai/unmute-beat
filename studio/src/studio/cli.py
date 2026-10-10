"""The `slng` command. `slng studio up` is the one a person types.

    slng studio up [--dev] [--no-services] [--no-runner] [--port N]
                                                         containers, engine, a runner, web; ctrl+c stops them all
    slng studio runner [--port N] [--max-calls N]        one more runner, linked to the studio that is up
    slng studio down [--volumes]                         stop and remove the containers (and the data)
    slng studio status                                   what is running
    slng studio agents                                   every agent.yaml and whether it compiles, offline
    slng studio bot <use case> [bot.py args]             run that use case's bot, its traces sent to the studio

`up` starts the engine first and lets it bring the rest up, so the browser shows the loading
screen from the first second (`studio.boot`). Containers are stopped, not removed, on ctrl+c:
the runs stay in the `slng-studio_pg` volume until `down --volumes`.

`up` also starts one runner (`voice_runner`): the process that carries calls, by voice from the
browser or by text, for every agent in the repository. `runner` starts another, on the next port, to
carry more calls at once; each links to the studio, which places every new run on the least busy one.

`bot` runs a use case's own `bot.py` with `OTEL_EXPORTER_OTLP_ENDPOINT` pointed at the studio's
collector, which `core.observability.setup` reads: for transports the studio's page does not own
(`-t twilio`). A call from the Agents page needs none of it: its runner already sends its spans.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from studio import discovery
from studio.settings import STUDIO, Settings


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog='slng', description='SLNG voice tools.')
    groups = top.add_subparsers(dest='group', required=True)
    studio = groups.add_parser('studio', help='the local studio')
    commands = studio.add_subparsers(dest='command', required=True)

    up = commands.add_parser('up', help='start the containers, the engine and the web app')
    up.add_argument('--port', type=int, help='the engine and web port (default 7800, or STUDIO_PORT)')
    up.add_argument('--dev', action='store_true', help="also run Vite's dev server, with hot reload")
    up.add_argument('--no-services', action='store_true', help='leave Docker alone: no Postgres, no Redis')
    up.add_argument(
        '--no-runner', action='store_true', help='start no runner: add one with `slng studio runner`'
    )

    runner = commands.add_parser('runner', help='start one more runner, linked to the studio')
    runner.add_argument(
        '--port', dest='runner_port', type=int, help='its port (default: the next free from 7811)'
    )
    runner.add_argument('--max-calls', type=int, default=4, help='calls it carries at once (default 4)')

    down = commands.add_parser('down', help='stop and remove the containers')
    down.add_argument('--volumes', action='store_true', help='also delete the stored runs')

    commands.add_parser('status', help='what is running')
    commands.add_parser('agents', help='every agent.yaml and whether it compiles')

    bot = commands.add_parser('bot', help="run a use case's bot with its traces sent to the studio")
    bot.add_argument('use_case', help='a folder under use-cases/ with a bot.py, e.g. salon')
    bot.add_argument('bot_args', nargs=argparse.REMAINDER, help='passed on to bot.py, e.g. -t twilio')
    return top


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    settings = Settings.from_env()
    if getattr(args, 'port', None):
        settings = replace(settings, port=args.port)
    match args.command:
        case 'up':
            return up(settings, dev=args.dev, services=not args.no_services, runner=not args.no_runner)
        case 'runner':
            return runner(settings, args.runner_port, args.max_calls)
        case 'down':
            return compose(settings, 'down', *(['--volumes'] if args.volumes else []))
        case 'status':
            return status(settings)
        case 'agents':
            return agents(settings)
        case 'bot':
            return bot(settings, args.use_case, args.bot_args)
    return 2


def compose_argv(settings: Settings, *args: str) -> list[str]:
    return ['docker', 'compose', '-f', str(settings.compose_file), '-p', settings.project, *args]


def compose(settings: Settings, *args: str) -> int:
    try:
        return subprocess.run(compose_argv(settings, *args), env=settings.compose_env, check=False).returncode
    except FileNotFoundError:
        print('docker: not found. The studio needs Docker with compose v2.', file=sys.stderr)
        return 127


def up(settings: Settings, *, dev: bool, services: bool, runner: bool = True) -> int:
    """Serve until ctrl+c, then stop what was started. Imports uvicorn here so `agents` stays light."""
    import uvicorn

    from studio.boot import Boot
    from studio.server import create_app

    app = create_app(settings, Boot(settings, services=services), runner=runner)
    web = None
    if dev:
        web = subprocess.Popen(
            [
                'npm',
                '--prefix',
                str(STUDIO / 'web'),
                'run',
                'dev',
                '--',
                '--port',
                str(settings.web_dev_port),
            ],
            env={**settings.compose_env, 'STUDIO_ENGINE': f'http://{settings.host}:{settings.port}'},
        )
    url = f'http://localhost:{settings.web_dev_port if dev else settings.port}'
    print(f'\n  slng studio  →  {url}\n  ctrl+c stops everything\n', flush=True)
    if runner:
        print(
            f'  runner on :{settings.runner_port}: start a call from Agents; `slng studio runner` adds one\n',
            flush=True,
        )
    if services:
        print(f'  traces in Jaeger at http://localhost:{settings.jaeger_port}\n', flush=True)
    try:
        uvicorn.run(app, host=settings.host, port=settings.port, log_level='warning')
    finally:
        if web is not None:
            web.terminate()
        if services:
            compose(settings, 'stop')
    return 0


def runner(settings: Settings, port: int | None, max_calls: int, run: Run | None = None) -> int:
    """One more runner (`python -m voice_runner`), linked to the studio on `settings.port`, until ctrl+c."""
    import socket

    if port is None:
        port = settings.runner_port + 1
        while True:
            with socket.socket() as probe:
                if probe.connect_ex((settings.host, port)) != 0:
                    break
            port += 1
    env = {**os.environ, 'OTEL_EXPORTER_OTLP_ENDPOINT': settings.otlp_endpoint, 'PYDANTIC_AI_NO_BANNER': '1'}
    argv = [
        sys.executable, '-m', 'voice_runner', '--port', str(port), '--max-calls', str(max_calls),
        '--studio', f'ws://{settings.host}:{settings.port}/api/runners/link', '--root', str(settings.root),
    ]  # fmt: skip
    print(
        f'  runner on :{port}, linked to the studio on :{settings.port}; ctrl+c stops it after its calls\n',
        flush=True,
    )
    return (run or _run)(argv, settings.root, env)


def status(settings: Settings) -> int:
    compose(settings, 'ps')
    try:
        with urllib.request.urlopen(
            f'http://{settings.host}:{settings.port}/api/health', timeout=2
        ) as response:
            print('engine', json.load(response).get('engine', '?'))
    except urllib.error.URLError, TimeoutError:
        print('engine  not running')
    return 0


def agents(settings: Settings) -> int:
    """Every manifest and whether `core.load` accepts it. Exit 1 if any does not."""
    found = discovery.find(settings.root, settings.agents)
    for agent in found:
        mark = '■' if agent.status == 'compiled' else '✕'
        shape = (
            f'{_count(agent.steps, "step")} · {_count(agent.operations, "operation")}'
            if agent.status == 'compiled'
            else agent.error
        )
        print(f'{mark} {agent.id:<18} {agent.path:<52} {shape}')
    if not found:
        print(f'no agent.yaml under {settings.root} matching {settings.agents}')
    return 1 if any(agent.status == 'error' for agent in found) else 0


Run = Callable[[list[str], Path, dict[str, str]], int]
"""Runs one command in a folder with an environment and returns its exit code; tests pass their own."""


def _run(argv: list[str], cwd: Path, env: dict[str, str]) -> int:
    """The child shares the terminal, so ctrl+c reaches it too; this waits for it to finish stopping."""
    child = subprocess.Popen(argv, cwd=cwd, env=env)
    try:
        return child.wait()
    except KeyboardInterrupt:
        return child.wait()


def bot(settings: Settings, use_case: str, bot_args: Sequence[str], run: Run = _run) -> int:
    """Start `use-cases/<use_case>/bot.py` with its spans going to the studio's collector.

    A collector named in the root `.env` still wins, since every `bot.py` loads it with
    `override=True`; that is said rather than hidden. So is a bot not built on core (the two
    baselines), whose own tracing sends nothing to a local collector. A studio that is not up does
    not stop the bot: the call works, and the warning says its timings will be missing.
    """
    from studio.boot import answers

    folders = settings.root / 'use-cases'
    folder = folders / use_case
    if not (folder / 'bot.py').exists():
        known = sorted(path.parent.name for path in folders.glob('*/bot.py'))
        print(f'no bot.py in {folder}. Use cases with one: {", ".join(known) or "none"}', file=sys.stderr)
        return 2
    if not any(folder.glob('*/agent.yaml')):
        print(
            f'warning: {use_case} is not built on core, so it sends no traces to the studio.', file=sys.stderr
        )
    dotenv = settings.root / '.env'
    if dotenv.exists() and any(
        line.strip().startswith('OTEL_EXPORTER_OTLP_ENDPOINT=') for line in dotenv.read_text().splitlines()
    ):
        print(f'warning: {dotenv} sets OTEL_EXPORTER_OTLP_ENDPOINT, and bot.py lets it win.', file=sys.stderr)
    if not answers(settings.jaeger_url):
        print(
            f'warning: the studio is not collecting traces (nothing at {settings.jaeger_url}). '
            "Start it with `slng studio up`, or this call's timings will be missing.",
            file=sys.stderr,
        )
    print(f'  {use_case}: traces to {settings.otlp_endpoint}, timings on the Metrics page\n', flush=True)
    env = {**os.environ, 'OTEL_EXPORTER_OTLP_ENDPOINT': settings.otlp_endpoint}
    return run(['uv', 'run', 'bot.py', *bot_args], folder, env)


def _count(n: int, word: str) -> str:
    return f'{n} {word}' + ('' if n == 1 else 's')
