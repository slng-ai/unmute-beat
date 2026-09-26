"""The salon behind a small bounded runner on one host: uvicorn's own worker processes, one port, one diary.

    DIARY_PATH=salon.db RUNNER_DB=runner.db uv run uvicorn server:app --workers 2 --port 7860

What each part owns:

* uvicorn's supervisor: the port every worker accepts on, and a dead worker
  replaced (it checks every half second).
* each worker, for its whole life: the compiled flow (`salon.app.FLOW`, at
  import), the model clients, opened and warmed once (`model_clients`, every
  warm-up request counted), and a loop-lag sampler. `bot.py` still runs one
  call on its own for development.
* each call: its own `FlowCall`, history, approvals and speech ledger
  (`bot.run_bot`), the call ceiling, and failures of its own.
* the runner file (`RUNNER_DB`, SQLite, shared by the workers): which calls
  are admitted, active or over, with the carrier's CallSid, the stream, the
  worker that carried it, the Langfuse session (the CallSid) and the
  campaign that sent it. The diary's intents carry the same CallSid, so a
  call, its trace and its backend effects are joined by one id.

Admission is bounded, never queued. Twilio's webhook (`POST /`) is admitted
while fewer than `MAX_CALLS` (5) calls are admitted or active across every
worker, counted in one `BEGIN IMMEDIATE` transaction; past it the caller is
refused with `<Reject reason="busy"/>`. A media stream (`/ws`) runs only for
a call that was admitted and not yet claimed; any other is closed with code
1013. An admitted call whose stream never comes is dropped after
`ADMIT_SECONDS`, and a call whose worker died is closed as `lost` the next
time anyone asks.

Drain is the worker's own. uvicorn, told to stop, closes every open
websocket at once (code 1012), which would cut the calls in progress. So a
worker wraps uvicorn's handler: on SIGTERM it lets its calls run to their
end, at most `DRAIN_SECONDS`, and only then hands the signal to uvicorn,
which closes its clients and exits; the supervisor starts a replacement. A
second SIGTERM stops at once. The others keep serving. A dead worker's calls
are not moved: their audio connection died with it.

ponytail: a draining worker still takes a stream the kernel hands it, since
the workers share one socket. Behind a load balancer, take it out of
rotation first.

`VOICE_DOUBLES=1` swaps the model, STT and TTS for the offline doubles in
`evals/doubles.py` and turns tracing off, so the whole runner can be loaded
with no provider and no bill.

ponytail: same-host liveness (`os.kill(pid, 0)`) and one SQLite file. A
second host needs a lease with a heartbeat instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sqlite3
import statistics
import time
from collections import deque
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any
from urllib.parse import parse_qs
from uuid import uuid4
from xml.sax.saxutils import quoteattr

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from loguru import logger
from pipecat.runner.types import WebSocketRunnerArguments
from pipecat.runner.utils import create_transport

import bot
from core.graph import model_clients
from salon.diary import create_schema

MAX_CALLS = int(os.getenv('MAX_CALLS') or 5)
"""Calls admitted or active at once, over every worker. The Stage C gate is five."""
ADMIT_SECONDS = 15.0
"""How long an admitted call may take to open its media stream before its place is given back."""
RUNNER_DB = os.getenv('RUNNER_DB') or 'runner.db'
PUBLIC_HOST = os.getenv('PUBLIC_HOST', '')
"""The host Twilio reaches the stream on (a tunnel's). Empty: the host the webhook came to."""
DOUBLES = bool(os.getenv('VOICE_DOUBLES'))
LAG_EVERY = 0.05
"""How often the loop-lag sampler wakes: its lag is how late each wake-up was."""
DRAIN_SECONDS = float(os.getenv('DRAIN_SECONDS') or 200)
"""How long a draining worker waits for its calls: the call ceiling plus the time a turn may take to finish."""
REAP_EVERY = 2.0
"""How often each worker closes the calls of a worker that died (`Admission._reclaim`), with no traffic needed."""


class Admission:
    """The runner's one record of calls, shared by every worker through one SQLite file."""

    SCHEMA = """
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS calls (
        call_sid TEXT PRIMARY KEY,
        campaign TEXT, from_number TEXT, to_number TEXT,
        state TEXT NOT NULL,
        admitted_by INTEGER, worker INTEGER, stream_sid TEXT,
        admitted_at REAL, started_at REAL, ended_at REAL, ended TEXT
    );
    """

    def __init__(self, path: str) -> None:
        self.path = path
        create_schema(path, self.SCHEMA)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    @staticmethod
    def _alive(pid: int | None) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _reclaim(self, db: sqlite3.Connection, now: float) -> None:
        """Give back the place of a call whose stream never came, and close one whose worker died."""
        db.execute(
            "UPDATE calls SET state = 'ended', ended = 'never_connected', ended_at = ? "
            "WHERE state = 'admitted' AND admitted_at < ?",
            (now, now - ADMIT_SECONDS),
        )
        for row in db.execute("SELECT call_sid, worker FROM calls WHERE state = 'active'").fetchall():
            if not self._alive(row['worker']):
                db.execute(
                    "UPDATE calls SET state = 'ended', ended = 'lost', ended_at = ? WHERE call_sid = ?",
                    (now, row['call_sid']),
                )

    def admit(self, call_sid: str, campaign: str, from_number: str, to_number: str) -> bool:
        """Take a place for this call, if one is free. The same CallSid twice is one call."""
        now = time.time()
        with self._tx() as db:
            self._reclaim(db, now)
            if db.execute('SELECT 1 FROM calls WHERE call_sid = ?', (call_sid,)).fetchone():
                return True
            busy = db.execute("SELECT COUNT(*) FROM calls WHERE state IN ('admitted', 'active')").fetchone()[
                0
            ]
            if busy >= MAX_CALLS:
                return False
            db.execute(
                'INSERT INTO calls (call_sid, campaign, from_number, to_number, state, admitted_by, admitted_at) '
                "VALUES (?, ?, ?, ?, 'admitted', ?, ?)",
                (call_sid, campaign, from_number, to_number, os.getpid(), now),
            )
        return True

    def claim(self, call_sid: str, stream_sid: str) -> dict[str, Any] | None:
        """This worker carries the admitted call's media now. None for a call never admitted, or claimed already."""
        now = time.time()
        with self._tx() as db:
            self._reclaim(db, now)
            done = db.execute(
                "UPDATE calls SET state = 'active', worker = ?, stream_sid = ?, started_at = ? "
                "WHERE call_sid = ? AND state = 'admitted'",
                (os.getpid(), stream_sid, now, call_sid),
            ).rowcount
            row = db.execute('SELECT * FROM calls WHERE call_sid = ?', (call_sid,)).fetchone()
        return dict(row) if done and row else None

    def release(self, call_sid: str, why: str) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE calls SET state = 'ended', ended = ?, ended_at = ? WHERE call_sid = ? AND state = 'active'",
                (why, time.time(), call_sid),
            )

    def over(self) -> set[str]:
        """The CallSids of calls that are over, their worker's death included: what a new call may settle."""
        with self._tx() as db:
            self._reclaim(db, time.time())
            return {row[0] for row in db.execute("SELECT call_sid FROM calls WHERE state = 'ended'")}

    def calls(self) -> list[dict[str, Any]]:
        with self._tx() as db:
            self._reclaim(db, time.time())
            return [dict(row) for row in db.execute('SELECT * FROM calls ORDER BY admitted_at')]


ADMISSION = Admission(RUNNER_DB)
LAG: deque[float] = deque(maxlen=20_000)
"""Loop-lag samples in seconds, newest last: how late the sampler woke each time."""
ACTIVE: set[str] = set()
"""The CallSids this worker is carrying now."""
WARMED: list[dict[str, Any]] = []
"""This worker's warm-up requests, one record each (`model_clients`)."""

if DOUBLES:
    import pydantic_ai.models

    from evals import doubles

    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    if os.getenv('AUDIT_DIR'):
        doubles.audits(os.environ['AUDIT_DIR'])
    FLOW, SPEECH = doubles.flow(), doubles.speech
else:
    FLOW, SPEECH = bot.FLOW, None


STATS_DIR = os.getenv('STATS_DIR', '')
"""Where each worker writes its own `/stats` once a second (`<pid>.json`): a load driver reads every worker's."""


async def _sample_lag() -> None:
    """Measure how late the loop wakes, every `LAG_EVERY`; and, with `STATS_DIR`, write this worker's stats each second."""
    ticks = 0
    while True:
        started = time.perf_counter()
        await asyncio.sleep(LAG_EVERY)
        LAG.append(time.perf_counter() - started - LAG_EVERY)
        ticks += 1
        if ticks % int(REAP_EVERY / LAG_EVERY) == 0:
            await asyncio.to_thread(ADMISSION.over)
        if STATS_DIR and ticks % int(1 / LAG_EVERY) == 0:
            path = os.path.join(STATS_DIR, f'{os.getpid()}.json')
            with open(path + '.tmp', 'w') as out:
                json.dump(_stats() | {'at': time.time()}, out)
            os.replace(path + '.tmp', path)


DRAINING: list[float] = []
"""When this worker was told to drain, if it was."""


def _drain_on_signal() -> None:
    """Wrap uvicorn's SIGTERM handler: drain this worker's calls first, then let uvicorn stop."""
    loop = asyncio.get_running_loop()
    stop = signal.getsignal(signal.SIGTERM)

    async def drain() -> None:
        started = time.monotonic()
        logger.warning('worker {} draining: {} call(s) running', os.getpid(), len(ACTIVE))
        while ACTIVE and time.monotonic() - started < DRAIN_SECONDS:
            await asyncio.sleep(0.25)
        logger.warning(
            'worker {} drained in {:.1f}s; {} call(s) left',
            os.getpid(),
            time.monotonic() - started,
            len(ACTIVE),
        )
        if callable(stop):
            stop(signal.SIGTERM, None)

    def handler(signum: int, frame: Any) -> None:
        if DRAINING and callable(stop):
            stop(signum, frame)
            return
        DRAINING.append(time.time())
        loop.call_soon_threadsafe(lambda: loop.create_task(drain()))

    signal.signal(signal.SIGTERM, handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """One worker's life: its clients opened and warmed once, its sampler, then drain on the way out."""
    _drain_on_signal()
    sampler = asyncio.create_task(_sample_lag())
    global WARMED
    async with model_clients(FLOW.nodes, warm=True) as warmed:
        app.state.warmed = WARMED = warmed
        app.state.started = time.time()
        logger.info('worker {} ready, warming its model clients once', os.getpid())
        yield
    sampler.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sampler


app = FastAPI(lifespan=lifespan)


def _twiml(body: str) -> Response:
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?>\n<Response>{body}</Response>', media_type='text/xml'
    )


@app.post('/')
async def webhook(request: Request) -> Response:
    """Twilio's voice webhook: admit the call and point it at our stream, or refuse it as busy. Nothing waits."""
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    call_sid = form.get('CallSid', '')
    campaign = request.query_params.get('campaign', '')
    if not call_sid:
        return _twiml('<Reject reason="rejected"/>')
    admitted = await asyncio.to_thread(
        ADMISSION.admit, call_sid, campaign, form.get('From', ''), form.get('To', '')
    )
    if not admitted:
        logger.warning('call {} refused: {} calls already admitted or active', call_sid, MAX_CALLS)
        return _twiml('<Reject reason="busy"/>')
    host = PUBLIC_HOST or request.headers.get('host', '')
    params = ''.join(
        f'<Parameter name={quoteattr(name)} value={quoteattr(value)}/>'
        for name, value in (('campaign', campaign), ('fixture', request.query_params.get('fixture', '')))
        if value
    )
    return _twiml(f'<Connect><Stream url="wss://{host}/ws">{params}</Stream></Connect>')


@app.websocket('/ws')
async def media(websocket: WebSocket) -> None:
    """One call's media stream on this worker: run it if it was admitted, and free its place however it ends."""
    await websocket.accept()
    runner_args = WebSocketRunnerArguments(websocket=websocket, session_id=uuid4().hex)
    transport = await create_transport(runner_args, bot.transport_params)  # ty: ignore[invalid-argument-type]
    call_data = runner_args.call_data
    call_sid = str(getattr(call_data, 'call_id', '') or '')
    row = await asyncio.to_thread(ADMISSION.claim, call_sid, str(getattr(call_data, 'stream_id', '') or ''))
    if row is None:
        logger.warning(
            'stream for {} closed: the call was not admitted, or is already carried', call_sid or '?'
        )
        await websocket.close(code=1013)
        return
    if DOUBLES:
        inner = getattr(getattr(transport, '_params', None), 'serializer', None)
        if inner is not None and hasattr(inner, '_params'):
            inner._params.auto_hang_up = False
    ACTIVE.add(call_sid)
    why = 'pipeline_end'
    try:
        if DOUBLES:
            doubles.begin(call_sid, (getattr(call_data, 'body', None) or {}).get('fixture', ''))
        over = await asyncio.to_thread(ADMISSION.over)
        await bot.run_bot(
            transport,
            runner_args,
            flow=FLOW,
            speech=SPEECH,
            warm=False,
            trace=not DOUBLES,
            from_number=row['from_number'],
            settle=over.__contains__,
        )
    except Exception as error:
        why = f'failed: {type(error).__name__}'
        logger.exception('call {} failed; the worker goes on', call_sid)
    finally:
        ACTIVE.discard(call_sid)
        await asyncio.to_thread(ADMISSION.release, call_sid, why)


def _local(request: Request) -> bool:
    """Whether a request came from this host, not through the tunnel: cloudflared forwards with these headers.

    `/ready`, `/stats` and `/calls` carry callers' numbers and the runner's
    state, so only a local reader (the load driver, `coval_sim`) gets them.
    """
    return not any(h in request.headers for h in ('cf-connecting-ip', 'x-forwarded-for', 'cf-ray'))


def _fds() -> int:
    """Open file descriptors of this process (macOS and Linux both list them in /dev/fd)."""
    return len(os.listdir('/dev/fd'))


@app.get('/ready')
async def ready(request: Request) -> JSONResponse:
    if not _local(request):
        return JSONResponse({}, status_code=404)
    return JSONResponse(
        {'pid': os.getpid(), 'warmed': request.app.state.warmed, 'started': request.app.state.started}
    )


@app.get('/stats')
async def stats(request: Request, reset: bool = False) -> JSONResponse:
    """This worker now: its calls, tasks, descriptors and loop lag since the last reset."""
    if not _local(request):
        return JSONResponse({}, status_code=404)
    found = _stats() | {'warmed': request.app.state.warmed}
    if reset:
        LAG.clear()
    return JSONResponse(found)


def _stats() -> dict[str, Any]:
    samples = sorted(LAG)
    lag = (
        {
            'n': len(samples),
            'p50_ms': 1000 * statistics.median(samples),
            'p99_ms': 1000 * samples[min(len(samples) - 1, int(0.99 * len(samples)))],
            'max_ms': 1000 * samples[-1],
        }
        if samples
        else {'n': 0}
    )
    return {
        'pid': os.getpid(),
        'active': sorted(ACTIVE),
        'draining': bool(DRAINING),
        'tasks': len(asyncio.all_tasks()),
        'fds': _fds(),
        'lag': lag,
        'warmed': WARMED,
    }


@app.get('/calls')
async def calls(request: Request) -> JSONResponse:
    """The runner's call record, over every worker."""
    if not _local(request):
        return JSONResponse({}, status_code=404)
    return JSONResponse(await asyncio.to_thread(ADMISSION.calls))
