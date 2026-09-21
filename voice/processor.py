"""The Pipecat side of a caller turn: events in, frames out.

A Pipecat LLM node is a frame contract, not a class. On an ``LLMContextFrame``
it pushes ``LLMFullResponseStartFrame``, then one ``LLMTextFrame`` per chunk of
speech, then ``LLMFullResponseEndFrame``. TTS, the assistant aggregator, the
transcript and the interruption machinery are all built on those three frames
and on nothing else, so anything that speaks them can sit here.

Running the turn is not here. ``voice.core`` does that and knows nothing about
Pipecat; this file translates its event stream into those frames, and supplies
the two things only this side can know: Pipecat's open turn span, and the
moment a word actually reached the wire.

What this does *not* use is Pipecat's own context: the agents keep their history
as ``list[ModelMessage]`` on the call. ``LLMContext`` still fills up beside it,
and that is deliberate -- it is the aggregators' record of what was actually
spoken, which is what ``note_spoken`` below hands to the core. It is never sent
to a model.

Tracing crosses the same seam. Pipecat opens a span for the call and one per
turn, and passes the open one through ``FrameProcessorSetup``; each caller turn
here opens a span inside it, and the agents' own spans land under that. Without
it every agent run starts a trace of its own and a call arrives in pieces.
"""

from __future__ import annotations

import time

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

from voice import core
from voice.core import EXHAUSTED, STUMBLE  # noqa: F401  -- re-exported for tests and bots
from voice.harness import CallContext, Node, TurnBudget

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
    """Runs the current agent for one caller turn, and follows its handoffs."""

    def __init__(
        self,
        call: CallContext,
        agents: dict[str, Node],
        *,
        budget: TurnBudget = TurnBudget(),
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._call = call
        self._agents = agents
        self._budget = budget
        # This turn's shared tally and limits, replaced at the top of each
        # turn. One per turn and not one per run: see `TurnBudget`.
        self._usage, self._limits = budget.account()
        self._tracing = None
        self._interrupted = False
        # This turn's barge-in signal. The runtime hears the caller; the core
        # decides what interrupting means. Replaced per turn, so a stale
        # signal cannot fence the next one.
        self._barge = core.Barge()
        # How the last turn ended, for a caller that wants to know.
        self._finished: core.TurnFinished | None = None
        call._say = self.say
        call._end = self.hang_up

    def can_generate_metrics(self) -> bool:
        return True

    async def setup(self, setup):
        await super().setup(setup)
        # Pipecat hands its open turn span around in this object rather than in
        # OTel's ambient context: the span is started by an observer running on
        # another task, so there is nothing ambient here to inherit.
        self._tracing = setup.tracing_context

    async def say(self, text: str) -> None:
        """Put one fixed line on the wire, past the one-per-gap rule.

        TTSSpeakFrame is a data frame, so it is queued and speech starts while
        whatever prompted it is still running.

        Anything covering a silence goes through `CallState.say`, which allows
        one line per gap and then calls this. Only the greeting and the error
        line come here directly, because both must be heard whatever else was
        said.
        """
        await self.push_frame(TTSSpeakFrame(text))

    async def hang_up(self) -> None:
        """Close the line, once whatever is queued ahead of this has been heard.

        `EndWorkerFrame` is a control frame, so it travels in order: the goodbye
        pushed a moment earlier by the tool is played, then the sink bounces this
        back up, the source turns it into an `EndFrame` and the pipeline drains.

        A frame rather than `PipelineWorker.end()`, which is what this used to
        call: `end()` waits for a flush probe to cross the pipeline, and the only
        caller is a tool running inside this processor's `process_frame`. The
        probe queued behind the very coroutine that was waiting for it, and the
        call sat there until the caller's browser dropped the connection.
        """
        await self.push_frame(EndWorkerFrame())

    async def greet(self, line: str) -> None:
        """Open the call with a fixed line, and record it as said.

        Fixed rather than generated: the caller hears it as the transport
        connects instead of a model round trip later, and it is the same
        sentence on every call. Recording it keeps the agents' history honest
        about who spoke first.
        """
        await self.say(line)
        self._call.messages.append(ModelResponse(parts=[TextPart(content=line)]))

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InterruptionFrame):
            # System frames run on their own task, so this lands while a turn is
            # still streaming below. That is exactly the point: without it,
            # barge-in would queue behind the answer it is meant to cut off.
            self.interrupt()
            await self.push_frame(frame, direction)
        elif isinstance(frame, LLMContextFrame):
            await self._turn(_last_user_text(frame.context))
        else:
            await self.push_frame(frame, direction)

    def interrupt(self) -> None:
        """The caller spoke over us. Stop the run instead of finishing into a void."""
        self._interrupted = True
        self._barge.fire()

    def _parent(self):
        """Pipecat's open turn span, so ours joins that trace instead of starting one.

        None under `talk.py`, which has no pipeline and so no turn: there the
        agents' own spans stay roots, exactly as they were.
        """
        if self._tracing is None:
            return None
        return self._tracing.get_turn_context() or self._tracing.get_conversation_context()

    async def _turn(self, prompt: str | None) -> None:
        """One caller turn, translated from the core's events into frames.

        Everything about *running* the turn is in `voice.core`. What is left
        here is the translation: an event stream in, Pipecat frames out, plus
        the metrics and the span, both of which need things only this side
        knows -- Pipecat's own turn context, and when the first word reached
        the wire.
        """
        # One span for the whole chain: `start_as_current_span` attaches it, and
        # the agents run in this coroutine, so pydantic-ai's own run spans land
        # underneath instead of each opening a trace of its own.
        with _tracer.start_as_current_span('agents', context=self._parent()) as span:
            # The turn's clock, not the speaking hop's. This used to start
            # inside the run that happened to speak, so a turn that spent two
            # routing requests before anybody said anything reported only the
            # last one and the silence the caller sat through was invisible.
            turn_started = time.perf_counter()
            spoke = False
            # Whether a response frame is open. The core can leave a run
            # without closing it -- a turn that blows its request limit or
            # hits the deadline unwinds through an exception, and the event
            # that would have said RunEnded is never yielded. Pairing the
            # frames is this side's job anyway: the contract they belong to
            # is Pipecat's, and an unclosed response wedges the aggregators.
            open_response = False
            self._barge = core.Barge()
            self._interrupted = False
            try:
                async for event in core.run_turn(self._call, self._agents, prompt, self._budget, self._barge):
                    match event:
                        case core.RunStarted():
                            open_response = True
                            await self.push_frame(LLMFullResponseStartFrame())
                            await self.start_ttfb_metrics()
                            await self.start_processing_metrics()
                        case core.TextDelta(text=text):
                            if not spoke:
                                spoke = True
                                await self.stop_ttfb_metrics()
                                # Pydantic AI's OTel instrumentation emits no
                                # first-token event, so Langfuse shows every
                                # generation with a null `timeToFirstToken`
                                # and a trace cannot say how much of a round
                                # trip was waiting. This is the moment, and we
                                # are standing in it.
                                span.set_attribute(
                                    'turn_first_word', round(time.perf_counter() - turn_started, 3)
                                )
                            await self.push_frame(LLMTextFrame(text))
                        case core.RunEnded():
                            open_response = False
                            await self.stop_processing_metrics()
                            await self.push_frame(LLMFullResponseEndFrame())
                        case core.FixedSpeech(text=text, reason=reason):
                            # Past the one-per-gap gate on purpose: an error
                            # or an exhausted turn must be heard whatever a
                            # filler already said.
                            if reason == 'error':
                                await self.push_error(f'agent run failed: {event.text}')
                            await self.say(text)
                        case core.TurnFinished() as done:
                            # Per provider request, timed at the model
                            # boundary by `request_clock`, with what each one
                            # produced first. This used to be one number
                            # called `request_ttft` measured from the start of
                            # the agent run, which on any turn with a tool in
                            # it reported the tool's whole round trip as time
                            # to first token.
                            timed = [r for r in self._call.requests if r.ttft is not None]
                            span.set_attribute('request_ttft', [r.ttft for r in timed])
                            span.set_attribute('request_first_output', [r.kind for r in timed])
                            spoken = self._call.spoken
                            span.set_attribute('fixed_lines', sum(s.kind == 'fixed' for s in spoken))
                            # The first text of *any* kind handed to TTS,
                            # beside the first the model produced. A filler
                            # going out quickly is not an answer going out
                            # quickly, and a benchmark with only one of these
                            # can be won by saying "let me check" sooner and
                            # answering later.
                            #
                            # Text handed to TTS, and named that way: audio
                            # the caller heard is a boundary nothing in this
                            # process can see, and a name that implied
                            # otherwise would be read as the latency the
                            # whole decision turns on.
                            if spoken:
                                span.set_attribute(
                                    'turn_first_text_out',
                                    round(min(s.at for s in spoken) - turn_started, 3),
                                )
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
            finally:
                if open_response:
                    # The core left a run open on its way out. Close it, so
                    # every start has an end whatever happened in between.
                    await self.push_frame(LLMFullResponseEndFrame())
                # A turn that ends without speech leaves TTFB armed, and the next
                # turn reports its own first word as seconds late. Idempotent:
                # a turn that did speak stopped it already.
                await self.stop_ttfb_metrics()
                await self.stop_processing_metrics()

    def note_spoken(self, content: str, interrupted: bool) -> None:
        """Tell the core what the caller actually heard.

        Barge-in stops TTS mid-sentence, but the run recorded every token the
        model produced. The assistant aggregator has already worked out the
        spoken prefix, so read it from there rather than counting TTS frames
        -- and hand it across, because the core cannot know it.

        Wire it to the aggregator's ``on_assistant_turn_stopped`` event.
        """
        core.note_spoken(self._call, content, interrupted)
