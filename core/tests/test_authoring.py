"""The ordinary authoring contract, end to end, on the clinic: a manifest, models, prompts and functions, nothing else.

The clinic (`tests/clinic_flow`) is written two ways from one `clinic.py`:
one conversational agent (`agent.yaml`) and a front desk handing booking to
a specialist (`handoff.yaml`). Each check drives the public boundary
(`core.load`, `compile`, `open`, `text_turn`, `acknowledge`) with a scripted
model where a provider would be (`tests.script`), and reads the backend,
the receipts and the evidence.

What is checked, against session 3's acceptance list:

* a bad manifest fails before any model is built, naming the step or operation;
* a delivered read-back and a yes make one booking and its confirmation, in
  every pattern; an unheard read-back makes none;
* only an offered time can be picked;
* a no, a change, a failed receipt or an unknown outcome completes nothing and
  claims nothing; an unknown outcome is settled, never repeated;
* cancelling the booking runs the real cancel, with its own read-back and receipt;
* ordinary conversation pays for no reading;
* the manifest's entry, services factory, locale, version and persona spec
  reach the session;
* what the application wrote, counted against the native Pipecat salon (N13).
"""

import ast
import asyncio
from pathlib import Path
from typing import Any

from pydantic import ValidationError

import core
from core.flowspec import load as load_text
from core.locale import ENGLISH
from tests.clinic_flow import clinic
from tests.clinic_flow.clinic import Backend, ClinicState
from tests.script import Call, Move, Reply, Script

HERE = Path(__file__).parent / 'clinic_flow'
PATTERNS = ('agent.yaml', 'handoff.yaml')
FILL = (Call('options', update={'patient': 'P-1234'}), Call('book', update={'slot': '1000'}))
"""The patient recorded as the times are offered, then the pick read back: two requests."""
READ_BACK = 'Shall I book Tuesday at 10:00 AM for P-1234?'


def opened(
    manifest: str, script: Script, backend: Backend | None = None, *, text: str = ''
) -> tuple[core.StepFlow, core.FlowCall]:
    """A call on this manifest, or on `text` written as a variant of it."""
    spec = load_text(text, root=HERE) if text else core.load(HERE / manifest)
    flow = spec.compile(model=script.model)
    script.call = flow.open(ClinicState(), services=backend if backend is not None else Backend())
    return flow, script.call


async def turn(flow, call, script: Script, words: str, *moves: Move, heard: bool = True) -> core.TextReply:
    """One caller turn; the reader gets every segment of the reply unless `heard` is False."""
    script.moves = list(moves)
    reply = await core.text_turn(flow, call, words)
    if heard:
        call.acknowledge(*(segment.id for segment in reply.segments))
    return reply


def said(reply: core.TextReply) -> list[str]:
    return [segment.text for segment in reply.segments]


def book_of(flow: core.StepFlow) -> Any:
    """The flow's one `book` operation, wherever its steps hold it."""
    found = {
        id(op): op
        for step in flow.steps
        for op in (getattr(step, 'operation', None), *getattr(step, 'operations', ()))
        if op is not None and op.name == 'book'
    }
    [operation] = found.values()
    return operation


def definitions_fail_before_any_model():
    """Each broken manifest is refused by `problems()` or `load`, before any agent or provider exists."""
    base = (HERE / 'agent.yaml').read_text()
    desk = '    agent: receptionist\n    prompt: prompts/desk.md\n'

    def problems(text: str) -> list[str]:
        return load_text(text, root=HERE).problems()

    for broken, expected in (
        (
            base.replace('function: clinic.py:book', 'function: nowhere.py:book'),
            "'nowhere.py' does not exist",
        ),
        (
            base.replace('args: {patient: patient, slot: slot}', 'args: {patient: patient}'),
            "'slot' is not bound",
        ),
        (
            base.replace('args: {patient: patient, slot: slot}', 'args: {patient: patient, slot: notes}'),
            'different kinds',
        ),
        (
            base.replace('assign: {reference: reference}', 'assign: {slot: slot}'),
            'a receipt may not overwrite',
        ),
        (base.replace('assign: {reference: reference}', 'assign: {reference: code}'), "'code', which Booked"),
        (base.replace('succeeds: {status: booked}', 'succeeds: {state: booked}'), "succeeds on 'state'"),
        (base.replace('    readback: clinic.py:booking_words\n', '', 1), 'needs a readback function'),
        (
            base.replace('operations: [book, cancel]', 'operations: [book, cancel]\n    exits: [nowhere]'),
            "'nowhere', which is not a step",
        ),
        (base.replace('prompt: prompts/desk.md', 'prompt: prompts/broken.md'), "'nickname'"),
        (
            base.replace('choices: {slot: clinic.py:free_times}', 'choices: {notes: clinic.py:free_times}'),
            'does not ask for',
        ),
        (base.replace(desk, desk.replace('receptionist', 'nurse')), "'nurse'"),
        (base.replace('id: clinic-desk\n', 'id: clinic-desk\nentry: lobby\n'), "'lobby' is not a step"),
    ):
        found = problems(broken)
        assert any(expected in problem for problem in found), (expected, found)
    for broken, expected in (
        (base.replace('version: 2', 'version: 3', 1), 'version'),
        (base.replace(desk, desk + '    kind: confirmation\n'), 'confirmation'),
        (base.replace(desk, desk + '    colour: red\n'), 'colour'),
    ):
        try:
            load_text(broken, root=HERE)
        except (ValidationError, core.FlowSpecError) as error:
            assert expected in str(error), (expected, str(error)[:300])
        else:
            raise AssertionError(f'{expected}: loaded')
    unset = base.replace('services: clinic.py:Backend\n', 'services: clinic.py:Backend\nmodel: ""\n')
    try:
        load_text(unset, root=HERE).compile()
    except core.FlowSpecError as error:
        assert any('no model' in problem for problem in error.problems), error.problems
    else:
        raise AssertionError('compiled with no model')


async def the_clinic_books_and_cancels():
    """Offer two ids, read one back, book it on a heard yes; then cancel it through its own read-back and receipt."""
    script = Script()
    flow, call = opened('agent.yaml', script)
    backend = call.services
    await turn(flow, call, script, 'P-1234.', FILL[0], Reply('Nine or ten on Tuesday?'))
    assert call._offers == {'slot': clinic.TIMES}, call._offers
    reply = await turn(flow, call, script, 'Ten, please.', FILL[1])
    assert said(reply) == [READ_BACK] and backend.calls == 0
    reply = await turn(flow, call, script, 'Yes.', Call('book'))
    assert said(reply) == ['Booked for Tuesday at 10:00 AM, reference CL001.'], said(reply)
    assert backend.bookings == {'CL001': ('P-1234', '1000')} and call.state.facts.reference == 'CL001'
    assert [e.operation for e in call.evidence] == ['book']

    reply = await turn(flow, call, script, 'Actually, cancel it.', Call('cancel'))
    assert said(reply) == ['Shall I cancel booking CL001?'] and backend.bookings, (
        'nothing is undone before a yes'
    )
    reply = await turn(flow, call, script, 'Yes, cancel it.', Call('cancel'))
    assert said(reply) == ['Booking CL001 is cancelled.'], said(reply)
    assert backend.bookings == {}
    assert [e.operation for e in call.evidence] == ['book', 'cancel'], 'the booking stays on record'


async def only_an_offered_time_can_be_picked():
    """9:00 is taken, so only 10:00 is offered; a model recording 9:00 anyway is refused and nothing is kept."""
    script = Script()
    backend = Backend()
    backend.bookings['CL900'] = ('P-9999', '0900')
    flow, call = opened('agent.yaml', script, backend)
    await turn(flow, call, script, 'P-1234.', FILL[0], Reply('Ten on Tuesday?'))
    assert call._offers == {'slot': {'1000': clinic.TIMES['1000']}}, call._offers
    await turn(
        flow, call, script, 'Nine.', Call('book', update={'slot': '0900'}), Reply('Nine is taken, sorry.')
    )
    assert call.state.facts.slot is None and backend.calls == 0, call.state


async def a_no_or_a_change_books_nothing():
    """At the read-back, a no (`decline`) and a changed mind (the slot cleared) each book nothing."""
    for answer in (
        Reply('All right, nothing booked.', decline=True),
        Reply('Which time instead?', slot=None),
    ):
        script = Script()
        flow, call = opened('agent.yaml', script)
        await turn(flow, call, script, 'P-1234, ten.', *FILL)
        await turn(flow, call, script, 'No, not that.', answer)
        await turn(flow, call, script, 'Hmm.', Call('book'))
        assert call.services.calls == 0, (answer, call.state)
        if 'update' in answer.args:
            assert call.state.facts.slot is None, 'a revision forgets the answer it revises'


async def an_unknown_outcome_is_settled_not_repeated():
    """The diary booked and then stopped answering: unknown, not failed; settling reconciles, never books again."""
    script = Script()
    backend = Backend()
    backend.failing = True
    text = (
        (HERE / 'agent.yaml')
        .read_text()
        .replace(
            '    readback: clinic.py:booking_words\n',
            '    readback: clinic.py:booking_words\n    reconcile: clinic.py:find_booking\n',
            1,
        )
    )
    flow, call = opened('agent.yaml', script, backend, text=text)
    await turn(flow, call, script, 'P-1234, ten.', *FILL)
    reply = await turn(flow, call, script, 'Yes.', Call('book'), Reply('Let me check.'))
    assert "couldn't confirm" in ' '.join(said(reply)), said(reply)
    assert backend.calls == 1 and not call.evidence and call.state.facts.reference == ''
    book = book_of(flow)
    receipt = await book.run(call, book.args_type(patient='P-1234', slot='1000'))
    assert backend.calls == 1, 'settling asked the diary what it holds; it did not book again'
    assert receipt.reference == 'CL001' and call.state.facts.reference == 'CL001'
    assert [e.receipt['reference'] for e in call.evidence] == ['CL001']


async def every_pattern_books_once_on_a_heard_yes():
    """The same `clinic.book`, in both patterns: an unheard read-back books nothing, a heard one books once."""
    handed = Call('hand_over', to='booking', because='an appointment')
    for manifest in PATTERNS:
        for heard in (False, True):
            script = Script()
            flow, call = opened(manifest, script)
            assert book_of(flow).function is clinic.book, 'one domain function, whatever the pattern'
            moves = FILL if manifest == 'agent.yaml' else (handed, *FILL)
            reply = await turn(flow, call, script, 'P-1234, Tuesday at ten.', *moves, heard=heard)
            assert said(reply)[-1] == READ_BACK, (manifest, said(reply))
            requests = len(script.requests)
            reply = await turn(flow, call, script, 'Yes.', Call('book'))
            booked = call.services.bookings
            if not heard:
                assert booked == {}, (manifest, 'a yes to a read-back nobody heard booked')
                continue
            assert booked == {'CL001': ('P-1234', '1000')}, (manifest, booked)
            assert said(reply)[0] == 'Booked for Tuesday at 10:00 AM, reference CL001.', (
                manifest,
                said(reply),
            )
            assert len(script.requests) == requests + 1, (
                manifest,
                'the yes is the step agent reading it, once',
            )


async def the_model_path_refuses_what_the_direct_path_refuses():
    """One agent: the model calling `book` again cannot skip the read-back; a full diary claims nothing; unknown is said as unknown."""
    script = Script()
    flow, call = opened('agent.yaml', script)
    await turn(flow, call, script, 'P-1234, ten.', *FILL, heard=False)
    reply = await turn(flow, call, script, 'Go on, book it.', Call('book'))
    assert call.services.calls == 0 and said(reply)[-1].startswith('Shall I book'), said(reply)

    for trouble in ('full', 'failing'):
        script = Script()
        backend = Backend()
        setattr(backend, trouble, True)
        flow, call = opened('agent.yaml', script, backend)
        await turn(flow, call, script, 'P-1234, ten.', *FILL)
        reply = await turn(flow, call, script, 'Yes.', Call('book'), Reply('Let me see what happened there.'))
        words = ' '.join(said(reply))
        assert 'Booked for' not in words and call.state.facts.reference == '', (trouble, words)
        if trouble == 'full':
            [filed] = call.evidence
            assert filed.receipt['status'] == 'full', 'the answer is on record, and it is not a booking'
        else:
            assert "couldn't confirm" in words, words
            await turn(flow, call, script, 'Yes, book it.', Call('book'), Reply('I cannot yet.'))
            assert backend.calls == 1, 'an unknown outcome with nothing to settle it is not retried'


async def ordinary_conversation_reads_nothing():
    """One question, one request: the step's own agent. (`Script.reading` fails any other kind.)"""
    script = Script()
    flow, call = opened('agent.yaml', script)
    reply = await turn(flow, call, script, 'When are you open?', Reply('Weekdays, nine to five.'))
    assert said(reply) == ['Weekdays, nine to five.'] and len(script.requests) == 1, script.steps


def the_manifest_reaches_the_session():
    """Entry, services factory, locale, version and the persona's native spec, as the manifest says."""
    script = Script()
    text = (
        (HERE / 'agent.yaml')
        .read_text()
        .replace('id: clinic-desk\n', "id: clinic-desk\napp_version: '2026.09.25'\n")
        .replace(
            '    prompt: prompts/receptionist.md\n',
            '    prompt: prompts/receptionist.md\n    spec: {model: "openai:gpt-9", model_settings: {temperature: 0.1}}\n',
        )
    )
    flow = load_text(text, root=HERE).compile(model=script.model)
    call = flow.open(ClinicState())
    assert isinstance(call.services, Backend) and call.services is not flow.open(ClinicState()).services
    assert flow.entry == 'desk' and flow.version == '2026.09.25' and call.locale is ENGLISH
    agent = flow.nodes['desk'].agent
    assert agent.model is script.model, 'an explicit model wins over the persona'
    assert (agent.model_settings or {}).get('temperature') == 0.1, agent.model_settings
    instructions = str(load_text(text, root=HERE).prompt('desk'))
    assert instructions.startswith("You answer the clinic's phone line."), 'the persona comes first'


def what_the_application_wrote():
    """N13: the clinic's authored files, by kind, next to what the native salon writes for the same guarantees.

    Domain is models, the service and its functions; configuration is the
    two manifests; prompts are prompts. Orchestration (consent, delivery,
    speech, budgets, reconciliation) is none: no class of the clinic's
    derives from core, and it imports nothing of core but the one exception
    its service raises.
    """
    source = (HERE / 'clinic.py').read_text()
    tree = ast.parse(source)
    bases = {ast.unparse(b) for node in ast.walk(tree) if isinstance(node, ast.ClassDef) for b in node.bases}
    assert bases <= {'BaseModel'}, bases
    assert 'from core.' not in source and 'import core\n' not in source
    lines = lambda path: sum(1 for line in path.read_text().splitlines() if line.strip())  # noqa: E731
    inventory = {
        'domain (clinic.py)': lines(HERE / 'clinic.py'),
        f'configuration ({len(PATTERNS)} manifests)': sum(lines(HERE / m) for m in PATTERNS),
        'prompts': sum(lines(p) for p in (HERE / 'prompts').glob('*.md') if p.name != 'broken.md'),
        'orchestration': 0,
    }
    native = Path(__file__).parents[2] / 'use-cases' / 'salon-pipecat-flows' / 'graph_salon'
    if native.exists():
        inventory['native salon orchestration (call, handlers, speech, writes)'] = sum(
            lines(native / name) for name in ('call.py', 'handlers.py', 'speech.py', 'writes.py')
        )
    print('authored, non-blank lines:', inventory)


async def main() -> None:
    definitions_fail_before_any_model()
    await the_clinic_books_and_cancels()
    await only_an_offered_time_can_be_picked()
    await a_no_or_a_change_books_nothing()
    await an_unknown_outcome_is_settled_not_repeated()
    await every_pattern_books_once_on_a_heard_yes()
    await the_model_path_refuses_what_the_direct_path_refuses()
    await ordinary_conversation_reads_nothing()
    the_manifest_reaches_the_session()
    what_the_application_wrote()
    print('authoring ok: manifest, operations, choices, cancel, two patterns, early diagnostics')


if __name__ == '__main__':
    asyncio.run(main())
