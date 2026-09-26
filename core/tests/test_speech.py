"""Stage C0.6: on a rendered step (`speech: rendered`) no model words reach the caller, whatever they claim.

The desk step of the support desk is rendered. Its agent still reads the
caller and chooses; code says every word. Each check has a scripted model
try to claim the ticket is solved, in Spanish, in English and in
paraphrases no word list holds, beside the outcomes that make the claim
false:

* S1: after a lookup alone;
* S2: after an answer about another ticket (unknown);
* S3: after an answer that says `pending` (unknown, and never sent again);
* S4: after the ticket was resolved and then reopened;
* S5: in a fresh process that restores the old resolve after the reopen;
* S6: an authoritative no (a stale revision) is said in core's words;
* S7: the loader refuses a rendered step that offers choices.

"No model words" is checked on the call's own record: no segment the caller
was sent is of kind `model`. What a prose step may still say is a separate,
known limit (`tests.defects`).
"""

import asyncio
import tempfile
from pathlib import Path

import core
from tests.test_tickets import HERE as DESK
from tests.test_tickets import desk_in, opened, turn
from tests.ticket_flow.worker import TO_READ_BACK, to_read_back

CLAIMS = (
    'El ticket T-101 ha quedado solucionado definitivamente.',
    'Listo, ya está todo arreglado.',
    'Your ticket is resolved, all done.',
    'Hecho. Cerrado para siempre.',
    'No te preocupes, eso ya quedó resuelto.',
)


def no_model_words(call) -> None:
    spoken = [s for s in call.all_spoken if s.kind == 'model']
    assert not spoken, ('a rendered step said model words', spoken)


async def after_a_lookup(folder: Path):
    """S1."""
    for index, claim in enumerate(CLAIMS):
        desk = desk_in(folder / f'lookup{index}')
        script, flow, call = opened(desk)
        said = await turn(
            flow,
            call,
            script,
            'Ticket T-101, cuenta ACC-1234.',
            *TO_READ_BACK[:3],
            ('say', claim),
            ('call', 'wait', {}),
        )
        assert said == ['Tengo el ticket T-101.', call.locale.pardon], (claim, said)
        no_model_words(call)
        assert desk.ticket('T-101') == (1, 'open') and not call.effective()


async def after_an_unknown_answer(folder: Path):
    """S2 and S3."""
    for fault in ('wrong_ticket', 'pending'):
        desk = desk_in(folder / fault)
        script, flow, call = opened(desk)
        await to_read_back(flow, call, script)
        setattr(desk, fault, True)
        said = await turn(flow, call, script, 'Sí.', ('call', 'resolve_ticket', {}), ('say', CLAIMS[0]))
        assert said == [call.locale.unsettled], (fault, said)
        [intent] = [i for i in call.intents if i.operation == 'resolve_ticket']
        assert intent.status == 'unknown', (fault, intent)
        setattr(desk, fault, False)
        said = await turn(flow, call, script, '¿Entonces?', ('say', CLAIMS[2]), ('call', 'wait', {}))
        assert not any(claim in said for claim in CLAIMS), (fault, said)
        if fault == 'wrong_ticket':
            assert said == ['Ticket T-101 resuelto. ¿Algo más?'], (
                'the write landed: its receipt, by code',
                said,
            )
        else:
            assert said == [call.locale.pardon] and not call.effective(), (fault, said)
        no_model_words(call)
        if fault == 'pending':
            desk.pending = True
            said = await turn(flow, call, script, 'Hazlo otra vez.', ('call', 'resolve_ticket', {}))
            assert desk.sent() == 0 and [
                i.status for i in call.intents if i.operation == 'resolve_ticket'
            ] == ['unknown'], ('a pending intent got a new intent or a success', said)


async def after_a_reopen(folder: Path):
    """S4 and S5."""
    desk = desk_in(folder / 'reopened')
    script, flow, call = opened(desk)
    await to_read_back(flow, call, script)
    await turn(flow, call, script, 'Sí.', ('call', 'resolve_ticket', {}))
    await turn(flow, call, script, 'Reábrelo.', ('call', 'reopen_ticket', {}))
    await turn(flow, call, script, 'Sí.', ('call', 'reopen_ticket', {}))
    said = await turn(flow, call, script, '¿Cómo está?', ('say', CLAIMS[1]), ('call', 'wait', {}))
    assert said == [call.locale.pardon], said
    no_model_words(call)
    [resolve] = [i for i in call.intents if i.operation == 'resolve_ticket']

    script, flow, fresh = opened(desk, intents=[resolve.model_copy(update={'status': 'dispatched'})])
    said = await turn(
        flow,
        fresh,
        script,
        'Hola, el ticket T-101, cuenta ACC-1234.',
        *TO_READ_BACK[:3],
        ('say', CLAIMS[4]),
        ('call', 'wait', {}),
    )
    assert not any('resuelto' in s for s in said) and not any(c in said for c in CLAIMS), said
    no_model_words(fresh)


async def a_refusal_in_core_words(folder: Path):
    """S6."""
    desk = desk_in(folder / 'stale')
    script, flow, call = opened(desk)
    await to_read_back(flow, call, script)
    desk.edit('T-101')
    said = await turn(flow, call, script, 'Sí.', ('call', 'resolve_ticket', {}), ('say', CLAIMS[0]))
    assert said == [call.locale.withheld], said
    assert [i.status for i in call.intents if i.operation == 'resolve_ticket'] == ['rejected']
    no_model_words(call)


def no_rendered_choices(folder: Path):
    """S7."""
    copy = folder / 'choices'
    copy.mkdir()
    for path in DESK.iterdir():
        if path.is_file():
            (copy / path.name).write_text(path.read_text())
    (copy / 'prompts').mkdir()
    for path in (DESK / 'prompts').iterdir():
        (copy / 'prompts' / path.name).write_text(path.read_text())
    text = (
        (DESK / 'agent.yaml')
        .read_text()
        .replace(
            '    speech: rendered\n',
            '    speech: rendered\n    choices: {ticket_id: tickets.py:find_effect}\n',
        )
    )
    (copy / 'agent.yaml').write_text(text)
    try:
        core.load(copy / 'agent.yaml').compile()
    except core.FlowSpecError as error:
        assert 'rendered' in str(error), error
        return
    raise AssertionError('a rendered step was allowed to offer choices')


async def rendered_claims() -> None:
    """S1 and S6 on their own: what the N6 row of the native comparison runs."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        for index in range(len(CLAIMS)):
            (folder / f'lookup{index}').mkdir()
        (folder / 'stale').mkdir()
        await after_a_lookup(folder)
        await a_refusal_in_core_words(folder)


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        for name in (
            *(f'lookup{i}' for i in range(len(CLAIMS))),
            'wrong_ticket',
            'pending',
            'reopened',
            'stale',
        ):
            (folder / name).mkdir()
        await after_a_lookup(folder)
        await after_an_unknown_answer(folder)
        await after_a_reopen(folder)
        await a_refusal_in_core_words(folder)
        no_rendered_choices(folder)
    print('speech ok: S1-S7, a rendered step says no model words')


if __name__ == '__main__':
    asyncio.run(main())
