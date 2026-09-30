"""One call on the support desk in its own process, which dies while resolving a ticket: `python -m tests.ticket_flow.worker DB MODE`.

MODE is `after` (the change commits, then the process is killed before
anything answers) or `before` (the process is killed inside the
transaction, so nothing commits). Either way the intent was recorded by the
desk before dispatch (`Desk.note_intent`), and a fresh process finds it
there. The exit code is the desk's: 3 after the commit, 4 before it.
"""

import asyncio
import os
import sys
from pathlib import Path

import pydantic_ai.models

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
os.environ.setdefault('GOOGLE_API_KEY', 'offline-test-placeholder')

import core  # noqa: E402
from tests.script import Call, Move, Script  # noqa: E402
from tests.ticket_flow.tickets import Desk, DeskState  # noqa: E402

HERE = Path(__file__).parent

TO_READ_BACK = (
    Call('hand_over', to='desk', because='resolve a ticket'),
    Call(
        'hand_over',
        to='look_up',
        because='find its revision',
        update={'account_id': 'ACC-1234', 'ticket_id': 'T-101', 'resolution': 'la impresora ya imprime'},
    ),
    Call('resolve_ticket'),
)
"""The scripted replies that take a call from the first words to the resolve read-back: the update rides the hand-over."""


async def to_read_back(flow, call, script: Script, moves: tuple[Move, ...] = TO_READ_BACK) -> list[str]:
    script.moves = list(moves)
    reply = await core.text_turn(
        flow, call, 'Hola, la impresora del ticket T-101 ya funciona. Cuenta ACC-1234.'
    )
    call.acknowledge(*(s.id for s in reply.segments))
    return [s.text for s in reply.segments]


async def main(path: str, mode: str) -> None:
    desk = Desk(path)
    script = Script()
    flow = core.load(HERE / 'agent.yaml').compile(model=script.model)
    call = flow.open(DeskState(), services=desk)
    script.call = call
    await to_read_back(flow, call, script)
    desk.die_after_commit, desk.die_before_commit = mode == 'after', mode == 'before'
    script.moves = [Call('resolve_ticket')]
    await core.text_turn(flow, call, 'Sí.')
    raise SystemExit('the worker was meant to die inside the write')


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1], sys.argv[2]))
