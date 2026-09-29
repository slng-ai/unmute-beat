"""Run: uv run python -m tests.test_context

What each step's agent is sent (`context:`), a capability loaded on demand
and kept, and a consultation that comes back (`consults:`), on the clinic
(`clinic_flow/consult.yaml`). Asserts, no pytest.

* `history: messages` sends earlier turns as what was said and heard, no
  tool exchanges; `facts: [..]` shows only those fields; `context: none`
  sends nothing from before this turn;
* a loaded capability survives the filter, so a follow-up does not load it
  again, and it costs one request;
* a consultation answers in the specialist's words, comes back with no
  second request, and puts the waiting read-back again, fresh: a yes after
  it books once, nothing said to the specialist books anything;
* a consultation from a prerequisite step keeps the step waiting on it.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

import core
from tests.clinic_flow.clinic import Backend
from tests.clinic_flow.consult import REFUND_POLICY, CheckedIn, DeskState

HERE = Path(__file__).parent / 'clinic_flow'


class Script:
    """Each request takes the next move; every request's messages and instructions are kept to be read."""

    def __init__(self) -> None:
        self.moves: list[tuple[Any, ...]] = []
        self.seen: list[tuple[list[Any], str, set[str]]] = []
        self.model = FunctionModel(stream_function=self.agent)

    async def agent(self, messages: Any, info: AgentInfo):
        tools = {t.name for t in [*info.function_tools, *info.output_tools]}
        self.seen.append((list(messages), info.instructions or '', tools))
        if not self.moves:
            yield 'Sorry, could you say that again?'
            return
        kind, *rest = self.moves.pop(0)
        if kind == 'collect':
            yield {0: DeltaToolCall(name='collect', json_args=json.dumps({'values': rest[0]}))}
        elif kind == 'call':
            yield {0: DeltaToolCall(name=rest[0], json_args=json.dumps(rest[1]))}
        elif kind == 'answer':
            yield rest[0]
            yield {1: DeltaToolCall(name='done', json_args='{}')}
        else:
            yield rest[0]


def opened(script: Script, state: DeskState) -> tuple[core.StepFlow, core.FlowCall]:
    flow = core.load(HERE / 'consult.yaml').compile(model=script.model)
    return flow, flow.open(state, services=Backend())


async def turn(flow, call, script: Script, words: str, *moves: tuple[Any, ...]) -> list[str]:
    script.moves = list(moves)
    reply = await core.text_turn(flow, call, words)
    call.acknowledge(*(segment.id for segment in reply.segments))
    return [segment.text for segment in reply.segments]


def checked_in() -> DeskState:
    return DeskState(patient='P-1234', checked_in=CheckedIn(patient='P-1234'))


def tool_calls(messages: list[Any]) -> list[str]:
    return [
        p.tool_name
        for m in messages
        if isinstance(m, ModelResponse)
        for p in m.parts
        if isinstance(p, ToolCallPart)
    ]


def texts(messages: list[Any]) -> list[str]:
    return [str(p.content) for m in messages for p in m.parts if isinstance(p, UserPromptPart | TextPart)]


async def messages_and_facts_are_what_the_step_may_see():
    script = Script()
    flow, call = opened(script, checked_in())
    await turn(
        flow,
        call,
        script,
        'Also, I use a wheelchair.',
        ('collect', {'note': 'uses a wheelchair'}),
        ('say', 'Noted.'),
    )
    await turn(flow, call, script, 'When are you open?', ('say', 'Weekdays, nine to five.'))
    messages, instructions, _ = script.seen[-1]
    assert 'collect' not in tool_calls(messages), 'an earlier tool exchange reached a messages-only step'
    said = texts(messages)
    assert 'Also, I use a wheelchair.' in said and 'Noted.' in said, said
    facts = next(line for line in instructions.splitlines() if line.startswith('Facts:'))
    assert '"checked_in"' in facts and '"slot"' in facts and 'wheelchair' not in facts, facts
    assert len(script.seen) == 3, 'one request for an ordinary local answer'


async def a_loaded_capability_is_not_loaded_again():
    script = Script()
    flow, call = opened(script, checked_in())
    await turn(
        flow,
        call,
        script,
        'Do I get my money back if I cancel?',
        ('call', 'load_capability', {'id': 'refunds'}),
        ('say', 'In full up to a day before, half after that.'),
    )
    assert len(script.seen) == 2, 'load, then answer: two requests'
    await turn(flow, call, script, 'And on the day itself?', ('say', 'Half.'))
    messages, instructions, _ = script.seen[-1]
    assert len(script.seen) == 3, 'the follow-up is one request'
    assert tool_calls(messages).count('load_capability') == 1, 'the load record was kept, once'
    assert (
        REFUND_POLICY in instructions
        or any(REFUND_POLICY in t for t in texts(messages))
        or any(
            REFUND_POLICY in str(p.content)
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
            if getattr(p, 'tool_name', '') == 'load_capability'
        )
    ), 'the policy stays in reach after the filter'


async def a_consultation_comes_back_to_a_fresh_read_back():
    script = Script()
    flow, call = opened(script, checked_in())
    said = await turn(
        flow, call, script, 'Tuesday at ten, please.', ('collect', {'slot': '1000'}), ('call', 'book', {})
    )
    readback = 'Shall I book Tuesday at 10:00 AM for P-1234?'
    assert said[-1] == readback, said
    before = len(script.seen)
    said = await turn(
        flow,
        call,
        script,
        'Wait, can I eat before the appointment?',
        ('call', 'hand_over', {'to': 'nurse', 'because': 'a medical question'}),
        ('answer', 'Yes, eat as you normally would.'),
    )
    assert len(script.seen) - before == 2, 'the parent hands over, the nurse answers: two requests'
    assert said == ['Yes, eat as you normally would.', readback], said
    assert call.state.agent == 'desk' and call.state.consulting is None
    assert call.services.calls == 0, 'nothing was booked during the detour'
    nurse_messages, nurse_instructions, nurse_tools = script.seen[-1]
    assert 'done' in nurse_tools and 'Facts:' not in nurse_instructions, 'context: none'
    assert 'Tuesday at ten, please.' not in texts(nurse_messages), 'the nurse saw an earlier turn'
    said = await turn(flow, call, script, 'Yes, book it.', ('call', 'book', {}))
    assert call.services.bookings == {'CL001': ('P-1234', '1000')}, (
        'the yes after the fresh copy booked, once'
    )


async def a_consultation_that_asks_first_still_comes_back_to_the_read_back():
    script = Script()
    flow, call = opened(script, checked_in())
    await turn(flow, call, script, 'Tuesday at ten.', ('collect', {'slot': '1000'}), ('call', 'book', {}))
    await turn(
        flow,
        call,
        script,
        'Is it OK to eat before?',
        ('call', 'hand_over', {'to': 'nurse', 'because': 'a medical question'}),
        ('say', 'What time is your appointment?'),
    )
    assert call.state.agent == 'nurse', 'the nurse asked first, and the caller stays with the nurse'
    said = await turn(flow, call, script, 'Ten in the morning.', ('answer', 'Then eat a light breakfast.'))
    assert said == ['Then eat a light breakfast.', 'Shall I book Tuesday at 10:00 AM for P-1234?'], said
    await turn(flow, call, script, 'Yes.', ('call', 'book', {}))
    assert call.services.bookings == {'CL001': ('P-1234', '1000')}


async def a_consultation_keeps_the_prerequisite_waiting():
    script = Script()
    flow, call = opened(script, DeskState())
    call.commit(agent='front', pending='desk')
    await turn(
        flow,
        call,
        script,
        'Before I say my number: is the clinic step-free?',
        ('call', 'hand_over', {'to': 'nurse', 'because': 'access'}),
        ('answer', 'Yes, there is a ramp.'),
    )
    assert (call.state.agent, call.state.pending, call.state.consulting) == ('front', 'desk', None)
    await turn(
        flow,
        call,
        script,
        'It is P-1234.',
        ('collect', {'patient': 'P-1234'}),
        ('call', 'check_in', {}),
        ('say', 'Which time suits you?'),
    )
    assert call.state.agent == 'desk' and call.state.pending is None, (call.state.agent, call.state.pending)
    assert call.state.facts.checked_in == CheckedIn(patient='P-1234'), 'assigned whole, by its type'


def the_loader_refuses_a_consultation_chain():
    text = (
        (HERE / 'consult.yaml')
        .read_text()
        .replace(
            '    context: none\n', "    context: none\n    consults:\n      - {to: front, purpose: 'x'}\n"
        )
    )
    problems = ' | '.join(core.flowspec.load(text, root=HERE).problems())
    assert 'one consultation at a time' in problems, problems


if __name__ == '__main__':
    asyncio.run(messages_and_facts_are_what_the_step_may_see())
    asyncio.run(a_loaded_capability_is_not_loaded_again())
    asyncio.run(a_consultation_comes_back_to_a_fresh_read_back())
    asyncio.run(a_consultation_that_asks_first_still_comes_back_to_the_read_back())
    asyncio.run(a_consultation_keeps_the_prerequisite_waiting())
    the_loader_refuses_a_consultation_chain()
    print('context ok: history and facts per step, a capability kept, consultations that come back')
