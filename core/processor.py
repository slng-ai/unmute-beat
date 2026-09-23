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
import time
from typing import Any

from opentelemetry import trace
from pipecat.frames.frames import (
    EndWorkerFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pydantic_ai import ModelResponse, TextPart

from core import turn as core
from core.graph import Node
from core.policies import TurnBudget
from core.state import CallContext
from core.turn import EXHAUSTED, STUMBLE  # noqa: F401  -- re-exported for tests

_tracer = trace.get_tracer('slng-voice-harness')


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
        call._say = self.say
        call._end = self.hang_up

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

    async def greet(self, line: str) -> None:
        """Open the call with a fixed line, and record it as said.

        Fixed rather than generated: the caller hears it as the transport
        connects instead of a model round trip later. Recording it keeps the
        agents' history honest about who spoke first.
        """
        self._call.note_said('fixed', line, origin='entry')
        await self.say(line)
        self._call.messages.append(
            ModelResponse(parts=[TextPart(content=line)], metadata={'speech_ids': [self._call.spoken[-1].id]})
        )

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
        elif isinstance(frame, LLMContextFrame):
            users = [
                m for m in frame.context.get_messages() if isinstance(m, dict) and m.get('role') == 'user'
            ]
            unseen = users[self._context_users :]
            self._context_users = len(users)
            if unseen:
                await self._turn(' '.join(filter(None, (_last_user_text(LLMContext([m])) for m in unseen))))
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

    def interrupt(self) -> None:
        """The caller spoke over us. Stop the run instead of finishing into a void."""
        self._interrupted = True
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

        ponytail: a run of cancelled turns joins into one long carried prompt.
        Bound it if a real call ever carries more than a sentence or two.
        """
        previous_messages = tuple(self._call.messages)
        previous_parts = frozenset(id(p) for m in previous_messages for p in m.parts)
        if self._carried:
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
                if (
                    prompt
                    and self._call.turn not in self._call._retained_turns
                    and not self._call.delivered(prompt, excluding=previous_parts)
                ):
                    self._carried = prompt
                self._active_playback = None
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
