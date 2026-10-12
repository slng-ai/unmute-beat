"""The studio in a window of its own: Chrome or Edge with `--app`, so no tabs and no address bar.

`slng studio up --app` opens it once the boot is ready, or on the loading screen when a step failed, so
the window shows what to do. Its own profile (`data/studio/app-window`) gives it a window and a Dock or
taskbar entry of its own, apart from the person's tabs. Closing the window leaves the studio running;
stopping the studio closes the window, since the page shows nothing without the engine.

Only the Chromium family is looked for: a browser call's playback reports (`core/client/playout.js`) are
tested on Chrome alone, and Safari and Firefox have no `--app` mode.
"""

import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

from pydantic import ValidationError

from studio.boot import BootState
from studio.settings import Settings

type Launch = Callable[[list[str]], subprocess.Popen[bytes]]


class AppWindow:
    """One app window for one studio: found, opened when the boot is ready, closed when the studio stops.

    Where to look is per OS, in order: Chrome, then Edge, then Chromium. On macOS the app bundles in
    `/Applications` and `~/Applications`; on Windows under `%ProgramFiles%`, `%ProgramFiles(x86)%` and
    `%LocalAppData%`; elsewhere each name on `PATH`. The OS, the environment, the home folder and the file
    check are arguments, so a test looks on every OS from any machine. `STUDIO_APP_FLAGS` adds flags to
    the browser's command line (a debugging port, a fake microphone).
    """

    MAC = (
        'Google Chrome.app/Contents/MacOS/Google Chrome',
        'Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
        'Chromium.app/Contents/MacOS/Chromium',
    )
    WINDOWS = (
        ('Google', 'Chrome', 'Application', 'chrome.exe'),
        ('Microsoft', 'Edge', 'Application', 'msedge.exe'),
        ('Chromium', 'Application', 'chrome.exe'),
    )
    WINDOWS_ROOTS = ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA')
    LINUX = (
        'google-chrome',
        'google-chrome-stable',
        'microsoft-edge',
        'microsoft-edge-stable',
        'chromium',
        'chromium-browser',
    )
    POLL = 0.5

    def __init__(
        self,
        settings: Settings,
        *,
        platform: str = sys.platform,
        env: Mapping[str, str] = os.environ,
        home: Path | None = None,
        exists: Callable[[Path], bool] = Path.is_file,
        which: Callable[[str], str | None] = shutil.which,
        launch: Launch | None = None,
    ) -> None:
        self.settings = settings
        self.platform = platform
        self.env = env
        self.home = home or Path.home()
        self.exists = exists
        self.which = which
        self.launch = launch or self.detached
        self.process: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        """The studio's address. Always 127.0.0.1: an install belongs to one origin, and localhost is another."""
        return f'http://{self.settings.host}:{self.settings.port}/'

    @property
    def profile(self) -> Path:
        """The window's own browser profile."""
        return self.settings.data / 'app-window'

    def candidates(self) -> list[Path]:
        """Every place this OS keeps a Chromium browser, best first."""
        if self.platform == 'darwin':
            roots = (Path('/Applications'), self.home / 'Applications')
            return [root / bundle for bundle in self.MAC for root in roots]
        if self.platform == 'win32':
            roots = [self.env[name] for name in self.WINDOWS_ROOTS if self.env.get(name)]
            return [Path(root, *parts) for parts in self.WINDOWS for root in roots]
        return [Path(found) for name in self.LINUX if (found := self.which(name))]

    def find(self) -> Path | None:
        """The first browser found, or None."""
        return next((path for path in self.candidates() if self.exists(path)), None)

    def argv(self, browser: Path, page: str = '') -> list[str]:
        """The browser's command line: the studio as an app, in its own profile."""
        return [
            str(browser),
            f'--app={self.url}{page}',
            f'--user-data-dir={self.profile}',
            '--no-first-run',
            '--no-default-browser-check',
            *shlex.split(self.env.get('STUDIO_APP_FLAGS', '')),
        ]

    def open(self, page: str = '') -> bool:
        """Open the window on `page` (a hash route, or '' for the start). False, with the URL printed, when no browser is found."""
        browser = self.find()
        if browser is None:
            print(
                f'\n  no Chrome or Edge found for --app: open {self.url}{page} in any browser\n', flush=True
            )
            return False
        self.profile.mkdir(parents=True, exist_ok=True)
        self.process = self.launch(self.argv(browser, page))
        print(
            f'\n  opened the studio in its own window ({browser.name}); closing it leaves the studio running\n',
            flush=True,
        )
        return True

    def boot(self) -> BootState | None:
        """The engine's start-up as it stands, or None while it does not answer."""
        try:
            with urllib.request.urlopen(f'{self.url}api/boot', timeout=2) as answer:
                return BootState.model_validate_json(answer.read())
        except urllib.error.URLError, TimeoutError, ConnectionError, ValidationError:
            return None

    def when_ready(self) -> str:
        """Wait until the boot is ready ('', the start page) or a step failed ('#/boot', the loading screen)."""
        while True:
            state = self.boot()
            if state is not None and (state.ready or state.failed):
                return '' if state.ready else '#/boot'
            time.sleep(self.POLL)

    def start(self) -> threading.Thread:
        """Wait for the boot and open the window, beside the server: a daemon thread, gone when the studio stops."""
        thread = threading.Thread(target=lambda: self.open(self.when_ready()), name='app-window', daemon=True)
        thread.start()
        return thread

    def close(self) -> None:
        """Close the window this studio opened, if it is still open."""
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()

    @staticmethod
    def detached(argv: list[str]) -> subprocess.Popen[bytes]:
        """The browser in a session of its own, so ctrl+c reaches the studio first, which then closes it."""
        return subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
