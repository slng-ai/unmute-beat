"""The installed-wheel scenario, session 4d: Stage C's, and session 4d's manifest keys, offline, from `/`.

Run by `package-proof.sh` with the working directory at `/`, inside a fresh
virtual environment holding only the core wheel. Every manifest is found
through `importlib.resources`. Stage B's checks (the clinic books once; the
desk's lost answer is reconciled by its intent; a persisted intent is
reconciled in a fresh session), and Stage C's, all through the installed
package:

* the desk's write step is `speech: rendered`: a claim the model writes
  after a lookup is never said, in any words;
* a receipt that says `pending` settles nothing (`rejects` names the only
  terminal no), and no new intent is made;
* after a reopen, a fresh session that restores the old resolve says
  nothing of it (`version`, `resource`: it is history);
* no Pipecat, Langfuse or dotenv module is loaded;
* session 4d: the clinic lookup that declares `restated: confirms` is
  confirmed by its value said again, and a booking read-back is not; the
  desk's `choices.yaml` keeps the ticket when the category changes.
"""

import asyncio
import json
import os
import sys
import tempfile
from importlib.resources import files
from pathlib import Path

import pydantic_ai.models
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

from clinic_flow.clinic import Backend, ClinicState  # noqa: E402
from ticket_flow.tickets import Desk, DeskState  # noqa: E402

import core  # noqa: E402

assert 'site-packages' in core.__file__ and '.claude' not in core.__file__, core.__file__
assert os.getcwd() == '/', os.getcwd()
assert core.Refused.__mro__[1].__name__ == 'ModelRetry'
print('core from', core.__file__)

moves: list[tuple] = []


def reading(messages, info) -> ModelResponse:
    raise AssertionError('only step agents run on the public path')


async def agent(messages, info):
    if not moves:
        yield 'Sorry?'
        return
    kind, *rest = moves.pop(0)
    if kind == 'say':
        yield rest[0]
    else:
        yield {0: DeltaToolCall(name=rest[0], json_args=json.dumps(rest[1]))}


async def say(flow, call, words: str, *script: tuple) -> list[str]:
    moves[:] = list(script)
    reply = await core.text_turn(flow, call, words)
    call.acknowledge(*(s.id for s in reply.segments))
    return [s.text for s in reply.segments]


READ_BACK = (
    ('call', 'hand_over', {'to': 'desk', 'because': 'resolve a ticket'}),
    (
        'call',
        'collect',
        {'values': {'account_id': 'ACC-1234', 'ticket_id': 'T-101', 'resolution': 'ya imprime'}},
    ),
    ('call', 'hand_over', {'to': 'look_up', 'because': 'its revision'}),
    ('call', 'resolve_ticket', {}),
)


def desk_in(folder: Path, name: str) -> Desk:
    desk = Desk(folder / f'{name}.db')
    desk.seed('ACC-1234', 'T-101')
    return desk


async def main() -> None:
    model = FunctionModel(reading, stream_function=agent)

    spec = core.load(files('clinic_flow') / 'one_agent.yaml')
    assert spec.problems() == [], spec.problems()
    flow = spec.compile(model=model)
    call = flow.open(ClinicState(), services=Backend())
    fill = (
        ('call', 'options', {}),
        ('call', 'collect', {'values': {'patient': 'P-1234', 'slot': '1000'}}),
        ('call', 'book', {}),
    )
    await say(flow, call, 'P-1234 at ten.', *fill)
    heard = await say(flow, call, 'Yes.', ('call', 'book', {}))
    assert call.services.bookings == {'CL001': ('P-1234', '1000')}, call.services.bookings
    print('clinic booked once:', heard[0])

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        flow = core.load(files('ticket_flow') / 'agent.yaml').compile(model=model)

        desk = desk_in(folder, 'lost')
        call = flow.open(DeskState(), services=desk)
        await say(flow, call, 'Cuenta ACC-1234, ticket T-101: ya funciona.', *READ_BACK)
        desk.lose_answer = True
        lost = await say(flow, call, 'Sí.', ('call', 'resolve_ticket', {}))
        assert lost == [call.locale.unsettled] and desk.sent() == 1, lost
        settled = await say(flow, call, '¿Ya está?', ('call', 'wait', {}))
        assert settled[0] == 'Ticket T-101 resuelto. ¿Algo más?' and desk.sent() == 1, settled
        print('desk resolved once, the lost answer reconciled by its intent:', settled[0])
        fresh = flow.open(DeskState(), services=desk, intents=desk.unresolved())
        again = await say(flow, fresh, 'Hola.', ('say', 'Dime.'))
        assert again[0] == 'Ticket T-101 resuelto. ¿Algo más?' and desk.sent() == 1, again
        print('a fresh session reconciled the persisted intent, nothing sent again')

        call = flow.open(DeskState(), services=desk_in(folder, 'rendered'))
        claim = 'El ticket T-101 ha quedado solucionado definitivamente.'
        said = await say(
            flow, call, 'Ticket T-101, cuenta ACC-1234.', *READ_BACK[:3], ('say', claim), ('call', 'wait', {})
        )
        assert said == ['Tengo el ticket T-101.', call.locale.pardon], said
        assert not [s for s in call.all_spoken if s.kind == 'model'], 'a rendered step said model words'
        print('a rendered step said no model words:', said)

        desk = desk_in(folder, 'pending')
        call = flow.open(DeskState(), services=desk)
        await say(flow, call, 'Cuenta ACC-1234, ticket T-101: ya funciona.', *READ_BACK)
        desk.pending = True
        await say(flow, call, 'Sí.', ('call', 'resolve_ticket', {}))
        await say(flow, call, 'Otra vez.', ('call', 'resolve_ticket', {}))
        resolves = [i.status for i in call.intents if i.operation == 'resolve_ticket']
        assert resolves == ['unknown'] and desk.ticket('T-101') == (1, 'open'), resolves
        print('a pending answer settled nothing, and made no second intent')

        desk = desk_in(folder, 'history')
        call = flow.open(DeskState(), services=desk)
        await say(flow, call, 'Cuenta ACC-1234, ticket T-101: ya funciona.', *READ_BACK)
        await say(flow, call, 'Sí.', ('call', 'resolve_ticket', {}))
        await say(flow, call, 'Reábrelo.', ('call', 'reopen_ticket', {}))
        await say(flow, call, 'Sí.', ('call', 'reopen_ticket', {}))
        [old] = [i for i in call.intents if i.operation == 'resolve_ticket']
        fresh = flow.open(
            DeskState(), services=desk, intents=[old.model_copy(update={'status': 'dispatched'})]
        )
        said = await say(flow, fresh, 'Hola.', ('say', 'Dime.'))
        assert desk.ticket('T-101') == (3, 'open') and not any('resuelto' in s for s in said), said
        print('an old resolve restored after the reopen is history, never said')

    patient = {'values': {'patient': 'P-1234'}}
    flow = core.load(files('clinic_flow') / 'restated.yaml').compile(model=model)
    call = flow.open(ClinicState(), services=Backend())
    await say(flow, call, 'P-1234.', ('call', 'collect', patient))
    said = await say(flow, call, 'No, it is P-1234.', ('call', 'collect', patient))
    assert said[:2] == [call.locale.restated, 'Thanks, P-1234.'], said
    flow = core.load(files('clinic_flow') / 'reads_back.yaml').compile(model=model)
    call = flow.open(ClinicState(), services=Backend())
    await say(flow, call, 'P-1234 at ten.', *fill[:2])
    said = await say(
        flow,
        call,
        'No, do not book ten.',
        ('call', 'collect', {'values': {'slot': '1000'}}),
        ('say', 'Nothing booked.'),
    )
    assert call.services.bookings == {} and said == ['Nothing booked.'], said
    print('restated: confirms on a lookup only; a booking value said again booked nothing')

    with tempfile.TemporaryDirectory() as tmp:
        desk = desk_in(Path(tmp), 'choices')
        flow = core.load(files('ticket_flow') / 'choices.yaml').compile(model=model)
        call = flow.open(DeskState(), services=desk)
        await say(
            flow,
            call,
            'Cuenta ACC-1234, facturación.',
            ('call', 'collect', {'values': {'account_id': 'ACC-1234', 'category': 'billing'}}),
            ('call', 'options', {}),
            ('say', '¿Cuál?'),
        )
        await say(
            flow,
            call,
            'T-101, reembolso.',
            ('call', 'collect', {'values': {'ticket_id': 'T-101', 'resolution': 'refund'}}),
            ('say', 'Vale.'),
        )
        await say(
            flow,
            call,
            'Es de acceso.',
            ('call', 'collect', {'values': {'category': 'access'}}),
            ('say', 'Vale.'),
        )
        facts = call.state.facts
        assert (facts.ticket_id, facts.resolution) == ('T-101', ''), facts
        print('a new category took the resolution and kept the ticket (after:)')

    blocked = sorted(m for m in sys.modules if m.split('.')[0] in ('pipecat', 'langfuse', 'dotenv'))
    assert blocked == [], blocked
    print('installed scenario ok')


asyncio.run(main())
