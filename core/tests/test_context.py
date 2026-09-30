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
import os
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

import core
from core.history import History, Payload
from tests.clinic_flow.clinic import Backend
from tests.clinic_flow.consult import REFUND_POLICY, CheckedIn, DeskState
from tests.test_public import CLOCK

HERE = Path(__file__).parent / 'clinic_flow'
GOLDEN = Path(__file__).parent / 'golden' / 'context.json'


class Script:
    """Each request takes the next move; every request's messages and instructions are kept to be read."""

    def __init__(self) -> None:
        self.moves: list[tuple[Any, ...]] = []
        self.seen: list[tuple[list[Any], str, set[str]]] = []
        self.payloads: list[Payload] = []
        """Every request whole: instructions, history, tool and output schemas (`core.history.Payload`)."""
        self.model = FunctionModel(stream_function=self.agent)

    async def agent(self, messages: Any, info: AgentInfo):
        tools = {t.name for t in [*info.function_tools, *info.output_tools]}
        self.seen.append((list(messages), info.instructions or '', tools))
        self.payloads.append(Payload.of(messages, info))
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


def opened(
    script: Script, state: DeskState, manifest: str = 'consult.yaml', flow: core.StepFlow | None = None
) -> tuple[core.StepFlow, core.FlowCall]:
    flow = flow or core.load(HERE / manifest).compile(model=script.model)
    return flow, flow.open(state, services=Backend(), clock=lambda: CLOCK)


def refused(text: str) -> str:
    """Every problem the loader finds in a manifest written here, as one string ('' when it compiles)."""
    try:
        return ' | '.join(core.flowspec.load(text, root=HERE).problems())
    except (core.FlowSpecError, ValueError) as error:
        return str(error)


def variant(manifest: str, old: str, new: str) -> str:
    text = (HERE / manifest).read_text()
    assert old in text, old
    return text.replace(old, new)


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


def retries(payload: Payload) -> list[str]:
    """The retry texts a request carries: what a refused tool told the model."""
    return [
        str(part.get('content'))
        for message in json.loads(payload.history)
        for part in message['parts']
        if part.get('part_kind') == 'retry-prompt'
    ]


def v1_node_context_replaces_the_flows_whole():
    """v1 unchanged: a step's `{facts: [..]}` under a flow's `history: messages` has history `all`, as before."""
    text = variant(
        'consult.yaml',
        'context: {history: messages, facts: [checked_in, slot]}',
        'context: {facts: [checked_in, slot]}',
    )
    script = Script()
    flow = core.flowspec.load(text, root=HERE).compile(model=script.model)
    assert flow.nodes['desk'].history == 'all' and flow.nodes['desk'].scope is None, flow.nodes[
        'desk'
    ].history
    assert flow.nodes['front'].history == 'messages', 'the flow context still reaches a step with none'
    flow, call = opened(script, checked_in(), flow=flow)
    asyncio.run(
        turn(
            flow, call, script, 'I use a wheelchair.', ('collect', {'note': 'wheelchair'}), ('say', 'Noted.')
        )
    )
    asyncio.run(turn(flow, call, script, 'When are you open?', ('say', 'Weekdays.')))
    assert 'collect' in tool_calls(script.seen[-1][0]), 'history all: the earlier tool exchange is sent'


def scoped_members_replace_one_by_one():
    """Scoped: the flow's context is the default, and each member a step sets replaces it, as YAML-CONTRACT says."""
    script = Script()
    nodes = core.load(HERE / 'scoped.yaml').compile(model=script.model).nodes
    root = History(scope='node', turns=1)
    assert (nodes['desk'].history, nodes['desk'].scope.facts) == (root, ('slot',)), 'absent members inherit'
    assert (nodes['front'].history, nodes['front'].scope.facts) == (root, ('patient',)), (
        'facts replace, not add'
    )
    assert nodes['nurse'].scope.facts == () and nodes['nurse'].history == root
    text = variant('scoped.yaml', '    context: {facts: none}\n', '    context: none\n')
    nodes = core.flowspec.load(text, root=HERE).compile(model=script.model).nodes
    assert (nodes['lobby'].history, nodes['lobby'].scope.facts) == ('none', ()), 'context: none'
    text = variant('scoped.yaml', '    context: {facts: [patient]}', '    context: {history: messages}')
    nodes = core.flowspec.load(text, root=HERE).compile(model=script.model).nodes
    assert (nodes['front'].history, nodes['front'].scope.facts) == ('messages', ('slot',)), (
        'a history string replaces the object as a unit, and the step is still scoped'
    )
    text = variant(
        'consult.yaml',
        'context: {history: messages, facts: [checked_in, slot]}',
        'context: {history: {scope: conversation}}',
    )
    nodes = core.flowspec.load(text, root=HERE).compile(model=script.model).nodes
    assert nodes['desk'].scope is not None and nodes['desk'].scope.facts is None, 'the root v1 facts, all'
    assert nodes['front'].scope is None and nodes['nurse'].scope is None, (
        'opting one step in leaves the rest v1'
    )


def invalid_context_is_refused_at_load():
    """Selectors, history objects and templates that the scoped contract does not allow fail before any call."""
    for selector in ('checked_in.patient', 'notes[0]', '*'):
        problems = refused(variant('scoped.yaml', 'facts: [patient]', f"facts: ['{selector}']"))
        assert 'facts are whole top-level fields' in problems, (selector, problems)
    for history, expected in (
        ('{scope: persona}', "'node' or 'conversation'"),
        ('{scope: node, turns: 0}', 'greater than 0'),
        ("{scope: node, turns: '8'}", 'valid integer'),
        ('{scope: node, window: 3}', 'Extra inputs'),
    ):
        problems = refused(variant('scoped.yaml', 'history: {scope: node, turns: 1}', f'history: {history}'))
        assert expected in problems, (history, problems)
    reaches = 'prompt: prompts/front.md\n    fields: [patient]'
    for prompt, facts, fails in (
        ('slot.md', '[patient]', False),
        ('slot.md', '[slot]', True),
        ('call.md', '[patient]', True),
    ):
        text = variant('scoped.yaml', reaches, f'prompt: prompts/{prompt}\n    fields: [patient]')
        text = text.replace('context: {facts: [patient]}', f'context: {{facts: {facts}}}')
        problems = refused(text)
        assert ("reaches past this step's context" in problems) == fails, (prompt, facts, problems)
    v1 = variant('consult.yaml', 'prompt: prompts/front.md', 'prompt: prompts/call.md')
    assert refused(v1) == '', 'the same template loads in v1, which renders against the whole call'


def automatic_sources_show_only_the_steps_view(manifest: str = 'scoped.yaml'):
    """Canaries in every automatic source: none reaches a step that does not see them; the call keeps them all.

    The caller's patient number P-7777 is bound by `book` from the state and
    shown to no step but `front`; the note is written by `desk` and shown to
    no one else. The raw receipt, the offers, the read-back, the intent's
    arguments and the recap all hold one or the other. In version 2 (`scoped.v2.yaml`)
    `desk` sees the note it may change; in version 1 it may change it unseen.
    """
    script = Script()
    flow, call = opened(
        script,
        DeskState(patient='P-7777', checked_in=CheckedIn(patient='P-7777'), note='CANARY-NOTE'),
        manifest,
    )
    sees_note = manifest == 'scoped.v2.yaml'
    call.commit(agent='desk')
    said = asyncio.run(
        turn(
            flow, call, script, 'Tuesday at ten, please.', ('collect', {'slot': '1000'}), ('call', 'book', {})
        )
    )
    assert said[-1] == 'Shall I book Tuesday at 10:00 AM for P-7777?', said
    desk = script.payloads[-1]
    assert 'The caller is answering: "Shall I book' not in desk.instructions, 'asked after this request'
    assert ('CANARY-NOTE' in desk.instructions) == sees_note and 'P-7777' not in desk.instructions, (
        desk.instructions
    )
    asyncio.run(
        turn(
            flow,
            call,
            script,
            'Wait, can I eat before?',
            ('call', 'hand_over', {'to': 'nurse', 'because': 'a medical question'}),
            ('answer', 'Yes, eat as you normally would.'),
        )
    )
    desk, nurse = script.payloads[-2], script.payloads[-1]
    assert 'The caller is answering: "Shall I book Tuesday at 10:00 AM for P-7777?"' in desk.instructions, (
        'the step that owns the read-back is told it whole'
    )
    for leaked in (
        'P-7777',
        'CANARY-NOTE',
        'Shall I book',
        '"1000"',
        'On offer to the caller now',
        'notes on file',
    ):
        assert leaked not in nurse, (leaked, nurse.pinned())
    assert "Another step's question is waiting" in nurse.instructions, nurse.instructions
    assert 'Wait, can I eat before?' in nurse.history, 'the current words reach the specialist'
    assert 'hand_over' not in nurse.history, (
        "the parent's tool exchange of this caller turn is not the nurse's"
    )
    asyncio.run(turn(flow, call, script, 'Yes, book it.', ('call', 'book', {})))
    asyncio.run(turn(flow, call, script, 'Thanks.', ('say', 'You are welcome.')))
    desk = script.payloads[-1]
    assert 'Done on this call: book {"slot": "1000"} -> succeeded' in desk.instructions, desk.instructions
    assert 'your visit Tuesday at 10:00 AM' in desk.instructions, 'the owner may recap its booking'
    for leaked in ('P-7777', *(() if sees_note else ('CANARY-NOTE',)), 'CL001'):
        assert leaked not in desk.instructions, (leaked, desk.instructions)
    assert 'reference CL001' in desk.history, 'the receipt the caller heard is conversation, sent as said'
    call.services.failing = True
    asyncio.run(
        turn(
            flow,
            call,
            script,
            'And the nine o clock too.',
            ('collect', {'slot': '0900'}),
            ('call', 'book', {}),
        )
    )
    asyncio.run(turn(flow, call, script, 'Yes.', ('call', 'book', {}), ('say', 'Let me check.')))
    assert call.unresolved(), 'the diary did not answer: the outcome is unknown'
    call.services.bookings.pop('CL002')
    """Nothing for reconciliation to find at the next turn's start, so the outcome stays unknown."""
    asyncio.run(
        turn(
            flow,
            call,
            script,
            'Is fasting needed?',
            ('call', 'hand_over', {'to': 'nurse', 'because': 'fasting'}),
            ('answer', 'No.'),
        )
    )
    nurse = script.payloads[-1]
    assert "Outcome unknown: another step's action." in nurse.instructions, nurse.instructions
    for leaked in ('P-7777', '0900', 'your visit', 'Done on this call', 'CANARY-NOTE'):
        assert leaked not in nurse.instructions, (leaked, nurse.instructions)
    assert call.state.facts.note == 'CANARY-NOTE' and call.state.facts.patient == 'P-7777', (
        'hidden, still stored'
    )
    assert call.services.bookings['CL001'] == ('P-7777', '1000'), 'the binding used the identity no step saw'
    assert any(e.receipt.get('patient') == 'P-7777' for e in call.evidence), (
        'the full receipt is kept as evidence'
    )
    for payload in script.payloads:
        assert 'P-7777' not in payload.tools and 'P-7777' not in payload.outputs, (
            'schemas carry no state values'
        )
        assert 'notes on file' not in payload, (
            "a capability only front registers reaches front's requests alone"
        )


def a_refusal_says_only_what_the_step_sees():
    """A service's no goes back to the model as the step's view of the receipt, not the receipt; v1 still dumps it."""
    for manifest, shows in (('consult.yaml', True), ('scoped.yaml', False)):
        script = Script()
        flow, call = opened(
            script, DeskState(patient='P-7777', checked_in=CheckedIn(patient='P-7777')), manifest
        )
        call.commit(agent='desk')
        call.services.full = True
        asyncio.run(turn(flow, call, script, 'Ten.', ('collect', {'slot': '1000'}), ('call', 'book', {})))
        asyncio.run(turn(flow, call, script, 'Yes.', ('call', 'book', {}), ('say', 'That time is taken.')))
        [refusal] = [r for r in retries(script.payloads[-1]) if 'service answered' in r]
        assert ('P-7777' in refusal) == shows, (manifest, refusal)


def words_said_stay_words(manifest: str = 'scoped.yaml'):
    """Scope minimises context, it does not redact: what the caller says, and what they were read, is sent as said."""
    script = Script()
    flow, call = opened(script, DeskState(patient='P-7777', checked_in=CheckedIn(patient='P-7777')), manifest)
    call.commit(agent='desk')
    asyncio.run(
        turn(
            flow,
            call,
            script,
            'Put in a note: CANARY-SAID. And Tuesday at ten.',
            ('collect', {'note': 'CANARY-SAID', 'slot': '1000'}),
            ('call', 'book', {}),
        )
    )
    asyncio.run(turn(flow, call, script, 'How long is it?', ('say', 'Half an hour.')))
    desk = script.payloads[-1]
    assert call.state.facts.note == 'CANARY-SAID', 'the whole mixed request was recorded, the note included'
    assert 'CANARY-SAID' in desk.history, desk.history
    assert ('CANARY-SAID' in desk.instructions) == (manifest == 'scoped.v2.yaml'), (
        'a fact only where it is seen'
    )
    assert 'for P-7777?' in desk.history, 'the read-back the caller heard names the hidden number'


def history_is_the_nodes_own(manifest: str = 'scoped.yaml'):
    """`scope: node` is by step id, across visits; a parent's tool exchange is not the consulted step's."""
    script = Script()
    flow, call = opened(script, DeskState(patient='P-7777', checked_in=CheckedIn(patient='P-7777')), manifest)
    call.commit(agent='desk')
    asyncio.run(turn(flow, call, script, 'Tuesday at ten.', ('collect', {'slot': '1000'}), ('say', 'Noted.')))
    asyncio.run(
        turn(
            flow,
            call,
            script,
            'Is it OK to eat before?',
            ('call', 'hand_over', {'to': 'nurse', 'because': 'food'}),
            ('say', 'What time is your appointment?'),
        )
    )
    asyncio.run(turn(flow, call, script, 'Ten in the morning.', ('answer', 'Then eat a light breakfast.')))
    nurse = script.payloads[-1]
    assert 'Is it OK to eat before?' in nurse.history and 'What time is your appointment?' in nurse.history, (
        'the nurse keeps its own earlier turn'
    )
    assert 'Tuesday at ten.' not in nurse.history and 'hand_over' not in nurse.history, nurse.pinned()[
        'history'
    ]
    asyncio.run(turn(flow, call, script, 'Great, and the price?', ('say', 'It is covered.')))
    everything = History(scope='node')
    turns = {
        step: sorted(everything.window(call, call.messages, step)[0]) for step in ('desk', 'nurse', 'front')
    }
    assert turns == {'desk': [1, 2, 3], 'nurse': [2, 3], 'front': []}, (
        'each step its own turns, by id: front shares the persona and took part in none, and the turn '
        "the nurse answered for the desk is the desk's too (session 2: what the caller said there is theirs to it)"
    )
    desk = script.payloads[-1]
    assert 'Ten in the morning.' in desk.history, 'the window keeps the latest turn held for the desk'
    assert 'Is it OK to eat before?' not in desk.history and 'Tuesday at ten.' not in desk.history, 'turns: 1'


def anchors_outlive_the_window(manifest: str = 'scoped.yaml'):
    """With `turns: 1`, the request that sent the caller here, the step's own question and a loaded capability stay."""
    script = Script()
    flow, call = opened(script, DeskState(), manifest)
    asyncio.run(
        turn(
            flow,
            call,
            script,
            'I would like Tuesday at ten, please.',
            ('call', 'hand_over', {'to': 'desk', 'because': 'an appointment'}),
            ('say', 'Your patient number, please.'),
        )
    )
    assert call.state.agent == 'front' and call.state.pending == 'desk', 'the prerequisite was interposed'
    asyncio.run(turn(flow, call, script, 'One moment.', ('say', 'Take your time.')))
    before = len(script.payloads)
    said = asyncio.run(
        turn(
            flow,
            call,
            script,
            'It is P-7777.',
            ('collect', {'patient': 'P-7777'}),
            ('call', 'check_in', {}),
            ('call', 'load_capability', {'id': 'refunds'}),
            ('say', 'Which time suits you?'),
        )
    )
    assert call.state.agent == 'desk' and said[-1] == 'Which time suits you?', said
    for desk in script.payloads[before + 2 :]:
        assert (
            '"tool_name": "check_in"' not in desk.history and '"tool_name": "collect"' not in desk.history
        ), "front's tool exchange in this caller turn is not the desk's"
        assert 'It is P-7777.' in desk.history, 'the current words still reach the step that took over'
    asyncio.run(turn(flow, call, script, 'Ten.', ('collect', {'slot': '1000'}), ('call', 'book', {})))
    before = len(script.payloads)
    asyncio.run(
        turn(flow, call, script, 'And can I get a refund if I cancel?', ('say', 'Half, after a day.'))
    )
    assert len(script.payloads) == before + 1, 'answered in one request: nothing loaded again'
    desk = script.payloads[-1]
    kept, beyond = flow.nodes['desk'].history.window(call, call.messages, 'desk', {'book'})
    assert 1 in beyond and 4 in kept, (sorted(kept), sorted(beyond))
    assert 'I would like Tuesday at ten, please.' in desk.history, 'the words that asked for this step'
    assert 'Shall I book Tuesday at 10:00 AM for P-7777?' in desk.history, 'its own pending question'
    assert 'One moment.' not in desk.history and 'It is P-7777.' not in desk.history, 'past the window'
    loads = [
        part
        for message in json.loads(desk.history)
        for part in message['parts']
        if part.get('tool_name') == 'load_capability' and part['part_kind'] == 'tool-call'
    ]
    assert len(loads) == 1 and REFUND_POLICY in desk, 'the load record is kept once, outside the window'


def each_request_is_rebuilt_for_its_own_call(manifest: str = 'scoped.yaml'):
    """Two calls on one compiled flow, interleaved: each request is built fresh from its own call, never the other."""
    script = Script()
    flow = core.load(HERE / manifest).compile(model=script.model)
    one = opened(script, DeskState(note='NOTE-ONE'), flow=flow)[1]
    two = opened(script, DeskState(note='NOTE-TWO'), flow=flow)[1]
    for call in (one, two):
        call.commit(agent='front')
    mine: dict[int, list[Payload]] = {id(one): [], id(two): []}
    for call, words, patient in ((one, 'P-1111', 'P-1111'), (two, 'P-2222', 'P-2222'), (one, 'Again?', '')):
        before = len(script.payloads)
        moves = [('collect', {'patient': patient})] if patient else []
        asyncio.run(turn(flow, call, script, words, *moves, ('say', 'Thanks.')))
        mine[id(call)] += script.payloads[before:]
    first, *_, last = mine[id(one)]
    assert '"patient":null' in first.instructions and '"patient":"P-1111"' in last.instructions, (
        'the facts are read again for every request'
    )
    for payload in mine[id(one)]:
        assert 'P-2222' not in payload and 'NOTE-TWO' not in payload, 'another call on the same flow'
    assert 'Front desk notes on file: NOTE-ONE.' in last.instructions, (
        'a native capability is trusted code: its callback read the note front is not shown, and said it'
    )


def pinned(requests: dict[str, Payload], golden: Path, bless: str) -> None:
    """Compare whole requests with a golden file; `bless=1` in the environment rewrites it after review."""
    text = json.dumps(
        {k: p.pinned() for k, p in requests.items()}, indent=2, sort_keys=True, ensure_ascii=False
    )
    if os.environ.get(bless):
        golden.parent.mkdir(exist_ok=True)
        golden.write_text(text + '\n')
    assert golden.exists() and golden.read_text() == text + '\n', (
        f'a request changed: review the diff of {golden.name}, then {bless}=1'
    )


async def v1_requests_are_pinned():
    """The clinic's requests as `consult.yaml` (v1) sends them, pinned whole before the scoped view existed.

    Instructions, history, tool and output schemas, byte for byte at the
    model boundary: a v1 manifest must send exactly this after any change.
    """
    requests: dict[str, Payload] = {}
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
    requests['desk, a local answer after a note'] = script.payloads[-1]
    await turn(
        flow, call, script, 'Tuesday at ten, please.', ('collect', {'slot': '1000'}), ('call', 'book', {})
    )
    requests['desk, the read-back request'] = script.payloads[-1]
    await turn(
        flow,
        call,
        script,
        'Wait, can I eat before the appointment?',
        ('call', 'hand_over', {'to': 'nurse', 'because': 'a medical question'}),
        ('answer', 'Yes, eat as you normally would.'),
    )
    requests['nurse, consulted'] = script.payloads[-1]
    script = Script()
    flow, call = opened(script, DeskState())
    call.commit(agent='front', pending='desk')
    await turn(
        flow,
        call,
        script,
        'It is P-1234.',
        ('collect', {'patient': 'P-1234'}),
        ('call', 'check_in', {}),
        ('say', 'Which time?'),
    )
    requests['front, the prerequisite'] = script.payloads[0]
    requests['desk, after the prerequisite returned'] = script.payloads[-1]
    pinned(requests, GOLDEN, 'BLESS_PAYLOADS')


async def legacy_collect_is_frozen():
    """What `collect` means today, frozen for Session 2's structured patch, which must keep this path working.

    The schema offers each of the step's fields as its own type or null,
    null by default, with the state's constraints and description and
    nothing else of the state: no default, no other field. Applying it, an
    omitted field and an explicit null both leave the value as it was
    (legacy `collect` cannot clear), and '' is a value like any other.
    """
    script = Script()
    flow, call = opened(script, checked_in())
    await turn(
        flow,
        call,
        script,
        'Tuesday at ten.',
        ('collect', {'slot': '1000', 'note': 'wheelchair'}),
        ('say', 'Noted.'),
    )
    collect = json.loads(script.payloads[0].tools)
    [values] = [t['parameters_json_schema'] for t in collect if t['name'] == 'collect']
    assert set(values['properties']) == {'slot', 'note'} and values['additionalProperties'] is False, values
    slot = values['properties']['slot']
    assert slot['default'] is None and slot['anyOf'] == [{'$ref': '#/$defs/Slot'}, {'type': 'null'}], slot
    assert values['$defs']['Slot'] == {'enum': ['0900', '1000'], 'type': 'string'}, values['$defs']
    assert values['properties']['note'] == {
        'anyOf': [{'type': 'string'}, {'type': 'null'}],
        'default': None,
        'description': 'Anything else the caller mentioned.',
    }, 'the state default ("") is not the tool default'
    assert 'P-1234' not in script.payloads[0].tools and 'reference' not in script.payloads[0].tools
    await turn(flow, call, script, 'Hmm.', ('collect', {'slot': None}), ('say', 'Noted.'))
    assert (call.state.facts.slot, call.state.facts.note) == ('1000', 'wheelchair'), (
        'an explicit null is left out, and an omitted field kept: legacy collect cannot clear'
    )
    await turn(flow, call, script, 'Forget the note.', ('collect', {'note': ''}), ('say', 'Noted.'))
    assert call.state.facts.note == '', "'' is a value, recorded like any other"


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
    asyncio.run(v1_requests_are_pinned())
    asyncio.run(legacy_collect_is_frozen())
    v1_node_context_replaces_the_flows_whole()
    scoped_members_replace_one_by_one()
    invalid_context_is_refused_at_load()
    a_refusal_says_only_what_the_step_sees()
    for scoped in ('scoped.yaml', 'scoped.v2.yaml'):
        automatic_sources_show_only_the_steps_view(scoped)
        words_said_stay_words(scoped)
        history_is_the_nodes_own(scoped)
        anchors_outlive_the_window(scoped)
        each_request_is_rebuilt_for_its_own_call(scoped)
    print('context ok: history and facts per step, a capability kept, consultations that come back')
