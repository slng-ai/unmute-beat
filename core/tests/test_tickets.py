"""The reuse proof: a support desk with no phone, date, appointment or slot, on the public loader and engine.

The same guarantees the salon and the clinic get, with the desk's own names,
its own Spanish words and its own durable backend (`ticket_flow/tickets.py`):

* R1: an informational step answers with no identity and no write;
* R2: a resolve is read back, and only a heard yes resolves it, once;
* R3: a revision someone else moved on is an authoritative refusal: nothing
  changes and nothing is claimed;
* R4: an answer about another ticket, and an answer lost after the commit,
  leave the intent unknown; the next turn reconciles it by its id, with
  nothing sent again;
* R5: resolve then reopen: a goodbye can recap only what still stands, in
  the desk's words; a reference nobody issued is refused;
* R6: the desk step is rendered: after a lookup alone, a claimed
  resolution is never said, whatever its words, and a goodbye has nothing
  to recap;
* R7: a fresh process reconciles what a killed one did: after its commit,
  found and not sent again; before it, sent again as the same intent, once;
* R8: every field and operation renamed, and nothing in core changed;
* R9: a receipt cut off is said again only while it stands, and never re-sent;
* R10: its write audit is the salon's envelope, and names the desk's own effect.
* R12: a pick counts only against an offer made for the state an update ends in,
  and the desk's validators read the call's clock from core's validation context.
* R15: what a rendered step says about an offer is exactly what can still be picked from it.
* R16: only the agent's `decline` turns a read-back down; a question in reply leaves it waiting.
* R17: a receipt said again on request, past the automatic limit, only while it stands, with nothing sent.
* R18: after a change, the resources it named are offered again by the desk's own function.
* R19: a read-back with no playback evidence is withdrawn once, and stays withdrawn; the caller can go on.

Scripted models prove runtime properties, not language understanding.
"""

import asyncio
import importlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

import core
from tests.test_nodes import Script
from tests.ticket_flow.tickets import Desk, DeskState
from tests.ticket_flow.worker import TO_READ_BACK, to_read_back

HERE = Path(__file__).parent / 'ticket_flow'
SPANISH_GOODBYE = 'Gracias por llamar al soporte. Hasta pronto.'


def desk_in(folder: Path) -> Desk:
    desk = Desk(folder / 'desk.db')
    desk.seed('ACC-1234', 'T-101')
    return desk


def opened(desk: Desk, manifest: Path = HERE / 'agent.yaml', **open_with):
    script = Script()
    flow = core.load(manifest).compile(model=script.model)
    call = flow.open(DeskState(), services=desk, **open_with)
    script.call = call
    return script, flow, call


async def turn(flow, call, script: Script, words: str, *moves) -> list[str]:
    script.moves = list(moves)
    reply = await core.text_turn(flow, call, words)
    call.acknowledge(*(s.id for s in reply.segments))
    return [s.text for s in reply.segments]


async def an_informational_step_needs_nothing(folder: Path):
    """R1."""
    script, flow, call = opened(desk_in(folder))
    said = await turn(
        flow, call, script, '¿Qué horario tenéis?', ('say', 'De nueve a cinco, de lunes a viernes.')
    )
    assert said == ['De nueve a cinco, de lunes a viernes.'] and call.state.agent == 'faq', said
    assert not call.intents and not call.evidence and call.identity() == ''


async def a_heard_yes_resolves_once(folder: Path):
    """R2."""
    desk = desk_in(folder)
    script, flow, call = opened(desk)
    said = await to_read_back(flow, call, script)
    assert said[-2:] == [
        'Tengo el ticket T-101.',
        '¿Marco el ticket T-101 como resuelto: la impresora ya imprime?',
    ], said
    assert desk.ticket('T-101') == (1, 'open') and desk.sent() == 0
    said = await turn(flow, call, script, 'Sí.', ('call', 'resolve_ticket', {}))
    assert said == ['Ticket T-101 resuelto. ¿Algo más?'], said
    assert desk.ticket('T-101') == (2, 'resolved') and desk.sent() == 1
    [standing] = call.effective()
    assert standing.resource == 'T-101' and call.state.facts.revision == 2


async def a_stale_revision_changes_nothing(folder: Path):
    """R3."""
    desk = desk_in(folder)
    script, flow, call = opened(desk)
    await to_read_back(flow, call, script)
    desk.edit('T-101')
    said = await turn(
        flow,
        call,
        script,
        'Sí.',
        ('call', 'resolve_ticket', {}),
        ('say', 'Alguien lo ha cambiado. Lo miro otra vez.'),
    )
    assert desk.ticket('T-101') == (2, 'open'), 'a stale revision changed the ticket'
    assert [i.status for i in call.intents if i.operation == 'resolve_ticket'] == ['rejected']
    assert not call.effective() and not any('resuelto.' in s for s in said), said


async def a_doubtful_answer_is_reconciled_by_intent(folder: Path):
    """R4."""
    for fault in ('wrong_ticket', 'lose_answer'):
        desk = desk_in(folder / fault)
        script, flow, call = opened(desk)
        await to_read_back(flow, call, script)
        setattr(desk, fault, True)
        said = await turn(flow, call, script, 'Sí.', ('call', 'resolve_ticket', {}))
        assert said == [call.locale.unsettled], (fault, said)
        [intent] = [i for i in call.intents if i.operation == 'resolve_ticket']
        assert intent.status == 'unknown' and desk.sent() == 1, (fault, intent)
        desk.wrong_ticket = False
        said = await turn(flow, call, script, '¿Ya está?')
        assert said == ['Ticket T-101 resuelto. ¿Algo más?'], (fault, said)
        assert desk.sent() == 1 and [i.status for i in call.intents if i.id == intent.id] == ['succeeded'], (
            fault
        )


async def only_what_stands_is_recapped(folder: Path):
    """R5."""
    desk = desk_in(folder)
    script, flow, call = opened(desk)
    await to_read_back(flow, call, script)
    await turn(flow, call, script, 'Sí.', ('call', 'resolve_ticket', {}))
    resolved = call.effective()[0].intent[:8]
    said = await turn(flow, call, script, 'Uy, ha vuelto a fallar. Reábrelo.', ('call', 'reopen_ticket', {}))
    assert said == ['¿Reabro el ticket T-101?'], said
    await turn(flow, call, script, 'Sí.', ('call', 'reopen_ticket', {}))
    assert desk.ticket('T-101') == (3, 'open')
    [standing] = call.effective()
    reopened = standing.intent[:8]
    said = await turn(
        flow,
        call,
        script,
        'Gracias, adiós.',
        ('call', 'goodbye', {'recap': [resolved]}),
        ('call', 'goodbye', {'recap': [reopened]}),
    )
    assert said == [f'El ticket T-101 vuelve a estar abierto. {SPANISH_GOODBYE}'], said
    assert call.ended and call.departed

    desk = desk_in(folder / 'by_words')
    script, flow, call = opened(desk)
    await to_read_back(flow, call, script)
    await turn(flow, call, script, 'Sí.', ('call', 'resolve_ticket', {}))
    requests = len(script.steps)
    said = await turn(
        flow, call, script, 'Adiós.', ('call', 'goodbye', {'recap': ['El ticket T-101 queda resuelto.']})
    )
    assert said == [f'El ticket T-101 queda resuelto. {SPANISH_GOODBYE}'], (
        'a standing recap by its words',
        said,
    )
    assert len(script.steps) == requests + 1, 'the recap by its own words cost a retry'
    desk = desk_in(folder / 'paraphrase')
    script, flow, call = opened(desk)
    await to_read_back(flow, call, script)
    await turn(flow, call, script, 'Sí.', ('call', 'resolve_ticket', {}))
    said = await turn(
        flow,
        call,
        script,
        'Adiós.',
        ('call', 'goodbye', {'recap': ['Todo arreglado y cerrado para siempre.']}),
        ('call', 'goodbye', {}),
    )
    assert said == [SPANISH_GOODBYE], ('a recap in words nobody rendered was said', said)

    desk = desk_in(folder / 'invented')
    script, flow, call = opened(desk)
    said = await turn(
        flow, call, script, 'Adiós.', ('call', 'goodbye', {'recap': ['0000abcd']}), ('call', 'goodbye', {})
    )
    assert said == [SPANISH_GOODBYE], said


async def a_lookup_backs_no_claim(folder: Path):
    """R6."""
    desk = desk_in(folder)
    script, flow, call = opened(desk)
    moves = [*TO_READ_BACK[:3], ('say', 'He resuelto el ticket T-101.'), ('call', 'wait', {})]
    said = await turn(flow, call, script, 'Ticket T-101, cuenta ACC-1234.', *moves)
    assert said == ['Tengo el ticket T-101.', call.locale.pardon], ('a rendered step said model words', said)
    assert [e.operation for e in call.evidence] == ['look_up_ticket'] and not call.effective()
    said = await turn(flow, call, script, 'Vale, adiós.', ('call', 'goodbye', {}))
    assert said == [SPANISH_GOODBYE], said


async def a_fresh_process_reconciles(folder: Path):
    """R7."""
    for mode, code in (('after', 3), ('before', 4)):
        place = folder / mode
        place.mkdir()
        desk = desk_in(place)
        died = subprocess.run(
            [sys.executable, '-m', 'tests.ticket_flow.worker', str(place / 'desk.db'), mode],
            cwd=Path(__file__).parent.parent,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert died.returncode == code, (mode, died.returncode, died.stderr[-2000:])
        [lost] = desk.unresolved()
        assert lost.operation == 'resolve_ticket', lost
        script, flow, call = opened(desk, intents=[lost])
        assert [i.status for i in call.intents] == ['unknown'], mode
        if mode == 'after':
            assert desk.ticket('T-101') == (2, 'resolved') and desk.sent() == 1
            said = await turn(flow, call, script, 'Hola, ¿se resolvió?')
            assert said == ['Ticket T-101 resuelto. ¿Algo más?'], said
            assert desk.sent() == 1, 'the fresh process sent the intent again'
        else:
            assert desk.ticket('T-101') == (1, 'open') and desk.sent() == 0
            await to_read_back(flow, call, script)
            await turn(flow, call, script, 'Sí.', ('call', 'resolve_ticket', {}))
            assert desk.ticket('T-101') == (2, 'resolved') and desk.sent() == 1
            assert [e.intent for e in call.evidence if e.operation == 'resolve_ticket'] == [lost.id], (
                'the retry was not the same intent'
            )


async def a_cut_receipt_is_said_again_only_while_it_stands(folder: Path):
    """R9: a receipt the caller cut off is said again, with nothing sent; once superseded, it is not."""
    for superseded in (False, True):
        desk = desk_in(folder / ('superseded' if superseded else 'standing'))
        script, flow, call = opened(desk)
        await to_read_back(flow, call, script)
        script.moves = [('call', 'resolve_ticket', {})]
        reply = await core.text_turn(flow, call, 'Sí.')
        receipt = next(s for s in reply.segments if s.text.startswith('Ticket T-101 resuelto'))
        call.acknowledge(receipt.id, heard='')
        if superseded:
            [standing] = call.effective()
            call.note_evidence(
                'reopen_ticket',
                standing.args,
                {**standing.receipt, 'status': 'open'},
                intent='e' * 32,
                resource='T-101',
                kind=standing.kind,
                version=(standing.version or 0) + 1,
            )
        said = await turn(flow, call, script, '¿Perdona?', ('call', 'wait', {}))
        assert desk.sent() == 1, 'saying a receipt again sent the write again'
        expected = [call.locale.pardon] if superseded else ['Ticket T-101 resuelto. ¿Algo más?']
        assert said == expected, (superseded, said)


async def the_audit_names_the_backend_effect(folder: Path):
    """R10: the write audit the desk's resolve leaves is the same envelope the salon's is, and names the effect.

    Its intent is the key the desk filed the effect under; the arguments its
    read-back named were captured when it was asked; a text run's delivery is
    simulated, so it is labelled so and never written as transport playback.
    """
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    desk = desk_in(folder)
    script, flow, call = opened(desk)
    await to_read_back(flow, call, script)
    await turn(flow, call, script, 'Sí.', ('call', 'resolve_ticket', {}))
    audits = [
        json.loads(str(s.attributes['write.audit']))
        for s in exporter.get_finished_spans()
        if s.name == 'write' and s.attributes and 'write.audit' in s.attributes
    ]
    [audit] = [a for a in audits if a['action'] == 'resolve_ticket']
    assert desk.effect(audit['intent']) == audit['receipt'], ('the audit names another effect', audit)
    assert audit['operation'] == audit['intent'] and audit['proposal_args'] == audit['args'], audit
    assert audit['needs_approval'] is True and audit['changes'] is True and audit['status'] == 'ok', audit
    assert audit['playback'] is None and audit['simulated_delivery'], (
        'text delivery is not transport playback',
        audit,
    )
    assert not any(k in json.dumps(audit) for k in ('phone', 'slot', 'appointment', 'booking')), audit


RENAMES = {
    'account_id': 'customer_ref',
    'ticket_id': 'case_ref',
    'revision': 'version',
    'resolve_ticket': 'close_case',
    'reopen_ticket': 'reopen_case',
    'look_up_ticket': 'fetch_case',
}


async def renamed_it_still_holds(folder: Path):
    """R8: a copy of the desk with every field and operation renamed runs the same call, with core untouched."""
    copy = folder / 'renamed'
    shutil.copytree(HERE, copy / 'ticket_flow', ignore=shutil.ignore_patterns('__pycache__'))
    for path in (copy / 'ticket_flow').rglob('*'):
        if path.suffix in ('.py', '.yaml', '.md'):
            text = path.read_text()
            for old, new in RENAMES.items():
                text = text.replace(old, new)
            path.write_text(text)
    sys.path.insert(0, str(copy))
    try:
        renamed_module = importlib.import_module('ticket_flow.tickets')
        desk = renamed_module.Desk(folder / 'renamed.db')
        desk.seed('ACC-1234', 'T-101')
        script = Script()
        flow = core.load(copy / 'ticket_flow' / 'agent.yaml').compile(model=script.model)
        call = flow.open(renamed_module.DeskState(), services=desk)
        script.call = call
        text = json.dumps(TO_READ_BACK)
        for old, new in RENAMES.items():
            text = text.replace(old, new)
        renamed = [tuple(move) for move in json.loads(text)]
        said = await turn(flow, call, script, 'Ticket T-101, cuenta ACC-1234, ya funciona.', *renamed)
        assert said[-1] == '¿Marco el ticket T-101 como resuelto: la impresora ya imprime?', said
        said = await turn(flow, call, script, 'Sí.', ('call', 'close_case', {}))
        assert said == ['Ticket T-101 resuelto. ¿Algo más?'] and desk.ticket('T-101') == (2, 'resolved'), said
    finally:
        sys.path.remove(str(copy))
        for name in [m for m in sys.modules if m == 'ticket_flow' or m.startswith('ticket_flow.')]:
            del sys.modules[name]


async def picks_rest_only_on_what_their_offer_read(folder: Path):
    """R11 (session 4d, defect 4): two choices that rest on different fields, with no booking anywhere.

    A new category takes the resolutions on offer and the one picked, and
    keeps the ticket. Another account takes both: they were offered to
    someone else. `after:` must name another field of the step.
    """
    desk = desk_in(folder)
    desk.seed('ACC-1234', 'T-102')
    script, flow, call = opened(desk, HERE / 'choices.yaml')
    await turn(
        flow,
        call,
        script,
        'Cuenta ACC-1234, es de facturación.',
        ('call', 'collect', {'values': {'account_id': 'ACC-1234', 'category': 'billing'}}),
        ('call', 'options', {}),
        ('say', '¿Qué ticket y qué solución?'),
    )
    assert set(call._offers) == {'ticket_id', 'resolution'}, call._offers
    await turn(
        flow,
        call,
        script,
        'El T-102, con un reembolso.',
        ('call', 'collect', {'values': {'ticket_id': 'T-102', 'resolution': 'refund'}}),
        ('say', 'De acuerdo.'),
    )
    facts = call.state.facts
    assert (facts.ticket_id, facts.resolution) == ('T-102', 'refund'), facts
    await turn(
        flow,
        call,
        script,
        'Perdona, es de acceso.',
        ('call', 'collect', {'values': {'category': 'access'}}),
        ('say', 'Entendido.'),
    )
    facts = call.state.facts
    assert facts.ticket_id == 'T-102', 'the ticket rests on no field, and a new category took it away'
    assert facts.resolution == '' and 'resolution' not in call._offers, (
        'a billing resolution outlived its category'
    )
    assert 'T-102' in call._offers['ticket_id'], 'the ticket offer went with the category'
    said = await turn(
        flow,
        call,
        script,
        'Con un reembolso.',
        ('call', 'collect', {'values': {'resolution': 'refund'}}),
        ('say', 'Vale.'),
    )
    assert call.state.facts.resolution == '', ('a resolution from the old offer was recorded', said)
    await turn(
        flow,
        call,
        script,
        'Mejor la cuenta ACC-5678.',
        ('call', 'collect', {'values': {'account_id': 'ACC-5678'}}),
        ('say', 'Vale.'),
    )
    facts = call.state.facts
    assert facts.ticket_id is None and not call._offers, 'a pick offered to another account was kept'

    broken = HERE / '_choices_broken.yaml'
    text = (HERE / 'choices.yaml').read_text()
    for change, expected in (
        (('after: [category]', 'after: [priority]'), "after 'priority'"),
        (('after: [category]', 'after: [resolution]'), "after 'resolution'"),
    ):
        broken.write_text(text.replace(*change))
        try:
            core.load(broken).compile(model='test')
        except core.FlowSpecError as error:
            assert expected in str(error), str(error)
        else:
            raise AssertionError(f'{change} was accepted')
        finally:
            broken.unlink()


async def picks_follow_the_state_they_end_in(folder: Path):
    """R12 (session 4e): a pick counts only against an offer made for the state the update ends in.

    The ticket, the category offered for it and the resolution offered for
    that category are all choices (`chained.yaml`). A: a new category takes
    the old resolution and its offer. B: a new category and a resolution in
    the same update: the resolution was offered for the old category, so it
    is refused, whatever else changes with it, and with a plain category
    (`choices.yaml`) too. A new ticket takes the category, and with it the
    resolution. A field nothing rests on keeps every pick, and an update
    that fails validation changes nothing.
    """
    desk = desk_in(folder)
    desk.seed('ACC-1234', 'T-102')
    script, flow, call = opened(desk, HERE / 'chained.yaml')

    async def pick(words: str, values: dict, *more) -> list[str]:
        return await turn(flow, call, script, words, ('call', 'collect', {'values': values}), *more)

    await pick(
        'Cuenta ACC-1234.', {'account_id': 'ACC-1234'}, ('call', 'options', {}), ('say', '¿Qué ticket?')
    )
    await pick(
        'El T-101.', {'ticket_id': 'T-101'}, ('call', 'options', {}), ('say', '¿Facturación o acceso?')
    )
    await pick('Facturación.', {'category': 'billing'}, ('call', 'options', {}), ('say', '¿Qué solución?'))
    await pick('Un reembolso.', {'resolution': 'refund'}, ('say', 'Vale.'))
    facts = call.state.facts
    assert (facts.category, facts.resolution) == ('billing', 'refund'), facts

    await pick('No, es de acceso.', {'category': 'access'}, ('say', 'Entendido.'))
    facts = call.state.facts
    assert facts.category == 'access', facts
    assert facts.resolution == '' and 'resolution' not in call._offers, (
        'A: a resolution offered for billing outlived its category, itself a choice',
        facts,
        call._offers,
    )

    await pick('Facturación otra vez.', {'category': 'billing'}, ('call', 'options', {}), ('say', '¿Cuál?'))
    assert 'refund' in call._offers['resolution'], call._offers
    await pick(
        'Acceso, con un reembolso.',
        {'category': 'access', 'resolution': 'refund'},
        ('say', 'Vale.'),
    )
    facts = call.state.facts
    assert facts.category == 'access', ('the category was lost beside a refused resolution', facts)
    assert facts.resolution == '', ('B: a resolution offered for billing came in with access', facts)

    await pick('Facturación.', {'category': 'billing'}, ('call', 'options', {}), ('say', '¿Cuál?'))
    await pick('Un abono.', {'resolution': 'credit'}, ('say', 'Vale.'))
    await pick('Es sobre la impresora.', {'account_id': 'ACC-1234'}, ('say', 'Vale.'))
    facts = call.state.facts
    assert (facts.ticket_id, facts.category, facts.resolution) == ('T-101', 'billing', 'credit'), (
        'a value that did not change took picks away',
        facts,
    )
    offered = {name: dict(options) for name, options in call._offers.items()}
    try:
        call.collect(('category', 'resolution'), ticket_id='T-1', category='access')
    except ValidationError:
        pass
    else:
        raise AssertionError('an invalid candidate was accepted')
    assert call.state.facts == facts and call._offers == offered, (
        'an update that failed validation changed something',
        call.state.facts,
        call._offers,
    )

    await pick('Perdona, es el T-102.', {'ticket_id': 'T-102'}, ('say', 'Vale.'))
    facts = call.state.facts
    assert (facts.ticket_id, facts.category, facts.resolution) == ('T-102', '', ''), (
        'a pick resting on a pick that went outlived it',
        facts,
    )
    assert set(call._offers) == {'ticket_id'}, call._offers

    desk = desk_in(folder / 'standing')
    script, flow, call = opened(desk, HERE / 'choices.yaml')
    await pick(
        'Cuenta ACC-1234, facturación.',
        {'account_id': 'ACC-1234', 'category': 'billing'},
        ('call', 'options', {}),
        ('say', '¿Qué solución?'),
    )
    await pick(
        'Acceso, con un reembolso.',
        {'category': 'access', 'resolution': 'refund'},
        ('say', 'Vale.'),
    )
    facts = call.state.facts
    assert (facts.category, facts.resolution) == ('access', ''), (
        'B, plain category: a resolution offered for billing came in with access',
        facts,
    )

    clock = lambda: datetime(2026, 9, 30, 23, 50)  # noqa: E731
    flow = core.load(HERE / 'agent.yaml').compile(model=Script().model)
    call = flow.open(DeskState(follow_up='mañana'), services=desk, clock=clock)
    assert call.state.facts.follow_up == '2026-10-01', ('open validated without the call clock', call.state)
    call.update(follow_up='hoy')
    assert call.state.facts.follow_up == '2026-09-30', call.state.facts
    call.commit(agent=call.state.agent)
    assert call.state.facts.follow_up == '2026-09-30', ('a commit read the date again', call.state.facts)


async def a_rendered_step_offers_in_the_desks_words(folder: Path):
    """R13 (session 4e): choices on a rendered step. The agent reads and chooses; the desk says every word.

    The offers are the choice functions' own words (`say`), in the order the
    step lists them. A model that writes text, a claim or a copy of the
    options it was shown, says nothing. A pick nobody offered gets the
    desk's refusal, then the fresh offer. A pick on offer is read back and
    carried out on a heard yes, as on any step.
    """
    desk = desk_in(folder)
    desk.seed('ACC-1234', 'T-102')
    script, flow, call = opened(desk, HERE / 'rendered_choices.yaml')
    offered = (
        'Veo el ticket T-101, open y el ticket T-102, open. ¿Cuál es? '
        'Puedo dejarlo como un reembolso o un abono en la cuenta. ¿Qué prefiere?'
    )
    said = await turn(
        flow,
        call,
        script,
        'Cuenta ACC-1234, es de facturación.',
        ('call', 'collect', {'values': {'account_id': 'ACC-1234', 'category': 'billing'}}),
        ('call', 'options', {}),
        ('call', 'wait', {}),
    )
    assert said == [offered], said

    said = await turn(
        flow,
        call,
        script,
        'El T-101.',
        ('say', 'Listo, ya está resuelto.'),
        ('say', '{"resolution": {"options": {"refund": "un reembolso"}}}'),
    )
    assert not [s for s in said if 'resuelto' in s or '{' in s], ('model text reached the caller', said)
    assert desk.sent() == 0

    said = await turn(
        flow,
        call,
        script,
        'Con un cambio de contraseña.',
        ('call', 'collect', {'values': {'resolution': 'reset'}}),
        ('call', 'options', {}),
        ('call', 'wait', {}),
    )
    assert said == [f'reset no es una de las opciones que tengo. {offered}'], said

    said = await turn(
        flow,
        call,
        script,
        'El T-101, con un reembolso.',
        ('call', 'collect', {'values': {'ticket_id': 'T-101', 'resolution': 'refund'}}),
        ('call', 'hand_over', {'to': 'look_up', 'because': 'find its revision'}),
        ('call', 'resolve_ticket', {}),
    )
    assert said[-2:] == ['Tengo el ticket T-101.', '¿Marco el ticket T-101 como resuelto: refund?'], said
    said = await turn(flow, call, script, 'Sí.', ('call', 'resolve_ticket', {}))
    assert said == ['Ticket T-101 resuelto. ¿Algo más?'] and desk.ticket('T-101') == (2, 'resolved'), said

    wordless = HERE / '_rendered_wordless.yaml'
    wordless.write_text((HERE / 'rendered_choices.yaml').read_text().replace('ticket_offer', 'open_tickets'))
    try:
        script, flow, call = opened(desk_in(folder / 'by_words'), wordless)
        await turn(
            flow,
            call,
            script,
            'Cuenta ACC-1234.',
            ('call', 'collect', {'values': {'account_id': 'ACC-1234'}}),
            ('call', 'options', {}),
            ('call', 'wait', {}),
        )
        assert 'ticket_id' not in call._offers, 'a rendered step offered choices it had no words for'
    finally:
        wordless.unlink()

    broken = HERE / '_rendered_broken.yaml'
    broken.write_text(
        (HERE / 'rendered_choices.yaml')
        .read_text()
        .replace('speech: rendered', 'speech: rendered\n    guarded: true')
    )
    try:
        core.load(broken).compile(model='test')
    except core.FlowSpecError as error:
        assert 'rendered and guarded' in str(error), str(error)
    else:
        raise AssertionError('a rendered step was allowed to be guarded')
    finally:
        broken.unlink()


async def a_no_is_not_left_undone(folder: Path):
    """R14 (session 4e, live call 7): a goodbye after a read-back the caller heard and turned down.

    The agent read the reply as a no (`decline`, beside `goodbye` in one
    response): that was the caller's choice, not a change left waiting, so
    the goodbye does not say `left_undone`. A read-back the caller never
    heard in full still gets it as they leave. Neither reads a word of the
    reply: what counts is what the agent chose.
    """
    desk = desk_in(folder)
    script, flow, call = opened(desk)
    await to_read_back(flow, call, script)
    requests = len(script.steps)
    said = await turn(
        flow, call, script, 'No, déjalo así. Adiós.', ('calls', [('decline', {}), ('goodbye', {})])
    )
    assert len(script.steps) == requests + 1, ('a no and a goodbye took more than one request', script.steps)
    assert call.locale.left_undone not in ' '.join(said), ('a no was told it was left undone', said)
    assert said[-1].endswith(SPANISH_GOODBYE) and desk.sent() == 0, said

    desk = desk_in(folder / 'standing')
    script, flow, call = opened(desk)
    script.moves = list(TO_READ_BACK)
    await core.text_turn(flow, call, 'Hola, la impresora del ticket T-101 ya funciona. Cuenta ACC-1234.')
    said = await turn(flow, call, script, 'Adiós.', ('call', 'goodbye', {}), ('call', 'goodbye', {}))
    assert call.locale.left_undone in ' '.join(said), ('an unheard read-back left undone was not said', said)


async def a_changed_resource_stays_on_offer(folder: Path):
    """R18 (session 4f): after a change, the resources it named are offered again, by the desk's own function.

    The same rule as the salon's V13, with no booking: `resolve_ticket` binds
    `ticket_id` to its subject and says its receipts are about a `ticket`, so
    after a resolve the desk's `ticket_offer` is asked again, quietly. The
    caller can go on to another of their tickets, or back to this one, with
    no second lookup; a ticket that is not theirs is still refused.
    """
    desk = desk_in(folder)
    desk.seed('ACC-1234', 'T-102')
    script, flow, call = opened(desk, HERE / 'rendered_choices.yaml')
    await turn(
        flow,
        call,
        script,
        'Cuenta ACC-1234, facturación, el T-101 con un reembolso.',
        (
            'calls',
            [('collect', {'values': {'account_id': 'ACC-1234', 'category': 'billing'}}), ('options', {})],
        ),
        ('call', 'collect', {'values': {'ticket_id': 'T-101', 'resolution': 'refund'}}),
        ('call', 'hand_over', {'to': 'look_up', 'because': 'find its revision'}),
        ('call', 'resolve_ticket', {}),
    )
    said = await turn(flow, call, script, 'Sí.', ('call', 'resolve_ticket', {}))
    assert said == ['Ticket T-101 resuelto. ¿Algo más?'], said
    assert set(call._offers['ticket_id']) == {'T-101', 'T-102'}, (
        'the tickets were not offered again',
        dict(call._offers),
    )
    assert call._offers.words('desk', ('ticket_id',), call.turn) == '', 'a quiet refresh was announced'
    await turn(
        flow,
        call,
        script,
        'Es el T-999.',
        ('call', 'collect', {'values': {'ticket_id': 'T-999'}}),
        ('call', 'wait', {}),
    )
    assert call.state.facts.ticket_id == 'T-101', ('a ticket nobody offered was recorded', call.state.facts)
    await turn(
        flow,
        call,
        script,
        'Y el T-102.',
        ('call', 'collect', {'values': {'ticket_id': 'T-102'}}),
        ('call', 'wait', {}),
    )
    assert call.state.facts.ticket_id == 'T-102', (
        'a ticket on the account needed a second lookup',
        call.state.facts,
    )


async def a_receipt_is_said_again_when_asked(folder: Path):
    """R17 (session 4f): the caller asks to hear what was done, and code says it again: no write, no new intent.

    The automatic repeat of a receipt the caller cut off stays at one
    (`RECEIPT_REPEATS`). Past it, an explicit request still gets the receipt
    (`say_again`), in the desk's words, from the receipt that still stands:
    after a reopen superseded the resolve, the reopen is what is said. A
    requested copy the caller cuts off is not marked heard and brings no
    automatic repeat. Nothing is ever sent again.
    """
    receipt = 'Ticket T-101 resuelto. ¿Algo más?'
    desk = desk_in(folder)
    script, flow, call = opened(desk)
    await to_read_back(flow, call, script)
    script.moves = [('call', 'resolve_ticket', {})]
    reply = await core.text_turn(flow, call, 'Sí.')
    call.acknowledge(reply.segments[-1].id, heard='')
    script.moves = [('call', 'wait', {})]
    reply = await core.text_turn(flow, call, '¿Perdona?')
    assert [s.text for s in reply.segments] == [receipt], reply.segments
    call.acknowledge(reply.segments[-1].id, heard='')
    said = await turn(flow, call, script, '¿Qué?', ('call', 'wait', {}))
    assert receipt not in said, ('the automatic repeat went past its limit', said)
    intents = len(call.intents)

    script.moves = [('call', 'say_again', {})]
    reply = await core.text_turn(flow, call, '¿Me lo repites, por favor?')
    assert [s.text for s in reply.segments] == [receipt], ('a requested repeat was not said', reply.segments)
    call.acknowledge(reply.segments[-1].id, heard='')
    assert reply.segments[-1].delivered != 'delivered', 'a requested copy the caller cut off counted as heard'
    said = await turn(flow, call, script, 'Vale.', ('call', 'wait', {}))
    assert receipt not in said, ('a cut requested copy brought an automatic repeat', said)
    assert desk.sent() == 1 and len(call.intents) == intents, (
        'saying a receipt again sent something',
        desk.sent(),
    )

    [standing] = call.effective()
    call.note_evidence(
        'reopen_ticket',
        standing.args,
        {**standing.receipt, 'status': 'open'},
        intent='e' * 32,
        resource='T-101',
        kind=standing.kind,
        version=(standing.version or 0) + 1,
    )
    said = await turn(flow, call, script, '¿Cómo quedó?', ('call', 'say_again', {}))
    assert said == ['Ticket T-101 reabierto. ¿Algo más?'], (
        'a superseded receipt was said as the outcome',
        said,
    )

    script, flow, call = opened(desk_in(folder / 'standing'), HERE / 'rendered_choices.yaml')
    said = await turn(flow, call, script, '¿Me lo repites?', ('call', 'say_again', {}))
    assert said and not any('resuelto' in s for s in said), (
        'nothing stands, and something was said as done',
        said,
    )
    assert said == ['No veo tickets en esa cuenta. ¿El problema es de facturación o de acceso?'], (
        'with nothing standing, a rendered step says what wait would',
        said,
    )


async def a_question_is_not_a_no(folder: Path):
    """R16 (session 4f): only the agent's `decline` turns a read-back down; a question in reply leaves it waiting.

    The reproducer: the resolve is read back and heard, the caller asks the
    opening hours first, the desk hands them to the FAQ, and they leave. No
    write happened and the request is still open, so the goodbye says it was
    left undone. Against it: an explicit no (nothing said about it, and a
    later yes approves nothing), a revision (its own fresh read-back, left
    waiting if they go), a resolve carried out (no line), a no to a
    read-back they never heard in full (nothing to turn down), and a no
    spoken in half a sentence the caller then carried on from (dropped with
    the piece it was read from).

    The revision also shows a field set by a receipt resting on that
    receipt's own request: the revision the look-up found stands when the
    resolution changes, since the look-up never sent a resolution.
    """

    async def leave(flow, call, script) -> str:
        return ' '.join(
            await turn(flow, call, script, 'Adiós.', ('call', 'goodbye', {}), ('call', 'goodbye', {}))
        )

    desk = desk_in(folder)
    script, flow, call = opened(desk)
    await to_read_back(flow, call, script)
    await turn(
        flow,
        call,
        script,
        'Antes de responder, ¿cuál es su horario?',
        ('call', 'hand_over', {'to': 'faq', 'because': 'a general question'}),
        ('say', 'De nueve a cinco, de lunes a viernes.'),
    )
    said = await leave(flow, call, script)
    [intent] = [i for i in call.intents if i.operation == 'resolve_ticket']
    assert call.locale.left_undone in said, ('a question was taken as a no', said)
    assert desk.sent() == 0 and intent.status == 'prepared', (desk.sent(), intent.status)

    desk = desk_in(folder / 'standing')
    script, flow, call = opened(desk)
    await to_read_back(flow, call, script)
    said = await turn(flow, call, script, 'No.', ('calls', [('decline', {}), ('wait', {})]))
    assert said == [call.locale.declined], ('a no was not said back as a no', said)
    said = await turn(flow, call, script, 'Bueno, sí, hazlo.', ('call', 'resolve_ticket', {}))
    assert desk.sent() == 0 and said[-1].startswith('¿Marco el ticket T-101'), (
        'a yes after a no approved the read-back it turned down',
        said,
    )
    await turn(flow, call, script, 'No, mejor no.', ('calls', [('decline', {}), ('wait', {})]))
    said = await leave(flow, call, script)
    assert call.locale.left_undone not in said and desk.sent() == 0, said

    desk = desk_in(folder / 'superseded')
    script, flow, call = opened(desk)
    await to_read_back(flow, call, script)
    said = await turn(
        flow,
        call,
        script,
        'Mejor pon que la arregló el técnico.',
        ('call', 'collect', {'values': {'resolution': 'la arregló el técnico'}}),
        ('call', 'resolve_ticket', {}),
    )
    assert said[-1] == '¿Marco el ticket T-101 como resuelto: la arregló el técnico?', (
        'a new resolution took the revision the look-up found, and the look-up would not run again',
        said,
    )
    said = await leave(flow, call, script)
    assert call.locale.left_undone in said and desk.sent() == 0, (
        'a revision left waiting was not said',
        said,
    )

    desk = desk_in(folder / 'wrong_ticket')
    script, flow, call = opened(desk)
    await to_read_back(flow, call, script)
    await turn(flow, call, script, 'Sí.', ('call', 'resolve_ticket', {}))
    said = await leave(flow, call, script)
    assert call.locale.left_undone not in said and desk.sent() == 1, said

    desk = desk_in(folder / 'lose_answer')
    script, flow, call = opened(desk)
    script.moves = list(TO_READ_BACK)
    await core.text_turn(flow, call, 'Hola, la impresora del ticket T-101 ya funciona. Cuenta ACC-1234.')
    await turn(flow, call, script, 'No.', ('calls', [('decline', {}), ('wait', {})]))
    assert not call._declined, ('a read-back never heard in full was turned down', call._declined)

    desk = desk_in(folder / 'invented')
    script, flow, call = opened(desk)
    await to_read_back(flow, call, script)
    script.moves = [('calls', [('decline', {}), ('wait', {})])]
    reply = await core.text_turn(flow, call, 'No.')
    call.acknowledge(reply.segments[0].id, heard='')
    call.note_barge()
    await turn(
        flow,
        call,
        script,
        '... espera, ¿cuál es el horario?',
        ('call', 'hand_over', {'to': 'faq', 'because': 'a general question'}),
        ('say', 'De nueve a cinco.'),
    )
    assert not call._declined, ('a no kept from a piece the caller carried on from', call._declined)
    said = await leave(flow, call, script)
    assert call.locale.left_undone in said, said


async def spoken_offers_go_with_their_options(folder: Path):
    """R15 (session 4f): what `wait` says about an offer is exactly what can still be picked from it.

    The words for an offer and its ids are one record: whatever takes the
    ids (a field the offer rests on changing, another caller) takes the
    words, and an offer whose dependencies did not change keeps both. A step
    says only the offers it made itself, so a step reached by a hand-over in
    the same turn never says another step's offer. An update that fails
    validation takes nothing.
    """
    billing_words = ('reembolso', 'abono')
    offered = [
        ('collect', {'values': {'account_id': 'ACC-1234', 'category': 'billing'}}),
        ('options', {}),
    ]

    script, flow, call = opened(desk_in(folder), HERE / 'rendered_choices.yaml')
    said = ' '.join(
        await turn(
            flow,
            call,
            script,
            'Cuenta ACC-1234, facturación. No, perdona, es de acceso.',
            ('calls', offered),
            ('calls', [('collect', {'values': {'category': 'access'}}), ('wait', {})]),
        )
    )
    assert not any(word in said for word in billing_words), ('an offer outlived its options', said)
    assert 'resolution' not in call._offers and 'T-101' in said, (said, dict(call._offers))

    script, flow, call = opened(desk_in(folder / 'standing'), HERE / 'rendered_choices.yaml')
    said = ' '.join(
        await turn(
            flow,
            call,
            script,
            'Cuenta ACC-1234, facturación.',
            ('calls', offered),
            ('calls', [('collect', {'values': {'account_id': 'ACC-1234'}}), ('wait', {})]),
        )
    )
    assert 'un reembolso o un abono' in said, ('an offer whose dependencies did not change was lost', said)
    records = dict(call._offers._by_choice)
    try:
        call.collect(('resolution',), ticket_id='T-1', category='access')
    except ValidationError:
        pass
    else:
        raise AssertionError('an invalid candidate was accepted')
    assert call._offers._by_choice == records, (
        'an update that failed validation took an offer or its words',
        call._offers._by_choice,
    )

    script, flow, call = opened(desk_in(folder / 'superseded'), HERE / 'rendered_choices.yaml')
    said = ' '.join(
        await turn(
            flow,
            call,
            script,
            'Cuenta ACC-1234, facturación. Ah no, es la ACC-5678.',
            ('calls', offered),
            ('calls', [('collect', {'values': {'account_id': 'ACC-5678'}}), ('wait', {})]),
        )
    )
    assert 'T-101' not in said, ('an offer made to another account was said', said)

    aside = HERE / '_rendered_aside.yaml'
    aside.write_text(
        (HERE / 'rendered_choices.yaml')
        .read_text()
        .replace(
            '  - {id: look_up,',
            '  - id: aside\n    kind: conversation\n    agent: agent\n    prompt: prompts/faq.md\n'
            '    speech: rendered\n    fields: [account_id]\n    exits:\n      - {to: desk, purpose: back}\n'
            '  - {id: look_up,',
        )
        .replace(
            "    exits:\n      - {to: look_up, purpose: find the ticket's current revision",
            '    exits:\n      - {to: aside, purpose: something else}\n'
            "      - {to: look_up, purpose: find the ticket's current revision",
        )
    )
    try:
        script, flow, call = opened(desk_in(folder / 'by_words'), aside)
        said = ' '.join(
            await turn(
                flow,
                call,
                script,
                'Cuenta ACC-1234, facturación. Otra cosa.',
                ('calls', offered),
                ('call', 'hand_over', {'to': 'aside', 'because': 'something else'}),
                ('call', 'wait', {}),
            )
        )
        assert call.state.agent == 'aside', call.state.agent
        assert 'T-101' not in said and not any(word in said for word in billing_words), (
            "a step said another step's offer",
            said,
        )
    finally:
        aside.unlink()


def spans(exporter: InMemorySpanExporter) -> InMemorySpanExporter:
    """Collect this process's finished spans in `exporter`, on the one global provider."""
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


def estimated(call, reply) -> None:
    """Play a reply on a transport that only estimates playback: every mark settled `local`, as `LocalPlayout` did.

    The browser path before session 4f's repair: audio written, or time for
    it to play, and nothing from the far end. The caller's reply begins after.
    """
    now = time.perf_counter()
    for sent in reply.segments:
        segment = next(s for s in call.all_spoken if s.id == sent.id)
        segment.at = now - 2
        name = call.mark_for(sent.id)
        call.note_mark_sent(name, now - 1, media=320)
        call.note_mark_returned(name, now - 0.5, local=True)
    call.note_onset(now)


async def estimated_turn(flow, call, script: Script, words: str, *moves) -> list[str]:
    script.moves = list(moves)
    reply = await core.text_turn(flow, call, words)
    estimated(call, reply)
    return [s.text for s in reply.segments]


async def an_unconfirmed_read_back_is_withdrawn_once(folder: Path):
    """R19 (session 4f follow-up, Langfuse trace 369560b8): a read-back with no playback evidence ends, once.

    The browser incident on the desk: every segment only estimated as played,
    no simulation. The caller says yes three times; the agent calls the
    resolve each time. Before the repair the intent stayed prepared, and the
    caller heard `gave_up` again on every yes. Now the second unconfirmed
    copy withdraws it (`CallContext.withdraw`): `gave_up` once, nothing
    pending, the intent terminal, and a `confirmation` span naming the
    transport and why. Then, specified: confirming again hears `withdrawn`
    with no read-back, no new intent and no dispatch; a different question is
    answered; stopping says the goodbye, with `left_undone` (the caller may
    have talked over `gave_up` too); a corrected value is a new question, read back with its own
    allowance, and here acknowledged, so a yes to it resolves once. The
    model is told the evidence is missing, never that the caller did not hear.
    """
    exporter = spans(InMemorySpanExporter())
    desk = desk_in(folder)
    script, flow, call = opened(desk)
    call.note_transport('estimated')
    script.moves = list(TO_READ_BACK)
    estimated(call, await core.text_turn(flow, call, 'Resuelve el ticket T-101, cuenta ACC-1234.'))
    readback = '¿Marco el ticket T-101 como resuelto: la impresora ya imprime?'

    said = await estimated_turn(flow, call, script, 'Me parece bien.', ('call', 'resolve_ticket', {}))
    assert said == [readback], said
    told = script.told[-1]
    assert 'missing evidence, not a sign they missed it' in told and 'did not hear' not in told, told
    said = await estimated_turn(flow, call, script, 'Sí, correcto.', ('call', 'resolve_ticket', {}))
    assert said == [call.locale.gave_up], said
    [intent] = [i for i in call.intents if i.operation == 'resolve_ticket']
    assert intent.status == 'withdrawn' and call.pending_question() is None and desk.sent() == 0, intent
    [withdrawn] = [
        json.loads(str(s.attributes['confirmation.audit']))
        for s in exporter.get_finished_spans()
        if s.name == 'confirmation'
        and s.attributes
        and 'withdrawn' in str(s.attributes['confirmation.audit'])
    ]
    assert withdrawn['reason'] == 'unconfirmed' and withdrawn['transport'] == 'estimated', withdrawn
    assert {c['heard_by'] for c in withdrawn['copies']} == {'local'} and not withdrawn['simulated_playout']
    assert {c['hearing'] for c in withdrawn['copies']} == {'unconfirmed'}, withdrawn

    for words in ('¡Pero si te lo estoy confirmando!', 'Que sí, hazlo.'):
        said = await estimated_turn(flow, call, script, words, ('call', 'resolve_ticket', {}))
        assert said == [call.locale.withdrawn], ('confirming again read it back or looped the fallback', said)
    assert [i.id for i in call.intents if i.operation == 'resolve_ticket'] == [intent.id], call.intents
    assert call.intents[-1].readbacks == intent.readbacks and desk.sent() == 0, call.intents
    assert 'Withdrawn: resolve_ticket' in script.told[-1], 'the model was not told it was withdrawn'

    said = await estimated_turn(
        flow,
        call,
        script,
        '¿Cuál es vuestro horario?',
        ('call', 'hand_over', {'to': 'faq', 'because': 'a general question'}),
        ('say', 'De nueve a cinco.'),
    )
    assert said == ['De nueve a cinco.'] and call.pending_question() is None, said
    said = await estimated_turn(flow, call, script, 'Adiós.', ('call', 'goodbye', {}))
    assert said[-1].endswith(SPANISH_GOODBYE) and call.locale.left_undone in ' '.join(said), said
    assert desk.sent() == 0

    desk = desk_in(folder / 'standing')
    script, flow, call = opened(desk)
    call.note_transport('estimated')
    script.moves = list(TO_READ_BACK)
    estimated(call, await core.text_turn(flow, call, 'Resuelve el ticket T-101, cuenta ACC-1234.'))
    for words in ('Sí.', 'Sí.'):
        said = await estimated_turn(flow, call, script, words, ('call', 'resolve_ticket', {}))
    assert said == [call.locale.gave_up], said
    said = await turn(
        flow,
        call,
        script,
        'Pon mejor que la arregló el técnico.',
        ('call', 'collect', {'values': {'resolution': 'la arregló el técnico'}}),
        ('call', 'resolve_ticket', {}),
    )
    assert said == ['¿Marco el ticket T-101 como resuelto: la arregló el técnico?'], said
    first, second = [i for i in call.intents if i.operation == 'resolve_ticket']
    assert (first.status, second.status, second.readbacks) == ('withdrawn', 'prepared', 0), call.intents
    said = await turn(flow, call, script, 'Sí.', ('call', 'resolve_ticket', {}))
    assert desk.sent() == 1 and said == ['Ticket T-101 resuelto. ¿Algo más?'], (desk.sent(), said)


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        for index, check in enumerate(
            (
                an_informational_step_needs_nothing,
                a_heard_yes_resolves_once,
                a_stale_revision_changes_nothing,
                a_doubtful_answer_is_reconciled_by_intent,
                only_what_stands_is_recapped,
                a_lookup_backs_no_claim,
                a_fresh_process_reconciles,
                renamed_it_still_holds,
                a_cut_receipt_is_said_again_only_while_it_stands,
                the_audit_names_the_backend_effect,
                picks_rest_only_on_what_their_offer_read,
                picks_follow_the_state_they_end_in,
                a_rendered_step_offers_in_the_desks_words,
                a_no_is_not_left_undone,
                spoken_offers_go_with_their_options,
                a_question_is_not_a_no,
                a_receipt_is_said_again_when_asked,
                a_changed_resource_stays_on_offer,
                an_unconfirmed_read_back_is_withdrawn_once,
            )
        ):
            folder = Path(tmp) / str(index)
            for sub in (
                '',
                'wrong_ticket',
                'lose_answer',
                'invented',
                'standing',
                'superseded',
                'by_words',
                'paraphrase',
            ):
                (folder / sub).mkdir(parents=True, exist_ok=True)
            await check(folder)
    print('tickets ok: R1-R19, no phone, date, appointment or slot')


if __name__ == '__main__':
    asyncio.run(main())
