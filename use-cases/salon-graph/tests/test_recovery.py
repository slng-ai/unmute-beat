"""Stage B4: recovery is bounded, a joined turn spends one budget, and nothing outlives its turn or its call.

Driven through the real processor (`tests.test_continuation.Probe`, which
notes the caller's onset as the voice detector would and returns marks as the
transport would):

* B4.1, Coval call C26pPHK6: the caller says yes over the read-back, then yes
  again in pieces over each one said again. The early yes never books; each
  unheard copy counts once against the intent, across joined pieces; after two
  the caller hears a truthful fallback instead of a third read-back, and a
  goodbye says the change was not made;
* B4.2: one unheard copy, then a copy heard in full and a fresh yes: it
  books, once;
* B4.3: a turn joined over several pieces spends one model-request tally;
* B4.4, live call hzSdfMNn (session 4c): on a line whose marks come back,
  the caller talks over two copies, then hears the third in full and says
  yes: it books, once. The two cut copies spent the two-copy limit before;
* B4.5: on that line, a caller who talks over every copy never books, and
  after `CUT_READBACKS` copies hears the fallback instead of another;
* B4.6, the same call: a turn joined over nine pieces, one request each, is
  not cut off at the turn's 8 requests: each piece adds `per_piece`, up to
  twice the turn's own, and past that it is.

B4.1 and B4.2 run on a line that has proven nothing, as Coval's simulator
was: no mark ever comes back, so `READBACKS` applies.

A holding-line timer cut off with its work is checked in core
(`tests.test_nodes.a_cut_off_tool_leaves_no_timer`), and the call ceiling in
`tests.test_ceiling`.
"""

import asyncio

from core.policies import TurnBudget
from core.steps import CUT_READBACKS, READBACKS
from tests.test_continuation import (
    CLOCK,
    PHONE,
    Probe,
    booking_call,
    books,
    call_of,
    played_out,
    read_back_a_haircut,
    said,
    steps,
)


async def book_it(call):
    return ('call', 'book_appointment')


async def yes_over_every_read_back_is_bounded():
    """B4.1."""
    call = booking_call()
    call_of[0] = call
    probe = Probe(call)
    readback = await read_back_a_haircut(call, probe, played=False)
    with steps(booking=book_it):
        probe.interrupt()
        await probe._turn('Yes, please.')
        assert probe.fixed().count(readback) == 2, ('the read-back is said again, unheard', probe.fixed())
        probe.interrupt()
        await probe._turn('Yes.')
    intent = next(i for i in call.intents if i.operation == 'book_appointment')
    assert intent.readbacks == READBACKS and intent.status == 'prepared', intent
    assert books.bookings_for(PHONE, CLOCK.date()) == [], (
        'a yes nobody could have given to the read-back booked'
    )
    assert probe.fixed()[-1] == call.locale.gave_up, probe.fixed()
    assert probe.fixed().count(readback) == 2, 'a third read-back went out past the limit'

    async def leaving(call):
        return ('call', 'goodbye')

    with steps(booking=leaving):
        await probe._turn('OK, bye then.')
    assert call.ended and probe.fixed()[-1].startswith(call.locale.left_undone), probe.fixed()


async def a_heard_copy_after_an_unheard_one_books():
    """B4.2."""
    call = booking_call()
    call_of[0] = call
    probe = Probe(call)
    await read_back_a_haircut(call, probe, played=False)
    with steps(booking=book_it):
        probe.interrupt()
        await probe._turn('Yes, please.')
        played_out(call)
        call.turn_ended(interrupted=False)
        await asyncio.sleep(0.01)
        await probe._turn('Yes, that is right.')
    intent = next(i for i in call.intents if i.operation == 'book_appointment')
    assert intent.readbacks == 1 and intent.status == 'succeeded', intent
    assert len(books.bookings_for(PHONE, CLOCK.date())) == 1, (
        'the fresh yes to a heard copy did not book once'
    )


async def a_greeting_heard(call) -> None:
    """The line proves itself: the greeting's own mark comes back before anything is read back."""
    said(call, 'Hello?')
    await call.speak_now('Hi, you have reached the salon.', origin='entry')
    call.note_reached()
    played_out(call)
    call.turn_ended(interrupted=False)
    assert any(s.delivered == 'delivered' for s in call.all_spoken), 'the greeting was not delivered'


async def two_cut_copies_then_a_heard_one_books():
    """B4.4."""
    call = booking_call()
    call_of[0] = call
    probe = Probe(call)
    await a_greeting_heard(call)
    readback = await read_back_a_haircut(call, probe, played=False)
    with steps(booking=book_it):
        for _ in range(2):
            probe.interrupt()
            await probe._turn('Yes, please.')
        assert probe.fixed().count(readback) == 3 and call.locale.gave_up not in probe.fixed(), (
            'a caller who talked over two copies on a working line was given up on',
            probe.fixed(),
        )
        played_out(call)
        call.turn_ended(interrupted=False)
        await asyncio.sleep(0.01)
        await probe._turn('Yes, that is right.')
    intent = next(i for i in call.intents if i.operation == 'book_appointment')
    assert (intent.readbacks, intent.cut_readbacks, intent.status) == (0, 2, 'succeeded'), intent
    assert len(books.bookings_for(PHONE, CLOCK.date())) == 1, 'the yes to the heard copy did not book once'


async def talking_over_every_copy_is_bounded():
    """B4.5."""
    call = booking_call()
    call_of[0] = call
    probe = Probe(call)
    await a_greeting_heard(call)
    readback = await read_back_a_haircut(call, probe, played=False)
    with steps(booking=book_it):
        for _ in range(CUT_READBACKS):
            probe.interrupt()
            await probe._turn('Yes.')
    intent = next(i for i in call.intents if i.operation == 'book_appointment')
    assert (intent.readbacks, intent.cut_readbacks, intent.status) == (0, CUT_READBACKS, 'prepared'), intent
    assert books.bookings_for(PHONE, CLOCK.date()) == [], 'a yes over an unheard read-back booked'
    assert probe.fixed()[-1] == call.locale.gave_up, probe.fixed()
    assert probe.fixed().count(readback) == CUT_READBACKS, 'a read-back went out past the limit'


async def a_joined_turn_spends_one_budget():
    """B4.3."""
    call = booking_call()
    call_of[0] = call
    probe = Probe(call)
    await read_back_a_haircut(call, probe, played=False)
    gate = asyncio.Event()

    async def slow(call):
        gate.set()
        await asyncio.Event().wait()

    async def answer(call):
        return ('say', 'Sorry, go on.')

    with steps(booking=slow):
        probe.interrupt()
        first = asyncio.create_task(probe._turn('Yes'))
        await asyncio.wait_for(gate.wait(), 3)
        probe.interrupt()
        await asyncio.wait_for(first, 3)
    spent = call._turn_usage[1].requests
    assert spent >= 1, spent
    with steps(booking=answer):
        await probe._turn('please')
    assert call.turn == 1 and call._turn_usage[1].requests == spent + 1, (
        'a piece of the same turn started a fresh tally',
        call._turn_usage,
    )


async def many_pieces_get_more_requests_up_to_a_cap():
    """B4.6."""
    call = booking_call()
    call_of[0] = call
    probe = Probe(call)
    await read_back_a_haircut(call, probe, played=False)
    budget = TurnBudget()
    cap = 2 * budget.model_requests

    async def go_on(call):
        return ('say', 'Go on.')

    with steps(booking=go_on):
        for piece in range(1, cap + 2):
            probe.interrupt()
            await probe._turn(f'piece {piece}')
            spent = call._turn_usage[1].requests
            exhausted = call.locale.exhausted in probe.fixed()
            if piece <= 9:
                assert not exhausted and spent == piece, (piece, spent, probe.fixed())
    assert call.turn == 1 and exhausted and spent == cap, (
        'a joined turn spent past its cap',
        spent,
        call.turn,
    )


async def main() -> None:
    await yes_over_every_read_back_is_bounded()
    await a_heard_copy_after_an_unheard_one_books()
    await a_joined_turn_spends_one_budget()
    await two_cut_copies_then_a_heard_one_books()
    await talking_over_every_copy_is_bounded()
    await many_pieces_get_more_requests_up_to_a_cap()
    print('recovery ok: bounded read-backs, fresh yes, one budget per joined turn, cut copies said again')


if __name__ == '__main__':
    asyncio.run(main())
