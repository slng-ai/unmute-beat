"""Run: uv run python -m tests.test_cli

The `slng` command's shape, and the compose project it drives: checked offline, no container started.
"""

import contextlib
import io
import os
from pathlib import Path

import yaml

from studio import cli
from studio.settings import STUDIO, Settings


def the_commands_parse():
    up = cli.parser().parse_args(['studio', 'up', '--dev', '--no-services', '--port', '7900'])
    assert (up.command, up.dev, up.no_services, up.port) == ('up', True, True, 7900)
    assert cli.parser().parse_args(['studio', 'down', '--volumes']).volumes
    assert cli.parser().parse_args(['studio', 'agents']).command == 'agents'


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


if __name__ == '__main__':
    the_commands_parse()
    compose_targets_the_studio_project()
    every_port_stays_on_this_machine()
    agents_lists_and_exits_on_errors()
    print('cli ok: commands, compose project, local ports, agents')
