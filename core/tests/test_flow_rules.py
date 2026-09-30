"""The flow rules session 5 added, proven on the support desk (`ticket_flow/routed.yaml`), with no learner in it.

* F1: a call's fixed lines follow the language it is in (`language:` and `locales:`), per call;
* F2: an operation with no read-back is carried out by code once its field is recorded (`carries_out`),
  and never offered to the model, so it runs only on what this reply recorded;
* F3: its receipt names the step for the ticket (`route:`), and the caller moves there by code, with
  no model request; the step opens with its own read-back, or the turn simply ends;
* F4: a read-back waiting when the caller switches language is put again in the new words, and a yes
  to that copy does it once;
* F5: a greeting carries the entry step's read-back, so the first reply answers it;
* F6: a validator reads what the caller said this turn (`heard`), and a new account number nobody said
  is refused, while an unchanged one is left alone;
* F7: every rule is checked when the manifest loads.

Scripted models prove runtime properties, not language understanding.
"""

import asyncio
import tempfile
from pathlib import Path

import core
from core.flowspec import load
from tests.script import Call, Move, Record, Reply, Script
from tests.ticket_flow.tickets import Desk, RoutedState

HERE = Path(__file__).parent / 'ticket_flow'
MANIFEST = HERE / 'routed.yaml'


def opened(folder: Path, manifest: str | None = None, state: RoutedState | None = None, status: str = 'open'):
    folder.mkdir(parents=True, exist_ok=True)
    desk = Desk(folder / 'desk.db')
    desk.seed('ACC-1234', 'T-101', status=status)
    script = Script()
    spec = core.load(MANIFEST) if manifest is None else load(manifest, root=HERE)
    flow = spec.compile(model=script.model)
    call = flow.open(state or RoutedState(), services=desk)
    desk.language = lambda: call.state.facts.idioma
    script.call = call
    return desk, script, flow, call


async def turn(flow, call, script: Script, words: str, *moves: Move) -> list[str]:
    script.moves = list(moves)
    reply = await core.text_turn(flow, call, words)
    call.acknowledge(*(s.id for s in reply.segments))
    return [s.text for s in reply.segments if s.text]


RECORD = Record(account_id='ACC-1234', ticket_id='T-101')


async def the_language_is_the_calls(folder: Path):
    """F1."""
    _, script, flow, call = opened(folder)
    assert call.locale.goodbye == 'Gracias por llamar al soporte. Hasta pronto.'
    said = await turn(
        flow,
        call,
        script,
        'Can we speak English? Bye for now.',
        Record(idioma='en'),
        Call('goodbye'),
    )
    assert call.state.facts.idioma == 'en' and said[-1] == 'Thanks for calling support. Goodbye.', said
    _, _, _, other = opened(folder / 'other')
    assert other.locale.goodbye.startswith('Gracias'), 'one call switching language changes no other call'


async def a_lookup_is_carried_out_by_code_and_moves_the_caller(folder: Path):
    """F2 and F3."""
    desk, script, flow, call = opened(folder)
    said = await turn(flow, call, script, 'Cuenta ACC-1234, ticket T-101.', RECORD)
    assert said == ['Tengo el ticket T-101.'], said
    assert call.state.agent == 'resolve' and script.steps == ['desk'], (call.state.agent, script.steps)
    assert 'find_ticket' not in script.requests[0].offered, (
        'a carried-out operation is never offered to the model'
    )
    assert call.state.facts.revision == 1 and not desk.sent()
    said = await turn(
        flow,
        call,
        script,
        'La impresora ya imprime.',
        Record(resolution='la impresora ya imprime'),
    )
    assert said == ['¿Marco el ticket T-101 como resuelto: la impresora ya imprime?'], said
    said = await turn(flow, call, script, 'Sí.', Call('resolve_ticket'))
    assert said == ['Ticket T-101 resuelto.'] and desk.ticket('T-101') == (2, 'resolved'), said

    desk, script, flow, call = opened(folder / 'resolved', status='resolved')
    said = await turn(flow, call, script, 'Cuenta ACC-1234, ticket T-101.', RECORD)
    assert said == ['¿Reabro el ticket T-101?'] and call.state.agent == 'reopen', (said, call.state.agent)
    assert script.steps == ['desk'], 'the move and the read-back needed no request of the step it moved to'

    desk, script, flow, call = opened(folder / 'unknown')
    said = await turn(
        flow,
        call,
        script,
        'Cuenta ACC-1234, ticket T-999.',
        Record(account_id='ACC-1234', ticket_id='T-999'),
        Reply('No encuentro ese ticket. ¿Me dice el número otra vez?'),
    )
    assert said == ['No encuentro ese ticket. ¿Me dice el número otra vez?'] and call.state.agent == 'desk', (
        said
    )
    assert script.steps == ['desk', 'desk'] and 'find_ticket refused' in script.told[1], (
        'a refused carried operation goes back to the step, with the refusal, in the same turn'
    )

    desk, script, flow, call = opened(folder / 'hostile')
    await turn(flow, call, script, 'Hola.', Call('find_ticket'))
    assert call.state.agent == 'desk' and not call.evidence, 'a model cannot call a carried-out operation'


async def a_waiting_read_back_follows_the_language(folder: Path):
    """F4."""
    desk, script, flow, call = opened(folder)
    await turn(flow, call, script, 'Cuenta ACC-1234, ticket T-101.', RECORD)
    await turn(
        flow,
        call,
        script,
        'La impresora ya imprime.',
        Record(resolution='la impresora ya imprime'),
    )
    first = call.open_question()
    said = await turn(flow, call, script, 'In English, please.', Record(idioma='en'))
    assert said == ['Shall I mark ticket T-101 as resolved: la impresora ya imprime?'], said
    again = call.open_question()
    assert again.value == first.value and again.intent == first.intent, 'the same question, a new copy'
    said = await turn(flow, call, script, 'Yes.', Call('resolve_ticket'))
    assert desk.ticket('T-101') == (2, 'resolved') and desk.sent() == 1, said


async def a_greeting_reads_back_the_entry(folder: Path):
    """F5."""
    text = MANIFEST.read_text().replace('entry: desk', 'entry: reopen')
    state = RoutedState(account_id='ACC-1234', ticket_id='T-101', revision=1)
    desk, script, flow, call = opened(folder, manifest=text, state=state, status='resolved')
    line = flow.greeting(call, 'Soporte, buenos días.')
    assert not isinstance(line, str) and line.spoken == 'Soporte, buenos días. ¿Reabro el ticket T-101?', line
    core.greet(call, line)
    call.acknowledge(call.spoken[-1].id)
    said = await turn(flow, call, script, 'Sí.', Call('reopen_ticket'))
    assert said == ['Ticket T-101 reabierto.'] and desk.ticket('T-101') == (2, 'open'), said

    _, _, flow, call = opened(folder / 'asks')
    assert flow.greeting(call, 'Soporte.') == 'Soporte. ¿Qué ticket es?'


async def a_validator_reads_what_the_caller_said(folder: Path):
    """F6."""
    _, script, flow, call = opened(folder)
    await turn(
        flow,
        call,
        script,
        'El ticket T-101.',
        Record(account_id='ACC-9999', ticket_id='T-101'),
    )
    assert call.state.facts.account_id is None, 'an account number nobody said is not recorded'
    await turn(
        flow,
        call,
        script,
        'Mi cuenta es ACC-1234.',
        Record(account_id='ACC-1234'),
    )
    assert call.state.facts.account_id == 'ACC-1234'
    await turn(flow, call, script, 'In English.', Record(idioma='en'))
    assert call.state.facts.account_id == 'ACC-1234' and call.state.facts.idioma == 'en', (
        'a value already recorded is not refused for words said later'
    )


def every_rule_is_checked_at_load():
    """F7."""
    base = MANIFEST.read_text()
    cases = [
        ('route: next', 'route: status', 'must be a Literal of step ids'),
        (
            'carries_out: {find_ticket: [ticket_id]}',
            'carries_out: {resolve_ticket: [ticket_id]}',
            'needs a read-back and a yes first',
        ),
        (
            'carries_out: {find_ticket: [ticket_id]}',
            'operations: [find_ticket]\n    carries_out: {find_ticket: [ticket_id]}',
            'offers it to the model too',
        ),
        ('language: idioma', 'language: tongue', 'no field or property'),
        ('language: idioma\n', '', 'language and locales go together'),
    ]
    for old, new, problem in cases:
        assert old in base, old
        problems = load(base.replace(old, new), root=HERE).problems()
        assert any(problem in p for p in problems), (new, problems)
    assert not load(base, root=HERE).problems()


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        for index, check in enumerate(
            (
                the_language_is_the_calls,
                a_lookup_is_carried_out_by_code_and_moves_the_caller,
                a_waiting_read_back_follows_the_language,
                a_greeting_reads_back_the_entry,
                a_validator_reads_what_the_caller_said,
            )
        ):
            await check(Path(tmp) / str(index))
    every_rule_is_checked_at_load()
    print('flow rules ok: F1-F7, language per call, carried and routed operations, greetings, heard')


if __name__ == '__main__':
    asyncio.run(main())
