"""The ordinary authoring contract, end to end, on the clinic: a manifest, models, prompts and functions, nothing else.

The clinic (`tests/clinic_flow`) is written three ways from one `clinic.py`:
a controlled flow (`agent.yaml`), one conversational agent
(`one_agent.yaml`) and a front desk handing booking to a specialist
(`handoff.yaml`). Each check drives the public boundary (`core.load`,
`compile`, `open`, `text_turn`, `acknowledge`) with a scripted model where a
provider would be, and reads the backend, the receipts and the evidence.

What is checked, against session 3's acceptance list:

* a bad manifest fails before any model is built, naming the step or operation;
* a delivered read-back and a yes make one booking and its confirmation, in
  every pattern; an unheard read-back makes none;
* only an offered time can be picked;
* a no, a change, a failed receipt or an unknown outcome completes nothing and
  claims nothing; an unknown outcome is settled, never repeated;
* taking the booking back runs the real cancel, with its own read-back and receipt;
* ordinary conversation pays for no reading;
* the manifest's entry, services factory, locale, version and persona spec
  reach the session;
* what the application wrote, counted against the native Pipecat salon (N13).
"""

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

import core
from core.flowspec import load as load_text
from core.locale import ENGLISH
from tests.clinic_flow import clinic
from tests.clinic_flow.clinic import Backend, ClinicState

HERE = Path(__file__).parent / 'clinic_flow'
PATTERNS = ('agent.yaml', 'one_agent.yaml', 'handoff.yaml')


class Script:
    """The scripted provider: each agent request takes the next move; each reading returns its label.

    A move is `('collect', values)`, `('call', tool, args)` or `('say', words)`.
    A request with no move left says it did not catch that, as a model would.
    """

    def __init__(self) -> None:
        self.labels = {'confirmation': 'unclear', 'take_back': 'keep'}
        self.moves: list[tuple[Any, ...]] = []
        self.readings: list[str] = []
        self.agent_requests = 0
        self.model = FunctionModel(self.reading, stream_function=self.agent)

    def reading(self, messages: Any, info: AgentInfo) -> ModelResponse:
        wanted = info.model_request_parameters.output_object
        key = (
            'take_back'
            if wanted is not None and 'take_back' in json.dumps(wanted.json_schema)
            else 'confirmation'
        )
        self.readings.append(key)
        return ModelResponse(parts=[TextPart(json.dumps({key: self.labels[key]}))])

    async def agent(self, messages: Any, info: AgentInfo):
        self.agent_requests += 1
        if not self.moves:
            yield 'Sorry, could you say that again?'
            return
        kind, *rest = self.moves.pop(0)
        if kind == 'collect':
            yield {0: DeltaToolCall(name='collect', json_args=json.dumps({'values': rest[0]}))}
        elif kind == 'call':
            yield {0: DeltaToolCall(name=rest[0], json_args=json.dumps(rest[1]))}
        else:
            yield rest[0]


def opened(
    manifest: str, script: Script, backend: Backend | None = None
) -> tuple[core.StepFlow, core.FlowCall]:
    flow = core.load(HERE / manifest).compile(model=script.model)
    return flow, flow.open(ClinicState(), services=backend if backend is not None else Backend())


async def turn(
    flow, call, script: Script, words: str, *moves: tuple[Any, ...], heard: bool = True
) -> core.TextReply:
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

    def problems(text: str) -> list[str]:
        return load_text(text, root=HERE).problems()

    for broken, expected in (
        (base.replace('id: slot\n', 'id: patient\n', 1), "step id 'patient' is used more than once"),
        (
            base.replace('function: clinic.py:book', 'function: nowhere.py:book'),
            "'nowhere.py' does not exist",
        ),
        (
            base.replace('args: {patient: patient, slot: slot}', 'args: {patient: patient}'),
            "'slot' is not bound",
        ),
        (
            base.replace('args: {patient: patient, slot: slot}', 'args: {patient: slot, slot: slot}'),
            'different kinds',
        ),
        (
            base.replace('assign: {reference: reference}', 'assign: {slot: slot}'),
            'a receipt may not overwrite',
        ),
        (base.replace('assign: {reference: reference}', 'assign: {reference: code}'), "'code', which Booked"),
        (base.replace('succeeds: {status: booked}', 'succeeds: {state: booked}'), "succeeds on 'state'"),
        (base.replace('    readback: clinic.py:booking_words\n', '', 1), 'needs a readback function'),
        (base.replace('then: done, on_error', 'then: nowhere, on_error'), "'nowhere', which is not a step"),
        (base.replace('prompt: prompts/slot.md', 'prompt: prompts/broken.md'), "Field 'nickname' not found"),
        (
            base.replace('choices: {slot: clinic.py:free_times}', 'choices: {patient: clinic.py:free_times}'),
            'does not ask for',
        ),
        (
            base.replace(
                'agent: receptionist, prompt: prompts/patient.md', 'agent: nurse, prompt: prompts/patient.md'
            ),
            "'nurse'",
        ),
        (
            base.replace('entry: ', 'entry: ').replace(
                "app_version: '2026.09.25'", "app_version: '2026.09.25'\nentry: lobby"
            ),
            "'lobby' is not a step",
        ),
        (
            base.replace(
                '  - {id: confirm, kind: confirm, operation: book, then: book, on_reject: slot, on_revise: slot}\n',
                '',
            ),
            'no confirm step for',
        ),
    ):
        found = problems(broken)
        assert any(expected in problem for problem in found), (expected, found)
    for broken, expected in (
        (base.replace('version: 1', 'version: 2', 1), 'version'),
        (base.replace('kind: confirm', 'kind: confirmation', 1), 'confirmation'),
        (base.replace('fields: [patient]}', 'fields: [patient], colour: red}'), 'colour'),
    ):
        try:
            load_text(broken, root=HERE)
        except ValidationError as error:
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


async def the_controlled_clinic_books_and_takes_back():
    """Offer two ids, read one back, book it on a heard yes; then take it back through the real cancel."""
    script = Script()
    flow, call = opened('agent.yaml', script)
    backend = call.services
    reply = await turn(flow, call, script, 'P-1234.', ('collect', {'patient': 'P-1234'}))
    assert said(reply) == ['I have Tuesday at 9:00 AM or Tuesday at 10:00 AM. Which works for you?'], said(
        reply
    )
    assert call._offered == {'slot': clinic.TIMES}, call._offered
    reply = await turn(flow, call, script, 'Ten, please.', ('collect', {'slot': '1000'}))
    assert said(reply) == ['Shall I book Tuesday at 10:00 AM for P-1234?'] and backend.calls == 0
    script.labels['confirmation'] = 'accept'
    reply = await turn(flow, call, script, 'Yes.')
    assert said(reply) == ['Booked for Tuesday at 10:00 AM, reference CL001.', 'Anything else?'], said(reply)
    assert backend.bookings == {'CL001': ('P-1234', '1000')} and call.state.facts.reference == 'CL001'
    assert call.state.outcome == 'completed' and [e.operation for e in call.evidence] == ['book']

    readings = len(script.readings)
    script.labels['take_back'] = 'withdraw'
    reply = await turn(flow, call, script, 'Actually, cancel it.')
    assert script.readings[readings:] == ['take_back'], script.readings
    assert said(reply) == ['Shall I cancel booking CL001?'] and backend.bookings, (
        'nothing is undone before a yes'
    )
    reply = await turn(flow, call, script, 'Yes, cancel it.')
    assert said(reply) == ['Booking CL001 is cancelled.', 'Nothing is booked now. Goodbye.'], said(reply)
    assert backend.bookings == {} and call.state.outcome == 'cancelled'
    assert [e.operation for e in call.evidence] == ['book', 'cancel'], 'the booking stays on record'


async def only_an_offered_time_can_be_picked():
    """9:00 is taken, so only 10:00 is offered; a model recording 9:00 anyway is refused and nothing is kept."""
    script = Script()
    backend = Backend()
    backend.bookings['CL900'] = ('P-9999', '0900')
    flow, call = opened('agent.yaml', script, backend)
    reply = await turn(flow, call, script, 'P-1234.', ('collect', {'patient': 'P-1234'}))
    assert said(reply) == ['I have Tuesday at 10:00 AM. Which works for you?'], said(reply)
    await turn(flow, call, script, 'Nine.', ('collect', {'slot': '0900'}), ('say', 'Nine is taken, sorry.'))
    assert call.state.facts.slot is None and call.state.agent == 'slot', call.state


async def a_no_or_a_change_books_nothing():
    for label, question in (('reject', 'I have Tuesday'), ('revise', 'I have Tuesday')):
        script = Script()
        flow, call = opened('agent.yaml', script)
        await turn(flow, call, script, 'P-1234.', ('collect', {'patient': 'P-1234'}))
        await turn(flow, call, script, 'Ten.', ('collect', {'slot': '1000'}))
        script.labels['confirmation'] = label
        reply = await turn(flow, call, script, 'No, not that.')
        assert call.services.calls == 0 and call.state.agent == 'slot', (label, call.state)
        assert said(reply)[-1].startswith(question), (label, said(reply))
        if label == 'revise':
            assert call.state.facts.slot is None, 'a revision forgets the answer it revises'


async def a_receipt_that_says_no_completes_nothing():
    """The diary answers "full": the receipt is filed, assigns nothing, claims nothing, and the flow fails."""
    script = Script()
    backend = Backend()
    backend.full = True
    flow, call = opened('agent.yaml', script, backend)
    await turn(flow, call, script, 'P-1234.', ('collect', {'patient': 'P-1234'}))
    await turn(flow, call, script, 'Ten.', ('collect', {'slot': '1000'}))
    script.labels['confirmation'] = 'accept'
    reply = await turn(flow, call, script, 'Yes.')
    assert said(reply) == ['Nothing was booked. Please call again.'], said(reply)
    assert call.state.facts.reference == '' and call.state.outcome == 'failed'
    [filed] = call.evidence
    assert filed.receipt['status'] == 'full', 'the answer is on record, and it is not a booking'


async def an_unknown_outcome_is_settled_not_repeated():
    """The diary booked and then stopped answering: pending, not failed; the retry reconciles, never books again."""
    script = Script()
    backend = Backend()
    backend.failing = True
    flow, call = opened('agent.yaml', script, backend)
    await turn(flow, call, script, 'P-1234.', ('collect', {'patient': 'P-1234'}))
    await turn(flow, call, script, 'Ten.', ('collect', {'slot': '1000'}))
    script.labels['confirmation'] = 'accept'
    reply = await turn(flow, call, script, 'Yes.')
    assert said(reply) == ['We will check the diary and call you back.'] and call.state.outcome == 'pending'
    assert backend.calls == 1 and not call.evidence and call.state.facts.reference == ''
    book = book_of(flow)
    receipt = await book.run(call, book.args_type(patient='P-1234', slot='1000'))
    assert backend.calls == 1, 'settling asked the diary what it holds; it did not book again'
    assert receipt.reference == 'CL001' and call.state.facts.reference == 'CL001'
    assert [e.receipt['reference'] for e in call.evidence] == ['CL001']


async def every_pattern_books_once_on_a_heard_yes():
    """The same `clinic.book`, in all three patterns: an unheard read-back books nothing, a heard one books once."""
    fill = (('call', 'options', {}), ('collect', {'patient': 'P-1234', 'slot': '1000'}), ('call', 'book', {}))
    for manifest in PATTERNS:
        for heard in (False, True):
            script = Script()
            flow, call = opened(manifest, script)
            assert book_of(flow).function is clinic.book, 'one domain function, whatever the pattern'
            if manifest == 'agent.yaml':
                await turn(flow, call, script, 'P-1234.', ('collect', {'patient': 'P-1234'}))
                reply = await turn(flow, call, script, 'Ten.', ('collect', {'slot': '1000'}), heard=heard)
            elif manifest == 'one_agent.yaml':
                reply = await turn(flow, call, script, 'P-1234, Tuesday at ten.', *fill, heard=heard)
            else:
                handed = ('call', 'hand_over', {'to': 'booking', 'because': 'an appointment'})
                reply = await turn(flow, call, script, 'P-1234, Tuesday at ten.', handed, *fill, heard=heard)
            assert said(reply)[-1] == 'Shall I book Tuesday at 10:00 AM for P-1234?', (manifest, said(reply))
            script.labels['confirmation'] = 'accept'
            requests = script.agent_requests
            reply = await turn(flow, call, script, 'Yes.')
            booked = call.services.bookings
            if not heard:
                assert booked == {}, (manifest, 'a yes to a read-back nobody heard booked')
                continue
            assert booked == {'CL001': ('P-1234', '1000')}, (manifest, booked)
            assert said(reply)[0] == 'Booked for Tuesday at 10:00 AM, reference CL001.', (
                manifest,
                said(reply),
            )
            assert script.agent_requests == requests, (manifest, 'the heard yes is carried out by code')


async def the_model_path_refuses_what_the_direct_path_refuses():
    """One agent: the model calling `book` again cannot skip the read-back; a full diary claims nothing; unknown is said as unknown."""
    fill = (('call', 'options', {}), ('collect', {'patient': 'P-1234', 'slot': '1000'}), ('call', 'book', {}))
    script = Script()
    flow, call = opened('one_agent.yaml', script)
    await turn(flow, call, script, 'P-1234, ten.', *fill, heard=False)
    reply = await turn(flow, call, script, 'Go on, book it.', ('call', 'book', {}))
    assert call.services.calls == 0 and said(reply)[-1].startswith('Shall I book'), said(reply)

    for trouble in ('full', 'failing'):
        script = Script()
        backend = Backend()
        setattr(backend, trouble, True)
        flow, call = opened('one_agent.yaml', script, backend)
        await turn(flow, call, script, 'P-1234, ten.', *fill)
        script.labels['confirmation'] = 'accept'
        reply = await turn(flow, call, script, 'Yes.', ('say', 'Let me see what happened there.'))
        words = ' '.join(said(reply))
        assert 'Booked for' not in words and call.state.facts.reference == '', (trouble, words)
        if trouble == 'failing':
            assert "couldn't confirm" in words, words
            await turn(flow, call, script, 'Yes, book it.', ('call', 'book', {}), ('say', 'I cannot yet.'))
            assert backend.calls == 1, 'an unknown outcome with nothing to settle it is not retried'


async def ordinary_conversation_reads_nothing():
    script = Script()
    flow, call = opened('one_agent.yaml', script)
    reply = await turn(flow, call, script, 'When are you open?', ('say', 'Weekdays, nine to five.'))
    assert said(reply) == ['Weekdays, nine to five.'] and script.readings == [], script.readings


def the_manifest_reaches_the_session():
    """Entry, services factory, locale, version and the persona's native spec, as the manifest says."""
    script = Script()
    spec = core.load(HERE / 'agent.yaml')
    flow = spec.compile(model=script.model)
    call = flow.open(ClinicState())
    assert isinstance(call.services, Backend) and call.services is not flow.open(ClinicState()).services
    assert flow.entry == 'patient' and flow.version == '2026.09.25' and call.locale is ENGLISH
    text = (
        (HERE / 'agent.yaml')
        .read_text()
        .replace(
            '    prompt: prompts/receptionist.md\n',
            '    prompt: prompts/receptionist.md\n    spec: {model: "openai:gpt-9", model_settings: {temperature: 0.1}}\n',
        )
    )
    tuned = load_text(text, root=HERE).compile(model=script.model)
    agent = tuned.nodes['patient'].agent
    assert agent.model is script.model, 'an explicit model wins over the persona'
    assert (agent.model_settings or {}).get('temperature') == 0.1, agent.model_settings
    instructions = str(load_text(text, root=HERE).prompt('slot'))
    assert instructions.startswith("You answer the clinic's phone line."), 'the persona comes first'


def what_the_application_wrote():
    """N13: the clinic's authored files, by kind, next to what the native salon writes for the same guarantees.

    Domain is models, the service and its functions; configuration is the
    three manifests; prompts are prompts. Orchestration (consent, delivery,
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
        'configuration (3 manifests)': sum(lines(HERE / m) for m in PATTERNS),
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
    await the_controlled_clinic_books_and_takes_back()
    await only_an_offered_time_can_be_picked()
    await a_no_or_a_change_books_nothing()
    await a_receipt_that_says_no_completes_nothing()
    await an_unknown_outcome_is_settled_not_repeated()
    await every_pattern_books_once_on_a_heard_yes()
    await the_model_path_refuses_what_the_direct_path_refuses()
    await ordinary_conversation_reads_nothing()
    the_manifest_reaches_the_session()
    what_the_application_wrote()
    print('authoring ok: manifest, operations, choices, take-back, three patterns, early diagnostics')


if __name__ == '__main__':
    asyncio.run(main())
