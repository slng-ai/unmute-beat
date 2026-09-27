"""Moves and cancellations against one shared diary file, with more than one caller on it (session 4d).

Every check drives the salon's real flow with the scripted provider, and a
second, independent caller writes to the same SQLite file through its own
`Diary`, as another worker would. Outcomes are judged from the file
(`snapshot`) and from the intents, never from what the agent said.

* V1: the move's destination is taken between its read-back and the yes: the
  diary's refusal is an authoritative no bound to the request (not unknown),
  the original appointment stands, and a fresh pick of another time moves it;
* V2: the booking changes elsewhere after it was shown: the stale move is
  refused, the picks are taken back, and a fresh offer moves it;
* V3: a refused move or cancel names the booking it was about, for another
  caller's booking, one already cancelled, and a fenced attempt; each is
  `rejected`, and nothing about it stands.
"""

import asyncio
import sqlite3

from salon.diary import Diary
from salon.functions import Cancel, Move, Salon, cancel, find_move, move
from tests.script import CLOCK, PHONE, slot, turn
from tests.test_business import FRIDAY, at_booking, seed

OTHER = '+447700900123'
"""A second, synthetic caller (Ofcom's drama range)."""


def snapshot(path: str) -> list[tuple]:
    """The active bookings, straight from the file: (booking_id, phone, day, at, revision)."""
    db = sqlite3.connect(path)
    try:
        return db.execute(
            'SELECT booking_id, phone, day, at, revision FROM bookings WHERE active = 1 ORDER BY id'
        ).fetchall()
    finally:
        db.close()


def take(path: str, phone: str, day: str, at: str) -> str:
    """Another caller books this time, through its own diary on the same file."""
    other = Diary(path=path, clock=CLOCK)
    other.find_or_create_customer(phone)
    other.find_slots(phone, day, 'haircut', CLOCK, offered_to='elsewhere', prefer=at)
    return other.save_booking(phone, 'book', slot(day, at), '', False, CLOCK, offered_to='elsewhere')[
        'booking_id'
    ]


async def ask_to_move(flow, call, script, held: str, at: str) -> list[str]:
    """The caller asks to move their Friday booking to `at` the same day; the move is read back."""
    await turn(
        flow,
        call,
        script,
        f'Can you move my Friday haircut to {at}?',
        ('collect', {'service': 'haircut', 'booking_day': 'Friday', 'booking_time': at}),
        ('call', 'options'),
        ('say', 'That is free. Shall I move it?'),
    )
    return await turn(
        flow,
        call,
        script,
        'Yes, that one.',
        ('collect', {'appointment': held, 'slot': slot(FRIDAY, at)}),
        ('call', 'move_appointment'),
    )


def the_move_intent(call) -> str:
    [status] = [i.status for i in call.intents if i.operation == 'move_appointment'][-1:]
    return status


async def a_taken_destination_is_refused_and_another_time_moves():
    """V1."""
    script, flow, call, diary = await at_booking()
    held = seed(diary)
    said = await ask_to_move(flow, call, script, held, '14:00')
    assert said[-1].startswith('Shall I move your haircut from Friday the 2nd at 9:00 AM'), said
    theirs = take(diary.path, OTHER, FRIDAY, '14:00')
    said = await turn(
        flow,
        call,
        script,
        'Yes, move it.',
        ('call', 'move_appointment'),
        ('say', 'Sorry, that time just went.'),
    )
    assert the_move_intent(call) == 'rejected', ('a diary refusal was left unknown', call.intents)
    assert call.locale.unsettled not in said, said
    assert snapshot(diary.path) == [(held, PHONE, FRIDAY, '09:00', 1), (theirs, OTHER, FRIDAY, '14:00', 1)]
    assert call.state.facts.slot == '' and call.state.facts.appointment == '', 'the refused picks stayed'
    retry = str(script.requests[-1].messages[-1].parts)
    assert 'no longer on offer: call options again' in retry, retry
    said = await ask_to_move(flow, call, script, held, '15:00')
    assert (
        said[-1] == 'Shall I move your haircut from Friday the 2nd at 9:00 AM to Friday the 2nd at 3:00 PM?'
    ), said
    said = await turn(flow, call, script, 'Yes.', ('call', 'move_appointment'))
    assert said == ["That's moved to Friday the 2nd at 3:00 PM. Anything else I can help with?"], said
    assert snapshot(diary.path) == [(held, PHONE, FRIDAY, '15:00', 2), (theirs, OTHER, FRIDAY, '14:00', 1)]


async def a_booking_changed_elsewhere_is_refused_then_offered_fresh():
    """V2: the same customer moves the booking from another line after this call was shown it."""
    script, flow, call, diary = await at_booking()
    held = seed(diary)
    await ask_to_move(flow, call, script, held, '14:00')
    elsewhere = Diary(path=diary.path, clock=CLOCK)
    elsewhere.show_holdings('app', PHONE, CLOCK.date())
    elsewhere.find_slots(PHONE, FRIDAY, 'haircut', CLOCK, offered_to='app', prefer='11:00')
    elsewhere.save_booking(
        PHONE, 'move', slot(FRIDAY, '11:00'), held, False, CLOCK, offered_to='app', expected_revision=1
    )
    await turn(flow, call, script, 'Yes.', ('call', 'move_appointment'), ('say', 'It changed; let me look.'))
    assert the_move_intent(call) == 'rejected', call.intents
    assert snapshot(diary.path) == [(held, PHONE, FRIDAY, '11:00', 2)], snapshot(diary.path)
    await ask_to_move(flow, call, script, held, '14:00')
    await turn(flow, call, script, 'Yes.', ('call', 'move_appointment'))
    assert snapshot(diary.path) == [(held, PHONE, FRIDAY, '14:00', 3)], snapshot(diary.path)


def refusals_name_the_request():
    """V3: at the function boundary, where the receipt is built, and through the operation's own binding rule."""
    from core import Intent
    from tests.script import Script, flow_for

    operations = {op.name: op for op in flow_for(Script()).operations()}
    diary = Diary(clock=CLOCK)
    salon = Salon(diary=diary, call='v3')
    mine = seed(diary)
    theirs = take(diary.path, OTHER, FRIDAY, '15:00')
    intent = Intent(id='a' * 32, operation='cancel_appointment', args='{}')
    diary.show_holdings('v3', PHONE, CLOCK.date())
    cases = {
        "another caller's booking": (
            'cancel_appointment',
            Cancel(phone=PHONE, booking_id=theirs),
            lambda args: cancel(salon, args, intent),
        ),
        'a move of it too': (
            'move_appointment',
            Move(phone=PHONE, slot=slot(FRIDAY, '16:00'), booking_id=theirs),
            lambda args: move(salon, args, intent),
        ),
        'a fenced attempt': (
            'move_appointment',
            Move(phone=PHONE, slot=slot(FRIDAY, '16:00'), booking_id=mine),
            lambda args: find_move(salon, args, Intent(id='b' * 32, operation='move_appointment', args='{}')),
        ),
    }
    diary.save_booking(PHONE, 'cancel', '', mine, False, CLOCK, offered_to='v3', expected_revision=1)
    cases['one already cancelled'] = (
        'cancel_appointment',
        Cancel(phone=PHONE, booking_id=mine),
        lambda args: cancel(salon, args, Intent(id='c' * 32, operation='cancel_appointment', args='{}')),
    )
    for name, (operation, args, run) in cases.items():
        receipt = run(args)
        op = operations[operation]
        assert receipt.status in ('refused', 'void') and receipt.booking_id == args.booking_id, (
            name,
            receipt,
        )
        assert receipt.revision == 0, (name, 'a refusal claimed a revision')
        assert op._bound(receipt.model_dump(), args) == [], (name, 'the refusal is not bound to its request')
        assert op.outcome(receipt.model_dump()) == 'rejected', name


async def main() -> None:
    await a_taken_destination_is_refused_and_another_time_moves()
    await a_booking_changed_elsewhere_is_refused_then_offered_fresh()
    refusals_name_the_request()
    print('moves ok: V1-V3, refusals bound to their request on a shared diary file')


if __name__ == '__main__':
    asyncio.run(main())
