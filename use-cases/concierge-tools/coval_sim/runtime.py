"""Child processes this tool starts and stops: the bot and the tunnel."""

import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Self


class NotReadyError(RuntimeError):
    """A process exited, or did not become ready in time."""


class Process:
    """A child process with its output in a log file.

    It runs in its own session, so stop() ends it and every child it started.
    uv, for one, starts the Python process as a child of its own.

    Args:
        name: What the process is, for error messages.
        cmd: The command line.
        cwd: The working directory.
        env: The whole environment.
        log: Where stdout and stderr go.
    """

    def __init__(self, name: str, cmd: list[str], cwd: Path, env: dict[str, str], log: Path) -> None:
        self.name = name
        self.log = log
        with log.open('w') as out:
            self._proc = subprocess.Popen(
                cmd, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True
            )

    def output(self) -> str:
        """Everything the process has written so far."""
        return self.log.read_text(errors='replace')

    def wait_until(self, ready: Callable[[], bool], timeout_s: float) -> None:
        """Block until ready() is true.

        A process that never gets ready is stopped before this raises, so the
        caller has nothing left to clean up.

        Raises:
            NotReadyError: If the process exits first, or timeout_s passes.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise NotReadyError(f'{self.name} exited; see {self.log}')
            if ready():
                return
            time.sleep(1)
        self.stop()
        raise NotReadyError(f'{self.name} was not ready after {timeout_s:.0f}s; see {self.log}')

    def stop(self) -> None:
        """End the process and its children, forcefully after 20 seconds."""
        if self._proc.poll() is not None:
            return
        os.killpg(self._proc.pid, signal.SIGTERM)
        try:
            self._proc.wait(20)
        except subprocess.TimeoutExpired:
            os.killpg(self._proc.pid, signal.SIGKILL)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()


CONNECTED = 'WebSocket connection accepted'
"""What Pipecat's runner logs when a Twilio call's media stream connects: where a call begins, seen from outside."""


def ceiling(bot: Process, seconds: float, marker: str = CONNECTED) -> threading.Thread:
    """Stop the bot `seconds` after its log first shows the call connecting: a call-length limit from outside.

    For a bot with no limit of its own (salon-pipecat-flows has no
    `MAX_CALL_SECONDS`), so its reference code stays unchanged. Stopping the
    bot drops the call, which ends the simulation at Coval.

    ponytail: one ceiling for the whole bot, from the first call, so `plan`
    allows such a bot one call at a time. A per-call ceiling (one watch per
    connection, closing that call's websocket) if its calls ever run at once.
    """

    def watch() -> None:
        while bot._proc.poll() is None:
            if marker in bot.output():
                time.sleep(seconds)
                bot.stop()
                return
            time.sleep(0.5)

    thread = threading.Thread(target=watch, name=f'{bot.name} ceiling', daemon=True)
    thread.start()
    return thread


def answers_http(url: str) -> bool:
    """Whether anything answers HTTP at url. An error status still counts."""
    try:
        urllib.request.urlopen(url, timeout=5)
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False
    return True


def resolves_publicly(host: str) -> bool:
    """Whether public DNS has an address for host, asked of Cloudflare's resolver over HTTPS.

    Over HTTPS rather than through this machine's resolver, whose cache would
    remember a lookup made before the host existed as missing.
    """
    request = urllib.request.Request(
        f'https://cloudflare-dns.com/dns-query?name={host}&type=A', headers={'accept': 'application/dns-json'}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            return bool(json.load(reply).get('Answer'))
    except OSError, ValueError:
        return False


class Tunnel(Process):
    """A cloudflared quick tunnel to a local port: no account, a new host each time.

    Ready means two things. cloudflared says the edge accepted the connection,
    which can come twenty seconds after it prints the host. And public DNS
    answers for the host: a run once launched every call against a host Coval
    could not resolve yet, and all five failed before reaching the bot.
    Probing the public URL through this machine's resolver does not work: a
    lookup made before the host exists is cached as missing.

    Args:
        port: The local port to expose.
        workdir: Where the log goes.
    """

    def __init__(self, port: int, workdir: Path) -> None:
        super().__init__(
            f'cloudflared on port {port}',
            ['cloudflared', 'tunnel', '--no-autoupdate', '--url', f'http://127.0.0.1:{port}'],
            workdir,
            dict(os.environ),
            workdir / f'tunnel-{port}.log',
        )
        self.wait_until(lambda: 'Registered tunnel connection' in self.output(), 120)
        found = re.search(r'https://([a-z0-9-]+\.trycloudflare\.com)', self.output())
        if not found:
            self.stop()
            raise NotReadyError(f'{self.name} printed no host; see {self.log}')
        self.host = found.group(1)
        self.wait_until(lambda: resolves_publicly(self.host), 120)


@contextmanager
def twilio_voice_url(number: str, url: str, binary: str = 'twilio') -> Iterator[str]:
    """Point one Twilio number's voice webhook at `url` for a job, and put the one it had back afterwards.

    Through the Twilio CLI's active profile, so no Twilio credential passes
    through this tool. Only this number is read or written, and only its
    voice URL and method: never a listing of every number, whose webhook URLs
    can hold other services' tokens. Yields the voice URL it replaced.

    ponytail: one job per number at a time. Two jobs sharing a number would
    restore each other's URL; give each its own number, or share one `serve`.
    """

    def cli(*args: str) -> list[dict]:
        done = subprocess.run([binary, *args, '-o', 'json'], capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise RuntimeError(f'twilio {args[0]}: {done.stderr.strip()[:300]}')
        return json.loads(done.stdout or '[]')

    found = cli('api:core:incoming-phone-numbers:list', '--phone-number', number)
    if len(found) != 1:
        raise RuntimeError(f'the Twilio account holds {len(found)} numbers matching {number}, not one')
    sid, previous, method = (
        found[0]['sid'],
        found[0].get('voiceUrl') or '',
        found[0].get('voiceMethod') or 'POST',
    )
    cli('api:core:incoming-phone-numbers:update', '--sid', sid, '--voice-url', url, '--voice-method', 'POST')
    try:
        yield previous
    finally:
        cli(
            'api:core:incoming-phone-numbers:update',
            '--sid',
            sid,
            '--voice-url',
            previous,
            '--voice-method',
            method,
        )
        now = cli('api:core:incoming-phone-numbers:list', '--phone-number', number)[0].get('voiceUrl') or ''
        if now != previous:
            print(f'warning: {number} voice URL is {shown(now)}, not {shown(previous)} as before', flush=True)


def shown(url: str) -> str:
    """A webhook URL without its query, which can hold a token: fit for a log."""
    return url.split('?', 1)[0] or '(none)'


CALL_SID = re.compile(r'\bCA[0-9a-f]{32}\b')


def call_sids(log: Path) -> list[str]:
    """The Twilio calls a bot log saw, in order.

    On a phone line Coval reports no call SID of its own (session 4b run
    FzMcHyvp), so the bot's log is the only link from a simulation to its
    Langfuse session, which the bot keys by this SID.
    """
    text = log.read_text(errors='replace') if log.exists() else ''
    return list(dict.fromkeys(CALL_SID.findall(text)))
