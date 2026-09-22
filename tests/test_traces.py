"""Run: uv run python -m tests.test_traces

Two silent failures, neither of which breaks a call: a caller turn that starts a
trace of its own instead of landing inside Pipecat's open turn span, and
Pipecat's stt and tts spans reaching Langfuse in words it does not render.

Asserts, no pytest, no network.
"""

import asyncio
import json

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from utils.observability import _Speech

spans = InMemorySpanExporter()
"""Installed before anything imports a tracer, or every module takes the no-op one.

Same order as `observability.setup`: the translation runs before the exporter.
"""
provider = TracerProvider()
provider.add_span_processor(_Speech())
provider.add_span_processor(SimpleSpanProcessor(spans))
trace.set_tracer_provider(provider)

from pipecat.utils.tracing.service_attributes import (  # noqa: E402
    add_stt_span_attributes,
    add_tts_span_attributes,
)
from pipecat.utils.tracing.tracing_context import TracingContext  # noqa: E402
from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel  # noqa: E402

Agent.instrument_all()

from core.processor import PydanticAgentProcessor  # noqa: E402
from salon.agents import AGENTS  # noqa: E402
from salon.models.state import open_call  # noqa: E402

PHONE = '+34600111222'


def named(name):
    return [span for span in spans.get_finished_spans() if span.name == name]


def says(text):
    async def stream(messages, info: AgentInfo):
        yield text

    return stream


def hands_to(name):
    """A scripted `hand_over`: the first output tool a step declares is always its handoff."""

    async def stream(messages, info: AgentInfo):
        yield {
            0: DeltaToolCall(
                name=info.output_tools[0].name,
                json_args=json.dumps({'to': name, 'because': 'test'}),
            )
        }

    return stream


class Probe(PydanticAgentProcessor):
    async def push_frame(self, frame, direction=None):
        pass


def open_turn() -> tuple[TracingContext, trace.Span, trace.Span]:
    """What Pipecat's TurnTraceObserver leaves behind for the rest of the pipeline."""
    tracer = trace.get_tracer('pipecat.turn')
    conversation = tracer.start_span('conversation')
    context = TracingContext()
    context.set_conversation_context(conversation.get_span_context(), 'call-1')
    turn = tracer.start_span('turn', context=context.get_conversation_context())
    turn.set_attribute('turn.number', 1)
    turn.set_attribute('conversation.id', 'call-1')
    context.set_turn_context(turn.get_span_context())
    return context, conversation, turn


async def one_trace_per_call():
    """The agents' span hangs off Pipecat's turn, and both agent runs sit under it.

    A verified caller in `booking` asks a general question; the step hands
    them to `concierge`, the only step whose model speech streams, and that
    step answers. Two runs, one turn, one tree.
    """
    context, conversation, turn = open_turn()

    call = open_call(PHONE)
    call.commit(agent='booking', customer_verified='existing', verified_phone=PHONE)
    probe = Probe(call, AGENTS)
    probe._tracing = context

    with (
        AGENTS['booking'].agent.override(model=FunctionModel(stream_function=hands_to('concierge'))),
        AGENTS['concierge'].agent.override(
            model=FunctionModel(stream_function=says('There is metered parking on the road outside.'))
        ),
    ):
        await probe._turn('is there parking nearby?')

    turn.end()
    conversation.end()

    finished = spans.get_finished_spans()
    by_id = {span.context.span_id: span for span in finished}

    agents = named('agents')
    assert len(agents) == 1, [span.name for span in finished]
    agents = agents[0]

    assert agents.parent is not None, 'the agents span started its own trace'
    assert agents.parent.span_id == turn.get_span_context().span_id
    assert agents.context.trace_id == conversation.get_span_context().trace_id

    assert agents.attributes['agent'] == 'concierge', agents.attributes
    assert agents.attributes['hops'] == 2, agents.attributes
    assert agents.attributes['handoffs'] == 'concierge', agents.attributes
    assert agents.attributes['interrupted'] is False, agents.attributes

    runs = [span for span in finished if span.instrumentation_scope.name == 'pydantic-ai']
    assert runs, 'pydantic-ai emitted nothing; is instrument_all() still called?'
    for span in runs:
        while span.parent is not None and span.context.span_id != agents.context.span_id:
            span = by_id.get(span.parent.span_id)
            assert span is not None, 'an agent span escaped the turn'
        assert span.context.span_id == agents.context.span_id

    assert named('turn')[0].attributes['langfuse.observation.metadata.conversation.id'] == 'call-1'


def speech_reaches_langfuse():
    """Built with Pipecat's own attribute helpers, so a rename over there fails here.

    The tts text is what the service was asked for, so it lands as the
    observation's input; the stt transcript is what came out, so it is the output.
    """
    tracer = trace.get_tracer('pipecat')

    heard = tracer.start_span('stt')
    add_stt_span_attributes(
        span=heard,
        service_name='SlngSTTService',
        model='soniox/speech-ai:rt-v5',
        transcript='can I book a haircut',
        is_final=True,
        language='en',
        ttfb=0.21,
    )
    heard.end()

    said = tracer.start_span('tts')
    add_tts_span_attributes(
        span=said,
        service_name='SlngTTSService',
        model='cartesia/sonic:3.5',
        voice_id='62ae83ad-4f6a-430b-af41-a9bede9286ca',
        text='Of course, when suits you?',
        character_count=26,
        ttfb=0.18,
    )
    said.end()

    heard = named('stt')[0].attributes
    assert heard['langfuse.observation.type'] == 'generation', heard
    assert heard['langfuse.observation.output'] == 'can I book a haircut', heard
    assert heard['langfuse.observation.model.name'] == 'soniox/speech-ai:rt-v5', heard
    assert heard['langfuse.observation.metadata.metrics.ttfb'] == 0.21, heard
    assert heard['langfuse.observation.metadata.language'] == 'en', heard

    said = named('tts')[0].attributes
    assert said['langfuse.observation.type'] == 'generation', said
    assert said['langfuse.observation.input'] == 'Of course, when suits you?', said
    assert said['langfuse.observation.model.name'] == 'cartesia/sonic:3.5', said
    assert said['langfuse.observation.metadata.metrics.character_count'] == 26, said
    assert said['langfuse.observation.metadata.voice_id'].startswith('62ae83ad'), said


async def main():
    await one_trace_per_call()
    speech_reaches_langfuse()
    print('ok')


if __name__ == '__main__':
    asyncio.run(main())
