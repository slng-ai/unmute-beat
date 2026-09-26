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
* R6: after a lookup alone, a claimed resolution is withheld by the desk's
  own claim words, and a goodbye has nothing to recap;
* R7: a fresh process reconciles what a killed one did: after its commit,
  found and not sent again; before it, sent again as the same intent, once;
* R8: every field and operation renamed, and nothing in core changed;
* R9: a receipt cut off is said again only while it stands, and never re-sent;
* R10: its write audit is the salon's envelope, and names the desk's own effect.

Scripted models prove runtime properties, not language understanding.
"""

import asyncio
import importlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

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
    moves = [*TO_READ_BACK[:3], ('say', 'He resuelto el ticket T-101.')]
    said = await turn(flow, call, script, 'Ticket T-101, cuenta ACC-1234.', *moves)
    assert 'He resuelto' not in ' '.join(said) and said[-1] == call.locale.withheld, said
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
        said = await turn(flow, call, script, '¿Perdona?', ('say', 'Dime.'))
        assert desk.sent() == 1, 'saying a receipt again sent the write again'
        expected = ['Dime.'] if superseded else ['Ticket T-101 resuelto. ¿Algo más?']
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
    print('tickets ok: R1-R10, no phone, date, appointment or slot')


if __name__ == '__main__':
    asyncio.run(main())
