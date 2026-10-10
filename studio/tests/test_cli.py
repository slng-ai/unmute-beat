"""Run: uv run python -m tests.test_cli

The `slng` command's shape, and the compose project it drives: checked offline, no container started.
"""

import contextlib
import io
import os
import tempfile
from dataclasses import replace
from pathlib import Path

import yaml

from studio import cli
from studio.settings import STUDIO, Settings


def the_commands_parse():
    up = cli.parser().parse_args(['studio', 'up', '--dev', '--no-services', '--port', '7900'])
    assert (up.command, up.dev, up.no_services, up.port) == ('up', True, True, 7900)
    assert cli.parser().parse_args(['studio', 'down', '--volumes']).volumes
    assert cli.parser().parse_args(['studio', 'agents']).command == 'agents'
    bot = cli.parser().parse_args(['studio', 'bot', 'salon', '-t', 'twilio'])
    assert (bot.command, bot.use_case, bot.bot_args) == ('bot', 'salon', ['-t', 'twilio'])


def compose_targets_the_studio_project():
    argv = cli.compose_argv(Settings.from_env(), 'stop')
    assert argv[:2] == ['docker', 'compose'] and argv[-1] == 'stop'
    assert (
        Path(argv[argv.index('-f') + 1]) == STUDIO / 'compose.yaml'
        and argv[argv.index('-p') + 1] == 'slng-studio'
    )


def every_port_stays_on_this_machine():
    """The containers hold transcripts: nothing binds beyond 127.0.0.1. Postgres and Redis have health checks;
    Jaeger's image has no shell, so `studio.boot` asks it over HTTP instead.
    """
    compose = yaml.safe_load((STUDIO / 'compose.yaml').read_text())
    assert set(compose['services']) == {'postgres', 'redis', 'jaeger'}, compose['services'].keys()
    for name, service in compose['services'].items():
        assert all(port.startswith('127.0.0.1:') for port in service['ports']), (name, service['ports'])
        assert ('healthcheck' in service) == (name != 'jaeger'), name
    mounts = compose['services']['postgres']['volumes']
    schema = next(mount for mount in mounts if 'schema.sql' in mount).split(':')[0]
    assert (STUDIO / schema).exists(), schema


def agents_lists_and_exits_on_errors():
    fixtures = str(Path(__file__).parent / 'fixtures')
    os.environ.update(STUDIO_ROOT=fixtures, STUDIO_AGENTS='*/agent.yaml')
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['studio', 'agents'])
    finally:
        del os.environ['STUDIO_ROOT'], os.environ['STUDIO_AGENTS']
    assert code == 0 and 'desk' in out.getvalue() and '4 steps' in out.getvalue(), out.getvalue()


def bot_points_the_use_case_at_the_studio():
    """Nothing to set by hand: the bot runs in its own folder with the studio's collector in its environment.
    A use case with no bot.py starts nothing and names the ones that have one.
    """
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / 'use-cases' / 'desk' / 'desk').mkdir(parents=True)
        (Path(root) / 'use-cases' / 'desk' / 'bot.py').touch()
        (Path(root) / 'use-cases' / 'desk' / 'desk' / 'agent.yaml').touch()
        settings = replace(Settings.from_env(), root=Path(root), jaeger_port=1)
        seen: list[tuple[list[str], Path, dict[str, str]]] = []

        def run(argv: list[str], cwd: Path, env: dict[str, str]) -> int:
            seen.append((argv, cwd, env))
            return 0

        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            assert cli.bot(settings, 'desk', ['-t', 'twilio'], run=run) == 0
            assert cli.bot(settings, 'nowhere', [], run=run) == 2
        argv, cwd, env = seen[0]
        assert len(seen) == 1 and argv == ['uv', 'run', 'bot.py', '-t', 'twilio'], seen
        assert (
            cwd == Path(root) / 'use-cases' / 'desk'
            and env['OTEL_EXPORTER_OTLP_ENDPOINT'] == settings.otlp_endpoint
        )
        assert 'timings will be missing' in err.getvalue() and 'not built on core' not in err.getvalue()
        assert 'Use cases with one: desk' in err.getvalue(), err.getvalue()


if __name__ == '__main__':
    the_commands_parse()
    compose_targets_the_studio_project()
    every_port_stays_on_this_machine()
    agents_lists_and_exits_on_errors()
    bot_points_the_use_case_at_the_studio()
    print('cli ok: commands, compose project, local ports, agents, bot')
