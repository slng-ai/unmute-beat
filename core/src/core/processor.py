"""The Pipecat side of a caller turn: events in, frames out.

A Pipecat LLM node is a frame contract, not a class. On an `LLMContextFrame`
it pushes `LLMFullResponseStartFrame`, then one `LLMTextFrame` per chunk of
speech, then `LLMFullResponseEndFrame`. TTS, the assistant aggregator, the
transcript and the interruption machinery are all built on those three frames
and on nothing else, so anything that speaks them can sit here.

Running the turn is not here. `core.turn` does that and knows nothing about
Pipecat; this file translates its event stream into those frames and supplies
the things only this side can know: Pipecat's open turn span, the moment a
word reached the wire, and what the caller actually heard.

The agents keep their history as `list[ModelMessage]` on the call. Pipecat's
own `LLMContext` still fills up beside it: it is the aggregators' record of
what was actually spoken, which is what `note_spoken` hands to the engine,
and it is never sent to a model.

Tracing crosses the same seam. Pipecat opens a span for the call and one per
turn and passes the open one through `FrameProcessorSetup`; each caller turn
here opens a span inside it, and the agents' own spans land under that.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from loguru import logger
from opentelemetry import trace

try:
    from pipecat.frames.frames import (
        AudioRawFrame,
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        EndWorkerFrame,
        ErrorFrame,
        Frame,
        InterruptionFrame,
        LLMContextFrame,
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
        OutputTransportMessageFrame,
        TTSSpeakFrame,
        UserStartedSpeakingFrame,
        VADUserStartedSpeakingFrame,
    )
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
    from pipecat.serializers.base_serializer import FrameSerializer
except ImportError as missing:
    raise ImportError(
        f'core.processor needs the voice extra: install slng-voice-core[voice] ({missing})'
    ) from missing

from core import turn as core
from core.graph import Node
from core.policies import TurnBudget
from core.state import CallContext, Question
from core.turn import EXHAUSTED, STUMBLE  # noqa: F401  -- re-exported for tests

_tracer = trace.get_tracer('slng-voice-harness')


@dataclass(kw_only=True)
class SegmentMark(OutputTransportMessageFrame):
    """A marker pushed right after one segment's speech, named for that segment (`CallContext.mark_for`).

    A data frame, so every processor keeps it in order behind the speech
    before it: Pipecat's TTS emits a frame queued after a `TTSSpeakFrame` only
    once that utterance's audio has all been pushed (its serialization
    queue), and the output transport writes it only after the audio chunks
    ahead of it (its audio queue). An `OutputTransportMessageFrame`, so the
    output transport hands it to the serializer, which turns it into a
    Twilio `mark` (`TwilioMarks`). For model speech streamed inside one
    response, the TTS holds it until the whole response's audio is out:
    late, never early.
    """

    mark: str = ''
    """The mark's name. Not `name`, which every Pipecat frame already has."""


class TwilioMarks(FrameSerializer):
    """Pipecat's Twilio serializer, with a mark after each segment and every returned mark told to the call.

    Wraps the serializer the runner built (so the stream, call and hang-up
    handling stay Pipecat's), and adds only what Twilio's Media Streams
    protocol gives for playback: an outbound `mark` after a segment's last
    media chunk, and the same name coming back once playout reaches it
    (https://www.twilio.com/docs/voice/media-streams/websocket-messages).
    Twilio also returns every pending mark when a `clear` empties its
    buffer, and the returned message does not say which it was, so the call
    is told of the clear before it goes out (`CallContext.note_cleared`) and
    those marks count for nothing. `stop` (the stream ended) is recorded as
    ending evidence of the media, not of the carrier call.

    Times are `time.perf_counter` at the moment each message is handled here:
    this process's own monotonic clock, the one caller speech onsets use.

    `echoes_marks=False` is for a far end that speaks the Media Streams
    protocol but never returns marks (Coval's simulated caller: session 4b
    call SdkVkoJj sent none back, so no read-back could ever be heard). The
    marks still go out. Each segment is then settled at the moment its audio
    has played out at real-time speed, from the media actually sent: its first
    chunk's send time plus the audio's own length, and never before its last
    chunk was written (the websocket output writes at twice real time). That
    is recorded as `local`, never as a telephone mark, so an evaluator does
    not count it as transport playback. A clear before that moment still
    voids it. Only a launcher that knows its far end sets this; nothing
    chooses it automatically.
    """

    def __init__(self, inner: FrameSerializer, call: CallContext, *, echoes_marks: bool = True) -> None:
        super().__init__()
        self._inner, self._call, self._echoes = inner, call, echoes_marks
        self._media = 0
        """Audio chunks serialized since the last mark: none before a mark means its segment had no audio."""
        self._first_media: float | None = None
        self._audio_seconds = 0.0
        """When this segment's first chunk went out, and how long its audio lasts at real time."""
        self._seen: set[str] = set()
        self._counts = {'sent': 0, 'returned': 0, 'settled': 0}
        self.stopped_at: float | None = None
        """When Twilio said the media stream stopped, if it did."""

    async def setup(self, setup: Any) -> None:
        await self._inner.setup(setup)

    async def serialize(self, frame: Frame) -> str | bytes | None:
        now = time.perf_counter()
        if isinstance(frame, AudioRawFrame):
            self._media += 1
            self._first_media = self._first_media if self._first_media is not None else now
            width = max(1, frame.sample_rate) * max(1, frame.num_channels) * 2
            self._audio_seconds += len(frame.audio) / width
        if isinstance(frame, SegmentMark):
            self._call.note_mark_sent(frame.mark, now, media=self._media)
            self._counts['sent'] += 1
            logger.info(
                'mark {} sent after {} chunks, {:.2f}s of audio', frame.mark, self._media, self._audio_seconds
            )
            if not self._echoes and self._media:
                ends = max(now, (self._first_media or now) + self._audio_seconds)
                asyncio.get_running_loop().call_later(ends - now, self._settle, frame.mark, ends)
            self._media, self._first_media, self._audio_seconds = 0, None, 0.0
            stream = getattr(self._inner, '_stream_sid', '')
            return json.dumps({'event': 'mark', 'streamSid': stream, 'mark': {'name': frame.mark}})
        if isinstance(frame, InterruptionFrame):
            self._call.note_cleared(now)
            self._media, self._first_media, self._audio_seconds = 0, None, 0.0
        return await self._inner.serialize(frame)

    def _settle(self, name: str, at: float) -> None:
        """The segment's audio has played out at real time: settle its mark, unless a clear came first."""
        if self._call.note_mark_returned(name, at, local=True):
            self._counts['settled'] += 1
            logger.info('mark {} settled at real time (no marks from this far end)', name)

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, str) and '"event"' in data:
            with contextlib.suppress(ValueError):
                message = json.loads(data)
                event = str(message.get('event'))
                if event not in self._seen and event != 'media':
                    self._seen.add(event)
                    logger.info('media stream: first {!r} event from the far end', event)
                if event == 'mark':
                    self._counts['returned'] += 1
                    name = str(message.get('mark', {}).get('name', ''))
                    counted = self._call.note_mark_returned(name, time.perf_counter())
                    logger.info('mark {} returned ({})', name, 'counted' if counted else 'ignored')
                    return None
                if event == 'stop':
                    self.stopped_at = time.perf_counter()
                    self._call.note_ending('media_stop', 'twilio stop event', observed=True)
                    logger.info('media stream stopped: marks {}', self._counts)
        return await self._inner.deserialize(data)


class Ceiling:
    """One call's ceiling (`MAX_CALL_SECONDS`): started when the caller connects, stopped by its owner, always.

    The owner is the launcher (salon-graph's `run_bot`), which stops it in a
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


def playout(transport: Any, call: CallContext, *, echoes_marks: bool = True) -> list[FrameProcessor]:
    """Wire playback evidence into a transport; returns what to place right after `transport.output()`.

    A Twilio media stream gets its serializer wrapped (`TwilioMarks`), so
    marks go out and come back on the wire, and nothing is added to the
    pipeline. `echoes_marks=False` is for a far end known to return none
    (see `TwilioMarks`). Any other transport gets a `LocalPlayout` after its output.
    Without one or the other, no read-back can ever be delivered, and no
    consequential write can be approved.
    """
    from pipecat.serializers.twilio import TwilioFrameSerializer

    params = getattr(transport, '_params', None)
    serializer = getattr(params, 'serializer', None)
    if isinstance(serializer, TwilioFrameSerializer):
        params.serializer = TwilioMarks(serializer, call, echoes_marks=echoes_marks)  # ty: ignore[invalid-assignment]
        return []
    if isinstance(serializer, TwilioMarks):
        return []
    return [LocalPlayout(call)]


class LocalPlayout(FrameProcessor):
    """After a local transport's output: a segment's mark arriving here means its audio was all written.

    For a development transport (WebRTC in a browser) that returns no marks.
    Written is not played; it is the nearest thing such a transport reports,
    and it is recorded as `local`, never as a telephone mark. Never place it
    in a Twilio pipeline: there the same frame passes here when the mark is
    sent, long before it plays.
    """

    def __init__(self, call: CallContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._call = call

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, SegmentMark):
            self._call.note_mark_returned(frame.mark, time.perf_counter(), local=True)
        await self.push_frame(frame, direction)


def _last_user_text(context: LLMContext) -> str | None:
    """What the caller just said, as the user aggregator wrote it down."""
    for message in reversed(context.get_messages()):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        content = message.get('content')
        if isinstance(content, str):
            return content.strip() or None
        if isinstance(content, list):
            text = ' '.join(part.get('text', '') for part in content if isinstance(part, dict)).strip()
            return text or None
    return None


class PydanticAgentProcessor(FrameProcessor):
    """Runs the current agent for one caller turn, and follows its handoffs.

    One barge-in signal per turn, replaced at the top of each, so a stale
    signal cannot fence the next one. Input a cancelled turn heard and never
    delivered is carried to the front of the next one, because the caller
    still said it.
    """

    def __init__(
        self,
        call: CallContext,
        agents: dict[str, Node[Any]],
        *,
        budget: TurnBudget = TurnBudget(),
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._call = call
        self._marking = ''
        """The model segment streaming now, whose mark goes out when it closes."""
        self._agents = agents
        self._budget = budget
        self._usage, self._limits = budget.account()
        self._tracing = None
        self._interrupted = False
        self._barge = core.Barge()
        self._finished: core.TurnFinished | None = None
        self._carried = ''
        self._context_users = 0
        self._playback_turns: dict[str, list[str] | None] = {}
        self._playback_runs: dict[tuple[int, int], list[str]] = {}
        self._active_playback: list[str] | None = None
        self._idle = asyncio.Event()
        self._idle.set()
        call._say = self.say
        call._end = self.hang_up
        call._playback_known = True

    def can_generate_metrics(self) -> bool:
        """No. This processor's own timings go on its span, not into Pipecat's metrics.

        Pipecat's TTS tracing takes the first `TTFBMetricsData` that arrives
        under the playing audio context without checking which processor
        emitted it, so this processor's time to first token was recorded as the
        TTS service's time to first audio. Nothing is lost: `request_ttft` is
        stamped per provider request by `request_clock` and `turn_first_word`
        is stamped here, both on the turn span named after the step.
        """
        return False

    async def setup(self, setup):
        """Take Pipecat's open turn span from the setup object.

        Pipecat hands it around here rather than in OTel's ambient context: the
        span is started by an observer running on another task, so there is
        nothing ambient to inherit.
        """
        await super().setup(setup)
        self._tracing = setup.tracing_context

    async def say(self, text: str) -> None:
        """Put one fixed line on the wire, past the one-per-gap rule.

        `TTSSpeakFrame` is a data frame, so it is queued and speech starts while
        whatever prompted it is still running. Anything covering a silence goes
        through `CallContext.say`, which allows one line per gap and then calls
        this; the greeting and the error lines come here directly because both
        must be heard whatever else was said.

        The ledger's own record of the segment rides along in the frame's
        metadata, so a trace can say which line a TTS span was and whether the
        caller heard it before the tool ran. Queue insertion is stamped here:
        it is not synthesis and not audio, and only the first of those three
        moments is one this process can see.
        """
        frame = TTSSpeakFrame(text)
        if segment := next((s for s in reversed(self._call.spoken) if s.text == text), None):
            frame.metadata['speech'] = {
                'segment': segment.id,
                'turn': segment.turn,
                'origin': segment.origin,
                'attempt': segment.attempt,
                'tool_call_id': segment.tool_call_id,
                'submitted_at': time.time(),
            }
        if segment is not None and self._active_playback is not None:
            self._active_playback.append(segment.id)
        await self.push_frame(frame)
        if segment is not None:
            await self._mark(segment.id)

    async def _mark(self, segment_id: str) -> None:
        """Push the mark that will say this segment has played: right behind its speech, so it follows the last audio."""
        if name := self._call.mark_for(segment_id):
            await self.push_frame(SegmentMark(message={}, mark=name))

    async def hang_up(self) -> None:
        """Close the line, once whatever is queued ahead of this has been heard.

        `EndWorkerFrame` is a control frame, so it travels in order: the goodbye
        pushed a moment earlier is played, then the sink bounces this back up,
        the source turns it into an `EndFrame` and the pipeline drains.

        A frame rather than `PipelineWorker.end()`: `end()` waits for a flush
        probe to cross the pipeline, and the only caller is a tool running
        inside this processor's `process_frame`, so the probe queued behind the
        very coroutine that was waiting for it.
        """
        await self.push_frame(EndWorkerFrame())

    async def greet(self, line: str | Question) -> None:
        """Open the call with a fixed line, and record it as said.

        Fixed rather than generated: the caller hears it as the transport
        connects instead of a model round trip later. Recording it keeps the
        agents' history honest about who spoke first. A `Question` greeting,
        such as a caller ID read back, is put on the call first, so the
        caller's first reply answers it (`core.turn.greet`).
        """
        await self.say(core.greet(self._call, line))

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Interruptions land while a turn is still streaming, which is the point.

        System frames run on their own task; without that, barge-in would queue
        behind the answer it is meant to cut off. Every unseen user message in
        the aggregator's context is consumed, joined into one turn, so a
        context frame discarded by barge-in still reaches the agents.
        """
        await super().process_frame(frame, direction)

        if isinstance(frame, InterruptionFrame):
            self.interrupt()
            await self.push_frame(frame, direction)
        elif isinstance(frame, ErrorFrame) and direction == FrameDirection.UPSTREAM:
            self._call.note_speech_failed(time.perf_counter())
            await self.push_frame(frame, direction)
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._call.note_reached()
            await self.push_frame(frame, direction)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._call.note_drained()
            await self.push_frame(frame, direction)
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._call.note_onset(time.perf_counter() - (frame.start_secs or 0.0))
            await self.push_frame(frame, direction)
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._call.note_onset(time.perf_counter())
            await self.push_frame(frame, direction)
        elif isinstance(frame, LLMContextFrame):
            users = [
                m for m in frame.context.get_messages() if isinstance(m, dict) and m.get('role') == 'user'
            ]
            unseen = users[self._context_users :]
            self._context_users = len(users)
            if unseen:
                await self.serve(' '.join(filter(None, (_last_user_text(LLMContext([m])) for m in unseen))))
        else:
            await self.push_frame(frame, direction)

    async def _start_interruption(self):
        """Pipecat's own interruption hook, taken before it cancels this turn's task.

        The `InterruptionFrame` branch in `process_frame` is too late: Pipecat
        handles a system frame by calling this and then cancelling the task
        running `process_frame`, so the turn would unwind with a bare
        `CancelledError` before the frame reached that branch and the barge
        would never reach the engine.
        """
        self.interrupt()
        await super()._start_interruption()

    async def serve(self, prompt: str | None) -> None:
        """One caller turn, marked as running until it is over, so a dropped line can let it finish."""
        self._idle.clear()
        try:
            await self._turn(prompt)
        finally:
            self._idle.set()

    async def remind(self) -> None:
        """The caller was silent for the runtime's idle timeout after our last words played (`core.turn.remind`).

        Pipecat's `on_user_turn_idle` calls this. Nothing while a turn is
        still running: silence while the agents work is not the caller's.
        """
        if self._idle.is_set():
            await core.remind(self._call)

    async def finish_turn(self, timeout: float) -> None:
        """Wait, for up to `timeout` seconds, for a turn already running to finish.

        For the moment the line drops: the caller's last words may be the yes
        to a read-back, and the turn reading them is still running. Cancelled
        there, an approved complaint was never recorded (Coval run 4eDqRVLR,
        K4). Allowed to finish, it writes only what its guards authorise, as
        on any turn; what it says goes nowhere.
        """
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._idle.wait(), timeout)

    def interrupt(self) -> None:
        """The caller spoke over us. Stop the run instead of finishing into a void.

        Told to the call as well, even with no run going: a caller who starts
        again before the last answer played is still in the same turn.
        """
        self._interrupted = True
        self._call.note_barge()
        self._barge.fire()

    def _parent(self):
        """Pipecat's open turn span, so ours joins that trace instead of starting one.

        None when there is no pipeline, in which case the agents' own spans
        stay roots.
        """
        if self._tracing is None:
            return None
        return self._tracing.get_turn_context() or self._tracing.get_conversation_context()

    async def _turn(self, prompt: str | None) -> None:
        """One caller turn, translated from the engine's events into frames.

        One span for the whole chain, named after the step the caller is with
        and renamed to the one that answered: `start_as_current_span` attaches it and
        the agents run in this coroutine, so Pydantic AI's own run spans land
        underneath instead of each opening a trace of its own. The clock is
        the turn's, not the speaking hop's, so the silence a caller sits
        through before anybody speaks is visible.

        Pairing the response frames is this side's job: the engine can leave a
        run open on its way out when a budget or deadline unwinds it through
        an exception, and an unclosed response wedges the aggregators.

        Two different things cancel this task and only one is the transport:
        the pipeline being torn down, and Pipecat cutting the turn off because
        the caller spoke again. `ended` is written once, so a barge-in must not
        be recorded as a disconnect. A turn that unwinds without a
        `TurnFinished` is given one here, because an absent outcome is not a
        good one.

        Every attribute stamped on the span names what it measured. `turn_first_word`
        is the model's first token and `turn_first_text_out` the first text of
        *any* kind handed to TTS, because a question going out quickly is not
        an answer going out quickly. `unbacked_claim` is on the span because a
        latency table that cannot see it will reward inventing an outcome. What
        the turn was asked and what it answered go on it in Langfuse's own keys,
        so the numbers can be read against the sentence that produced them.

        Words the last turn never got into the history are carried to the front
        of this one, unless this is more of the same turn, in which case the
        call already holds them as that turn's unsent pieces (`CallContext.fresh`).

        ponytail: a run of cancelled turns joins into one long carried prompt.
        Bound it if a real call ever carries more than a sentence or two.
        """
        if self._carried:
            if not self._call.continues_turn():
                prompt = f'{self._carried} {prompt}'.strip() if prompt else self._carried
            self._carried = ''
        with _tracer.start_as_current_span(self._call.state.agent, context=self._parent()) as span:
            if prompt:
                span.set_attribute('langfuse.observation.input', prompt)
            turn_started = time.perf_counter()
            spoke = False
            open_response = False
            self._barge = core.Barge()
            self._interrupted = False
            self._finished = None
            try:
                async for event in core.run_turn(self._call, self._agents, prompt, self._budget, self._barge):
                    match event:
                        case core.RunStarted():
                            open_response = True
                            run = (self._call.turn, self._call._attempt)
                            self._active_playback = self._playback_runs[run] = []
                            frame = LLMFullResponseStartFrame()
                            frame.metadata['voice_run'] = run
                            await self.push_frame(frame)
                        case core.TextDelta(text=text):
                            if not spoke:
                                spoke = True
                                span.set_attribute(
                                    'turn_first_word', round(time.perf_counter() - turn_started, 3)
                                )
                            frame = LLMTextFrame(text)
                            if self._call.spoken:
                                segment = self._call.spoken[-1]
                                if segment.kind == 'model' and segment.id != self._marking:
                                    if self._marking:
                                        await self._mark(self._marking)
                                    self._marking = segment.id
                                frame.metadata['speech'] = {'segment': segment.id, 'turn': segment.turn}
                                if (
                                    self._active_playback is not None
                                    and segment.id not in self._active_playback
                                ):
                                    self._active_playback.append(segment.id)
                            await self.push_frame(frame)
                        case core.RunEnded():
                            self._active_playback = None
                            open_response = False
                            await self.push_frame(LLMFullResponseEndFrame())
                            if self._marking:
                                await self._mark(self._marking)
                                self._marking = ''
                        case core.FixedSpeech(text=text, reason=reason):
                            if reason == 'error':
                                await self.push_error(f'agent run failed: {event.text}')
                            if reason == 'unbacked':
                                span.set_attribute('unbacked_claim', True)
                            await self.say(text)
                        case core.TurnFinished() as done:
                            classifications = [r for r in self._call.requests if r.kind == 'classifier']
                            span.set_attribute('classifier_requests', len(classifications))
                            span.set_attribute(
                                'classifier_duration',
                                [r.duration for r in classifications if r.duration is not None],
                            )
                            span.set_attribute(
                                'classifier_fallback', any(r.fallback for r in classifications)
                            )
                            span.set_attribute('classifier_outcome', [r.outcome for r in classifications])
                            span.set_attribute('classifier_labels', [r.labels for r in classifications])
                            timed = [(r.ttft, r.kind) for r in self._call.requests if r.ttft is not None]
                            span.set_attribute('request_ttft', [ttft for ttft, _ in timed])
                            span.set_attribute('request_first_output', [kind for _, kind in timed])
                            spoken = self._call.spoken
                            if said := ' '.join(s.text for s in spoken if s.text):
                                span.set_attribute('langfuse.observation.output', said)
                            span.set_attribute('fixed_lines', sum(s.kind == 'fixed' for s in spoken))
                            span.set_attribute('speech_origins', [s.origin for s in spoken])
                            span.set_attribute('speech_segments', [s.id for s in spoken])
                            span.set_attribute('attempt', self._call.attempt)
                            span.set_attribute('turn_pieces', len(self._call._pieces))
                            if self._call._continued_from:
                                span.set_attribute('continued_from', self._call._continued_from)
                            if spoken:
                                span.set_attribute(
                                    'turn_first_text_out',
                                    round(min(s.at for s in spoken) - turn_started, 3),
                                )
                            span.update_name(done.agent)
                            span.set_attribute('langfuse.observation.type', 'agent')
                            span.set_attribute('agent', done.agent)
                            span.set_attribute('status', done.status)
                            span.set_attribute('hops', len(done.handoffs) + 1)
                            span.set_attribute('interrupted', done.status == 'interrupted')
                            span.set_attribute('model_requests', done.requests)
                            if done.handoffs:
                                span.set_attribute('handoffs', ', '.join(done.handoffs))
                            if done.detail:
                                span.set_attribute('detail', done.detail)
                            self._finished = done
            except asyncio.CancelledError:
                if not self._interrupted:
                    self._call.end_requested('transport')
                raise
            finally:
                if self._finished is None:
                    ended = self._call.ended
                    status = 'interrupted' if self._interrupted else 'failed'
                    self._finished = core.TurnFinished(
                        status=status,
                        agent=self._call.state.agent,
                        handoffs=(),
                        requests=len(self._call.requests),
                        detail=f'the turn never finished ({ended or "no reason recorded"})',
                    )
                    span.set_attribute('status', self._finished.status)
                    span.set_attribute('detail', self._finished.detail)
                if self._call.ended:
                    span.set_attribute('ended', self._call.ended)
                self._call.turn_ended(interrupted=self._interrupted)
                if prompt and not self._call.input_retained:
                    self._carried = self._call.fresh
                self._active_playback = None
                self._marking = ''
                if open_response:
                    await self.push_frame(LLMFullResponseEndFrame())

    def playback_started(self, timestamp: str, *, run: tuple[int, int] | None = None) -> None:
        """Bind the speech about to play to the run that produced it, by the identity the runtime frame carries.

        ponytail: unframed TTS has no segment identity, so only one pending
        segment is accepted. A runtime with playout ids can pass segment ids
        directly.
        """
        if run is not None:
            self._playback_turns[timestamp] = self._playback_runs.get(run)
        else:
            pending = [s.id for s in self._call.all_spoken if s.delivery == 'submitted']
            self._playback_turns[timestamp] = pending if len(pending) == 1 else None

    def note_spoken(
        self,
        content: str,
        interrupted: bool,
        *,
        timestamp: str | None = None,
        segment_ids: tuple[str, ...] = (),
    ) -> None:
        """Tell the engine what the caller actually heard.

        The assistant aggregator has already worked out the spoken prefix; it
        is a runtime estimate, applied only to segments it can be matched to.
        Wired to the aggregator's `on_assistant_turn_stopped` event.
        """
        if timestamp is not None:
            identified = self._playback_turns.pop(timestamp, None)
            if not identified:
                return
            segment_ids = tuple(identified)
        core.note_spoken(self._call, content, interrupted, segment_ids=segment_ids)
