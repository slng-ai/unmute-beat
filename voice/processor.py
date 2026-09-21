"""A pydantic-ai agent standing where Pipecat's LLM service would.

A Pipecat LLM node is a frame contract, not a class. On an ``LLMContextFrame``
it pushes ``LLMFullResponseStartFrame``, then one ``LLMTextFrame`` per chunk of
speech, then ``LLMFullResponseEndFrame``. TTS, the assistant aggregator, the
transcript and the interruption machinery are all built on those three frames
and on nothing else, so anything that speaks them can sit here.

What this does *not* use is Pipecat's own context: the agents keep their history
as ``list[ModelMessage]`` on ``CallState``. ``LLMContext`` still fills up beside
it, and that is deliberate -- it is the aggregators' record of what was actually
spoken, which is what ``note_spoken`` below reads. It is never sent to a model.

Tracing crosses the same seam. Pipecat opens a span for the call and one per
turn, and passes the open one through ``FrameProcessorSetup``; each caller turn
here opens a span inside it, and the agents' own spans land under that. Without
it every agent run starts a trace of its own and a call arrives in pieces.
"""

from __future__ import annotations

import time

from loguru import logger
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
from pydantic_ai import (
    Agent,
    AgentRunResultEvent,
    FunctionToolResultEvent,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    RunCancelled,
    TextPart,
    TextPartDelta,
)

from voice.harness import CallContext, Handoff, Node
from voice.harness import moved_by as _moved_by
from voice.harness import pick as _pick
from voice.harness import route as _route

STUMBLE = 'Sorry, I had a problem there. Could you say that again?'

_tracer = trace.get_tracer('slng-voice-harness')


def _speech(event) -> str:
    """The text of an event that is speech, and '' for everything that is not.

    A `Handoff` arrives as a tool call part, so it never matches here and never
    reaches the caller's ears.
    """
    match event:
        case PartStartEvent(part=TextPart(content=text)):
            return text
        case PartDeltaEvent(delta=TextPartDelta(content_delta=text)):
            return text
    return ''


def _last_user_text(context: LLMContext) -> str | None:
    """What the caller just said, as the user aggregator wrote it down."""
    for message in reversed(context.get_messages()):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        content = message.get('content')
        if isinstance(content, str):
            return content.strip() or None
        if isinstance(content, list):
            text = ' '.join(
                part.get('text', '') for part in content if isinstance(part, dict)
            ).strip()
            return text or None
    return None


class PydanticAgentProcessor(FrameProcessor):
    """Runs the current agent for one caller turn, and follows its handoffs."""

    def __init__(
        self,
        call: CallContext,
        agents: dict[str, Node],
        *,
        max_hops: int = 3,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._call = call
        self._agents = agents
        self._max_hops = max_hops
        self._events = None
        self._interrupted = False
        self._tracing = None
        # Tools that ran *and returned* in the run that just finished, read
        # by `_turn` to see whether one of them was a free edge out of this
        # step. A refusal or a cancellation is not in here.
        self._succeeded: set[str] = set()
        # This turn's span, while it is still waiting for its first word.
        self._turn_span = None
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
        if self._events is not None:
            self._events.cancel()

    def _parent(self):
        """Pipecat's open turn span, so ours joins that trace instead of starting one.

        None under `talk.py`, which has no pipeline and so no turn: there the
        agents' own spans stay roots, exactly as they were.
        """
        if self._tracing is None:
            return None
        return self._tracing.get_turn_context() or self._tracing.get_conversation_context()

    async def _turn(self, prompt: str | None) -> None:
        """One caller turn: run the current agent, follow handoffs until one speaks."""
        # One span for the whole chain: `start_as_current_span` attaches it, and
        # the agents run in this coroutine, so pydantic-ai's own run spans land
        # underneath instead of each opening a trace of its own.
        with _tracer.start_as_current_span('agents', context=self._parent()) as span:
            handoffs: list[str] = []
            self._turn_span = span
            try:
                self._interrupted = False
                # A new silence to cover, so one fixed line is allowed again.
                self._call._covered = False
                self._call.turn_started()
                # `_max_hops` counts model runs, which is what a caller waits
                # for. The graph's own moves are not in here to count: `route`
                # replaces the destination the model named with the step that
                # has to run first, so interposing a prerequisite costs the same
                # one run that entering the destination would have.
                for _ in range(self._max_hops):
                    node = self._agents[self._call.state.agent]
                    output = await self._speak(node, prompt)
                    if not isinstance(output, Handoff):
                        # The free edge. A borrowed tool *succeeded*, so the
                        # caller is already in the step that owns it, and the
                        # request that moved them is the one that just answered
                        # them. Set after the run, not during, so the step that
                        # spoke is the step whose prompt wrote the words.
                        if (landed := _moved_by(self._call.state, node, self._succeeded)) != self._call.state.agent:
                            logger.info(
                                'moved {} -> {} (via {})',
                                self._call.state.agent, landed, ', '.join(sorted(self._succeeded)),
                            )
                            handoffs.append(landed)
                            self._call.commit(agent=landed)
                        return
                    # Where they were sent, then where the graph actually puts
                    # them: `booking` before anyone is identified means `verify`
                    # first, with `booking` recorded to resume into.
                    going_to, waiting = _route(self._call.state, self._agents, output.to)
                    logger.info(
                        'handoff {} -> {} ({})', self._call.state.agent, going_to, output.because or '-'
                    )
                    handoffs.append(going_to)
                    # One commit: the step and what is waiting behind it are the
                    # same decision, and a caller moved into `verify` with no
                    # record of where they were going has lost their errand.
                    self._call.commit(agent=going_to, pending=waiting)
                    # `prompt` is deliberately kept: the agent taking over is
                    # asked the caller's own question again. It used to be
                    # cleared, on the reasoning that the turn is already in the
                    # history -- and it is, two messages up. But the *last*
                    # message the receiving run then reads is somebody else's
                    # spent tool call, `hand_over` answered with "Final result
                    # processed." and nothing to reply to. Measured on the call
                    # of 2026-09-20: Gemini returned a candidate with no parts
                    # at all, pydantic-ai sent back "Please return text or
                    # include your response in a tool call", and the reply came
                    # on the retry -- 0.64s of the 3.94s that turn cost, spent
                    # producing nothing. Reading the question twice is cheaper
                    # than reading a finished errand.
                    if self._interrupted:
                        # Barged in during the hop. Starting the next agent now
                        # would answer a question the caller has moved on from.
                        return
                    if lines := self._agents[going_to].enter:
                        # Cover the hop: a whole model request stands between the
                        # caller finishing and the next agent's first word. Through
                        # the state, so a tool firing next does not cover it twice.
                        await self._call.say(_pick(f'enter:{output.to}', lines))
                logger.warning(
                    'handoff budget of {} spent, making {} answer', self._max_hops, self._call.state.agent
                )
                # Every hop handed the caller on, so nobody has said anything and
                # the line has been silent since the last fixed line. Run whoever
                # they ended up with once more with `str` as the only output: with
                # `hand_over` and the finishers off the table, speech is all it can
                # produce. That run cannot book or verify, which costs a turn -- a
                # caller sitting in silence has already lost one.
                await self._speak(self._agents[self._call.state.agent], prompt, output_type=str)
            finally:
                # In `finally`: the loop returns early both when an agent speaks
                # and when the caller barges in mid-hop.
                span.set_attribute('agent', self._call.state.agent)
                span.set_attribute('hops', len(handoffs) + 1)
                span.set_attribute('interrupted', self._interrupted)
                if handoffs:
                    span.set_attribute('handoffs', ', '.join(handoffs))
                # A turn that ends without speech leaves TTFB armed, and the next
                # turn reports its own first word as seconds late. Idempotent:
                # a turn that did speak stopped it already.
                await self.stop_ttfb_metrics()

    async def _speak(self, node: Node, prompt: str | None, output_type=None):
        """Run one step's agent, streaming whatever it says straight to TTS.

        `output_type` narrows what this one run may return. Left `None`, the
        step's own `outputs` builds it from the live state, which is how a
        guarded exit leaves the `hand_over` schema the moment its guard stops
        holding. A step with no `outputs` keeps the agent's own. Its tools are
        untouched either way.
        """
        agent: Agent = node.agent
        if output_type is None and node.outputs is not None:
            output_type = node.outputs(self._call.state)
        spoke = False
        output = None
        self._succeeded = set()
        started = time.perf_counter()
        await self.push_frame(LLMFullResponseStartFrame())
        await self.start_ttfb_metrics()
        await self.start_processing_metrics()
        try:
            async with agent.run_stream_events(
                prompt,
                deps=self._call,
                message_history=list(self._call.messages),
                output_type=output_type,
            ) as events:
                self._events = events
                async for event in events:
                    if text := _speech(event):
                        if not spoke:
                            await self.stop_ttfb_metrics()
                            spoke = True
                            # Pydantic AI's OTel instrumentation emits no
                            # first-token event, so Langfuse shows every
                            # generation with a null `timeToFirstToken` and a
                            # trace cannot say how much of a round trip was
                            # waiting. This is the moment, and we are standing
                            # in it. On the turn span, not the current one: the
                            # current one belongs to pydantic-ai and changes
                            # under us, and what a caller feels is the first
                            # word of the *turn*, which is why it is written
                            # once and the second hop does not overwrite it.
                            if self._turn_span is not None:
                                self._turn_span.set_attribute(
                                    'ttft', round(time.perf_counter() - started, 3)
                                )
                                self._turn_span = None
                            # The model is talking, so the gap is closed: a tool
                            # called later in this turn may speak its own line.
                            self._call._covered = False
                        await self.push_frame(LLMTextFrame(text))
                    elif isinstance(event, FunctionToolResultEvent):
                        # Which tools *succeeded* is how a free edge is taken: a
                        # step lends its neighbour's entry tool, and completing
                        # it means the caller is now in the neighbour.
                        #
                        # The result, never the call. This used to watch
                        # `FunctionToolCallEvent`, which fires when the model
                        # asks for a tool -- before it runs, and regardless of
                        # whether it then raises, retries or is cancelled by
                        # barge-in. A `read_diary` that refused an unidentified
                        # caller still moved them into `booking`. A
                        # `RetryPromptPart` is the tool saying no, so it is not
                        # a move.
                        if not isinstance(event.part, RetryPromptPart):
                            self._succeeded.add(event.part.tool_name)
                    elif isinstance(event, AgentRunResultEvent):
                        self._call.messages = event.result.all_messages()
                        output = event.result.output
        except RunCancelled as cancelled:
            # Barge-in. Keep what completed: a tool that ran and returned is a
            # thing that happened, and the next turn has to know about it.
            self._call.messages = cancelled.all_messages()
        except Exception as error:
            logger.exception('agent run failed')
            await self.push_error(f'agent run failed: {error}', exception=error)
            if not spoke:
                # An error the caller can hear beats silence on an open line, so
                # this one goes past the gate: a filler may already have spoken.
                await self.say(STUMBLE)
        finally:
            self._events = None
            await self.stop_processing_metrics()
            await self.push_frame(LLMFullResponseEndFrame())
        return output

    def note_spoken(self, content: str, interrupted: bool) -> None:
        """Cut the last answer down to what the caller actually heard.

        Barge-in stops TTS mid-sentence, but the run recorded every token the
        model produced. Left alone the agent believes it said things nobody
        heard, and says "as I mentioned" about them on the next turn. The
        assistant aggregator has already worked out the spoken prefix, so read
        it from there rather than counting TTS frames.

        Wire it to the aggregator's ``on_assistant_turn_stopped`` event.
        """
        if not interrupted:
            return
        for index in range(len(self._call.messages) - 1, -1, -1):
            message = self._call.messages[index]
            if not isinstance(message, ModelResponse):
                continue
            if not any(isinstance(part, TextPart) for part in message.parts):
                continue  # a tool-call-only response: nothing was ever spoken
            kept = [part for part in message.parts if not isinstance(part, TextPart)]
            if content:
                kept.append(TextPart(content=content))
            if kept:
                message.parts = kept
            else:
                # Nothing was heard and nothing else happened. An empty response
                # is not a message any provider will take back.
                del self._call.messages[index]
            return
