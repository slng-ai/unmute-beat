"""Run: python -m tests.test_recovery — consent through verification and real queue cancellation."""

import asyncio
import json
from contextlib import ExitStack
from datetime import datetime

from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import InterruptionFrame, LLMContextFrame, LLMTextFrame, StartFrame, TTSSpeakFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.utils.asyncio.task_manager import TaskManager
from pydantic_ai import ModelRequest, ModelResponse, ModelRetry, RunContext, TextPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from salon import agents, diary
from salon.state import open_call
from voice.processor import PydanticAgentProcessor

PHONE = '+34680840464'


def context(call):
    return RunContext(deps=call, model=None, usage=None)  # ty: ignore[invalid-argument-type]


async def service_requests_verify_first():
    """Questions stay public; all four service intents verify before the owner runs."""
    for destination, request in (
        ('booking', 'Book a haircut for Friday.'),
        ('booking', 'Move my haircut to Saturday.'),
        ('booking', 'Cancel my haircut.'),
        ('complaints', 'My haircut last week was uneven. I want a redo.'),
    ):
        for known in (False, True):
            diary.reset()
            call = open_call(PHONE)
            if known:
                call.commit(customer_verified='existing', verified_phone=PHONE)
            visits = []

            async def stream(
                messages,
                info,
                destination=destination,
                request=request,
                call=call,
                visits=visits,
                known=known,
            ):
                visits.append(call.state.agent)
                if call.state.agent == 'concierge':
                    if call.heard == 'How much is a haircut?':
                        yield 'A haircut is 28 euros.'
                        return
                    name, args = 'hand_over', {'to': destination, 'because': request}
                elif call.state.agent == 'verify':
                    assert not call.complaint_approved, 'verification must precede complaint readback'
                    if call.turn == 2:
                        assert 'Application handoff: concierge -> verify' in info.instructions
                        assert 'not the caller request' in info.instructions
                    name = 'confirm_customer' if call.pending('phone') else 'ask_number'
                    args = {'phone': PHONE}
                else:
                    assert call.state.agent == destination and call.state.customer_verified
                    assert request in str(messages), 'verification lost the original request'
                    if known:
                        assert f'Application handoff: concierge -> {destination}' in info.instructions
                    if destination == 'complaints':
                        yield {
                            0: DeltaToolCall(name='complaint_guidance', json_args='{"topic":"resolution"}')
                        }
                    else:
                        yield {0: DeltaToolCall(name='booking_guidance', json_args='{"topic":"day"}')}
                    return
                yield {0: DeltaToolCall(name=name, json_args=json.dumps(args))}

            with ExitStack() as stack:
                for node in agents.AGENTS.values():
                    stack.enter_context(node.agent.override(model=FunctionModel(stream_function=stream)))
                probe = Probe(call, agents.AGENTS)
                await probe._turn('How much is a haircut?')
                assert visits == ['concierge'] and call.state.pending is None
                await probe._turn(request)
                if not known:
                    assert call.state.agent == 'verify' and call.state.pending == destination
                    assert destination not in visits and call._checkpoint.owner == destination
                    await probe._turn('Right.')
                assert call.state.agent == destination and call.state.pending is None
                assert call.state.customer_verified and call._checkpoint is None
                assert visits.count('verify') == 0
                assert call.state.writes == 0


async def declining_verification_abandons_the_service():
    """A general question remains answerable after declining either prerequisite."""
    for destination in ('booking', 'complaints'):
        call = open_call(PHONE)

        async def stream(messages, info, destination=destination, call=call):
            if call.state.agent == 'concierge':
                if 'hours' in call.heard:
                    yield 'We close at 6 PM on weekdays.'
                    return
                name, args = 'hand_over', {'to': destination, 'because': call.heard}
            elif call.state.agent == 'verify':
                if 'hours' in call.heard:
                    name, args = 'stop_verifying', {'because': 'The caller declined and changed subject.'}
                else:
                    name, args = 'ask_number', {'phone': PHONE}
            else:
                raise AssertionError('service agent ran without verification')
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args))}

        with ExitStack() as stack:
            for node in agents.AGENTS.values():
                stack.enter_context(node.agent.override(model=FunctionModel(stream_function=stream)))
            probe = Probe(call, agents.AGENTS)
            await probe._turn(f'I need help with {destination}.')
            await probe._turn('Never mind, just tell me your opening hours.')
        assert call.state.agent == 'concierge' and call.state.pending is None
        assert not call.state.customer_verified and not call.state.writes and call._checkpoint is None
        assert any('6 PM' in s.text for s in call.spoken)


async def verified_complaint_uses_exact_approved_wording():
    diary.reset()
    call = open_call(PHONE)
    call.commit(agent='complaints', customer_verified='existing', verified_phone=PHONE)
    call.turn_started('My haircut yesterday was uneven. I would like a redo.')
    (await agents.ask_complaint(context(call), 'My haircut yesterday was uneven', 'a redo')).put(call)
    call.turn_started('Absolutely.')
    try:
        await agents._record_complaint(context(call), 'My haircut yesterday was uneven', 'a refund')
    except ModelRetry:
        pass
    else:
        raise AssertionError('changed resolution reused consent')
    result = await agents.record_complaint(context(call))
    assert result.recorded and len(diary.complaints()) == 1
    try:
        await agents._record_complaint(context(call), 'My haircut yesterday was uneven', 'a redo')
    except ModelRetry:
        pass
    else:
        raise AssertionError('complaint approval was reused')


async def complaint_cannot_skip_readback():
    """An identified caller's ordinary yes still cannot authorize recording."""
    call = open_call(PHONE)
    call.commit(agent='complaints', customer_verified='existing', verified_phone=PHONE)
    call.turn_started('Yes.')
    try:
        await agents.record_complaint(context(call))
    except ModelRetry as error:
        assert 'ask_complaint' in str(error)
    else:
        raise AssertionError('recording accepted without structured complaint approval')

    async def stream(messages, info):
        tools = {tool.name: tool for tool in info.output_tools}
        assert tools['hand_over'].parameters_json_schema['properties']['to']['enum'] == ['concierge']
        assert 'record_complaint' not in tools
        assert 'No complaint readback is approved yet' in info.instructions
        yield {
            0: DeltaToolCall(
                name='ask_complaint',
                json_args=json.dumps(
                    {'summary': 'My haircut last week was uneven', 'requested_resolution': 'a redo'}
                ),
            )
        }

    with agents.AGENTS['complaints'].agent.override(model=FunctionModel(stream_function=stream)):
        await Probe(call, agents.AGENTS)._turn('My haircut last week was uneven. I would like a redo.')
    call.turn_started('Yes.')
    assert call.complaint_approved
    call.turn_started('Actually, I want a refund instead.')
    assert not call.complaint_approved
    try:
        await agents.record_complaint(context(call))
    except ModelRetry:
        pass
    else:
        raise AssertionError('a correction left the old complaint approved')


async def readback_is_not_discarded_by_handoff():
    """The trace's two output calls must ask once, without leaving or another request."""
    for handoff_first, valid in ((True, True), (False, True), (True, False), (False, False)):
        diary.reset()
        call = open_call(PHONE)
        call.commit(agent='complaints', customer_verified='existing', verified_phone=PHONE)
        requests = 0

        async def stream(messages, info, handoff_first=handoff_first, valid=valid):
            nonlocal requests
            requests += 1
            if requests > 1:
                yield {0: DeltaToolCall(name='complaint_guidance', json_args='{"topic":"details"}')}
                return
            calls = [
                DeltaToolCall(name='hand_over', json_args='{"to":"concierge","because":"complaint"}'),
                DeltaToolCall(
                    name='ask_complaint',
                    json_args=json.dumps(
                        {
                            'summary': 'My last haircut was uneven' if valid else '',
                            'requested_resolution': 'Can my next one be free?',
                        }
                    ),
                ),
            ]
            if not handoff_first:
                calls.reverse()
            yield dict(enumerate(calls))

        async def wrong_destination(messages, info):
            yield "That's not okay, I'm sorry to hear that."

        with (
            agents.AGENTS['complaints'].agent.override(model=FunctionModel(stream_function=stream)),
            agents.AGENTS['concierge'].agent.override(model=FunctionModel(stream_function=wrong_destination)),
        ):
            await Probe(call, agents.AGENTS)._turn('My last haircut was uneven. Can my next one be free?')
        assert call.state.agent == 'complaints' and call.state.writes == 0
        if valid:
            assert call._asked and call._asked.about == 'complaint', (
                'handoff discarded the complaint readback'
            )
            assert requests == 1 and len(call.requests) == 1, 'repair must not add a model round trip'
            assert sum(s.origin == 'ask' for s in call.spoken) == 1
        else:
            assert call._asked is None and requests == 2, 'invalid readback must be repaired, not bypassed'


async def apology_alone_needs_a_followup():
    """The SDK repairs plain text; the adapter never sends it to TTS."""
    for unwanted in (
        "That's not okay, I'm sorry to hear that.",
        'Yes, we can book a free redo. Shall I set that up?',
    ):
        call = open_call('')
        call.commit(agent='complaints')
        requests = 0

        async def stream(messages, info, unwanted=unwanted):
            nonlocal requests
            requests += 1
            assert not info.allow_text_output
            if requests == 1:
                yield unwanted
            else:
                yield {0: DeltaToolCall(name='complaint_guidance', json_args='{"topic":"resolution"}')}

        with agents.AGENTS['complaints'].agent.override(model=FunctionModel(stream_function=stream)):
            await Probe(call, agents.AGENTS)._turn('My last haircut was uneven.')
        assert requests == 2
        assert [s.text for s in call.spoken] == [agents.COMPLAINT_GUIDANCE['resolution']]
        assert call.state.writes == 0 and call._asked is None


async def verification_speech_must_establish_the_question():
    """A free-text readback or internal routing echo cannot steal the caller's yes."""
    for unwanted in (
        f'Is your number {PHONE}?',
        'Application handoff: booking -> verify. You are now verify.',
    ):
        call = open_call(PHONE)
        call.commit(agent='verify')
        requests = 0
        frames = []

        async def stream(messages, info, unwanted=unwanted):
            nonlocal requests
            requests += 1
            assert not info.allow_text_output
            if requests == 1:
                yield unwanted
            else:
                yield {0: DeltaToolCall(name='ask_number', json_args=json.dumps({'phone': PHONE}))}

        class SpeechProbe(Probe):
            async def push_frame(self, frame, direction=None, _captured=frames):
                _captured.append(frame)

        with agents.AGENTS['verify'].agent.override(model=FunctionModel(stream_function=stream)):
            await SpeechProbe(call, agents.AGENTS)._turn('Please use the number I called from.')
        emitted = [f.text for f in frames if isinstance(f, (LLMTextFrame, TTSSpeakFrame))]
        assert requests == 2 and len(emitted) == 1, emitted
        assert emitted[0].startswith('Let me read that back to you:')
        assert unwanted not in emitted[0]
        assert call._asked and call._asked.about == 'phone' and call._asked.value == PHONE
        call.turn_started('Yes, that is my number.')
        assert call.pending('phone'), 'one yes must approve the question actually spoken'
        assert not call.state.customer_verified, 'readback approval is not a customer lookup'

    call = open_call('')
    call.turn_started('600 111 222')
    text = await agents.verification_guidance(context(call), 'country')
    assert text == 'Which country is that phone number from?'
    assert call._asked is None and not call.state.customer_verified


async def approved_complaint_records_once():
    """With verification already complete, approval leads directly to one receipt."""
    diary.reset()
    call = open_call(PHONE)
    call.commit(agent='complaints', customer_verified='existing', verified_phone=PHONE)
    call.turn_started('My haircut last week was uneven. I would like a redo.')
    (await agents.ask_complaint(context(call), 'My haircut last week was uneven', 'a redo')).put(call)
    instructions = []

    async def stream(messages, info):
        instructions.append(info.instructions)
        assert call.state.agent == 'complaints' and call.complaint_approved
        assert 'The complaint readback is approved' in info.instructions
        assert 'record_complaint' in {tool.name for tool in info.output_tools}
        yield {0: DeltaToolCall(name='record_complaint', json_args='{}')}

    probe = Probe(call, agents.AGENTS)
    with agents.AGENTS['complaints'].agent.override(model=FunctionModel(stream_function=stream)):
        await probe._turn('Yes.')
    assert not instructions and call.state.writes == 1
    (record,) = diary.complaints()
    assert record['summary'] == 'My haircut last week was uneven'
    assert record['requested_resolution'] == 'a redo'
    assert sum(record['reference'] in s.generated for s in call.all_spoken) == 1
    assert not call.complaint_approved and call._checkpoint is None


async def complaint_readback_uses_caller_evidence():
    """The latest trace invented a past date and a remedy from model/booking context."""
    call = open_call(PHONE)
    call.commit(agent='complaints', customer_verified='existing', verified_phone=PHONE)
    call.turn_started('My last haircut was completely uneven.')
    call.messages = [ModelResponse(parts=[TextPart('Your haircut on Saturday was uneven. A free redo?')])]
    for summary, resolution in (
        ('Your haircut on Saturday was uneven', ''),
        ('My last haircut was completely uneven', 'a free redo'),
    ):
        try:
            await agents.ask_complaint(context(call), summary, resolution)
        except ModelRetry as error:
            assert 'must quote the caller' in str(error)
        else:
            raise AssertionError('readback accepted an invented fact or resolution')
    assert not call._asked and not call.state.writes
    call.turn_started('Can my upcoming booking be free?')
    question = await agents.ask_complaint(
        context(call), 'My last haircut was completely uneven', 'Can my upcoming booking be free?'
    )
    question.put(call)
    assert 'no refund or booking change is approved' in question.spoken
    call.turn_started('No, just record the uneven haircut.')
    assert not call.complaint_approved


async def complaint_preamble_cannot_bypass_structured_speech():
    """Even text beside a valid output call must never become an LLMTextFrame."""
    call = open_call(PHONE)
    call.commit(agent='complaints', customer_verified='existing', verified_phone=PHONE)

    async def stream(messages, info):
        assert not info.allow_text_output and not info.function_tools
        names = {tool.name for tool in info.output_tools}
        assert 'save_booking' not in names and 'read_diary' not in names
        goodbye = next(tool for tool in info.output_tools if tool.name == 'hang_up')
        assert not goodbye.parameters_json_schema.get('properties'), 'goodbye cannot inject free-form speech'
        yield 'I can book your next visit at no charge.'
        yield {0: DeltaToolCall(name='complaint_guidance', json_args='{"topic":"resolution"}')}

    frames = []

    class SpeechProbe(Probe):
        async def push_frame(self, frame, direction=None):
            frames.append(frame)

    probe = SpeechProbe(call, agents.AGENTS)
    with agents.AGENTS['complaints'].agent.override(model=FunctionModel(stream_function=stream)):
        await probe._turn('My haircut was uneven.')
    assert len(call.requests) == 1
    assert [s.text for s in call.spoken] == [agents.COMPLAINT_GUIDANCE['resolution']]
    assert [f.text for f in frames if isinstance(f, (LLMTextFrame, TTSSpeakFrame))] == [
        agents.COMPLAINT_GUIDANCE['resolution']
    ]
    assert call.state.agent == 'complaints' and not call.state.writes


async def complaint_approval_ends_with_its_intent():
    """Abandonment and a changed known identity cannot reuse approved wording."""
    for abandon in (True, False):
        call = open_call(PHONE)
        call.commit(agent='complaints')
        call.turn_started('My haircut yesterday was uneven. I want a redo.')
        (await agents.ask_complaint(context(call), 'My haircut yesterday was uneven', 'a redo')).put(call)
        call.turn_started('Yes.')
        assert call.complaint_approved
        if abandon:
            call.commit(agent='verify', pending='complaints')
            await agents.stop_verifying(context(call), 'Never mind the complaint.')
            assert call.state.pending is None
        else:
            call.commit(customer_phone='+34600999888')
        assert not call.complaint_approved
        try:
            await agents.record_complaint(context(call))
        except ModelRetry:
            pass
        else:
            raise AssertionError('abandoned or identity-stale complaint was recorded')


class Probe(PydanticAgentProcessor):
    async def push_frame(self, frame, direction=None):
        pass


async def cancelled_queue_keeps_all_accepted_input(fragmented=False):
    """A phone context queued behind a running turn is flushed by Pipecat's interruption."""
    diary.reset()
    diary.set_clock(datetime(2026, 9, 21, 10, tzinfo=diary.TZ))
    call = open_call('')
    prior = ['Earlier A', 'Earlier B'] if fragmented else []
    call.messages = [ModelRequest(parts=[UserPromptPart(words)]) for words in prior]
    first, queued_words, last = (
        ('+34.', '680 840.', '464.')
        if fragmented
        else ('Sure', "It's +34 680 840 464.", 'Are you still with me?')
    )
    probe = Probe(call, agents.AGENTS)
    manager = TaskManager(loop=asyncio.get_running_loop())
    await probe.setup(
        FrameProcessorSetup(
            clock=SystemClock(),
            task_manager=manager,
            pipeline_worker=None,  # ty: ignore[invalid-argument-type]
        )
    )
    started, cancelled, answered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    prompts = []
    requests = []

    async def stream(messages, info):
        requests.append([p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)])
        prompt = messages[-1].parts[-1].content
        prompts.append(prompt)
        if len(prompts) == 1:
            started.set()
            await asyncio.Event().wait()
        else:
            yield 'I have your number.'
            answered.set()

    @probe.event_handler('on_error')
    async def error_handler(processor, error):
        raise AssertionError(error)

    original_interrupt = probe._start_interruption

    async def interrupted():
        await original_interrupt()
        cancelled.set()

    probe._start_interruption = interrupted  # ty: ignore[invalid-assignment]
    history = LLMContext([{'role': 'user', 'content': first}])
    with ExitStack() as stack:
        for node in agents.AGENTS.values():
            stack.enter_context(node.agent.override(model=FunctionModel(stream_function=stream)))
        try:
            await probe.queue_frame(StartFrame())
            await probe.queue_frame(LLMContextFrame(history))
            await asyncio.wait_for(started.wait(), 3)
            history.add_message({'role': 'user', 'content': queued_words})
            await probe.queue_frame(LLMContextFrame(history))

            async def queued():
                while not probe._FrameProcessor__process_queue.qsize():
                    await asyncio.sleep(0)

            await asyncio.wait_for(queued(), 3)
            await probe.queue_frame(InterruptionFrame())
            await asyncio.wait_for(cancelled.wait(), 3)
            history.add_message({'role': 'user', 'content': last})
            await probe.queue_frame(LLMContextFrame(history))
            await asyncio.wait_for(answered.wait(), 3)
            assert requests[-1] == [*prior, first, f'{queued_words} {last}'], requests
            assert call.ended != 'transport'
        finally:
            await probe.cleanup()


async def selected_time_needs_no_extra_confirmation():
    for words in ('01:00, please.', '1:00 PM', 'one, please.'):
        diary.reset()
        diary.set_clock(datetime(2026, 9, 21, 10, tzinfo=diary.TZ))
        call = open_call(PHONE)
        call.commit(agent='booking', customer_verified='existing', verified_phone=PHONE)
        slots = diary.find_slots(PHONE, 'Friday', 'haircut', diary.now(), offered_to=call.id)['slots']
        slot = next(s for s in slots if s['time'] == '13:00')

        async def stream(messages, info, slot=slot):
            yield {
                0: DeltaToolCall(
                    name='save_booking',
                    json_args=json.dumps(
                        {
                            'action': 'book',
                            'slot_id': slot['slot_id'],
                            'booking_id': '',
                        }
                    ),
                )
            }

        with agents.AGENTS['booking'].agent.override(model=FunctionModel(stream_function=stream)):
            await Probe(call, agents.AGENTS)._turn(words)
        assert call.state.writes == 1 and len(call.requests) == 1, words


def consent_and_history_boundaries():
    call = open_call(PHONE)
    call.turn_started('No, thanks.')
    call.messages = [ModelResponse(parts=[TextPart(content='Would you like a redo?')])]
    assert not agents._can_end(call)
    call.messages = [ModelResponse(parts=[TextPart(content='Anything else?')])]
    assert agents._can_end(call)
    call.note_model_text('Anything else?')
    call.note_heard('')
    assert not agents._can_end(call), 'unheard speech cannot establish permission to hang up'
    call.turn_started('No thanks, but can you move my booking?')
    assert not agents._can_end(call)
    call.commit(agent='complaints')
    assert call.claims_completion('I have recorded your request for a redo.')

    call.messages = [ModelRequest(parts=[UserPromptPart(content='yes')])]
    previous = frozenset(id(p) for m in call.messages for p in m.parts)
    assert not call.delivered('yes', excluding=previous)
    call.messages = [ModelRequest(parts=[UserPromptPart(content='yes')])]
    assert call.delivered('yes', excluding=previous)


def process_provenance_is_immutable():
    from utils import arm

    before = arm.settings()
    original = arm._git

    def changed(*args: str) -> str:
        return 'changed-after-startup'

    try:
        arm._git = changed  # ty: ignore[invalid-assignment]
        after = arm.settings()
        assert before['revision'] == after['revision']
        assert before['source_sha256'] == after['source_sha256']
    finally:
        arm._git = original


async def main():
    consent_and_history_boundaries()
    process_provenance_is_immutable()
    await service_requests_verify_first()
    await declining_verification_abandons_the_service()
    await verified_complaint_uses_exact_approved_wording()
    await complaint_cannot_skip_readback()
    await readback_is_not_discarded_by_handoff()
    await apology_alone_needs_a_followup()
    await verification_speech_must_establish_the_question()
    await approved_complaint_records_once()
    await complaint_readback_uses_caller_evidence()
    await complaint_preamble_cannot_bypass_structured_speech()
    await complaint_approval_ends_with_its_intent()
    await selected_time_needs_no_extra_confirmation()
    await cancelled_queue_keeps_all_accepted_input()
    await cancelled_queue_keeps_all_accepted_input(fragmented=True)
    print('recovery ok')


if __name__ == '__main__':
    asyncio.run(main())
