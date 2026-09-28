"""Child processes this tool starts and stops: the bot and the tunnel."""

import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Self


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


TERMINAL_WAIT_S = 900.0
"""How long a campaign waits for its calls to end before it gives the number back. Past it, it leaves the number."""


class Campaign:
    """One campaign owns one Twilio number's voice webhook for its whole life, and gives it back only once.

    The number is pointed at `https://<host>/?campaign=<label>` once, when the
    campaign starts (`serve`, or a lone `run`), never per job: jobs sent at
    it with `run --host` read the record and touch nothing
    (`owning`). The record is a file (`records/campaign-<number>.json`):
    which campaign owns the number, the URL it replaced and the one it set,
    and the owner's pid. A second campaign for the number is refused while
    the owner lives; a record left by an owner that died is taken over,
    keeping the URL from before it, so the first URL is still the one
    restored.

    On the way out the number is given back only when every call is over: no
    call admitted or active in the runner's file, and none queued, ringing or
    in progress to the number at Twilio. A polling deadline passing is not a
    call ending, so after `TERMINAL_WAIT_S` it stops waiting and leaves the
    number pointed, and says so. And it restores only if the number still
    holds this campaign's URL: a later change by anybody else is left alone.

    Through the Twilio CLI's active profile, so no credential passes through
    this tool. Only this number is read or written, and only its voice URL
    and method.
    """

    def __init__(
        self,
        number: str,
        host: str,
        label: str,
        records: Path,
        *,
        runner_db: Path | None = None,
        diary: Path | None = None,
        binary: str = 'twilio',
        wait_s: float = TERMINAL_WAIT_S,
        poll_s: float = 5.0,
    ) -> None:
        self.number, self.label, self.binary = number, label, binary
        self.url = f'https://{host}/?campaign={urllib.parse.quote(label)}'
        self.path = records / f'campaign-{number.lstrip("+")}.json'
        self.runner_db, self.diary = runner_db, diary
        self.wait_s, self.poll_s = wait_s, poll_s
        self.previous = ''
        self._record: dict[str, Any] = {}

    def cli(self, *args: str) -> list[dict]:
        done = subprocess.run([self.binary, *args, '-o', 'json'], capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise RuntimeError(f'twilio {args[0]}: {done.stderr.strip()[:300]}')
        return json.loads(done.stdout or '[]')

    def _number(self) -> dict:
        found = self.cli('api:core:incoming-phone-numbers:list', '--phone-number', self.number)
        if len(found) != 1:
            raise RuntimeError(
                f'the Twilio account holds {len(found)} numbers matching {self.number}, not one'
            )
        return found[0]

    @staticmethod
    def read(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text())
        except FileNotFoundError, ValueError:
            return None

    @classmethod
    def owning(cls, number: str, host: str, records: Path) -> dict:
        """The live campaign whose URL points `number` at `host`, for a job sent at it. Anything else is refused."""
        held = cls.read(records / f'campaign-{number.lstrip("+")}.json')
        if held is None or not _alive(held.get('pid', 0)):
            raise RuntimeError(f'no live campaign owns {number}: start `serve --phone-number {number}` first')
        if urllib.parse.urlsplit(held['url']).hostname != host:
            raise RuntimeError(f'{number} is owned by campaign {held["label"]!r} at another host, not {host}')
        return held

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        held = self.read(self.path)
        if held is not None and _alive(held.get('pid', 0)):
            raise RuntimeError(f'{self.number} is owned by campaign {held["label"]!r} (pid {held["pid"]})')
        line = self._number()
        self.previous = held['previous'] if held else line.get('voiceUrl') or ''
        method = held['method'] if held else line.get('voiceMethod') or 'POST'
        if held:
            print(
                f'taking {self.number} over from campaign {held["label"]!r}, whose owner is gone', flush=True
            )
        record: dict[str, Any] = {
            'label': self.label,
            'number': self.number,
            'sid': line['sid'],
            'previous': self.previous,
            'method': method,
            'url': self.url,
            'pid': os.getpid(),
            'runner_db': str(self.runner_db or ''),
            'diary': str(self.diary or ''),
            'started': time.time(),
        }
        self.path.write_text(json.dumps(record))
        self.cli(
            'api:core:incoming-phone-numbers:update',
            '--sid',
            line['sid'],
            '--voice-url',
            self.url,
            '--voice-method',
            'POST',
        )
        print(f'{self.number} now owned by campaign {self.label!r} (was {shown(self.previous)})', flush=True)
        self._record = record
        return self

    def calls_left(self) -> list[str]:
        """What still holds a call: the runner's admitted or active calls, and Twilio's live calls to the number."""
        left = []
        if self.runner_db and self.runner_db.exists():
            import sqlite3

            db = sqlite3.connect(self.runner_db)
            try:
                left += [
                    f'runner {sid}'
                    for (sid,) in db.execute(
                        "SELECT call_sid FROM calls WHERE state IN ('admitted', 'active')"
                    )
                ]
            finally:
                db.close()
        for status in ('queued', 'ringing', 'in-progress'):
            left += [
                f'twilio {c.get("sid")}'
                for c in self.cli('api:core:calls:list', '--to', self.number, '--status', status)
            ]
        return left

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, tb: TracebackType | None
    ) -> None:
        deadline = time.monotonic() + self.wait_s
        while left := self.calls_left():
            if time.monotonic() > deadline:
                print(
                    f'warning: {len(left)} call(s) still running after {self.wait_s:.0f}s ({", ".join(left[:5])}); '
                    f'{self.number} is left pointed at campaign {self.label!r}, and its record kept at {self.path}',
                    flush=True,
                )
                return
            time.sleep(self.poll_s)
        now = self._number().get('voiceUrl') or ''
        if now == self.url:
            record = self._record
            self.cli(
                'api:core:incoming-phone-numbers:update',
                '--sid',
                record['sid'],
                '--voice-url',
                record['previous'],
                '--voice-method',
                record['method'],
            )
            back = self._number().get('voiceUrl') or ''
            if back != record['previous']:
                print(
                    f'warning: {self.number} voice URL is {shown(back)}, not {shown(record["previous"])}',
                    flush=True,
                )
            else:
                print(f'{self.number} given back: {shown(back)}', flush=True)
        else:
            print(
                f'warning: {self.number} was changed since this campaign set it ({shown(now)}): left as it is',
                flush=True,
            )
        self.path.unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def correlate(
    simulations: Sequence[Mapping[str, Any]],
    calls: dict[str, set[str]],
    phone_of_case: dict[str, str],
) -> dict[str, str]:
    """Which call each Coval simulation was, by evidence, never by order: its CallSid, or 'unknown'.

    `simulations` carry `simulation_id`, `test_case_id` and Coval's own
    `call_sid` when it has one. `calls` maps each runner call of the
    campaign to the caller identities its diary intents were made for. A
    simulation binds to a call when Coval names it, or when exactly one call
    acted for the phone its test case gives the caller. Anything else, and
    any call two simulations would share, is unknown: ambiguous evidence is
    no evidence.
    """
    bound: dict[str, str] = {}
    for sim in simulations:
        sid = sim.get('call_sid') or ''
        if sid in calls:
            bound[str(sim['simulation_id'])] = sid
            continue
        phone = phone_of_case.get(str(sim.get('test_case_id') or ''), '')
        matches = [call for call, who in calls.items() if phone and phone in who]
        bound[str(sim['simulation_id'])] = matches[0] if len(matches) == 1 else 'unknown'
    taken = [call for call in bound.values() if call != 'unknown']
    return {
        sim: call if call == 'unknown' or taken.count(call) == 1 else 'unknown' for sim, call in bound.items()
    }


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
