"""Offline doubles for the whole voice runner: the model, STT and TTS, so `server.py` can be loaded for free.

`server.py` with `VOICE_DOUBLES=1` uses these instead of Gemini and SLNG.
Everything else is the production path: uvicorn workers, Pipecat's
transport, Twilio serializer and turn taking (Silero VAD, smart-turn), the
real adapter, core's engine, and the shared SQLite diary.

* `Brain`: the salon's step agents as a fixed policy over the call's state
  (which step, what is recorded, what was offered, what is waiting for a
  yes, what the caller just said). It proves runtime properties, not
  language understanding: every request is still one model request.
* `Ears`: an STT that hears the caller only when the real audio it is sent
  has speech followed by silence, then says that fixture's next words.
* `Voice`: a TTS that makes quiet audio at speaking pace after a short
  first-byte delay, and loses the audio of a segment a fixture names.

A caller is a `Caller` fixture, named by the webhook's `fixture` query
parameter; the load driver (`evals/load.py`) plays the same fixture's audio.
Fault points (`Caller.fault`) make a worker die, or hang, at a chosen place
in a booking write, for the worker-failure checks.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncGenerator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from pipecat.frames.frames import Frame, TranscriptionFrame, TTSAudioRawFrame
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.utils.time import time_now_iso8601
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

import core
from salon import diary as salon_diary
from salon.app import SPEC

CLOCK = datetime(2026, 9, 28, 8, 30, tzinfo=salon_diary.TZ)
"""Monday 28 September 2026, before opening: the diary's clock for every load call (`DIARY_CLOCK`)."""


@dataclass(frozen=True)
class Utterance:
    """What the caller says next: its pieces, with a pause after each piece but the last."""

    pieces: tuple[str, ...]
    gaps: tuple[float, ...] = ()
    barge: float | None = None
    """Start this many seconds after the bot's next audio begins, over it, instead of waiting for silence."""


@dataclass(frozen=True)
class Caller:
    name: str
    phone: str
    day: str
    time: str
    service: str = 'haircut'
    script: tuple[Utterance, ...] = ()
    lose_tts: str = ''
    """Words of a bot segment whose audio the TTS loses, the first time it is said."""
    fault: str = ''
    """`before`, `noted`, `committed` or `hang`: where a booking write kills (or stalls) its worker."""
    leave_after: float | None = None
    """Hang up this many seconds after the last line, without waiting for an answer."""

    @property
    def transcripts(self) -> list[str]:
        return [piece for utterance in self.script for piece in utterance.pieces]


def booking(name: str, phone: str, day: str, at: str, **kw: Any) -> Caller:
    """The plain booking call, as a caller says it: request, yes to the number, yes to the time, goodbye."""
    spoken_time = datetime.strptime(at, '%H:%M').strftime('%-I %p').lower().replace(' ', '')
    script = kw.pop('script', None) or (
        Utterance((f"Hi, I'd like a haircut on {day} at {spoken_time}.",)),
        Utterance(("Yes, that's my number.",)),
        Utterance(('Yes please.',)),
        Utterance(("No, that's all. Bye.",)),
    )
    return Caller(name, phone, day, at, script=script, **kw)


CALLERS: dict[str, Caller] = {}
"""Every fixture by name. `load.py` builds them with `booking` and registers them here (`register`)."""


def register(*callers: Caller) -> None:
    for caller in callers:
        CALLERS[caller.name] = caller


CALLER: ContextVar[Caller | None] = ContextVar('caller', default=None)
CALL: ContextVar[core.FlowCall | None] = ContextVar('call', default=None)
FIXTURES = Path(os.getenv('LOAD_FIXTURES') or '')
"""The driver writes the fixtures it plays here as JSON, so the workers read the same callers."""


def _load(name: str) -> Caller | None:
    if name in CALLERS:
        return CALLERS[name]
    path = FIXTURES / f'{name}.json'
    if not name or not path.is_file():
        return None
    data = json.loads(path.read_text())
    data['script'] = tuple(
        Utterance(tuple(u['pieces']), tuple(u.get('gaps', ())), u.get('barge')) for u in data['script']
    )
    return Caller(**data)


def begin(call_sid: str, fixture: str) -> None:
    """This worker task is now `call_sid`, playing `fixture`: what the doubles read for the rest of the call."""
    CALLER.set(_load(fixture))
    _Audits.call.set(call_sid)


class Brain:
    """The salon's step agents as a fixed policy. One move per model request, like the salon's tests (`tests.script`)."""

    def __init__(self) -> None:
        self.model = FunctionModel(self.reading, stream_function=self.stream)

    def reading(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        """Only a worker's warm-up asks without streaming (`core.graph.model_clients`): a one-word answer."""
        return ModelResponse(parts=[TextPart('ok')])

    async def stream(self, messages: list[ModelMessage], info: AgentInfo) -> AsyncGenerator[Any]:
        call, caller = CALL.get(), CALLER.get()
        move = self.move(call, caller)
        if call is not None and caller is not None:
            logger.debug(
                'brain {}: {} heard {!r} waiting {} -> {}',
                caller.name,
                call.state.agent,
                call.heard,
                getattr(call.pending_question(), 'about', None),
                move,
            )
        kind, *rest = move
        if kind == 'say':
            yield rest[0]
        elif kind == 'calls':
            yield {i: DeltaToolCall(name=n, json_args=json.dumps(a)) for i, (n, a) in enumerate(rest[0])}
        else:
            yield {0: DeltaToolCall(name=rest[0], json_args=json.dumps(rest[1] if len(rest) > 1 else {}))}

    @staticmethod
    def move(call: core.FlowCall | None, caller: Caller | None) -> tuple[Any, ...]:
        if call is None or caller is None:
            return ('say', 'ok')
        step, facts, heard = call.state.agent, call.state.facts, call.heard.casefold()
        waiting = call.pending_question()
        agrees = any(word in heard for word in ('yes', 'yeah', 'please', 'correct'))
        if 'bye' in heard:
            return ('call', 'goodbye', {})
        if step == 'concierge':
            if caller.day.casefold() not in heard:
                return ('say', 'Sure, go on.')
            if not facts.service:
                values = {'service': caller.service, 'booking_day': caller.day, 'booking_time': caller.time}
                return (
                    'calls',
                    [
                        ('collect', {'values': values}),
                        ('hand_over', {'to': 'booking', 'because': 'a booking'}),
                    ],
                )
            return ('call', 'hand_over', {'to': 'booking', 'because': 'a booking'})
        if step == 'verify':
            if waiting is not None and waiting.about == 'lookup_customer' and agrees:
                return ('call', 'lookup_customer')
            if not facts.customer_phone:
                return ('call', 'collect', {'values': {'customer_phone': caller.phone}})
            return ('call', 'lookup_customer')
        if step == 'booking':
            if waiting is not None and waiting.about == 'book_appointment' and agrees:
                return ('call', 'book_appointment')
            if facts.booked:
                return ('say', 'Is there anything else I can help with?')
            today = call.services.now().date()
            wanted = salon_diary.resolve_day(caller.day, today)
            if wanted and any(
                h['date'] == wanted.isoformat() and h['time'] == caller.time
                for h in call.services.diary.bookings_for(caller.phone, today)
            ):
                return ('say', 'You already have a haircut booked then. Is there anything else?')
            last = call.evidence[-1] if call.evidence else None
            if (
                last
                and last.operation == 'book_appointment'
                and not last.succeeded
                and last.turn == call.turn
            ):
                return ('say', 'Sorry, that time has just gone. Shall I find you another one?')
            refused = any(
                e.operation == 'book_appointment' and not e.succeeded and e.args.get('slot') == facts.slot
                for e in call.evidence
            )
            offered = call._offered.get('slot') or {}
            if facts.slot and not refused:
                return ('call', 'book_appointment')
            if offered and not refused:
                wanted = next((i for i in offered if f'T{caller.time.replace(":", "")}|' in i), None)
                return ('call', 'collect', {'values': {'slot': wanted or next(iter(offered))}})
            if refused and offered and facts.slot not in offered:
                return ('call', 'collect', {'values': {'slot': next(iter(offered))}})
            return ('call', 'options')
        return ('say', 'How can I help?')


def flow() -> core.StepFlow:
    """The salon compiled on the `Brain`, its `open` recording each call where the brain can find it."""
    compiled = SPEC.compile(model=Brain().model)
    opened = compiled.open

    def open_and_record(*args: Any, **kwargs: Any) -> core.FlowCall:
        call = opened(*args, **kwargs)
        CALL.set(call)
        _faults(call)
        return call

    compiled.open = open_and_record  # ty: ignore[invalid-assignment]
    return compiled


def _faults(call: core.FlowCall) -> None:
    """Arm this call's fault point, if its fixture has one, on its own services only."""
    caller = CALLER.get()
    if caller is None or not caller.fault:
        return
    salon = call.services
    books, note = salon.diary, salon.note_intent
    save = books.save_booking

    async def noted(intent: core.Intent) -> None:
        if caller.fault == 'before' and intent.operation == 'book_appointment':
            os._exit(9)
        await note(intent)
        if caller.fault == 'noted' and intent.operation == 'book_appointment':
            os._exit(9)

    def saving(*args: Any, **kwargs: Any) -> dict:
        if caller.fault == 'hang':
            time.sleep(float(os.getenv('HANG_SECONDS') or 12))
        result = save(*args, **kwargs)
        if caller.fault == 'committed':
            os._exit(9)
        return result

    salon.note_intent = noted
    salon.diary = _Faulty(books, saving)


class _Faulty:
    """The shared diary with one call's `save_booking` replaced: every other call keeps the real one."""

    def __init__(self, books: salon_diary.Diary, save: Any) -> None:
        self._books, self.save_booking = books, save

    def __getattr__(self, name: str) -> Any:
        return getattr(self._books, name)


class Ears(STTService):
    """Hears speech in the audio it is sent, and says the fixture's next words once that speech ends.

    Energy, not recognition: the caller's audio is synthetic speech over
    digital silence, so a frame is speech when its level is above a floor. A
    segment ends after `END_SECONDS` of silence, which a real STT's final
    result takes about as long to arrive after.
    """

    END_SECONDS = 0.25
    FLOOR = 300.0

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._words = list((CALLER.get() or Caller('none', '', '', '')).transcripts)
        self._speaking = False
        self._silence = 0.0

    def can_generate_metrics(self) -> bool:
        return False

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None]:
        samples = np.frombuffer(audio, dtype=np.int16)
        seconds = len(samples) / max(1, self.sample_rate)
        loud = samples.size and float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) > self.FLOOR
        if loud:
            if not self._speaking:
                logger.debug('ears: speech starts')
            self._speaking, self._silence = True, 0.0
        elif self._speaking:
            self._silence += seconds
            if self._silence >= self.END_SECONDS:
                self._speaking = False
                logger.debug('ears: speech ended, {} transcript(s) left', len(self._words))
                if self._words:
                    yield TranscriptionFrame(self._words.pop(0), '', time_now_iso8601(), finalized=True)
        yield None


class Voice(TTSService):
    """Quiet audio at speaking pace: 55 ms a character, after 120 ms, in 20 ms chunks at 24 kHz."""

    RATE = 24_000

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(push_start_frame=True, push_stop_frames=True, sample_rate=self.RATE, **kwargs)
        self._lose = (CALLER.get() or Caller('none', '', '', '')).lose_tts
        self._lost = False

    def can_generate_metrics(self) -> bool:
        return False

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None]:
        await asyncio.sleep(0.12)
        if self._lose and not self._lost and self._lose in text:
            self._lost = True
            return
        chunk = int(self.RATE * 0.02)
        tone = (200 * np.sin(np.arange(chunk) * 2 * np.pi * 220 / self.RATE)).astype(np.int16).tobytes()
        for index in range(max(10, int(len(text) * 0.055 / 0.02))):
            if index % 25 == 0:
                await asyncio.sleep(0)
            yield TTSAudioRawFrame(audio=tone, sample_rate=self.RATE, num_channels=1, context_id=context_id)


def speech() -> tuple[Ears, Voice]:
    return Ears(), Voice()


class _Audits(SpanExporter):
    """Every `write` span's audit, written per call to `AUDIT_DIR/<CallSid>.jsonl`, for the load driver to judge."""

    call: ContextVar[str] = ContextVar('audit_call', default='')

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def export(self, spans: Any) -> SpanExportResult:
        for span in spans:
            if not isinstance(span, ReadableSpan) or span.name != 'write' or not span.attributes:
                continue
            audit = span.attributes.get('write.audit')
            if audit is None:
                continue
            with (self.folder / f'{self.call.get() or "unknown"}.jsonl').open('a') as out:
                out.write(str(audit) + '\n')
        return SpanExportResult.SUCCESS


def audits(folder: str) -> None:
    """Capture write audits in this worker, with no Langfuse and no network."""
    path = Path(folder)
    path.mkdir(parents=True, exist_ok=True)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_Audits(path)))
    trace.set_tracer_provider(provider)
