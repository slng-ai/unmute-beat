"""Run: uv run python -m tests.test_cli

The `slng` command's shape, and the compose project it drives: checked offline, no container started.
"""

import contextlib
import io
import os
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

import yaml

from studio.boot import BootState
from studio.cli import Cli
from studio.settings import STUDIO, Settings
from studio.window import AppWindow


def the_commands_parse():
    up = Cli.parser().parse_args(['studio', 'up', '--dev', '--no-services', '--port', '7900'])
    assert (up.command, up.dev, up.no_services, up.port) == ('up', True, True, 7900)
    assert Cli.parser().parse_args(['studio', 'down', '--volumes']).volumes
    assert Cli.parser().parse_args(['studio', 'agents']).command == 'agents'
    bot = Cli.parser().parse_args(['studio', 'bot', 'salon', '-t', 'twilio'])
    assert (bot.command, bot.use_case, bot.bot_args) == ('bot', 'salon', ['-t', 'twilio'])
    assert not up.no_runner and Cli.parser().parse_args(['studio', 'up', '--no-runner']).no_runner
    more = Cli.parser().parse_args(['studio', 'runner', '--port', '7820', '--max-calls', '2'])
    assert (more.command, more.runner_port, more.max_calls) == ('runner', 7820, 2)
    assert Cli.parser().parse_args(['studio', 'up', '--app']).app and not up.app


def a_runner_links_to_the_studio():
    seen: list[tuple[list[str], Path, dict[str, str]]] = []
    settings = Settings.from_env()
    code = Cli(settings, run=lambda argv, cwd, env: seen.append((argv, cwd, env)) or 0).runner(7820, 2)
    [(argv, cwd, env)] = seen
    assert code == 0 and argv[1:3] == ['-m', 'voice_runner'] and cwd == settings.root
    assert argv[argv.index('--studio') + 1] == f'ws://{settings.host}:{settings.port}/api/runners/link'
    assert (
        argv[argv.index('--port') + 1] == '7820'
        and env['OTEL_EXPORTER_OTLP_ENDPOINT'] == settings.otlp_endpoint
    )


def compose_targets_the_studio_project():
    argv = Cli(Settings.from_env()).compose_argv('stop')
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
            code = Cli.main(['studio', 'agents'])
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
            assert Cli(settings, run=run).bot('desk', ['-t', 'twilio']) == 0
            assert Cli(settings, run=run).bot('nowhere', []) == 2
        argv, cwd, env = seen[0]
        assert len(seen) == 1 and argv == ['uv', 'run', 'bot.py', '-t', 'twilio'], seen
        assert (
            cwd == Path(root) / 'use-cases' / 'desk'
            and env['OTEL_EXPORTER_OTLP_ENDPOINT'] == settings.otlp_endpoint
        )
        assert 'timings will be missing' in err.getvalue() and 'not built on core' not in err.getvalue()
        assert 'Use cases with one: desk' in err.getvalue(), err.getvalue()


def the_app_window_finds_a_browser_on_every_os():
    """Chrome first, then Edge, then Chromium; on macOS the user's own Applications folder counts too."""
    settings = replace(Settings.from_env(), root=Path('/repo'))
    home = Path('/Users/ana')

    def window(platform: str, installed: set[str], **given) -> AppWindow:
        return AppWindow(
            settings, platform=platform, home=home, exists=lambda path: str(path) in installed, **given
        )

    edge = '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'
    mine = '/Users/ana/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
    assert window('darwin', {edge, mine}).find() == Path(mine)
    assert window('darwin', {edge}).find() == Path(edge)
    assert window('darwin', set()).find() is None
    env = {'ProgramFiles': 'C:/PF', 'LOCALAPPDATA': 'C:/Local'}
    local = str(Path('C:/Local', 'Google', 'Chrome', 'Application', 'chrome.exe'))
    assert window('win32', {local}, env=env).find() == Path(local)
    assert window('win32', {local}, env={}).find() is None
    on_path = {'chromium': '/usr/bin/chromium'}
    linux = window('linux', {'/usr/bin/chromium'}, which=on_path.get)
    assert linux.find() == Path('/usr/bin/chromium')
    argv = window('linux', set(), env={'STUDIO_APP_FLAGS': '--remote-debugging-port=9333'}).argv(
        Path('/usr/bin/chromium'), '#/boot'
    )
    assert argv == [
        '/usr/bin/chromium',
        f'--app=http://127.0.0.1:{settings.port}/#/boot',
        '--user-data-dir=/repo/data/studio/app-window',
        '--no-first-run',
        '--no-default-browser-check',
        '--remote-debugging-port=9333',
    ], argv


def the_app_window_opens_once_the_boot_is_ready():
    """No browser: it says so and gives the URL. Ready: the start page; a failed step: the loading screen."""
    with tempfile.TemporaryDirectory() as root:
        settings = replace(Settings.from_env(), root=Path(root))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert not AppWindow(settings, exists=lambda _: False, which=lambda _: None).open()
        assert 'no Chrome or Edge found' in out.getvalue() and f':{settings.port}/' in out.getvalue()

        launched: list[list[str]] = []

        def launch(argv: list[str]) -> subprocess.Popen[bytes]:
            launched.append(argv)
            return subprocess.Popen(['true'])

        class Booting(AppWindow):
            """A window whose engine answers from a script: not up yet, starting, then `last` for good."""

            POLL = 0

            def __init__(self, last: BootState) -> None:
                super().__init__(
                    settings,
                    platform='linux',
                    which=lambda _: '/usr/bin/chrome',
                    exists=lambda _: True,
                    launch=launch,
                )
                self.answers = iter([None, BootState(steps=[], ready=False, failed=False)])
                self.last = last

            def boot(self) -> BootState | None:
                return next(self.answers, self.last)

        assert Booting(BootState(steps=[], ready=True, failed=False)).when_ready() == ''
        found = Booting(BootState(steps=[], ready=False, failed=True))
        with contextlib.redirect_stdout(io.StringIO()):
            assert found.open(found.when_ready())
        assert launched[0][1].endswith('/#/boot') and (Path(root) / 'data' / 'studio' / 'app-window').is_dir()
        found.close()


if __name__ == '__main__':
    the_commands_parse()
    a_runner_links_to_the_studio()
    compose_targets_the_studio_project()
    every_port_stays_on_this_machine()
    agents_lists_and_exits_on_errors()
    bot_points_the_use_case_at_the_studio()
    the_app_window_finds_a_browser_on_every_os()
    the_app_window_opens_once_the_boot_is_ready()
    print('cli ok: commands, compose project, local ports, agents, bot, app window')
