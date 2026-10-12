"""A runner the studio starts itself (`python -m voice_runner`), which `slng studio up` does, and one more from the CLI."""

import os
import signal
import subprocess
import sys
from pathlib import Path

from studio.settings import Settings


class RunnerProcess:
    """One runner process, linked to the studio, stopped with it: its calls are let finish first."""

    def __init__(
        self, settings: Settings, port: int, log: Path | None = None, max_calls: int | None = None
    ) -> None:
        self.settings = settings
        self.port = port
        self.log = log
        self.max_calls = max_calls
        self.process: subprocess.Popen[bytes] | None = None

    @staticmethod
    def available() -> bool:
        """Whether the runner package is installed beside the studio."""
        try:
            import voice_runner  # noqa: F401
        except ImportError:
            return False
        return True

    def argv(self) -> list[str]:
        """The command that starts it, linked to this studio, carrying this repository's use cases."""
        calls = ['--max-calls', str(self.max_calls)] if self.max_calls is not None else []
        link = f'ws://{self.settings.host}:{self.settings.port}/api/runners/link'
        root = ['--root', str(self.settings.root)]
        return [
            sys.executable,
            '-m',
            'voice_runner',
            '--port',
            str(self.port),
            *calls,
            '--studio',
            link,
            *root,
        ]

    def env(self) -> dict[str, str]:
        """Its environment: spans to the studio's collector, and no banner in its log."""
        return {
            **os.environ,
            'OTEL_EXPORTER_OTLP_ENDPOINT': self.settings.otlp_endpoint,
            'PYDANTIC_AI_NO_BANNER': '1',
        }

    def start(self) -> None:
        """Start it in the background, its output in its log."""
        if self.log is None:
            raise ValueError('a runner started in the background needs a log file')
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open('ab') as out:
            self.process = subprocess.Popen(
                self.argv(), env=self.env(), stdout=out, stderr=subprocess.STDOUT, cwd=self.settings.root
            )

    @property
    def exited(self) -> int | None:
        """Its exit code once it has stopped; None while it runs or before it started."""
        return None if self.process is None else self.process.poll()

    def stop(self, wait: float = 20.0) -> None:
        """SIGTERM, so it drains its calls; again after `wait`; then kill."""
        if self.process is None or self.process.poll() is not None:
            return
        for signal_number, patience in ((signal.SIGTERM, wait), (signal.SIGTERM, 5.0)):
            self.process.send_signal(signal_number)
            try:
                self.process.wait(patience)
                return
            except subprocess.TimeoutExpired:
                continue
        self.process.kill()
