"""Session 4g: the two browser calls of 28 September 2026, replayed on the real flow, and the contract they found.

Langfuse traces `3771f9b3353836889ad8dfe60edbb9eb` (A: book once) and
`28723e6be948136b55783e693281906a` (B: book, move, cancel). Every caller line
below is the speech-to-text of that call. Each conversation is played twice on
a fresh diary on the fixed clock: with the moves the live model sent
(`observed`, flat tool arguments as Gemini sent them), and with the moves the
repaired contract lets a model send (`repaired`). A turn one variant does not
have is a caller repetition the other needed.

* G1 (B, 07:24:14): `collect(booking_time='09:00', slot=<Friday 9:00>)` recorded
  the time and refused the slot, because the same update took the offer the
  slot came from. A pick is now judged against the offer for the state the
  update ends in, made by code: read back in the same turn, one request.
* G2 (B, 07:24:38): the caller asked to move the booking over its receipt.
  The receipt was said again and the request was dropped: the caller said it
  again. Now the receipt is said again and the node's agent reads the request
  in the same turn: a fresh read-back, nothing moved, nothing booked twice.
* G3: a plain agreement over a cut receipt: the receipt again, nothing else,
  nothing changed.
* G4: stale, foreign, made-up and incompatible picks are still refused.
* G5 (B, 07:25:18): a cancel of the booking on offer needs no lookup; the
  rendered booking step has no `options` tool.
* G6: a refused value in the flat argument shape a live model sends gets the
  salon's refusal words (before, only the scripted shape did).
* G7: the read-back after a receipt, cut in its turn, approves nothing, and
  the caller's next words are still read.

`python -m tests.test_contract --table` prints requests per turn and caller
repetitions for both variants. Scripted models prove what the runtime allows
and what it costs, never what a live model will choose.
"""

import asyncio
import sqlite3
import sys
from dataclasses import dataclass

from tests.script import PHONE, Script, opened, slot, turn
from tests.test_business import FRIDAY, SATURDAY, at_booking, booked

CALLER = '+34680840464'
"""The number the browser caller gave in both traces: a new customer."""
FRI9, SAT9 = slot(FRIDAY, '09:00'), slot(SATURDAY, '09:00')
BOOKING = 'BK0001'
"""The id a fresh diary gives its first booking."""
MOVE_TO_SATURDAY = {'appointment': BOOKING, 'booking_day': SATURDAY, 'booking_time': '09:00'}
RECEIPT = "You're all set for Friday the 2nd at 9:00 AM. Anything else I can help with?"
MOVE_READ_BACK = 'Shall I move your haircut from Friday the 2nd at 9:00 AM to Saturday the 3rd at 9:00 AM?'


def collect(**values) -> tuple:
    """A `collect` in the flat argument shape a live model sends."""
    return ('call', 'collect', values)


def calls(*pairs) -> tuple:
    """One model response carrying these tool calls, in order."""
    return ('calls', list(pairs))


@dataclass(frozen=True)
class Line:
    """One caller turn: what they said, the moves each variant's model sends (None: the variant has no such turn)."""

    words: str
    observed: tuple | None
    repaired: tuple | None
    heard: bool = True
    """False: the caller cut in before any of the reply played (acknowledged with nothing heard)."""


OPENING = (
    Line(
        'Hey Robin, I would like to book a haircut appointment for Friday.',
        (
            collect(service='haircut', booking_day='Friday', booking_time=None),
            ('call', 'hand_over', {'to': 'booking', 'because': 'a haircut'}),
        ),
        (
            collect(service='haircut', booking_day='Friday', booking_time=None),
            ('call', 'hand_over', {'to': 'booking', 'because': 'a haircut'}),
        ),
    ),
)

MOVE_OVER_RECEIPT = (
    collect(**MOVE_TO_SATURDAY),
    calls(('collect', {'slot': SAT9}), ('move_appointment', {})),
)
"""The repaired replies to the move asked for over the cut receipt: the request, then the pick and the move."""

TRACE_A = (
    *OPENING,
    Line('+34 680 840 464.', (collect(customer_phone=CALLER),), (collect(customer_phone=CALLER),)),
    Line(
        'Yes.',
        (('call', 'lookup_customer'), ('call', 'options'), ('call', 'wait')),
        (('call', 'lookup_customer'), ('call', 'wait')),
    ),
    Line('9:00 AM.', (collect(slot=FRI9),), (collect(slot=FRI9),)),
    Line('Yes, please.', (('call', 'book_appointment'),), (('call', 'book_appointment'),)),
    Line("I'm good, thanks.", (('call', 'goodbye'),), (('call', 'goodbye'),)),
)
"""Trace 3771f9b3: verification and one booking, no tool errors."""

TRACE_B = (
    *OPENING,
    Line("It's +34 680 840 46. 4.", (collect(customer_phone=CALLER),), (collect(customer_phone=CALLER),)),
    Line(
        'That sounds about right.',
        (('call', 'lookup_customer'), ('call', 'options'), ('call', 'wait')),
        (('call', 'lookup_customer'), ('call', 'wait')),
    ),
    Line(
        "Uh, let's do 9:00 AM.",
        (
            collect(booking_time='09:00', slot=FRI9),
            ('call', 'options'),
            ('call', 'book_appointment'),
            ('call', 'wait'),
        ),
        (collect(booking_time='09:00', slot=FRI9),),
    ),
    Line('9:00 AM is perfect.', (collect(slot=FRI9),), None),
    Line('Yes, please.', (('call', 'book_appointment'),), (('call', 'book_appointment'),), heard=False),
    Line(
        'Sorry, can I— can you actually move the appointment to Saturday the same time?',
        (),
        MOVE_OVER_RECEIPT,
    ),
    Line(
        'If you can move the appointment to Saturday the same time.',
        (collect(**MOVE_TO_SATURDAY), ('call', 'options'), ('call', 'wait')),
        None,
    ),
    Line('Hmm, no, I asked for 9:00.', (collect(slot=SAT9), ('call', 'move_appointment')), None),
    Line('Yes, please.', (('call', 'move_appointment'),), (('call', 'move_appointment'),)),
    Line(
        'Can you actually cancel at all the appointment?',
        (collect(appointment=BOOKING), ('call', 'options'), ('call', 'cancel_appointment')),
        (('call', 'cancel_appointment'),),
    ),
    Line('Yes.', (('call', 'cancel_appointment'),), (('call', 'cancel_appointment'),)),
    Line("I'm good, thanks.", (('call', 'goodbye'),), (('call', 'goodbye'),)),
)
"""Trace 28723e6b: the refused slot, the receipt replay that dropped a move, the move and the cancel."""
MOVE_LINE = TRACE_B[6]
"""The move asked for over the cut receipt (07:24:38)."""


def history(path: str) -> list[tuple]:
    """Every booking in the file, active or not: (booking_id, phone, day, at, revision, active)."""
    db = sqlite3.connect(path)
    try:
        return db.execute(
            'SELECT booking_id, phone, day, at, revision, active FROM bookings ORDER BY id'
        ).fetchall()
    finally:
        db.close()


async def play(lines, variant: str):
    """Play a conversation in one variant on a fresh call: per turn, what was said and how many requests it took."""
    script, flow, call, diary = opened(phone='')
    played = []
    for line in lines:
        moves = getattr(line, variant)
        if moves is None:
            continue
        before = len(script.requests)
        said = await turn(flow, call, script, line.words, *moves, heard=line.heard)
        if not line.heard:
            call.acknowledge(call.all_spoken[-1].id, heard='')
        played.append((line.words, said, len(script.requests) - before))
    return played, script, call, diary


def said_at(played, words: str) -> tuple[list[str], int]:
    """What the turn with these caller words said, and its requests: the last one, when words repeat."""
    return next((said, requests) for heard, said, requests in reversed(played) if heard == words)


async def a_time_and_its_slot_recorded_together_are_read_back():
    """G1: the time and the slot the caller picked, recorded in one `collect`, are read back by code, one request."""
    played, script, call, diary = await play(TRACE_B[:4], 'repaired')
    said, requests = said_at(played, "Uh, let's do 9:00 AM.")
    assert said == ['Shall I book a haircut, Friday the 2nd at 9:00 AM?'] and requests == 1, (said, requests)
    assert call.state.facts.slot == FRI9 and call.state.facts.booking_time == '09:00', call.state.facts
    assert history(diary.path) == [], 'choosing booked something'
    said = await turn(call_flow(script), call, script, 'Yes, please.', ('call', 'book_appointment'))
    assert said == [RECEIPT] and [b[:5] for b in history(diary.path)] == [
        (BOOKING, CALLER, FRIDAY, '09:00', 1)
    ], said


def call_flow(script: Script):
    assert script.flow is not None
    return script.flow


async def cut_receipt():
    """Trace B up to its booking, picked by the slot alone, whose receipt the caller cut before any of it played.

    The pick is trace A's, so this setup does not rest on G1's repair.
    """
    _, script, call, diary = await play(TRACE_B[:3], 'repaired')
    flow = call_flow(script)
    await turn(flow, call, script, '9:00 AM.', collect(slot=FRI9))
    said = await turn(flow, call, script, 'Yes, please.', ('call', 'book_appointment'), heard=False)
    call.acknowledge(call.all_spoken[-1].id, heard='')
    assert said == [RECEIPT], said
    return script, flow, call, diary


async def a_request_over_a_cut_receipt_is_read_back_in_the_same_turn():
    """G2: the receipt is said again by code, then the node's agent reads the move, in that order, in one turn.

    It sees the booking it made, the receipt and then the caller's new words.
    The move is read back, not carried out; nothing is booked twice; a heard
    yes moves the one booking once.
    """
    script, flow, call, diary = await cut_receipt()
    words = MOVE_LINE.words
    before = len(script.requests)
    said = await turn(flow, call, script, words, *MOVE_OVER_RECEIPT)
    assert said == [RECEIPT, MOVE_READ_BACK], said
    assert len(script.requests) - before == 2, 'the move took more than the two requests it needs'
    first = script.requests[before]
    sent = str(first.messages)
    assert 0 <= sent.rfind(f'TextPart(content="{RECEIPT}') < sent.rfind(f"UserPromptPart(content='{words}"), (
        'the receipt did not come before the request'
    )
    assert 'Done on this call: book_appointment' in first.instructions, 'the node was not shown what was done'
    assert [b[:5] for b in history(diary.path)] == [(BOOKING, CALLER, FRIDAY, '09:00', 1)], (
        'moved or booked again'
    )
    assert [e.operation for e in call.evidence].count('book_appointment') == 1
    said = await turn(flow, call, script, 'Yes, please.', ('call', 'move_appointment'))
    assert said == ["That's moved to Saturday the 3rd at 9:00 AM. Anything else I can help with?"], said
    assert [b[:5] for b in history(diary.path)] == [(BOOKING, CALLER, SATURDAY, '09:00', 2)], history(
        diary.path
    )


async def an_agreement_over_a_cut_receipt_changes_nothing():
    """G3: "Okay, great." over the cut receipt: the receipt again, then nothing more, and nothing changes.

    The node's agent reads it and waits; `wait` says nothing after a receipt
    said again on this piece, and `say_again` would not say it twice.
    """
    for move in (('call', 'wait'), ('call', 'say_again')):
        script, flow, call, diary = await cut_receipt()
        facts, intents = call.state.facts, len(call.intents)
        said = await turn(flow, call, script, 'Okay, great.', move)
        assert said == [RECEIPT], (move, said)
        assert call.state.facts == facts and len(call.intents) == intents, 'an agreement changed something'
        assert [b[:5] for b in history(diary.path)] == [(BOOKING, CALLER, FRIDAY, '09:00', 1)]


async def choices_that_do_not_hold_for_the_final_state_are_refused():
    """G4: a pick counts only when the offer for the state the update ends in has it.

    Incompatible: Friday's 9:00 beside Saturday. Stale: Friday's 9:00 after
    the day moved to Saturday on an earlier turn. Made up: a 9:15 nobody
    offers. Foreign: a time offered to another call. None is recorded, and
    none is read back.
    """
    script, flow, call, diary = await at_booking(service='haircut', booking_day=FRIDAY)
    await turn(flow, call, script, 'Friday.', ('call', 'wait'))
    other, other_flow, other_call, _ = opened(diary=diary, verified_phone=PHONE, customer_status='existing')
    other_call.commit(agent='booking')
    await turn(
        other_flow,
        other_call,
        other,
        'Saturday at five.',
        collect(service='haircut', booking_day=SATURDAY, booking_time='17:00'),
        ('call', 'wait'),
    )
    cases = (
        ('incompatible', {'booking_day': SATURDAY, 'slot': FRI9}),
        ('stale', {'slot': FRI9}),
        ('made up', {'slot': slot(SATURDAY, '09:15')}),
        ('foreign', {'slot': slot(SATURDAY, '17:00')}),
    )
    for name, values in cases:
        said = await turn(flow, call, script, 'That one.', collect(**values), ('call', 'wait'))
        assert call.state.facts.slot == '', (name, call.state.facts.slot)
        assert not any(s.startswith('Shall I') for s in said), (name, said)
    assert booked(diary) == []


async def a_cancel_of_a_held_booking_needs_no_lookup():
    """G5: the rendered booking step has no `options` tool; cancelling the booking on offer is one request.

    Trace B's cancel came after a move, with the booking already recorded;
    here it comes straight after trace A's booking, so the id is recorded in
    the same reply, from the offer the application state shows.
    """
    _, script, call, _diary = await play(TRACE_A[:5], 'repaired')
    assert BOOKING in call._offers.get('appointment', {}), 'the booking just made is not on offer'
    before = len(script.requests)
    said = await turn(
        call_flow(script),
        call,
        script,
        'Can you actually cancel the appointment?',
        calls(('collect', {'appointment': BOOKING}), ('cancel_appointment', {})),
    )
    assert said == ['Shall I cancel your haircut Friday the 2nd at 9:00 AM?'], said
    assert len(script.requests) - before == 1, 'the cancel took more than one request'
    assert all('options' not in r.tools for r in script.requests if r.step == 'booking'), (
        'booking still offers options'
    )


async def a_refused_value_is_said_in_the_live_argument_shape():
    """G6: the salon's refusal words follow a refused pick in the flat shape a live model sends, as in the scripted one."""
    for move in (collect(slot=slot(FRIDAY, '09:15')), ('collect', {'slot': slot(FRIDAY, '09:15')})):
        script, flow, call, _ = await at_booking(service='haircut', booking_day=FRIDAY)
        await turn(flow, call, script, 'Friday.', ('call', 'wait'))
        said = await turn(flow, call, script, 'Quarter past nine.', move, ('call', 'wait'))
        assert said[-1].startswith("That time isn't one I have free."), (move, said)


async def a_read_back_after_a_receipt_cut_again_approves_nothing():
    """G7: the move read back after the receipt is cut in its turn; a yes to it moves nothing, and is read again."""
    script, flow, call, diary = await cut_receipt()
    await turn(flow, call, script, MOVE_LINE.words, *MOVE_OVER_RECEIPT, heard=False)
    call.acknowledge(*(s.id for s in call.spoken if s.text), heard='')
    said = await turn(flow, call, script, 'Yes, please.', ('call', 'move_appointment'))
    assert said[-1] == MOVE_READ_BACK, said
    assert [b[:5] for b in history(diary.path)] == [(BOOKING, CALLER, FRIDAY, '09:00', 1)], (
        'an unheard read-back moved'
    )
    said = await turn(flow, call, script, 'Yes.', ('call', 'move_appointment'))
    assert [b[:5] for b in history(diary.path)] == [(BOOKING, CALLER, SATURDAY, '09:00', 2)], said


async def table() -> None:
    """Requests per caller turn and caller repetitions, both variants of both traces, and each diary's end."""
    for name, lines in (('A 3771f9b3', TRACE_A), ('B 28723e6b', TRACE_B)):
        for variant in ('observed', 'repaired'):
            played, _, _, diary = await play(lines, variant)
            repeats = sum(
                1
                for line in lines
                if getattr(line, variant) is not None
                and getattr(line, 'repaired' if variant == 'observed' else 'observed') is None
            )
            note = (
                " (the live model's moves: meaningful only where `options` exists, at 0306f98)"
                if variant == 'observed'
                else ''
            )
            print(
                f'\n{name} {variant}{note}: {sum(r for *_, r in played)} requests, {len(played)} caller turns, {repeats} repetitions'
            )
            for words, said, requests in played:
                print(f'  {requests}  {words!r} -> {said}')
            print('  diary:', history(diary.path))


async def main() -> None:
    await a_time_and_its_slot_recorded_together_are_read_back()
    await a_request_over_a_cut_receipt_is_read_back_in_the_same_turn()
    await an_agreement_over_a_cut_receipt_changes_nothing()
    await choices_that_do_not_hold_for_the_final_state_are_refused()
    await a_cancel_of_a_held_booking_needs_no_lookup()
    await a_refused_value_is_said_in_the_live_argument_shape()
    await a_read_back_after_a_receipt_cut_again_approves_nothing()
    if '--table' in sys.argv:
        await table()
    print('ok')


if __name__ == '__main__':
    if '--table-only' in sys.argv:
        asyncio.run(table())
    else:
        asyncio.run(main())
