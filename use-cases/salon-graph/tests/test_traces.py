"""Run: uv run python -m tests.test_traces

Three silent failures, none of which breaks a call: a caller turn that starts a
trace of its own instead of landing inside Pipecat's open turn span, Pipecat's
stt and tts spans reaching Langfuse in words it does not render, and a root
observation that says nothing about the call while its children sit in a session
of their own.

Asserts, no pytest, no network.
"""

import asyncio
import contextvars
import json

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from core.observability import _CALL, _prompt, _readable, _said, _Speech

spans = InMemorySpanExporter()
"""Installed before anything imports a tracer, or every module takes the no-op one.

Same order as `observability.setup`: the translation runs before the exporter.
"""
speech = _Speech()
provider = TracerProvider()
provider.add_span_processor(speech)
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
from salon.app import FLOW  # noqa: E402
from tests.script import open_call  # noqa: E402

AGENTS = FLOW.nodes

PHONE = '+34600111222'


def named(name):
    return [span for span in spans.get_finished_spans() if span.name == name]


def says(text):
    async def stream(messages, info: AgentInfo):
        yield text

    return stream


def hands_to(name):
    """A scripted `hand_over`, the output every step with exits declares."""

    async def stream(messages, info: AgentInfo):
        yield {
            0: DeltaToolCall(
                name='hand_over',
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
    call.update(customer_status='existing', verified_phone=PHONE)
    call.commit(agent='booking')
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

    agents = named('concierge')
    assert len(agents) == 1, [span.name for span in finished]
    agents = agents[0]
    assert agents.attributes['langfuse.observation.type'] == 'agent', agents.attributes

    assert agents.parent is not None, 'the agents span started its own trace'
    assert agents.parent.span_id == turn.get_span_context().span_id
    assert agents.context.trace_id == conversation.get_span_context().trace_id

    assert agents.attributes['agent'] == 'concierge', agents.attributes
    assert agents.attributes['hops'] == 2, agents.attributes
    assert agents.attributes['handoffs'] == 'concierge', agents.attributes
    assert agents.attributes['interrupted'] is False, agents.attributes
    assert tuple(agents.attributes['classifier_outcome']) == (), agents.attributes
    assert agents.attributes['classifier_fallback'] is False, agents.attributes

    runs = [span for span in finished if span.instrumentation_scope.name == 'pydantic-ai']
    assert runs, 'pydantic-ai emitted nothing; is instrument_all() still called?'
    for span in runs:
        while span.parent is not None and span.context.span_id != agents.context.span_id:
            span = by_id.get(span.parent.span_id)
            assert span is not None, 'an agent span escaped the turn'
        assert span.context.span_id == agents.context.span_id

    assert named('turn')[0].attributes['langfuse.observation.metadata.conversation.id'] == 'call-1'

    outputs = {
        span.attributes['gen_ai.agent.name']: span.attributes.get('langfuse.observation.output')
        for span in runs
        if span.attributes.get('gen_ai.operation.name') == 'invoke_agent'
    }
    assert outputs['booking'] == 'handed over to concierge: test', outputs
    assert outputs['concierge'] == 'There is metered parking on the road outside.', outputs
    chats = [
        span.attributes.get('langfuse.observation.output')
        for span in runs
        if span.attributes.get('gen_ai.operation.name') == 'chat'
    ]
    assert any('"hand_over"' in str(out) for out in chats), chats
    assert 'There is metered parking on the road outside.' in chats, chats

    prompts = {
        (span.attributes['gen_ai.agent.name'], span.attributes['gen_ai.operation.name']): json.loads(
            str(span.attributes.get('langfuse.observation.input'))
        )
        for span in runs
        if span.attributes.get('gen_ai.operation.name') in ('invoke_agent', 'chat')
    }
    for (step, _operation), lines in prompts.items():
        assert lines[0]['role'] == 'system' and 'Robin' in lines[0]['content'], (step, lines[0])
        assert {'role': 'user', 'content': 'is there parking nearby?'} in lines, (step, lines)
        assert lines[-1]['role'] == 'user', (step, lines[-1])
    assert len(prompts) == 4, prompts.keys()


def agent_output_is_readable():
    """A run's result and a request's answer, as sentences rather than dumps."""
    assert _readable('{"to": "booking", "because": "wants a haircut", "status": "handoff"}') == (
        'handed over to booking: wants a haircut'
    )
    assert _readable('{"to": "booking", "because": "identified", "status": "completed"}') == (
        'finished; back to booking'
    )
    assert _readable('{"about": "phone", "value": "+34600111222", "spoken": "Is that +34600111222?"}') == (
        'Is that +34600111222?'
    )
    assert _readable('Have a great day!') == 'Have a great day!'
    assert (
        _readable('{"recorded": true, "reference": "CX0001"}') == '{"recorded": true, "reference": "CX0001"}'
    )

    text = json.dumps([{'role': 'assistant', 'parts': [{'type': 'text', 'content': 'Which day?'}]}])
    call = json.dumps(
        [
            {
                'role': 'assistant',
                'parts': [
                    {'type': 'tool_call', 'id': 'c1', 'name': 'read_diary', 'arguments': {'when': 'Friday'}}
                ],
            }
        ]
    )
    both = json.dumps(
        [
            {
                'role': 'assistant',
                'parts': [
                    {'type': 'text', 'content': 'One moment.'},
                    {'type': 'tool_call', 'name': 'hang_up', 'arguments': {'goodbye': 'Bye'}},
                ],
            }
        ]
    )
    assert _said(text) == 'Which day?'
    assert json.loads(_said(call)) == {
        'role': 'assistant',
        'content': '',
        'tool_calls': [
            {
                'id': 'c1',
                'type': 'function',
                'function': {'name': 'read_diary', 'arguments': '{"when": "Friday"}'},
            }
        ],
    }
    said = json.loads(_said(both))
    assert said['content'] == 'One moment.' and said['tool_calls'][0]['function']['name'] == 'hang_up', said
    assert _said('not json') == ''

    system = json.dumps([{'type': 'text', 'content': 'You are Robin.'}])
    assert json.loads(_prompt(system, call)) == [
        {'role': 'system', 'content': 'You are Robin.'},
        {
            'role': 'assistant',
            'content': '',
            'tool_calls': [
                {
                    'id': 'c1',
                    'type': 'function',
                    'function': {'name': 'read_diary', 'arguments': '{"when": "Friday"}'},
                }
            ],
        },
    ]
    assert json.loads(_prompt('', 'not json')) == []


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


def a_call_reads_as_a_call():
    """The root observation says what the call was, and every span shares its session.

    Langfuse v4 queries observations, so both failed silently and separately: a
    root showing `input: null, output: undefined`, and the agency's spend in a
    session of Pydantic AI's own invention while the call's own session held
    nothing but Pipecat's spans.
    """
    _CALL.set({'session.id': 'sess-1', 'trace.name': 'salon call'})

    tracer = trace.get_tracer('pipecat.turn')
    conversation = tracer.start_span('conversation')
    context = TracingContext()
    context.set_conversation_context(conversation.get_span_context(), 'sess-1')
    turn = tracer.start_span('turn', context=context.get_conversation_context())
    context.set_turn_context(turn.get_span_context())

    heard = tracer.start_span('stt', context=context.get_turn_context())
    add_stt_span_attributes(
        span=heard,
        service_name='SlngSTTService',
        model='soniox/speech-ai:rt-v5',
        transcript='is Tuesday free',
    )
    heard.end()
    said = tracer.start_span('tts', context=context.get_turn_context())
    add_tts_span_attributes(
        span=said,
        service_name='SlngTTSService',
        model='cartesia/sonic:3.5',
        voice_id='62ae83ad-4f6a-430b-af41-a9bede9286ca',
        text='Ten or half four.',
    )
    said.end()

    ours = trace.get_tracer('slng-voice-harness').start_span('agents')
    ours.end()

    turn.end()
    conversation.end()

    spoken = named('turn')[-1].attributes
    assert spoken['langfuse.observation.input'] == 'is Tuesday free', spoken
    assert spoken['langfuse.observation.output'] == 'Ten or half four.', spoken

    whole = named('conversation')[-1].attributes
    assert json.loads(whole['langfuse.observation.input']) == [
        {'role': 'user', 'content': 'is Tuesday free'},
        {'role': 'assistant', 'content': 'Ten or half four.'},
    ], whole
    assert whole['langfuse.observation.output'] == 'Ten or half four.', whole

    for span in (whole, spoken, named('stt')[-1].attributes, named('agents')[-1].attributes):
        assert span['langfuse.session.id'] == 'sess-1', span
        assert span['langfuse.trace.name'] == 'salon call', span


def two_calls_at_once_stay_apart():
    """Two calls served side by side keep their own session and their own transcript.

    One runner serves many calls, and their spans end interleaved. The call
    used to be one process-wide dict, so every span took the session of
    whichever call started last, and the rolled-up transcript mixed both.
    """
    tracer = trace.get_tracer('pipecat.turn')

    def begin(session: str):
        _CALL.set({'session.id': session, 'trace.name': 'salon call'})
        conversation = tracer.start_span('conversation')
        context = TracingContext()
        context.set_conversation_context(conversation.get_span_context(), session)
        turn = tracer.start_span('turn', context=context.get_conversation_context())
        context.set_turn_context(turn.get_span_context())
        return conversation, turn, context

    def heard(context, words: str):
        span = tracer.start_span('stt', context=context.get_turn_context())
        add_stt_span_attributes(span=span, service_name='SlngSTTService', model='m', transcript=words)
        span.end()

    first_call, second_call = contextvars.copy_context(), contextvars.copy_context()
    a_conversation, a_turn, a = first_call.run(begin, 'call-a')
    b_conversation, b_turn, b = second_call.run(begin, 'call-b')
    first_call.run(heard, a, 'yes please')
    second_call.run(heard, b, 'cancel it')
    first_call.run(a_turn.end)
    second_call.run(b_turn.end)
    second_call.run(b_conversation.end)
    first_call.run(a_conversation.end)

    turns = {t.attributes['langfuse.session.id']: t.attributes for t in named('turn')[-2:]}
    assert turns['call-a']['langfuse.observation.input'] == 'yes please', turns
    assert turns['call-b']['langfuse.observation.input'] == 'cancel it', turns
    for span in named('stt')[-2:]:
        words = span.attributes['langfuse.observation.output']
        assert span.attributes['langfuse.session.id'] == ('call-a' if words == 'yes please' else 'call-b'), (
            span
        )
    assert not speech._calls and not speech._lines and not speech._turns, 'a finished call left state behind'


async def every_write_names_the_question_it_answered():
    """The harness's writes each leave a `write` span naming the question the caller answered.

    Driven through the salon's real flow with scripted node agents: a
    booking, its cancellation and a complaint, each on a yes to its own
    read-back. A refused write leaves no span: nothing was attempted.
    """
    from tests.script import opened, slot
    from tests.script import turn as said

    script, flow, call, books = opened(verified_phone=PHONE, customer_status='existing')
    call.commit(agent='booking')
    spans.clear()
    friday = slot('2026-10-02', '09:00')
    await said(
        flow,
        call,
        script,
        'A haircut on Friday at nine.',
        ('collect', {'service': 'haircut', 'booking_day': 'Friday', 'booking_time': '09:00'}),
        ('call', 'options'),
        ('say', 'Nine is free on Friday.'),
    )
    booked = await said(
        flow, call, script, 'Nine, please.', ('collect', {'slot': friday}), ('call', 'book_appointment')
    )
    assert not named('write'), 'a read-back left an audit: nothing was attempted'
    await said(flow, call, script, 'Yes.', ('call', 'book_appointment'))
    held = books.bookings_for(PHONE, books.now().date())[0]['booking_id']
    cancel = await said(
        flow,
        call,
        script,
        'Cancel it.',
        ('call', 'options'),
        ('collect', {'appointment': held}),
        ('call', 'cancel_appointment'),
    )
    await said(flow, call, script, 'Yes.', ('call', 'cancel_appointment'))
    call.commit(agent='complaints')
    complaint = await said(
        flow,
        call,
        script,
        'The last cut was uneven. I would like a redo.',
        ('collect', {'complaint': 'The last cut was uneven', 'wanted_outcome': 'I would like a redo'}),
        ('call', 'record_complaint'),
    )
    await said(flow, call, script, 'Absolutely.', ('call', 'record_complaint'))

    audits = [json.loads(str(span.attributes['write.audit'])) for span in named('write')]
    assert [(a['action'], a['status']) for a in audits] == [
        ('book_appointment', 'ok'),
        ('cancel_appointment', 'ok'),
        ('record_complaint', 'ok'),
    ], audits
    for audit, asked in zip(audits, (booked[-1], cancel[-1], complaint[-1]), strict=True):
        assert audit['question'] == asked and audit['asked_turn'] < audit['turn'], audit
    assert [a['heard'] for a in audits] == ['Yes.', 'Yes.', 'Absolutely.']


async def main():
    await one_trace_per_call()
    await every_write_names_the_question_it_answered()
    agent_output_is_readable()
    speech_reaches_langfuse()
    a_call_reads_as_a_call()
    two_calls_at_once_stay_apart()
    print('ok')


if __name__ == '__main__':
    asyncio.run(main())
