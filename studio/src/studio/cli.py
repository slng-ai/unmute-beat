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
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from studio.agents.catalog import Catalog
from studio.boot import Shell
from studio.runs.process import RunnerProcess
from studio.settings import STUDIO, Settings

type Run = Callable[[list[str], Path, dict[str, str]], int]


class Cli:
    """The studio's commands, on one set of settings. `run` starts a child in the foreground; tests pass their own."""

    def __init__(self, settings: Settings, run: Run | None = None) -> None:
        self.settings = settings
        self.run = run or self.foreground

    @staticmethod
    def parser() -> argparse.ArgumentParser:
        """`slng studio <command>` and each command's options."""
        top = argparse.ArgumentParser(prog='slng', description='SLNG voice tools.')
        groups = top.add_subparsers(dest='group', required=True)
        commands = groups.add_parser('studio', help='the local studio').add_subparsers(
            dest='command', required=True
        )
        up = commands.add_parser('up', help='start the containers, the engine and the web app')
        up.add_argument('--port', type=int, help='the engine and web port (default 7800, or STUDIO_PORT)')
        up.add_argument('--dev', action='store_true', help="also run Vite's dev server, with hot reload")
        up.add_argument(
            '--no-services', action='store_true', help='leave Docker alone: no Postgres, no Redis'
        )
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

    @classmethod
    def main(cls, argv: Sequence[str] | None = None) -> int:
        """Parse the command line and run the command; its exit code."""
        args = cls.parser().parse_args(argv)
        settings = Settings.from_env()
        cli = cls(replace(settings, port=args.port) if getattr(args, 'port', None) else settings)
        match args.command:
            case 'up':
                return cli.up(dev=args.dev, services=not args.no_services, runner=not args.no_runner)
            case 'runner':
                return cli.runner(args.runner_port, args.max_calls)
            case 'down':
                return cli.compose('down', *(['--volumes'] if args.volumes else []))
            case 'status':
                return cli.status()
            case 'agents':
                return cli.agents()
            case 'bot':
                return cli.bot(args.use_case, args.bot_args)
        return 2

    def compose_argv(self, *args: str) -> list[str]:
        """A `docker compose` command on the studio's project."""
        return [
            'docker',
            'compose',
            '-f',
            str(self.settings.compose_file),
            '-p',
            self.settings.project,
            *args,
        ]

    def compose(self, *args: str) -> int:
        """Run `docker compose` on the studio's project."""
        try:
            return subprocess.run(
                self.compose_argv(*args), env=self.settings.compose_env, check=False
            ).returncode
        except FileNotFoundError:
            print('docker: not found. The studio needs Docker with compose v2.', file=sys.stderr)
            return 127

    def up(self, *, dev: bool, services: bool, runner: bool = True) -> int:
        """Serve until ctrl+c, then stop what was started. Imports uvicorn here so `agents` stays light."""
        import uvicorn

        from studio.api.app import Studio
        from studio.boot import Boot

        settings = self.settings
        app = Studio(settings, Boot(settings, services=services), runner=runner).app()
        web = self._vite() if dev else None
        print(
            f'\n  slng studio  →  http://localhost:{settings.web_dev_port if dev else settings.port}',
            flush=True,
        )
        print('  ctrl+c stops everything\n', flush=True)
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
                self.compose('stop')
        return 0

    def _vite(self) -> subprocess.Popen[bytes]:
        """Vite's dev server with hot reload, sending /api on to the engine."""
        argv = [
            'npm',
            '--prefix',
            str(STUDIO / 'web'),
            'run',
            'dev',
            '--',
            '--port',
            str(self.settings.web_dev_port),
        ]
        engine = f'http://{self.settings.host}:{self.settings.port}'
        return subprocess.Popen(argv, env={**self.settings.compose_env, 'STUDIO_ENGINE': engine})

    def runner(self, port: int | None, max_calls: int) -> int:
        """One more runner, linked to the studio on `settings.port`, until ctrl+c."""
        port = port or self._free_port(self.settings.runner_port + 1)
        process = RunnerProcess(self.settings, port, max_calls=max_calls)
        print(
            f'  runner on :{port}, linked to the studio on :{self.settings.port}; ctrl+c stops it after its calls\n',
            flush=True,
        )
        return self.run(process.argv(), self.settings.root, process.env())

    def _free_port(self, port: int) -> int:
        """The first port from this one that nothing listens on."""
        while True:
            with socket.socket() as probe:
                if probe.connect_ex((self.settings.host, port)) != 0:
                    return port
            port += 1

    def status(self) -> int:
        """The containers, and whether the engine answers."""
        self.compose('ps')
        try:
            with urllib.request.urlopen(
                f'http://{self.settings.host}:{self.settings.port}/api/health', timeout=2
            ) as answer:
                print('engine', json.load(answer).get('engine', '?'))
        except urllib.error.URLError, TimeoutError:
            print('engine  not running')
        return 0

    def agents(self) -> int:
        """Every manifest and whether `core.load` accepts it. Exit 1 if any does not."""
        found = Catalog(self.settings.root, self.settings.agents).agents()
        for agent in found:
            compiled = agent.status == 'compiled'
            shape = (
                f'{self._count(agent.steps, "step")} · {self._count(agent.operations, "operation")}'
                if compiled
                else agent.error
            )
            print(f'{"■" if compiled else "✕"} {agent.id:<18} {agent.path:<52} {shape}')
        if not found:
            print(f'no agent.yaml under {self.settings.root} matching {self.settings.agents}')
        return 1 if any(agent.status == 'error' for agent in found) else 0

    def bot(self, use_case: str, bot_args: Sequence[str]) -> int:
        """Start `use-cases/<use_case>/bot.py` with its spans going to the studio's collector.

        A collector named in the root `.env` still wins, since every `bot.py` loads it with
        `override=True`; that is said rather than hidden. So is a bot not built on core (the two
        baselines), whose own tracing sends nothing to a local collector. A studio that is not up does
        not stop the bot: the call works, and the warning says its timings will be missing.
        """
        folders = self.settings.root / 'use-cases'
        folder = folders / use_case
        if not (folder / 'bot.py').exists():
            known = sorted(path.parent.name for path in folders.glob('*/bot.py'))
            print(f'no bot.py in {folder}. Use cases with one: {", ".join(known) or "none"}', file=sys.stderr)
            return 2
        for warning in self._bot_warnings(use_case, folder):
            print(f'warning: {warning}', file=sys.stderr)
        print(
            f'  {use_case}: traces to {self.settings.otlp_endpoint}, timings on the Metrics page\n',
            flush=True,
        )
        env = {**os.environ, 'OTEL_EXPORTER_OTLP_ENDPOINT': self.settings.otlp_endpoint}
        return self.run(['uv', 'run', 'bot.py', *bot_args], folder, env)

    def _bot_warnings(self, use_case: str, folder: Path) -> list[str]:
        """What will keep a bot's timings from the studio: not built on core, a collector in `.env`, no studio up."""
        dotenv = self.settings.root / '.env'
        named = dotenv.exists() and any(
            line.strip().startswith('OTEL_EXPORTER_OTLP_ENDPOINT=')
            for line in dotenv.read_text().splitlines()
        )
        checks = (
            (
                not any(folder.glob('*/agent.yaml')),
                f'{use_case} is not built on core, so it sends no traces to the studio.',
            ),
            (named, f'{dotenv} sets OTEL_EXPORTER_OTLP_ENDPOINT, and bot.py lets it win.'),
            (
                not Shell.answers(self.settings.jaeger_url),
                f'the studio is not collecting traces (nothing at {self.settings.jaeger_url}). '
                "Start it with `slng studio up`, or this call's timings will be missing.",
            ),
        )
        return [warning for holds, warning in checks if holds]

    @staticmethod
    def foreground(argv: list[str], cwd: Path, env: dict[str, str]) -> int:
        """Run a child sharing the terminal, so ctrl+c reaches it too; wait for it to finish stopping."""
        child = subprocess.Popen(argv, cwd=cwd, env=env)
        try:
            return child.wait()
        except KeyboardInterrupt:
            return child.wait()

    @staticmethod
    def _count(n: int, word: str) -> str:
        """`1 step`, `4 steps`."""
        return f'{n} {word}' + ('' if n == 1 else 's')


def main(argv: Sequence[str] | None = None) -> int:
    """The `slng` entry point."""
    return Cli.main(argv)
