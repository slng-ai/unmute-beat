"""The installed-wheel scenario, session 4b stage B: the clinic and the support desk, offline, from `/`.

Run by `package-proof.sh` with the working directory at `/`, inside a fresh
virtual environment holding only the core wheel. Every manifest is found
through `importlib.resources`. It asserts `core` is the installed copy, books
once on the clinic's one-agent manifest, resolves a ticket on the desk whose
answer is lost after its commit (unknown, then reconciled by its intent with
nothing sent again), reconciles a persisted intent in a fresh session, and
checks that no Pipecat, Langfuse or dotenv module was loaded.
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
        desk = Desk(Path(tmp) / 'desk.db')
        desk.seed('ACC-1234', 'T-101')
        flow = core.load(files('ticket_flow') / 'agent.yaml').compile(model=model)
        call = flow.open(DeskState(), services=desk)
        await say(flow, call, 'Cuenta ACC-1234, ticket T-101: ya funciona.', *READ_BACK)
        desk.lose_answer = True
        lost = await say(flow, call, 'Sí.', ('call', 'resolve_ticket', {}))
        assert lost == [call.locale.unsettled] and desk.sent() == 1, lost
        settled = await say(flow, call, '¿Ya está?')
        assert settled == ['Ticket T-101 resuelto. ¿Algo más?'] and desk.sent() == 1, settled
        print('desk resolved once, the lost answer reconciled by its intent:', settled[0])

        fresh = flow.open(DeskState(), services=desk, intents=desk.unresolved())
        assert [i.status for i in fresh.intents] == ['unknown'], fresh.intents
        again = await say(flow, fresh, 'Hola.')
        assert again == ['Ticket T-101 resuelto. ¿Algo más?'] and desk.sent() == 1, again
        print('a fresh session reconciled the persisted intent, nothing sent again')

    blocked = sorted(m for m in sys.modules if m.split('.')[0] in ('pipecat', 'langfuse', 'dotenv'))
    assert blocked == [], blocked
    print('installed scenario ok')


asyncio.run(main())
