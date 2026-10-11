"""What every voice launcher shares, whatever the framework: the call's waits, its ceiling, how it ended, and `serve`.

A use case's `bot.py` runs one call of its `core.Entry` on Pipecat (`core.launch.run_call`) or on
LiveKit Agents (`core.livekit_launch.run_call`). Both read the same settings from the environment the
launcher loaded, bound the call the same way (`Ceiling`, `CallTiming`) and record its end the same way
(`record_ending`, `outcome`). `serve` is the one command line: `uv run bot.py` runs Pipecat's
development runner, as it always has; `uv run bot.py livekit dev` (or `console`, `start`, `connect`)
runs the same entry as a LiveKit agent.

Imports no framework: each launcher is imported only when it is asked for.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Literal

from opentelemetry import trace

if TYPE_CHECKING:
    from core.entry import Entry
    from core.state import CallContext

_tracer = trace.get_tracer('slng-voice-harness')


class CallTiming:
    """The two waits a call is built with.

    `FINISH_SECONDS` is how long a turn already running may go on after the caller hangs up: a reading
    and a write fit in it. ponytail: a fixed bound; take it from the turn budget's own deadline if a
    slower model or tool ever needs longer.

    `IDLE_SECONDS` is the silence after the bot's last words before the caller is asked if they are
    still there (`core.turn.remind`). At 8 s it talked over Coval's simulated callers, who often answer
    9 to 13 s after a read-back: the reminder said the read-back again, their yes cut into that copy,
    so it was asked again, up to five times (gate 1, BWqBhyLj). The calls that really stalled were
    silent for 70 to 150 s.
    """

    FINISH_SECONDS = 8.0
    IDLE_SECONDS = 20.0


def max_call_seconds() -> float:
    """`MAX_CALL_SECONDS`: end a call this long after the caller connects. 0, the default, never ends one.

    Coval has no per-call duration limit (its API has none, and its polling
    timeout is not one), so a simulated call is capped here, on the transport
    that places the audio. `coval_sim` sets it for every simulated call.

    ponytail: the call is torn down the way a hang-up is, turn finished first,
    so it may run up to `CallTiming.FINISH_SECONDS` longer.
    """
    return float(os.getenv('MAX_CALL_SECONDS') or 0)


def vad_stop_seconds() -> float:
    """`VAD_STOP_SECS`: silence the voice detector waits before smart-turn judges whether the caller is done. 0.3 s by default.

    Session 4 measured both sides of it: at 0.2 s, 30% of model requests were
    cancelled because the caller went on after a pause, and 0.58 runs per caller
    turn answered a piece of a turn already under way; at 0.6 s (8 calls) that
    fell to 13% and 0.29, but the median turn waited 0.55 s longer. 0.3 s is the
    chosen middle, not yet measured on its own.
    """
    return float(os.getenv('VAD_STOP_SECS') or 0.3)


def delivery() -> str:
    """`DELIVERY`: `marks` (the default), a read-back is heard only on the transport's own playback report for it.

    On a phone line that is its returned Twilio mark. In the browser it is the
    page's playout report (`core.processor.WebRTCPlayout`): core's page at
    /voice/ (Pipecat's own web client with core's playback reports,
    `core.processor.serve_client`); the runner's prebuilt client at /client
    reports no playback, and a call from it is refused before the greeting
    (`reports_playback`). `simulated` lets estimated playback count as heard
    instead (`core.processor.playout(simulate=True)`): only for Coval's
    simulated carrier, which returns no marks (`coval_sim` sets it with
    `TWILIO_MARKS=absent`), or a developer who asks for it.
    """
    return os.getenv('DELIVERY', 'marks')


def outcome(call: Any) -> Literal['goodbye', 'hangup', 'ceiling', 'failed', 'stopped']:
    """How a call ended, from what it recorded: the ceiling first, then a goodbye, a failure, else a hang-up."""
    kinds = {ending.get('kind') for ending in call.endings}
    if 'ceiling' in kinds:
        return 'ceiling'
    if call.ended == 'caller' or call.departed:
        return 'goodbye'
    if call.ended == 'error':
        return 'failed'
    return 'hangup'


class Ceiling:
    """One call's ceiling (`MAX_CALL_SECONDS`): started when the caller connects, stopped by its owner, always.

    The owner is the launcher (the salon's `run_bot`), which stops it in a
    `finally` around the pipeline, so a call that ends any way at all (the agent's goodbye, the
    caller hanging up, a cancellation, an error) takes its timer with it. On a
    shared bot (`coval_sim serve`) the old timer was cancelled only on a
    client disconnect: calls the agent ended left theirs running, and every
    "ceiling reached" line fired together at a deadline long after those calls
    were over. One call's ceiling knows nothing of any other call.

    `sleep` is injectable so a test can control time.
    """

    def __init__(
        self,
        seconds: float,
        reached: Callable[[], Awaitable[None]],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.seconds, self._reached, self._sleep = seconds, reached, sleep
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Start counting, once. A ceiling of 0 never ends a call."""
        if self.seconds and self._task is None:
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        await self._sleep(self.seconds)
        await self._reached()

    async def stop(self) -> None:
        """Cancel the timer if it is still running, and wait for it to be gone. Safe to call more than once."""
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def record_ending(call: CallContext, *, carrier: str = '') -> None:
    """One `call.ending` span for the call: every ending fact as it arrived, and what is still unknown.

    `endings` holds the application's intent to close (the goodbye, a
    ceiling), the pipeline stopping, the media stream stopping or its socket
    closing, each with whether it was observed. `carrier` is the carrier's
    own report of the call completing, when the launcher has one (Twilio's
    status callback); without it, carrier closure is `unknown`, whatever else
    ended. An `EndFrame` or a failed hang-up request is not a carrier report.
    """
    with _tracer.start_as_current_span('call.ending') as span:
        span.set_attribute('call', call.id)
        span.set_attribute('ended', call.ended or 'unknown')
        span.set_attribute('endings', json.dumps(call.endings))
        span.set_attribute('carrier', carrier or 'unknown')


FRAMEWORKS = ('pipecat', 'livekit')


def framework(argv: list[str]) -> tuple[str, list[str]]:
    """Which framework a command line asks for, and the arguments left for it.

    The first argument names it (`livekit`, `pipecat`); without one, `VOICE_FRAMEWORK`, else Pipecat.
    """
    if argv and argv[0] in FRAMEWORKS:
        return argv[0], argv[1:]
    chosen = os.getenv('VOICE_FRAMEWORK', 'pipecat')
    if chosen not in FRAMEWORKS:
        raise SystemExit(f'VOICE_FRAMEWORK={chosen!r}: name one of {", ".join(FRAMEWORKS)}')
    return chosen, argv


def serve(entry: Entry) -> None:
    """`uv run bot.py [pipecat|livekit] ...`: one entrypoint for a use case, on either framework.

    Pipecat: its development runner, which finds `bot(runner_args)` in the `bot.py` it was started
    from, with core's reporting page at `/voice/` (`core.launch.serve`). LiveKit: an agent server that
    runs `entry` for every room it is dispatched to, with LiveKit's own command line after the word
    `livekit` (`dev`, `start`, `console`, `connect --room NAME`; `core.livekit_launch.serve`).
    """
    chosen, rest = framework(sys.argv[1:])
    sys.argv[1:] = rest
    if chosen == 'livekit':
        from core.livekit_launch import serve as livekit

        livekit(entry)
        return
    from core.launch import serve as pipecat

    pipecat()
