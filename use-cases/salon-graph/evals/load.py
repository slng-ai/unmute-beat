"""The offline Stage C ramp: real runner, real transport and adapter, scripted providers, fake Twilio phones.

    uv run python -m evals.load ramp --calls 1,2,5 --seconds 60 --workers 2 --out DIR
    uv run python -m evals.load faults --out DIR

`ramp` starts `server.py` under uvicorn with `--workers`, with the doubles
(`evals/doubles.py`, `VOICE_DOUBLES=1`), then rings it from fake phones that
behave as Twilio does: each POSTs the voice webhook, opens the media stream
over a real TCP websocket, sends 20 ms µ-law frames of caller speech (made
with macOS `say`) on a steady clock, plays the bot's audio back at real
time, returns each mark when playout reaches it and answers a clear by
dropping its buffer and returning the pending marks at once. `N` lanes ring
side by side, each redialling with a new caller until `--seconds` have run.
The callers cover plain bookings, fragments at several pauses, a caller who
cuts into the read-back, a read-back whose TTS audio is lost, and two
callers racing for one time.

Everything is judged from records the callers did not write: the diary file
(bookings, intents), the runner file (calls), each worker's write audits and
stats files, and `ps` for memory. The frozen limits are in `LIMITS`.

`faults` runs the worker-failure checks on two workers: a kill after the
intent is recorded and before it is sent, a kill after the diary committed
and before anyone was told, a worker that hangs inside the write while the
caller rings again, overload beyond the ceiling, and a graceful drain.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import os
import platform
import signal
import sqlite3
import statistics
import subprocess
import sys
import time
import uuid
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import websockets

os.environ.setdefault('GOOGLE_API_KEY', 'offline')

from evals.doubles import CLOCK, Caller, Utterance, booking

HERE = Path(__file__).resolve().parents[1]
FRAME = 160
"""µ-law bytes in one 20 ms frame at 8 kHz."""
LIMITS = {
    'max_active_calls': 5,
    'unauthorised_effects': 0,
    'duplicate_effects': 0,
    'capacity_violations': 0,
    'cross_call_leaks': 0,
    'loop_lag_p99_ms': 100.0,
    'rss_share_of_ram': 0.80,
    'sustained_seconds_at_5': 60.0,
}
"""The engineering limits, frozen before any measurement (the Stage C brief)."""
DAYS = ('Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday')
HOURS = ('09:00', '10:00', '11:00', '12:00', '13:00', '14:00', '15:00')


def ulaw(pcm: np.ndarray) -> bytes:
    """G.711 µ-law, from 16-bit PCM."""
    bias, clip = 0x84, 32635
    x = np.clip(pcm.astype(np.int32), -clip, clip)
    sign = (x < 0).astype(np.int32) << 7
    x = np.abs(x) + bias
    exponent = np.floor(np.log2(x)).astype(np.int32) - 7
    exponent = np.clip(exponent, 0, 7)
    mantissa = (x >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8).tobytes()


SILENCE = ulaw(np.zeros(FRAME, dtype=np.int16))


def spoken(text: str, cache: Path) -> bytes:
    """The words as µ-law 8 kHz audio, from macOS `say`, cached by their text.

    Said without punctuation: `say` pauses up to 340 ms at a comma, which is a
    real caller's pause too, but here one piece must be one stretch of speech
    (at most 120 ms of silence inside it), so the STT double hears it as one.
    """
    cache.mkdir(parents=True, exist_ok=True)
    words = ''.join(c for c in text if c.isalnum() or c.isspace())
    path = cache / f'{hashlib.sha1(words.encode()).hexdigest()[:16]}.wav'
    if not path.exists():
        subprocess.run(['say', '-o', str(path), '--data-format=LEI16@8000', words], check=True)
    with wave.open(str(path)) as audio:
        pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16)
    return ulaw(pcm)


@dataclass
class Turn:
    said: str
    ended_at: float
    answered_at: float | None = None


@dataclass
class Record:
    fixture: str
    phone: str
    call_sid: str
    lane: int
    admitted: bool = False
    started: float = 0.0
    ended: float = 0.0
    closed_by: str = ''
    turns: list[Turn] = field(default_factory=list)
    bot_audio_seconds: float = 0.0
    caller_audio_seconds: float = 0.0
    marks_sent: int = 0
    marks_returned: int = 0
    clears: int = 0


class Phone:
    """One call as Twilio carries it, against the runner at `base`."""

    def __init__(self, base: str, caller: Caller, lane: int, cache: Path) -> None:
        self.base, self.caller, self.cache = base, caller, cache
        self.record = Record(caller.name, caller.phone, 'CA' + uuid.uuid4().hex, lane)
        self.stream = 'MZ' + uuid.uuid4().hex
        self._out: list[bytes] = []
        """Caller audio waiting to be sent, one frame each."""
        self._play: list[tuple[str, Any]] = []
        """Bot audio and marks, in the order they came, played at real time."""
        self._last_bot = 0.0
        self._spoke_at = 0.0
        """When the caller last finished speaking: the next line waits for an answer after it."""
        self._bot_started = asyncio.Event()
        self._closed = asyncio.Event()

    async def run(self) -> Record:
        async with httpx.AsyncClient(timeout=10) as http:
            answer = await http.post(
                f'http://{self.base}/?fixture={self.caller.name}',
                content=f'CallSid={self.record.call_sid}&From=%2B{self.caller.phone[1:]}&To=%2B447427919928',
                headers={'content-type': 'application/x-www-form-urlencoded'},
            )
        if '<Reject' in answer.text:
            self.record.closed_by = 'rejected'
            return self.record
        self.record.admitted, self.record.started = True, time.perf_counter()
        params = {'fixture': self.caller.name}
        async with websockets.connect(f'ws://{self.base}/ws', max_size=None) as ws:
            await ws.send(json.dumps({'event': 'connected', 'protocol': 'Call', 'version': '1.0.0'}))
            await ws.send(
                json.dumps(
                    {
                        'event': 'start',
                        'sequenceNumber': '1',
                        'streamSid': self.stream,
                        'start': {
                            'streamSid': self.stream,
                            'accountSid': 'AC' + '0' * 32,
                            'callSid': self.record.call_sid,
                            'tracks': ['inbound'],
                            'mediaFormat': {'encoding': 'audio/x-mulaw', 'sampleRate': 8000, 'channels': 1},
                            'customParameters': params,
                        },
                    }
                )
            )
            tasks = [
                asyncio.create_task(self._send(ws)),
                asyncio.create_task(self._receive(ws)),
                asyncio.create_task(self._playout(ws)),
            ]
            try:
                await self._script(ws)
            finally:
                for task in tasks:
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task
        self.record.ended = time.perf_counter()
        return self.record

    async def _script(self, ws) -> None:
        for utterance in self.caller.script:
            if self._closed.is_set():
                return
            if utterance.barge is not None:
                self._bot_started.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._bot_started.wait(), 15)
                await asyncio.sleep(utterance.barge)
            else:
                await self._until_quiet(since=self._spoke_at)
            await self._say(utterance)
            self._spoke_at = time.perf_counter()
        if self.caller.leave_after is not None:
            await asyncio.sleep(self.caller.leave_after)
        else:
            await self._until_quiet(since=self._spoke_at, quiet=2.0, limit=20)
        if not self._closed.is_set():
            self.record.closed_by = 'caller'
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({'event': 'stop', 'streamSid': self.stream, 'stop': {}}))
                await ws.close()

    async def _until_quiet(self, since: float, quiet: float = 0.8, limit: float = 12.0) -> None:
        """Wait for the bot to answer after `since` and then be silent `quiet` seconds; `limit` at most (a lost answer)."""
        started = time.perf_counter()
        while time.perf_counter() - started < limit and not self._closed.is_set():
            idle = not self._play and time.perf_counter() - self._last_bot >= quiet
            if idle and self._last_bot > since:
                return
            await asyncio.sleep(0.02)

    async def _say(self, utterance: Utterance) -> None:
        for index, piece in enumerate(utterance.pieces):
            audio = spoken(piece, self.cache)
            frames = [audio[i : i + FRAME].ljust(FRAME, SILENCE[:1]) for i in range(0, len(audio), FRAME)]
            self._out.extend(frames)
            self.record.caller_audio_seconds += len(frames) * 0.02
            while self._out and not self._closed.is_set():
                await asyncio.sleep(0.02)
            self.record.turns.append(Turn(piece, time.perf_counter()))
            if index < len(utterance.gaps):
                await asyncio.sleep(utterance.gaps[index])

    async def _send(self, ws) -> None:
        """Twenty milliseconds of caller audio, on a steady clock: speech when there is some, silence otherwise."""
        start, n = time.perf_counter(), 0
        while True:
            chunk = self._out.pop(0) if self._out else SILENCE
            n += 1
            message = {
                'event': 'media',
                'sequenceNumber': str(n + 1),
                'streamSid': self.stream,
                'media': {
                    'track': 'inbound',
                    'chunk': str(n),
                    'timestamp': str(n * 20),
                    'payload': base64.b64encode(chunk).decode(),
                },
            }
            try:
                await ws.send(json.dumps(message))
            except websockets.ConnectionClosed:
                self._closed.set()
                return
            await asyncio.sleep(max(0.0, start + n * 0.02 - time.perf_counter()))

    async def _receive(self, ws) -> None:
        try:
            async for data in ws:
                message = json.loads(data)
                event = message.get('event')
                if event == 'media':
                    audio = base64.b64decode(message['media']['payload'])
                    self._play.append(('media', audio))
                    self.record.bot_audio_seconds += len(audio) / 8000
                    now = time.perf_counter()
                    for turn in self.record.turns:
                        if turn.answered_at is None and turn.ended_at <= now:
                            turn.answered_at = now
                    self._bot_started.set()
                elif event == 'mark':
                    self.record.marks_sent += 1
                    self._play.append(('mark', message['mark']['name']))
                elif event == 'clear':
                    self.record.clears += 1
                    pending = [name for kind, name in self._play if kind == 'mark']
                    self._play.clear()
                    for name in pending:
                        await self._echo(ws, name)
        except websockets.ConnectionClosed:
            pass
        self._closed.set()
        if not self.record.closed_by:
            self.record.closed_by = 'bot'

    async def _echo(self, ws, name: str) -> None:
        self.record.marks_returned += 1
        with contextlib.suppress(websockets.ConnectionClosed):
            await ws.send(json.dumps({'event': 'mark', 'streamSid': self.stream, 'mark': {'name': name}}))

    async def _playout(self, ws) -> None:
        """Play the bot's audio at real time; a mark goes back when playout reaches it."""
        start, n, left = time.perf_counter(), 0, 0
        while True:
            while self._play and self._play[0][0] == 'mark':
                await self._echo(ws, self._play.pop(0)[1])
            budget = FRAME
            while budget and self._play and self._play[0][0] == 'media':
                audio = self._play[0][1]
                take = min(budget, len(audio) - left)
                left += take
                budget -= take
                if left >= len(audio):
                    self._play.pop(0)
                    left = 0
            if budget < FRAME:
                self._last_bot = time.perf_counter()
            n += 1
            await asyncio.sleep(max(0.0, start + n * 0.02 - time.perf_counter()))


def _fixture(lane: int, round_: int, step: str) -> Caller:
    """The caller lane `lane` rings with on its `round_`th call: distinct phone and time, and one variant per lane."""
    phone = f'+346990{lane}{round_:04d}'
    day, at = DAYS[lane % len(DAYS)], HOURS[round_ % len(HOURS)]
    name = f'{step}-l{lane}-r{round_}'
    request = f"Hi, I'd like a haircut on {day} at {datetime_words(at)}."
    number, yes, bye = "Yes, that's my number.", 'Yes please.', "No, that's all. Bye."
    variant = lane % 5
    if variant == 1:
        day = DAYS[0]
        at = HOURS[round_ % len(HOURS)]
        request = f"Hi, I'd like a haircut on {day} at {datetime_words(at)}."
        return booking(
            name,
            phone,
            day,
            at,
            script=(
                Utterance(("Hi, I'd like a haircut", f'on {day} at {datetime_words(at)}.'), gaps=(0.6,)),
                Utterance((number,)),
                Utterance((yes,)),
                Utterance(('Yes, another one please.',)),
                Utterance((yes,)),
                Utterance((bye,)),
            ),
        )
    if variant == 2:
        return booking(
            name,
            phone,
            day,
            at,
            script=(
                Utterance((request,)),
                Utterance((number,)),
                Utterance((yes,), barge=0.4),
                Utterance((yes,)),
                Utterance((yes,)),
                Utterance((bye,)),
            ),
        )
    if variant == 3:
        return booking(
            name,
            phone,
            day,
            at,
            lose_tts='Shall I book',
            script=(
                Utterance((request,)),
                Utterance((number,)),
                Utterance((yes,)),
                Utterance((yes,)),
                Utterance((yes,)),
                Utterance((bye,)),
            ),
        )
    if variant == 4:
        return booking(
            name,
            phone,
            day,
            at,
            script=(
                Utterance(
                    ("Hi, I'd like", 'a haircut', f'on {day} at {datetime_words(at)}.'), gaps=(0.45, 1.0)
                ),
                Utterance((number,)),
                Utterance((yes,)),
                Utterance((bye,)),
            ),
        )
    return booking(
        name,
        phone,
        day,
        at,
        script=(Utterance((request,)), Utterance((number,)), Utterance((yes,)), Utterance((bye,))),
    )


def datetime_words(at: str) -> str:
    hour = int(at[:2])
    return f'{(hour - 1) % 12 + 1} {"am" if hour < 12 else "pm"}'


def _write_fixture(folder: Path, caller: Caller) -> None:
    data = asdict(caller)
    data['script'] = [asdict(u) for u in caller.script]
    (folder / f'{caller.name}.json').write_text(json.dumps(data))


class Runner:
    """`server.py` under uvicorn, with the doubles, its own diary, runner file, audits and stats."""

    def __init__(self, out: Path, workers: int, port: int, **env: str) -> None:
        self.out, self.workers, self.port = out, workers, port
        for sub in ('audits', 'stats', 'fixtures', 'audio'):
            (out / sub).mkdir(parents=True, exist_ok=True)
        self.env = {
            **os.environ,
            'VOICE_DOUBLES': '1',
            'CARRIER_NUMBER': '1',
            'GOOGLE_API_KEY': 'offline',
            'SLNG_API_KEY': 'offline',
            'DIARY_PATH': str(out / 'diary.db'),
            'RUNNER_DB': str(out / 'runner.db'),
            'DIARY_CLOCK': CLOCK.isoformat(),
            'AUDIT_DIR': str(out / 'audits'),
            'STATS_DIR': str(out / 'stats'),
            'LOAD_FIXTURES': str(out / 'fixtures'),
            'MAX_CALL_SECONDS': '150',
            'TWILIO_ACCOUNT_SID': 'AC' + '0' * 32,
            'TWILIO_AUTH_TOKEN': 'offline',
            'LANGFUSE_TRACING_ENABLED': 'false',
            **env,
        }
        self.process: subprocess.Popen | None = None
        self.log = out / 'server.log'

    @property
    def base(self) -> str:
        return f'127.0.0.1:{self.port}'

    def start(self) -> float:
        started = time.perf_counter()
        self.process = subprocess.Popen(
            [
                sys.executable,
                '-m',
                'uvicorn',
                'server:app',
                '--workers',
                str(self.workers),
                '--host',
                '127.0.0.1',
                '--port',
                str(self.port),
                '--timeout-graceful-shutdown',
                '170',
            ],
            cwd=HERE,
            env=self.env,
            stdout=self.log.open('a'),
            stderr=subprocess.STDOUT,
        )
        while len(self.workers_now()) < self.workers:
            if self.process.poll() is not None:
                raise RuntimeError(f'the runner exited: see {self.log}')
            if time.perf_counter() - started > 120:
                raise RuntimeError('the runner did not become ready in 120 s')
            time.sleep(0.2)
        return time.perf_counter() - started

    def workers_now(self, fresh: float = 2.5) -> dict[int, dict[str, Any]]:
        """Each live worker's latest stats file, if written in the last `fresh` seconds."""
        found = {}
        for path in (self.out / 'stats').glob('*.json'):
            with contextlib.suppress(ValueError, OSError):
                data = json.loads(path.read_text())
                if time.time() - data['at'] <= fresh:
                    found[data['pid']] = data
        return found

    def rss_mb(self) -> dict[int, float]:
        pids = [str(pid) for pid in self.workers_now()]
        if self.process is not None:
            pids.append(str(self.process.pid))
        if not pids:
            return {}
        done = subprocess.run(['ps', '-o', 'pid=,rss=', '-p', ','.join(pids)], capture_output=True, text=True)
        return {
            int(p): int(r) / 1024
            for p, r in (line.split() for line in done.stdout.splitlines() if line.strip())
        }

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(30)
            except subprocess.TimeoutExpired:
                self.process.kill()


async def _monitor(runner: Runner, samples: list[dict[str, Any]], stop: asyncio.Event) -> None:
    while not stop.is_set():
        samples.append(
            {'at': time.perf_counter(), 'workers': runner.workers_now(), 'rss_mb': runner.rss_mb()}
        )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), 1.0)


async def ramp_step(
    out: Path,
    calls: int,
    seconds: float,
    workers: int,
    port: int,
    lanes: list[int] | None = None,
    idle: float = 0.0,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """`calls` lanes side by side for `seconds`, each redialling with a new caller, on a fresh runner.

    `idle` keeps the runner up that long after the last call, still sampled,
    so a memory run shows whether it gives memory back. Every sample is
    written to `samples.json` beside the report.
    """
    runner = Runner(out, workers, port, **(env or {}))
    ready = runner.start()
    samples: list[dict[str, Any]] = []
    stop = asyncio.Event()
    monitor = asyncio.create_task(_monitor(runner, samples, stop))
    records: list[Record] = []
    fixtures: list[Caller] = []
    began = time.perf_counter()

    async def lane(index: int) -> None:
        round_ = 0
        while time.perf_counter() - began < seconds or round_ == 0:
            caller = _fixture(index, round_, f'c{calls}')
            fixtures.append(caller)
            _write_fixture(out / 'fixtures', caller)
            records.append(await Phone(runner.base, caller, index, out / 'audio').run())
            round_ += 1

    try:
        await asyncio.gather(*(lane(i) for i in (lanes or range(calls))))
        samples.append({'at': time.perf_counter(), 'mark': 'calls over'})
        await asyncio.sleep(3 + idle)
    finally:
        stop.set()
        await monitor
        idle_workers = runner.workers_now()
        runner.stop()
    (out / 'samples.json').write_text(json.dumps(samples))
    return judge(out, calls, workers, ready, records, fixtures, samples, idle_workers)


def snapshot(path: Path) -> dict[str, list[tuple]]:
    db = sqlite3.connect(path)
    try:
        return {
            'bookings': db.execute(
                'SELECT booking_id, phone, day, at, starts, ends, revision, active FROM bookings ORDER BY id'
            ).fetchall(),
            'intents': db.execute('SELECT id, session, operation, subject FROM intents').fetchall(),
            'operations': db.execute('SELECT token, result FROM operations').fetchall(),
        }
    finally:
        db.close()


def _audits(folder: Path) -> dict[str, list[dict[str, Any]]]:
    found = {}
    for path in folder.glob('*.jsonl'):
        found[path.stem] = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return found


def authorised(audit: dict[str, Any]) -> bool:
    """A write that needed a yes ran only on transport playback of its own question, played before the answer began."""
    if not audit.get('needs_approval'):
        return True
    playback = audit.get('playback') or {}
    marks = [m for m in playback.get('marks', []) if m.get('proposal') == audit.get('proposal')]
    if not marks or audit.get('simulated_playout'):
        return False
    return marks[-1]['kind'] == 'played' and marks[-1]['at'] <= playback.get('answered_at', '')


def overlap(records: list[Record]) -> dict[str, Any]:
    """How many calls were connected at once, second by second, from the phones' own connect and hang-up times."""
    spans = [(r.started, r.ended) for r in records if r.admitted and r.ended]
    if not spans:
        return {'max': 0}
    t0, t1 = min(s for s, _ in spans), max(e for _, e in spans)
    counts = []
    t = t0
    while t < t1:
        counts.append(sum(1 for s, e in spans if s <= t < e))
        t += 0.25
    return {
        'max': max(counts),
        'seconds_at': {str(n): counts.count(n) * 0.25 for n in range(max(counts) + 1)},
        'longest_run_at_max_s': _longest(counts, max(counts)) * 0.25,
        'span_s': t1 - t0,
    }


def _longest(counts: list[int], value: int) -> int:
    best = run = 0
    for c in counts:
        run = run + 1 if c >= value else 0
        best = max(best, run)
    return best


def judge(
    out: Path,
    calls: int,
    workers: int,
    ready: float,
    records: list[Record],
    fixtures: list[Caller],
    samples: list[dict[str, Any]],
    idle: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    state = snapshot(out / 'diary.db')
    audits = _audits(out / 'audits')
    runner_calls = [dict(r) for r in _runner_calls(out / 'runner.db')]
    by_sid = {r.call_sid: r for r in records}
    phone_of = {r.call_sid: r.phone for r in records}
    active = [b for b in state['bookings'] if b[7] == 1]
    per_phone: dict[str, int] = {}
    for b in active:
        per_phone[b[1]] = per_phone.get(b[1], 0) + 1
    duplicates = sum(n - 1 for n in per_phone.values() if n > 1)
    clashes = sum(1 for i, a in enumerate(active) for b in active[i + 1 :] if a[4] < b[5] and b[4] < a[5])
    strangers = [b for b in active if b[1] not in phone_of.values()]
    leaks = [i for i in state['intents'] if i[1] in phone_of and i[3] != phone_of[i[1]]]
    writes = [
        (sid, a) for sid, rows in audits.items() for a in rows if a.get('status') == 'ok' and a.get('changes')
    ]
    unauthorised = [a for _, a in writes if not authorised(a)]
    booked_per_call = {
        sid: sum(1 for s, a in writes if s == sid and a.get('action') == 'book_appointment') for sid in by_sid
    }
    lags = [w['lag'].get('p99_ms', 0) for s in samples for w in s['workers'].values() if w['lag'].get('n')]
    rss = [sum(s['rss_mb'].values()) for s in samples if s['rss_mb']]
    ram_mb = (
        int(subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True, text=True).stdout) / 2**20
    )
    latencies = [t.answered_at - t.ended_at for r in records for t in r.turns if t.answered_at]
    connected = sum(r.bot_audio_seconds + r.caller_audio_seconds for r in records)
    busy = sum((r.ended - r.started) for r in records if r.admitted and r.ended)
    report = {
        'calls': calls,
        'workers': workers,
        'ready_s': ready,
        'placed': len(records),
        'admitted': sum(r.admitted for r in records),
        'rejected': sum(r.closed_by == 'rejected' for r in records),
        'closed_by': {k: sum(r.closed_by == k for r in records) for k in {r.closed_by for r in records}},
        'overlap': overlap(records),
        'booked_calls': sum(1 for n in booked_per_call.values() if n),
        'bookings_active': len(active),
        'duplicate_effects': duplicates,
        'capacity_violations': clashes,
        'bookings_for_nobody': len(strangers),
        'cross_call_leaks': len(leaks),
        'unauthorised_effects': len(unauthorised),
        'writes_audited': len(writes),
        'loop_lag_p99_ms_max': max(lags) if lags else None,
        'rss_mb_max': max(rss) if rss else None,
        'rss_mb_first_last': (rss[0], rss[-1]) if rss else None,
        'ram_mb': ram_mb,
        'turn_latency_s': _summary(latencies),
        'duty_cycle': {
            'speech_seconds': connected,
            'connected_seconds': busy,
            'share': connected / busy if busy else None,
            'bot_seconds': sum(r.bot_audio_seconds for r in records),
            'caller_seconds': sum(r.caller_audio_seconds for r in records),
        },
        'marks': {
            'sent': sum(r.marks_sent for r in records),
            'returned': sum(r.marks_returned for r in records),
            'clears': sum(r.clears for r in records),
        },
        'runner_calls': {
            k: sum(c['ended'] == k for c in runner_calls) for k in {c['ended'] for c in runner_calls}
        },
        'idle_workers': {pid: {k: w[k] for k in ('tasks', 'fds', 'active')} for pid, w in idle.items()},
        'warm_ups': {pid: w.get('warmed') for pid, w in idle.items()},
    }
    report['verdict'] = {
        'no_unauthorised': report['unauthorised_effects'] == 0,
        'no_duplicates': duplicates == 0,
        'no_capacity_violation': clashes == 0,
        'no_leak': not leaks and not strangers,
        'lag_p99_ok': report['loop_lag_p99_ms_max'] is not None
        and report['loop_lag_p99_ms_max'] <= LIMITS['loop_lag_p99_ms'],
        'rss_ok': report['rss_mb_max'] is not None
        and report['rss_mb_max'] < LIMITS['rss_share_of_ram'] * ram_mb,
        'ceiling_held': report['overlap']['max'] <= LIMITS['max_active_calls'],
    }
    (out / 'records.json').write_text(json.dumps([asdict(r) for r in records], indent=1))
    (out / 'samples.json').write_text(json.dumps(samples))
    (out / 'snapshot.json').write_text(json.dumps(state, indent=1))
    (out / 'report.json').write_text(json.dumps(report, indent=1))
    return report


def _summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {'n': 0}
    values = sorted(values)
    return {
        'n': len(values),
        'p50': statistics.median(values),
        'p90': values[min(len(values) - 1, int(0.9 * len(values)))],
        'max': values[-1],
    }


def _runner_calls(path: Path) -> list[sqlite3.Row]:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        return db.execute('SELECT * FROM calls').fetchall()
    finally:
        db.close()


def host() -> dict[str, Any]:
    def sysctl(name: str) -> str:
        return subprocess.run(['sysctl', '-n', name], capture_output=True, text=True).stdout.strip()

    return {
        'cpu': sysctl('machdep.cpu.brand_string'),
        'cores': sysctl('hw.ncpu'),
        'perflevel0_cores': sysctl('hw.perflevel0.physicalcpu'),
        'perflevel1_cores': sysctl('hw.perflevel1.physicalcpu'),
        'ram_bytes': int(sysctl('hw.memsize')),
        'os': platform.platform(),
        'python': platform.python_version(),
        'source': subprocess.run(
            ['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, cwd=HERE
        ).stdout.strip(),
        'dirty': bool(
            subprocess.run(
                ['git', 'status', '--porcelain'], capture_output=True, text=True, cwd=HERE
            ).stdout.strip()
        ),
    }


async def ramp(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    reports = {'host': host(), 'limits': LIMITS, 'steps': {}}
    for index, calls in enumerate(int(c) for c in args.calls.split(',')):
        seconds = args.seconds if calls == max(int(c) for c in args.calls.split(',')) else 0
        step = out / f'c{calls}'
        lanes = [int(x) for x in args.lanes.split(',')] if args.lanes else None
        env = ({'LIVE_STATS': '1'} if args.live else {}) | ({'TRACEMALLOC': '1'} if args.tracemalloc else {})
        report = await ramp_step(step, calls, seconds, args.workers, args.port + index, lanes, args.idle, env)
        reports['steps'][str(calls)] = report
        print(json.dumps({'calls': calls, 'overlap': report['overlap'], 'verdict': report['verdict']}))
        (out / 'ramp.json').write_text(json.dumps(reports, indent=1))
    failed = [k for step in reports['steps'].values() for k, ok in step['verdict'].items() if not ok]
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(prog='evals.load')
    sub = parser.add_subparsers(dest='command', required=True)
    r = sub.add_parser('ramp')
    r.add_argument('--calls', default='1,2,5')
    r.add_argument('--seconds', type=float, default=60.0)
    r.add_argument('--workers', type=int, default=2)
    r.add_argument('--port', type=int, default=7960)
    r.add_argument('--out', required=True)
    r.add_argument(
        '--lanes', default='', help='only these lanes (their variants), for looking at one kind of call'
    )
    r.add_argument(
        '--idle', type=float, default=0.0, help='seconds to keep the runner up after the last call'
    )
    r.add_argument('--live', action='store_true', help='workers count their live per-call objects')
    r.add_argument(
        '--tracemalloc', action='store_true', help='workers trace the Python heap (it costs memory)'
    )
    f = sub.add_parser('faults')
    f.add_argument('--port', type=int, default=7990)
    f.add_argument('--out', required=True)
    f.add_argument('--only', default='', help='only these cases, like F1,F3')
    args = parser.parse_args()
    if args.command == 'ramp':
        return asyncio.run(ramp(args))
    from evals.faults import faults

    return asyncio.run(faults(args))


if __name__ == '__main__':
    raise SystemExit(main())
