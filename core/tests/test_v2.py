"""Run: uv run python -m tests.test_v2

Manifest version 2 on the clinic: steps keyed by id, compiled to the same
steps table and runtime as version 1 (`core.flowspec.FlowSpec._normalized`).
Asserts, no pytest.

* every refusal names the step it is about: a key given twice, an id that
  is not its key, the other version's form, an unknown version or field, a
  path selector, a field the step may change but cannot see, a persona's own
  output schema, a receipt field made writable, a prompt reaching past the view;
* the defaults are short and an explicit key wins: a conversation, structured,
  no facts and the step's own history; `rendered`, `prose`, `action`, `end`
  and `context: none` kept; a step with no agent speaks in the root `voice`;
* a step's context replaces the flow's member by member;
* each version 2 twin is its version 1 source: the same spec, the same legal
  moves, and, for `structured.v2.yaml`, the same requests byte for byte;
* `context: none` still sends the caller's words, the step's own tool
  exchange this turn and its own pending question.
"""

import asyncio
import json

import core
from core.flowspec import ContextSpec
from core.history import History
from tests.clinic_flow.consult import CheckedIn
from tests.clinic_flow.structured import Clinic, VisitState
from tests.test_context import HERE, refused
from tests.test_public import CLOCK
from tests.test_structured import Script, reply, turn

TINY = """\
version: 2
id: tiny
state: structured.py:VisitState
identity: patient
services: structured.py:Clinic
voice: prompts/receptionist.md
operations:
  book: actions.py:Book
  quote: structured.py:Quote
steps:
  desk:
    prompt: prompts/visit.md
    fields: [service, slot]
    context: {facts: [service, slot]}
    exits: [confirm, front]
  confirm:
    prompt: prompts/booking.md
    operations: [book, quote]
    opens_with: book
    context: none
  front:
    id: front
    speech: prose
    prompt: prompts/check_in.md
    exits: [done]
  done: {kind: end, outcome: completed, say: Goodbye.}
"""
"""A small version 2 clinic: defaults everywhere, and each explicit key once."""


def refusals_name_their_step():
    """Every refusal is found before any model runs, and says which step and key it is about."""
    base = (HERE / 'structured.v2.yaml').read_text()
    for text, expected in (
        (base.replace('  front:\n', '  front:\n    prompt: prompts/visit.md\n'), "'prompt' is given twice"),
        (base + 'id: again\n', "'id' is given twice"),
        (
            base.replace('function: structured.py:free_times,', 'function: a, function: b,'),
            "'function' is given",
        ),
        (base.replace('  nurse:\n', '  nurse:\n    id: doctor\n'), "step 'nurse': its id 'doctor' differs"),
        (
            (HERE / 'structured.yaml').read_text().replace('version: 1', 'version: 2'),
            'version 2 keys its steps',
        ),
        (base.replace('version: 2', 'version: 1'), 'version 1 lists its steps'),
        (base.replace('version: 2', 'version: 3'), 'Input should be 1 or 2'),
        (base.replace('  nurse:\n', '  nurse:\n    colour: red\n'), "step 'nurse': colour: Extra inputs"),
        (base.replace('  nurse:\n', '  nurse:\n    kind: chat\n'), "step 'nurse': Input tag 'chat'"),
        (
            base.replace('context: {facts: [service]}', 'context: {facts: [slot.day]}'),
            "step 'nurse': context",
        ),
        (
            base.replace('context: {facts: [service]}', 'context: {facts: [service]}\n    fields: [note]'),
            "nurse: may change 'note' but cannot see it: add it to context.facts",
        ),
        (
            base.replace(
                '    prompt: prompts/receptionist.md',
                '    prompt: prompts/receptionist.md\n    spec:\n      output_schema: {type: object}',
            ),
            "desk: its agent 'receptionist' sets spec.output_schema",
        ),
        (base.replace('fields: [service, slot,', 'fields: [price, service, slot,'), "assigns 'price'"),
        (
            base.replace(
                '    operations: [book, quote]\n',
                '    offers: [note]\n    operations: [book, quote]\n',
            ),
            "desk: offers 'note' on arrival, which is not one of its choices",
        ),
        (
            base.replace('prompt: prompts/nurse.md', 'prompt: prompts/call.md'),
            "nurse: prompt 'prompts/call.md'",
        ),
    ):
        problems = refused(text)
        assert expected in problems, (expected, problems)
    assert 'nurse' in refused(base.replace('context: {facts: [service]}', 'context: {facts: [slot.day]}')), (
        'a selector is refused on the step that wrote it'
    )
    assert refused(base) == '' and refused(TINY) == '', 'both load as they are'


def defaults_are_short_and_explicit_keys_win():
    """A step is a structured conversation unless it says otherwise; the flow's context is no facts, own history."""
    spec = core.flowspec.load(TINY, root=HERE)
    assert [(s.id, s.kind, getattr(s, 'speech', '')) for s in spec.steps] == [
        ('desk', 'conversation', 'structured'),
        ('confirm', 'conversation', 'structured'),
        ('front', 'conversation', 'prose'),
        ('done', 'end', ''),
    ], spec.steps
    assert spec.context == ContextSpec(history=History(), facts='none'), spec.context
    assert History() == History(scope='node', turns=None), 'every turn of its own'
    flow = spec.compile(model=Script().model)
    desk, confirm, front = (flow.nodes[name] for name in ('desk', 'confirm', 'front'))
    assert (desk.scope.facts, confirm.scope.facts, front.scope.facts) == (('service', 'slot'), (), ()), (
        'each scoped: its own list, none, and the none it inherits'
    )
    assert desk.history == History() and front.history == History() and confirm.history == 'none'
    assert str(spec.prompt('front')).startswith('You answer the clinic'), 'no agent: the root voice first'
    for context in ('context: {history: messages}\n', 'context: none\n'):
        plain = core.flowspec.load(TINY.replace('operations:\n', context + 'operations:\n', 1), root=HERE)
        nodes = plain.compile(model=Script().model).nodes
        assert all(nodes[name].scope is not None for name in ('desk', 'confirm', 'front')), (
            'version 2 is scoped whatever history it names: a history string is not a version 1 context'
        )
    rendered = core.flowspec.load(
        TINY.replace(
            '    speech: prose\n', '    speech: rendered\n    fields: [note]\n    context: {facts: [note]}\n'
        ),
        root=HERE,
    )
    assert next(s for s in rendered.steps if s.id == 'front').speech == 'rendered', (
        'an explicit speech is kept'
    )
    code = core.flowspec.load(
        TINY.replace(
            '  done: {kind: end', '  look: {kind: action, operation: quote, then: done}\n  done: {kind: end'
        ),
        root=HERE,
    )
    assert [s.kind for s in code.steps][-2:] == ['action', 'end'], 'explicit kinds are kept'


def a_steps_context_replaces_the_flows_member_by_member():
    """Facts replace the flow's list (`[]` is none), a history replaces the flow's whole, absent members inherit."""
    text = TINY.replace(
        'operations:\n',
        'context: {facts: [service], history: {scope: conversation, turns: 3}}\noperations:\n',
    )
    text = text.replace('    context: {facts: [service, slot]}\n', '')
    text = text.replace('    fields: [service, slot]\n', '    fields: [service]\n')
    text = text.replace('    speech: prose\n', '    speech: prose\n    context: {history: {scope: node}}\n')
    text = text.replace('    context: none\n', '    context: {facts: []}\n')
    nodes = core.flowspec.load(text, root=HERE).compile(model=Script().model).nodes
    assert nodes['desk'].scope.facts == ('service',), 'absent: the flow facts'
    assert nodes['desk'].history == History(scope='conversation', turns=3), 'absent: the flow history'
    assert nodes['confirm'].scope.facts == () and nodes['confirm'].history == History(
        scope='conversation', turns=3
    )
    assert nodes['front'].scope.facts == ('service',) and nodes['front'].history == History(), (
        'a history replaces the flow history as a unit: its window is not inherited'
    )


def each_twin_is_its_source():
    """Normalized, a version 2 twin is its version 1 file: the spec, the legal moves and the requests."""
    for v1, v2 in (('structured.yaml', 'structured.v2.yaml'), ('scoped.yaml', 'scoped.v2.yaml')):
        one, two = core.load(HERE / v1), core.load(HERE / v2)
        first, second = one.model_dump(exclude={'version'}), two.model_dump(exclude={'version'})
        if v1 == 'scoped.yaml':
            assert first['context']['facts'] == ['slot'] and second['context']['facts'] == ['slot', 'note'], (
                'version 2 shows desk the note it may change; version 1 let it change it unseen'
            )
            first['context']['facts'] = second['context']['facts']
        assert first == second, v2
        script = Script()
        moves = [spec.compile(model=script.model).nodes.flow.moves for spec in (one, two)]
        assert moves[0] == moves[1], v2
    sent = [asyncio.run(requests_on(manifest)) for manifest in ('structured.yaml', 'structured.v2.yaml')]
    assert len(sent[0]) == 3 and sent[0] == sent[1], 'the same requests, byte for byte'


async def requests_on(manifest: str) -> list[dict]:
    """A local answer, then a read with its update and the answer: every request, pinned."""
    script = Script()
    flow = core.load(HERE / manifest).compile(model=script.model)
    call = flow.open(
        VisitState(patient='P-1234', checked_in=CheckedIn(patient='P-1234')),
        services=Clinic(),
        clock=lambda: CLOCK,
    )
    call.commit(agent='desk')
    await turn(
        flow, call, script, 'I come in a wheelchair.', reply('We will have the ramp ready.', wheelchair=True)
    )
    await turn(
        flow,
        call,
        script,
        'A cleaning. What does it cost?',
        ('call', 'quote', {'update': {'service': 'cleaning'}}),
        reply('Forty euros.'),
    )
    return [payload.pinned() for payload in script.payloads]


def no_context_keeps_the_turn_and_its_own_question():
    """`context: none`: no facts, no earlier turns; the caller's words, its own tool exchange and question stay."""
    script = Script()
    flow = core.flowspec.load(TINY, root=HERE).compile(model=script.model)
    clinic = Clinic()
    call = flow.open(VisitState(patient='P-1234'), services=clinic, clock=lambda: CLOCK)
    said, _ = asyncio.run(
        turn(
            flow,
            call,
            script,
            'A cleaning at ten, please.',
            (
                'call',
                'hand_over',
                {'to': 'confirm', 'because': 'book it', 'update': {'service': 'cleaning', 'slot': '1000'}},
            ),
        )
    )
    assert call.state.agent == 'confirm' and said[-1] == 'Shall I book Tuesday at 10:00 AM for P-1234?', said
    before = len(script.payloads)
    said, done = asyncio.run(
        turn(flow, call, script, 'How much is it?', ('call', 'quote', {}), reply('Forty euros.'))
    )
    assert said == ['Forty euros.'] and done.requests == 2 and clinic.quotes == ['cleaning'], (said, done)
    first, second = script.payloads[before:]
    for payload in (first, second):
        assert 'Facts:' not in payload.instructions, 'no facts'
        assert 'A cleaning at ten, please.' not in payload.history, 'no earlier turn'
        assert 'How much is it?' in payload.history, 'the words of this turn'
        assert (
            'The caller is answering: "Shall I book Tuesday at 10:00 AM for P-1234?"' in payload.instructions
        ), 'its own pending question'
    tools = [p.get('tool_name') for m in json.loads(second.history) for p in m['parts']]
    assert 'quote' in tools and 'hand_over' not in tools, "its own tool exchange; desk's is not its"
    assert [i.status for i in call.intents if i.operation == 'book'] == ['prepared'], (
        'a question is not a yes'
    )


if __name__ == '__main__':
    refusals_name_their_step()
    defaults_are_short_and_explicit_keys_win()
    a_steps_context_replaces_the_flows_member_by_member()
    each_twin_is_its_source()
    no_context_keeps_the_turn_and_its_own_question()
    print('v2 ok: refusals by step, short defaults, member-by-member context, twins equal, context none')
