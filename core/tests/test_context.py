"""Run: uv run python -m tests.test_context

What each step's agent is sent (`context:`), a capability loaded on demand
and kept, and a consultation that comes back (`consults:`), on the clinic
(`clinic_flow/consult.yaml`). Asserts, no pytest.

* a loaded capability survives the filter, so a follow-up does not load it
  again, and it costs one request;
* a consultation that asks first still comes back to the read-back waiting;
* the scoped view (`clinic_flow/scoped.yaml`): each member a step sets
  replaces the flow's, and no automatic source reaches a step that does not
  see it, while what was said is sent as said.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart

import core
from core.history import History, Payload
from tests.clinic_flow.clinic import Backend
from tests.clinic_flow.consult import REFUND_POLICY, CheckedIn, DeskState
from tests.script import Call, Move, Reply, Script
from tests.test_public import CLOCK

HERE = Path(__file__).parent / 'clinic_flow'


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


async def turn(flow, call, script: Script, words: str, *moves: Move) -> list[str]:
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


async def a_loaded_capability_is_not_loaded_again():
    script = Script()
    flow, call = opened(script, checked_in())
    await turn(
        flow,
        call,
        script,
        'Do I get my money back if I cancel?',
        Call('load_capability', id='refunds'),
        Reply('In full up to a day before, half after that.'),
    )
    assert len(script.requests) == 2, 'load, then answer: two requests'
    await turn(flow, call, script, 'And on the day itself?', Reply('Half.'))
    messages, instructions = script.requests[-1].messages, script.requests[-1].instructions
    assert len(script.requests) == 3, 'the follow-up is one request'
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


async def a_consultation_that_asks_first_still_comes_back_to_the_read_back():
    script = Script()
    flow, call = opened(script, checked_in())
    await turn(flow, call, script, 'Tuesday at ten.', Call('options'), Call('book', update={'slot': '1000'}))
    await turn(
        flow,
        call,
        script,
        'Is it OK to eat before?',
        Call('hand_over', to='nurse', because='a medical question'),
        Reply('What time is your appointment?'),
    )
    assert call.state.agent == 'nurse', 'the nurse asked first, and the caller stays with the nurse'
    said = await turn(
        flow, call, script, 'Ten in the morning.', Call('done', answer='Then eat a light breakfast.')
    )
    assert said == ['Then eat a light breakfast.', 'Shall I book Tuesday at 10:00 AM for P-1234?'], said
    await turn(flow, call, script, 'Yes.', Call('book'))
    assert call.services.bookings == {'CL001': ('P-1234', '1000')}


def retries(payload: Payload) -> list[str]:
    """The retry texts a request carries: what a refused tool told the model."""
    return [
        str(part.get('content'))
        for message in json.loads(payload.history)
        for part in message['parts']
        if part.get('part_kind') == 'retry-prompt'
    ]


def scoped_members_replace_one_by_one():
    """Scoped: the flow's context is the default, and each member a step sets replaces it, as YAML-CONTRACT says."""
    script = Script()
    nodes = core.load(HERE / 'scoped.yaml').compile(model=script.model).nodes
    root = History(scope='node', turns=1)
    assert (nodes['desk'].history, nodes['desk'].scope.facts) == (root, ('slot', 'note')), (
        'absent members inherit'
    )
    assert (nodes['front'].history, nodes['front'].scope.facts) == (root, ('patient',)), (
        'facts replace, not add'
    )
    assert nodes['nurse'].scope.facts == () and nodes['nurse'].history == root
    text = variant('scoped.yaml', '    context: {facts: none}\n', '    context: none\n')
    nodes = core.flowspec.load(text, root=HERE).compile(model=script.model).nodes
    assert (nodes['lobby'].history, nodes['lobby'].scope.facts) == ('none', ()), 'context: none'
    text = (
        (HERE / 'scoped.yaml')
        .read_text()
        .replace(
            'prompt: prompts/nurse.md\n    context: {facts: none}',
            'prompt: prompts/nurse.md\n    context: {history: {scope: conversation}}',
        )
    )
    nodes = core.flowspec.load(text, root=HERE).compile(model=script.model).nodes
    assert (nodes['nurse'].history, nodes['nurse'].scope.facts) == (
        History(scope='conversation'),
        ('slot', 'note'),
    ), 'a history replaces the object as a unit, and absent facts inherit'


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


def automatic_sources_show_only_the_steps_view():
    """Canaries in every automatic source: none reaches a step that does not see them; the call keeps them all.

    The caller's patient number P-7777 is bound by `book` from the state and
    shown to no step but `front`; the note is written by `desk` and shown to
    no one else. The raw receipt, the offers, the read-back, the intent's
    arguments and the recap all hold one or the other; `desk` sees the note it may change.
    """
    script = Script()
    flow, call = opened(
        script,
        DeskState(patient='P-7777', checked_in=CheckedIn(patient='P-7777'), note='CANARY-NOTE'),
        'scoped.yaml',
    )
    call.commit(agent='desk')
    said = asyncio.run(
        turn(
            flow,
            call,
            script,
            'Tuesday at ten, please.',
            Call('options'),
            Call('book', update={'slot': '1000'}),
        )
    )
    assert said[-1] == 'Shall I book Tuesday at 10:00 AM for P-7777?', said
    desk = script.payloads[-1]
    assert 'The caller is answering: "Shall I book' not in desk.instructions, 'asked after this request'
    assert 'CANARY-NOTE' in desk.instructions and 'P-7777' not in desk.instructions, desk.instructions
    asyncio.run(
        turn(
            flow,
            call,
            script,
            'Wait, can I eat before?',
            Call('hand_over', to='nurse', because='a medical question'),
            Call('done', answer='Yes, eat as you normally would.'),
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
    asyncio.run(turn(flow, call, script, 'Yes, book it.', Call('book')))
    asyncio.run(turn(flow, call, script, 'Thanks.', Reply('You are welcome.')))
    desk = script.payloads[-1]
    assert 'Done on this call: book {"slot": "1000"} -> succeeded' in desk.instructions, desk.instructions
    assert 'your visit Tuesday at 10:00 AM' in desk.instructions, 'the owner may recap its booking'
    for leaked in ('P-7777', 'CL001'):
        assert leaked not in desk.instructions, (leaked, desk.instructions)
    assert 'reference CL001' in desk.history, 'the receipt the caller heard is conversation, sent as said'
    call.services.failing = True
    asyncio.run(
        turn(
            flow,
            call,
            script,
            'And the nine o clock too.',
            Call('options'),
            Call('book', update={'slot': '0900'}),
        )
    )
    asyncio.run(turn(flow, call, script, 'Yes.', Call('book'), Reply('Let me check.')))
    assert call.unresolved(), 'the diary did not answer: the outcome is unknown'
    call.services.bookings.pop('CL002')
    """Nothing for reconciliation to find at the next turn's start, so the outcome stays unknown."""
    asyncio.run(
        turn(
            flow,
            call,
            script,
            'Is fasting needed?',
            Call('hand_over', to='nurse', because='fasting'),
            Call('done', answer='No.'),
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
    """A service's no goes back to the model as the step's view of the receipt, not the receipt."""
    for manifest, shows in (('scoped.yaml', False),):
        script = Script()
        flow, call = opened(
            script, DeskState(patient='P-7777', checked_in=CheckedIn(patient='P-7777')), manifest
        )
        call.commit(agent='desk')
        call.services.full = True
        asyncio.run(turn(flow, call, script, 'Ten.', Call('options'), Call('book', update={'slot': '1000'})))
        asyncio.run(turn(flow, call, script, 'Yes.', Call('book'), Reply('That time is taken.')))
        [refusal] = [r for r in retries(script.payloads[-1]) if 'service answered' in r]
        assert ('P-7777' in refusal) == shows, (manifest, refusal)


def words_said_stay_words():
    """Scope minimises context, it does not redact: what the caller says, and what they were read, is sent as said."""
    script = Script()
    flow, call = opened(
        script, DeskState(patient='P-7777', checked_in=CheckedIn(patient='P-7777')), 'scoped.yaml'
    )
    call.commit(agent='desk')
    asyncio.run(
        turn(
            flow,
            call,
            script,
            'Put in a note: CANARY-SAID.',
            Reply('Noted.', note='CANARY-SAID'),
        )
    )
    assert call.state.facts.note == 'CANARY-SAID', 'the note was recorded'
    asyncio.run(
        turn(
            flow, call, script, 'And Tuesday at ten.', Call('options'), Call('book', update={'slot': '1000'})
        )
    )
    desk = script.payloads[-2]
    assert 'CANARY-SAID' in desk.history, desk.history
    assert 'CANARY-SAID' in desk.instructions, 'a fact where it is seen'
    asyncio.run(turn(flow, call, script, 'How long is it?', Reply('Half an hour.')))
    desk = script.payloads[-1]
    assert 'for P-7777?' in desk.history, 'the read-back the caller heard names the hidden number'


def history_is_the_nodes_own():
    """`scope: node` is by step id, across visits; a parent's tool exchange is not the consulted step's."""
    script = Script()
    flow, call = opened(
        script, DeskState(patient='P-7777', checked_in=CheckedIn(patient='P-7777')), 'scoped.yaml'
    )
    call.commit(agent='desk')
    asyncio.run(turn(flow, call, script, 'Tuesday at ten.', Call('options'), Reply('Noted.', slot='1000')))
    asyncio.run(
        turn(
            flow,
            call,
            script,
            'Is it OK to eat before?',
            Call('hand_over', to='nurse', because='food'),
            Reply('What time is your appointment?'),
        )
    )
    asyncio.run(
        turn(flow, call, script, 'Ten in the morning.', Call('done', answer='Then eat a light breakfast.'))
    )
    nurse = script.payloads[-1]
    assert 'Is it OK to eat before?' in nurse.history and 'What time is your appointment?' in nurse.history, (
        'the nurse keeps its own earlier turn'
    )
    assert 'Tuesday at ten.' not in nurse.history and 'hand_over' not in nurse.history, nurse.pinned()[
        'history'
    ]
    asyncio.run(turn(flow, call, script, 'Great, and the price?', Reply('It is covered.')))
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


def anchors_outlive_the_window():
    """With `turns: 1`, the request that sent the caller here, the step's own question and a loaded capability stay."""
    script = Script()
    flow, call = opened(script, DeskState(), 'scoped.yaml')
    asyncio.run(
        turn(
            flow,
            call,
            script,
            'I would like Tuesday at ten, please.',
            Call('hand_over', to='desk', because='an appointment'),
            Reply('Your patient number, please.'),
        )
    )
    assert call.state.agent == 'front' and call.state.pending == 'desk', 'the prerequisite was interposed'
    asyncio.run(turn(flow, call, script, 'One moment.', Reply('Take your time.')))
    before = len(script.payloads)
    said = asyncio.run(
        turn(
            flow,
            call,
            script,
            'It is P-7777.',
            Call('check_in', update={'patient': 'P-7777'}),
            Call('load_capability', id='refunds'),
            Reply('Which time suits you?'),
        )
    )
    assert call.state.agent == 'desk' and said[-1] == 'Which time suits you?', said
    for desk in script.payloads[before + 1 :]:
        assert '"tool_name": "check_in"' not in desk.history, (
            "front's tool exchange in this caller turn is not the desk's"
        )
        assert 'It is P-7777.' in desk.history, 'the current words still reach the step that took over'
    asyncio.run(turn(flow, call, script, 'Ten.', Call('options'), Call('book', update={'slot': '1000'})))
    before = len(script.payloads)
    asyncio.run(turn(flow, call, script, 'And can I get a refund if I cancel?', Reply('Half, after a day.')))
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


def each_request_is_rebuilt_for_its_own_call():
    """Two calls on one compiled flow, interleaved: each request is built fresh from its own call, never the other."""
    script = Script()
    flow = core.load(HERE / 'scoped.yaml').compile(model=script.model)
    one = opened(script, DeskState(note='NOTE-ONE'), flow=flow)[1]
    two = opened(script, DeskState(note='NOTE-TWO'), flow=flow)[1]
    for call in (one, two):
        call.commit(agent='front')
    mine: dict[int, list[Payload]] = {id(one): [], id(two): []}
    for call, words, patient in ((one, 'P-1111', 'P-1111'), (two, 'P-2222', 'P-2222'), (one, 'Again?', '')):
        before = len(script.payloads)
        moves = [Reply('Thanks.', patient=patient)] if patient else [Reply('Thanks.')]
        asyncio.run(turn(flow, call, script, words, *moves))
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
    asyncio.run(a_loaded_capability_is_not_loaded_again())
    asyncio.run(a_consultation_that_asks_first_still_comes_back_to_the_read_back())
    the_loader_refuses_a_consultation_chain()
    scoped_members_replace_one_by_one()
    invalid_context_is_refused_at_load()
    a_refusal_says_only_what_the_step_sees()
    automatic_sources_show_only_the_steps_view()
    words_said_stay_words()
    history_is_the_nodes_own()
    anchors_outlive_the_window()
    each_request_is_rebuilt_for_its_own_call()
    print('context ok: history and facts per step, a capability kept, consultations that come back')
