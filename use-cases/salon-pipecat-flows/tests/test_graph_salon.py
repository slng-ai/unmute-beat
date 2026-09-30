"""Run: uv run python -m tests.test_graph_salon

The salon on declarative Pipecat flows, with no model and no running
pipeline: each node's tools are called the way `FlowManager` calls them,
through the real `Flow` built from `flow.yaml`, so Pipecat's own branch
tables decide every move. The guarantees the harness makes are checked here
too, the two processors between the LLM and TTS get real frames through
Pipecat's `run_test`, and a real `FlowManager` enters every node.
`python -m tests` runs it too.

Asserts, no pytest, no fixtures, same as the rest.
"""

import ast
import asyncio
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from pipecat.flows import NO_RESPONSE, FlowConfig, FlowManager, FlowsFunctionSchema, NodeConfig
from pipecat.flows.types import FlowFunctionHandler
from pipecat.frames.frames import (
    FunctionCallsStartedFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext, LLMSpecificMessage
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.google.llm import GoogleLLMService
from pipecat.tests.utils import run_test

from graph_salon import GraphSalon, speech
from graph_salon.call import TOOL_CALLS_PER_TURN, RefusalError, SalonCall
from graph_salon.flow import FLOW, seeded
from graph_salon.handlers import ARGS, listening, say
from graph_salon.models import PhoneChoice
from graph_salon.writes import UNCLEAR, WRITES
from salon.diary import TZ, Diary

MONDAY = datetime(2026, 9, 21, 8, 0, tzinfo=TZ)
KNOWN = '+34600111222'


HERE = Path(__file__).resolve().parents[1]
"""The use case's folder."""


def tools(node: NodeConfig) -> list[FlowsFunctionSchema]:
    """A node's tools. `Flow` builds every one as a `FlowsFunctionSchema`."""
    functions = node.get('functions', [])
    assert all(isinstance(f, FlowsFunctionSchema) for f in functions), node['name']
    return cast(list[FlowsFunctionSchema], functions)


class Line:
    """One call under test: the node it is on, and the stand-in flow manager its tools are handed.

    The stand-in has what the handlers read from a real `FlowManager`: the
    state the placeholders and the `say` action come from, the current node,
    and a real Pipecat `LLMContext`.
    """

    def __init__(self, caller: str = '') -> None:
        self.diary = Diary(clock=MONDAY)
        self.call = SalonCall(self.diary, 'call-1', caller)
        self.context = LLMContext()
        self.node: NodeConfig = FLOW.initial_node
        self.manager = SimpleNamespace(
            state=seeded(self.call),
            current_node=self.node['name'],
            get_current_context=self.context.get_messages,
        )

    def say(self, text: str) -> None:
        self.context.add_message({'role': 'user', 'content': text})

    def use(self, name: str, *, follow: bool = True, **args):
        """Call a tool on the current node, and follow the node Pipecat's branch table returns.

        The call is recorded first the way Gemini's service records it: as an
        `LLMSpecificMessage`, not a dict. The first live call crashed on
        exactly that (Langfuse trace 56e1b598). `follow=False` is a second
        call in the same reply: Pipecat moves only after all of them finish.
        """
        call = {'role': 'model', 'parts': [{'function_call': {'name': name, 'args': args}}]}
        self.context.add_message(LLMSpecificMessage(llm='google', message=call))
        function = next(f for f in tools(self.node) if f.name == name)
        handler = cast(FlowFunctionHandler, function.handler)
        result, after = asyncio.run(handler(args, cast(FlowManager, self.manager)))
        self.answered = after is not NO_RESPONSE
        if follow and after is not None and after is not NO_RESPONSE:
            self.node = after
            self.manager.current_node = after['name']
        return result

    @property
    def at(self) -> str:
        return self.node['name']

    def said(self) -> str:
        """What the node's `say` action speaks on entry: `state['say']`, as the action reads it."""
        speaks = any(action['type'] == 'say' for action in self.node.get('pre_actions', []))
        return self.manager.state['say'] if speaks else ''


def verified(where: str = 'booking') -> Line:
    """A caller identified the usual way: asked for a number by code, read back by code, a yes next turn."""
    line = Line()
    line.say(f'I want the {where}, please')
    line.use('hand_over', to=where)
    line.say('+34 600 111 222')
    line.use('ask_number', phone=KNOWN)
    line.say('yes')
    line.use('answer', reply='yes')
    assert line.at == where and line.call.verified
    return line


def proposed_booking() -> tuple[Line, str]:
    line = verified()
    line.say('a haircut on Tuesday')
    slots = line.use('read_diary', day='Tuesday', service='haircut')['slots']
    line.use('ask_booking', action='book', slot_id=slots[0]['slot_id'])
    return line, slots[0]['slot_id']


def counted(diary: Diary) -> list[int]:
    """Count the diary's reads from here on."""
    reads: list[int] = []
    find_slots = diary.find_slots

    def counting(*args, **kwargs):
        reads.append(1)
        return find_slots(*args, **kwargs)

    diary.find_slots = counting  # ty: ignore[invalid-assignment]
    return reads


def an_unidentified_booking_goes_through_verify_and_resumes():
    line = Line()
    assert line.at == 'concierge_listen' and line.said() == speech.GREETING
    line.say('I want to move my appointment')
    line.use('hand_over', to='booking')
    assert line.at == 'verify_listen' and line.said() == speech.ASK_NUMBER, 'code asks, no model request'
    assert line.call.state.resume == 'booking'
    line.say('+34 600 111 222')
    line.use('ask_number', phone=KNOWN)
    assert line.at == 'confirm' and line.node['respond_immediately'] is False
    assert line.said() == 'Let me read that back to you: +34 600 111 222. Have I got that right?'
    assert line.manager.state['question'] == line.said()
    line.say('yes')
    result = line.use('answer', reply='yes')
    assert result['customer_status'] == 'existing'
    assert line.at == 'booking' and line.node['respond_immediately'] is True, 'resumed, no routing'
    assert line.call.state.resume is None


def a_booking_asked_of_the_concierge_is_read_by_code_after_the_lookup():
    line = Line()
    reads = counted(line.diary)
    line.say('I would like a haircut on Friday')
    line.use('read_diary', day='Friday', service='haircut')
    assert line.at == 'verify_listen' and line.said() == speech.ASK_NUMBER
    assert reads == [], 'nothing is read for nobody'
    assert line.call.state.pending_read is not None
    line.say('+34 600 111 222')
    line.use('ask_number', phone=KNOWN)
    line.say('yes')
    result = line.use('answer', reply='yes')
    assert result['diary']['status'] == 'ok' and result['diary']['slots'], result
    assert reads == [1], 'code ran the read it was asked for, once'
    assert line.at == 'booking' and line.call.state.pending_read is None


def a_verified_concierge_read_goes_straight_to_booking():
    line = verified()
    line.use('hand_over', to='concierge')
    assert line.at == 'concierge'
    line.say('and what is free on Friday?')
    result = line.use('read_diary', day='Friday', service='haircut')
    assert result['slots'] and line.at == 'booking'


def a_carrier_number_is_read_back_by_code():
    line = Line(caller=KNOWN)
    line.say('I need to cancel my appointment')
    line.use('hand_over', to='booking')
    assert line.at == 'confirm', 'no model request to ask for a number the carrier gave'
    assert '+34 600 111 222' in line.said()
    assert not line.call.verified, 'never trusted without a yes'


def a_write_needs_a_proposal():
    line = verified()
    try:
        WRITES['booking'].run(line.call)
    except RefusalError:
        pass
    else:
        raise AssertionError('a booking with nothing asked must be refused')
    assert line.diary.bookings_for(KNOWN, MONDAY.date()) == []


def a_yes_in_the_same_turn_writes_nothing_and_a_later_yes_writes_once():
    line, slot_id = proposed_booking()
    result = line.use('answer', reply='yes')
    assert 'not answered' in result['error'], result
    assert line.at == 'booking', 'back to the step that asked'
    assert line.diary.bookings_for(KNOWN, MONDAY.date()) == []

    line.use('ask_booking', action='book', slot_id=slot_id)
    line.say('yes please')
    receipt = line.use('answer', reply='yes')
    assert receipt['status'] == 'booked'
    assert line.at == 'booking_listen' and line.node['respond_immediately'] is False, 'no model follow-up'
    assert line.said().startswith("You're all set for"), line.said()
    assert line.call.state.appointment is not None

    line.node = FLOW.node('confirm')
    """A reply asked on `confirm` whose `answer` runs after the move to `booking_listen`, as in trace 8b6ea51b."""
    line.say("Let's book this.")
    again = line.use('answer', reply='yes')
    assert 'No question is waiting' in again['error'], again
    assert not line.answered and line.manager.current_node == 'booking_listen', (
        'a second reading of the same yes stays put and runs no model: trace 8b6ea51b said the receipt twice'
    )
    assert line.said().startswith("You're all set for"), 'and leaves the receipt line to be spoken'

    try:
        WRITES['booking'].run(line.call)
    except RefusalError:
        pass
    else:
        raise AssertionError('the proposal is spent')
    assert len(line.diary.bookings_for(KNOWN, MONDAY.date())) == 1


def a_yes_from_somebody_else_is_refused():
    line, _ = proposed_booking()
    line.call.commit(phone='+34611111111')
    line.say('yes')
    result = line.use('answer', reply='yes')
    assert 'different caller' in result['error'], result
    assert line.diary.bookings_for(KNOWN, MONDAY.date()) == []


def a_no_takes_the_question_off_the_table():
    line, _ = proposed_booking()
    line.say('no, actually Wednesday')
    assert line.use('answer', reply='change')['reply'] == 'change'
    assert line.call.state.proposal is None
    assert line.at == 'booking' and line.node['respond_immediately'] is True


def a_malformed_receipt_changes_nothing():
    line, _ = proposed_booking()
    line.diary.save_booking = lambda *args, **kwargs: {'status': 'booked'}  # ty: ignore[invalid-assignment]
    line.say('yes')
    result = line.use('answer', reply='yes')
    assert result['error'] == UNCLEAR
    assert line.call.state.appointment is None


def a_complaint_is_recorded_after_its_read_back():
    line = verified('complaints')
    line.say('my colour came out patchy and I want a refund')
    line.use('ask_complaint', summary='my colour came out patchy', requested_resolution='a refund')
    assert line.said().startswith('Shall I record your complaint in your words: my colour came out patchy.')
    line.say('yes')
    receipt = line.use('answer', reply='yes')
    assert receipt['recorded'] is True
    assert line.at == 'complaints_listen'
    assert line.said().startswith(f"That's on record, reference {receipt['reference']}.")
    assert speech.MANAGER_CALLS in line.said()
    assert len(line.diary.complaints()) == 1


def the_turn_budget_ends_with_a_fixed_line():
    line = verified()
    line.say('what is free on Tuesday')
    for _ in range(TOOL_CALLS_PER_TURN):
        assert 'slots' in line.use('read_diary', day='Tuesday', service='any')
        assert line.at == 'booking', 'a read stays'
    line.use('read_diary', day='Tuesday', service='any')
    assert line.at == 'booking_listen' and line.said() == speech.BUDGET
    assert line.node['respond_immediately'] is False


def a_fault_is_a_fixed_line_not_an_error_for_the_model():
    line = verified()
    line.say('what is free on Tuesday')

    def broken(*args, **kwargs):
        raise RuntimeError('the diary is down')

    line.diary.find_slots = broken  # ty: ignore[invalid-assignment]
    assert line.use('read_diary', day='Tuesday', service='any')['error'] == 'internal fault'
    assert line.at == 'booking_listen' and line.said() == speech.FAULT
    assert line.node['respond_immediately'] is False, 'the model is not asked to explain it'


def one_action_per_reply():
    line = Line()
    line.say('book me in')
    line.use('hand_over', to='booking')
    line.say('+34 600 111 222')
    line.use('ask_number', follow=False, phone=KNOWN)
    second = line.use('ask_number', follow=False, phone='+34611111111')
    assert 'one action per reply' in second['error'], second
    proposal = line.call.state.proposal
    assert (
        proposal is not None and isinstance(proposal.choice, PhoneChoice) and proposal.choice.phone == KNOWN
    )


def arguments_are_validated_before_anything_runs():
    line = Line()
    line.say('book me in')
    line.use('hand_over', to='booking')
    result = line.use('ask_number', phone='+447700900123')
    assert 'error' in result and line.call.state.proposal is None, 'only Spanish numbers'
    assert line.at == 'verify_listen', 'a refusal stays, for the model to answer'


def the_claim_filter_drops_an_unbacked_claim():
    line = verified()
    claims = speech.ClaimFilter(line.call)
    assert not claims.passes("I've booked you in for Tuesday. ")
    assert not claims.passes("That's recorded, ")
    assert claims.passes("You're booked for Friday. "), 'recounting what they hold is not a claim'
    line.call.commit(receipt_turn=line.call.state.turn)
    assert claims.passes("I've booked you in for Tuesday. "), 'a receipt this turn backs it'
    assert not claims.passes("You're all set for Saturday at 9:30 AM. "), 'a copy never passes'

    claims = speech.ClaimFilter(verified().call)
    reply = [LLMTextFrame("I've booked you in. "), LLMTextFrame('Anything else?')]
    down, _ = asyncio.run(
        run_test(claims, frames_to_send=[LLMFullResponseStartFrame(), *reply, LLMFullResponseEndFrame()])
    )
    assert ''.join(f.text for f in down if isinstance(f, LLMTextFrame)) == 'Anything else?'


def a_copy_of_a_code_line_never_reaches_the_caller():
    """Trace 2b75413c: the model spoke its own read-back and receipt, and nothing was booked."""

    def through(*texts: str) -> tuple[str, list[str]]:
        claims = speech.ClaimFilter(verified().call)
        frames = [LLMFullResponseStartFrame(), *(LLMTextFrame(t) for t in texts), LLMFullResponseEndFrame()]
        down, _ = asyncio.run(run_test(claims, frames_to_send=frames))
        spoken = ''.join(f.text for f in down if isinstance(f, LLMTextFrame))
        return spoken, [f.text for f in down if isinstance(f, TTSSpeakFrame)]

    spoken, lines = through(
        "You're all set for Saturday at 9:30 AM. ", 'Is there anything else I can help with?'
    )
    assert spoken == '' and lines == [], 'all of it goes, and the empty reply is the guard next in line'
    spoken, _ = through("You're all moved to Thursday at 2:30 PM. ", 'Is there anything else?')
    assert spoken == '', 'a reworded receipt is a copy too: trace 8b6ea51b'
    spoken, lines = through(
        'No problem, I can switch that. ',
        'Shall I book you in for a haircut, ',
        'tomorrow at 9:30 AM instead?',
    )
    assert spoken == 'No problem, I can switch that. ', (
        'nothing after the copy, not even the rest of its sentence'
    )
    assert lines == [speech.MISSED], 'and the caller is asked again'


def every_code_line_is_one_the_filter_knows():
    lines = [
        speech.number_question(KNOWN),
        speech.booking_question('book', 'haircut', 'Friday at 9:00 AM'),
        speech.booking_question('move', 'haircut', 'Friday at 9:00 AM'),
        speech.booking_question('cancel', 'haircut', 'Friday at 9:00 AM'),
        speech.complaint_question('it came out patchy', 'a refund'),
        speech.acknowledgement('CX0001', ''),
        *(template.format(spoken='Friday at 9:00 AM') for template in speech.SAVED.values()),
    ]
    for text in lines:
        assert speech.SCRIPTED.match(text), f'the filter would let a copy of {text!r} through'


def the_filter_comes_before_the_empty_reply_guard():
    salon = GraphSalon(Diary(), 'call-1', GoogleLLMService(api_key='x'))
    assert [type(p) for p in salon.processors] == [speech.ClaimFilter, speech.EmptyReplyGuard]


def an_empty_reply_is_run_again_once_then_answered():
    reruns: list[int] = []

    async def rerun() -> None:
        reruns.append(1)

    guard = speech.EmptyReplyGuard(rerun)
    assert [guard.verdict(False), guard.verdict(False), guard.verdict(True), guard.verdict(False)] == [
        'rerun',
        'apologise',
        'ok',
        'rerun',
    ]
    assert speech.EmptyReplyGuard().verdict(False) == 'apologise', 'with nothing to rerun, never silence'

    empty = [LLMFullResponseStartFrame(), LLMFullResponseEndFrame()]
    down, _ = asyncio.run(run_test(speech.EmptyReplyGuard(rerun), frames_to_send=[*empty, *empty]))
    assert reruns == [1], 'the empty turn of trace a9b5ea5c is run again, once'
    assert any(isinstance(f, TTSSpeakFrame) and f.text == speech.MISSED for f in down), (
        'a second one is answered'
    )

    calls = [
        LLMFullResponseStartFrame(),
        FunctionCallsStartedFrame(function_calls=[]),
        LLMFullResponseEndFrame(),
    ]
    reruns.clear()
    asyncio.run(run_test(speech.EmptyReplyGuard(rerun), frames_to_send=calls))
    assert reruns == [], 'a turn that called a tool is not empty'

    down, _ = asyncio.run(
        run_test(speech.EmptyReplyGuard(rerun), frames_to_send=[UserStartedSpeakingFrame(), *empty])
    )
    assert reruns == [], (
        'a reply cut short by the caller is not rerun, their turn brings the next one: trace 8b6ea51b'
    )
    assert not any(isinstance(f, TTSSpeakFrame) for f in down), 'nor apologised for'
    stopped = [UserStartedSpeakingFrame(), UserStoppedSpeakingFrame(), *empty]
    asyncio.run(run_test(speech.EmptyReplyGuard(rerun), frames_to_send=stopped))
    assert reruns == [1], 'once they stop, an empty reply is'


def the_say_action_speaks_state_and_waits_for_nothing():
    queued: list = []

    async def queue_frame(frame) -> None:
        queued.append(frame)

    manager = SimpleNamespace(state={'say': 'Hello there.'}, worker=SimpleNamespace(queue_frame=queue_frame))
    asyncio.run(say({'type': 'say'}, cast(FlowManager, manager)))
    assert [(type(f), f.text) for f in queued] == [(TTSSpeakFrame, 'Hello there.')], 'no ActionFinishedFrame'
    manager.state['say'] = ''
    asyncio.run(say({'type': 'say'}, cast(FlowManager, manager)))
    assert len(queued) == 1, 'nothing to say, nothing queued'


def no_node_waits_on_a_frame_a_barge_in_drops():
    """Trace 2b75413c: a caller cut into a `tts_say` read-back and the move to `confirm` never finished.

    The worker here never runs, so no frame ever reaches the end of the
    pipeline: exactly what a barge-in does to `tts_say`'s `ActionFinishedFrame`.
    Every node must still be entered, start to finish.
    """
    for name in FLOW.config.nodes:
        actions = [action['type'] for action in FLOW.node(name).get('pre_actions', [])]
        assert 'tts_say' not in actions, f'{name} speaks with tts_say'

    async def enter_all() -> list[str]:
        salon = GraphSalon(Diary(), 'call-1', GoogleLLMService(api_key='x'), caller=KNOWN)
        pair = LLMContextAggregatorPair(salon.context)
        salon.attach(PipelineWorker(Pipeline([pair.user(), salon.llm, pair.assistant()])), pair)
        await asyncio.wait_for(salon.start(), 2)
        manager = salon._manager
        assert manager is not None
        entered = [manager.current_node or '']
        for name in FLOW.config.nodes:
            await asyncio.wait_for(manager.set_node_from_config(FLOW.node(name)), 2)
            entered.append(manager.current_node or '')
        return entered

    entered = asyncio.run(enter_all())
    assert entered == ['concierge_listen', *FLOW.config.nodes], entered


def every_tool_has_its_pydantic_schema():
    for name in FLOW.config.nodes:
        for function in tools(FLOW.node(name)):
            schema = ARGS[function.name].model_json_schema()
            assert set(function.properties) == set(schema.get('properties', {})), function.name
            assert all('title' not in spec for spec in function.properties.values()), function.name
    answer = tools(FLOW.node('confirm'))[0]
    assert answer.properties['reply']['enum'] == ['yes', 'no', 'change', 'other'], (
        'Pipecat alone sends no enum'
    )


def every_placeholder_is_in_the_seeded_state():
    keys = set(seeded(Line().call))
    assert 'say' in keys, 'the say action reads it'
    for name in FLOW.config.nodes:
        node = FLOW.node(name)
        texts = [node.get('role_message', ''), *(m['content'] for m in node['task_messages'])]
        for text in texts:
            used = set(re.findall(r'{{\s*([\w.]+)\s*}}', text))
            assert used <= keys, f'{name} reads {used - keys}, which the state never holds'


def every_tool_can_speak_a_fixed_line_from_its_node():
    for name, node in FLOW.config.nodes.items():
        for function in node.functions:
            target = function.transition_to
            cases = target.cases if isinstance(target, FlowConfig.Branch) else {}
            assert listening(name) in cases, f'{name}.{function.name} cannot route a fault or budget line'


def every_node_offers_at_most_one_write():
    for name in FLOW.config.nodes:
        names = [f.name for f in tools(FLOW.node(name))]
        assert len([n for n in names if n.startswith('ask_') or n == 'answer']) <= 1, names


def it_stands_on_pipecat_not_on_the_harness():
    for path in [*HERE.glob('graph_salon/*.py'), *HERE.glob('salon/**/*.py'), HERE / 'bot.py']:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split('.')[0] != 'core', f'{path.name} imports {node.module}'
                assert node.module != 'salon.baseline', f'{path.name} imports the baseline'


def main():
    an_unidentified_booking_goes_through_verify_and_resumes()
    a_booking_asked_of_the_concierge_is_read_by_code_after_the_lookup()
    a_verified_concierge_read_goes_straight_to_booking()
    a_carrier_number_is_read_back_by_code()
    a_write_needs_a_proposal()
    a_yes_in_the_same_turn_writes_nothing_and_a_later_yes_writes_once()
    a_yes_from_somebody_else_is_refused()
    a_no_takes_the_question_off_the_table()
    a_malformed_receipt_changes_nothing()
    a_complaint_is_recorded_after_its_read_back()
    the_turn_budget_ends_with_a_fixed_line()
    a_fault_is_a_fixed_line_not_an_error_for_the_model()
    one_action_per_reply()
    arguments_are_validated_before_anything_runs()
    the_claim_filter_drops_an_unbacked_claim()
    a_copy_of_a_code_line_never_reaches_the_caller()
    every_code_line_is_one_the_filter_knows()
    the_filter_comes_before_the_empty_reply_guard()
    an_empty_reply_is_run_again_once_then_answered()
    the_say_action_speaks_state_and_waits_for_nothing()
    no_node_waits_on_a_frame_a_barge_in_drops()
    every_tool_has_its_pydantic_schema()
    every_placeholder_is_in_the_seeded_state()
    every_tool_can_speak_a_fixed_line_from_its_node()
    every_node_offers_at_most_one_write()
    it_stands_on_pipecat_not_on_the_harness()
    print('graph salon ok')


if __name__ == '__main__':
    main()
