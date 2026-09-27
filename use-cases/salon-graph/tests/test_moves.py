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
  `rejected`, and nothing about it stands;
* V4: trace ce40c124 replayed, the day changed in booking and via concierge:
  book, pick the appointment, change the day, pick a time, move, cancel,
  with no pick refused;
* V5: two appointments held, the one picked survives a day change; an id
  nobody offered is refused, with a refusal that says what to do;
* V6: a value said again (a no, a question, a condition, a quotation; heard,
  unheard or stale) approves no booking, move, cancel or complaint;
* V7: a missing pick, and a day the diary cannot place, each say their own
  next step, never "ask the caller" or "nothing is free";
* V8: the live check's contender (`evals.contend`) takes the time when the
  move's read-back reaches the log, and the move is refused;
* V9: live call CAc82c12: a day recorded beside an id not on offer is kept,
  and the times offered next are that day's;
* V10: live call 5: a day said as a weekday and as a date is one day, and
  keeps the times on offer;
* V11: live call s4e-worker-bystander: the time the caller already holds is
  theirs, not "taken";
* V12 (session 4f): times offered for one day, then a new day in the same
  turn: the old day's times are not said, only what can be picked now;
* V13 (session 4f, trace e67c9034): the booking this call just made is on
  offer to move or cancel with no second lookup; another caller's booking is
  still refused; with two held, code picks neither.
"""

import asyncio
import sqlite3
import time

from salon.diary import Diary
from salon.functions import Cancel, Move, Salon, cancel, find_move, move
from tests.script import CLOCK, PHONE, slot, turn
from tests.test_business import FRIDAY, TUE, at_booking, read_back, seed

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


def refusals_in(script) -> list[str]:
    """Every tool refusal the agents were sent, oldest first."""
    from pydantic_ai.messages import RetryPromptPart

    return [
        str(part.content)
        for request in script.requests
        for message in request.messages[-1:]
        for part in message.parts
        if isinstance(part, RetryPromptPart)
    ]


async def book_move_and_cancel(via_concierge: bool):
    """V4 (trace ce40c124): book, ask to move, pick the appointment, change the day, pick a new time, move, cancel.

    The new day takes the times on offer away and keeps the appointment being
    moved; the cancel after the move names the same appointment, which this
    call has seen at the revision the move made.
    """
    script, flow, call, diary = await at_booking()
    await read_back_friday(flow, call, script)
    await turn(flow, call, script, 'Yes.', ('call', 'book_appointment'))
    [held] = [b['booking_id'] for b in diary.bookings_for(PHONE, CLOCK.date())]
    await turn(
        flow,
        call,
        script,
        'Actually, can you move the appointment?',
        ('call', 'options'),
        ('collect', {'appointment': held}),
        ('say', 'Which day would you like instead?'),
    )
    if via_concierge:
        await turn(
            flow,
            call,
            script,
            'Same time on Tuesday.',
            ('call', 'hand_over', {'to': 'concierge', 'because': 'they want Tuesday'}),
            ('collect', {'booking_day': 'Tuesday', 'booking_time': '14:00'}),
            ('call', 'hand_over', {'to': 'booking', 'because': 'move to Tuesday at two'}),
            ('say', 'Let me look at Tuesday.'),
        )
    else:
        await turn(
            flow,
            call,
            script,
            'Same time on Tuesday.',
            ('collect', {'booking_day': 'Tuesday', 'booking_time': '14:00'}),
            ('say', 'Let me look at Tuesday.'),
        )
    assert call.state.agent == 'booking' and call.state.facts.appointment == held, (
        'the day change forgot the appointment being moved',
        call.state,
    )
    said = await turn(
        flow,
        call,
        script,
        'Two, please.',
        ('call', 'options'),
        ('collect', {'slot': slot(TUE, '14:00')}),
        ('call', 'move_appointment'),
    )
    assert said[-1] == 'Shall I move your haircut from Friday the 2nd at 2:00 PM to tomorrow at 2:00 PM?', (
        said
    )
    await turn(flow, call, script, 'Yes.', ('call', 'move_appointment'))
    said = await turn(
        flow,
        call,
        script,
        'Actually, just cancel it.',
        ('collect', {'appointment': held}),
        ('call', 'cancel_appointment'),
    )
    assert said[-1] == 'Shall I cancel your haircut tomorrow at 2:00 PM?', said
    said = await turn(flow, call, script, 'Yes.', ('call', 'cancel_appointment'))
    assert said == ["That's cancelled for you. Anything else I can help with?"], said
    assert snapshot(diary.path) == [], snapshot(diary.path)
    assert [i.status for i in call.intents] == ['succeeded'] * 3, call.intents
    assert not [r for r in refusals_in(script) if 'on offer' in r], refusals_in(script)


async def read_back_friday(flow, call, script):
    await turn(
        flow,
        call,
        script,
        'A haircut Friday at two.',
        ('collect', {'service': 'haircut', 'booking_day': 'Friday', 'booking_time': '14:00'}),
        ('call', 'options'),
        ('say', 'Two is free.'),
    )
    await turn(
        flow, call, script, 'Two.', ('collect', {'slot': slot(FRIDAY, '14:00')}), ('call', 'book_appointment')
    )


async def two_holdings_and_a_reference_not_on_offer():
    """V5: with two appointments held, the one picked survives a day change; an id nobody offered is refused."""
    script, flow, call, diary = await at_booking()
    first = seed(diary)
    diary.find_slots(PHONE, FRIDAY, 'haircut', CLOCK, offered_to='seed', prefer='11:00')
    second = diary.save_booking(PHONE, 'book', slot(FRIDAY, '11:00'), '', True, CLOCK, offered_to='seed')[
        'booking_id'
    ]
    await turn(
        flow,
        call,
        script,
        'Move my eleven o clock.',
        ('call', 'options'),
        ('collect', {'appointment': 'BK9999'}),
        ('say', 'Which one?'),
    )
    assert call.state.facts.appointment == '', 'an appointment nobody offered was recorded'
    assert 'is not on offer for appointment: call options' in refusals_in(script)[-1], refusals_in(script)
    await turn(
        flow,
        call,
        script,
        'The eleven o clock, to Saturday.',
        ('collect', {'appointment': second}),
        ('collect', {'service': 'haircut', 'booking_day': 'Saturday', 'booking_time': '11:00'}),
        ('say', 'Saturday, then.'),
    )
    assert call.state.facts.appointment == second and first != second


REPLIES = (
    'No, do not book it. I am only checking if it is free.',
    'Is that one free?',
    'Only if it is still free on Friday.',
    'I said "that one". Not yet, though.',
)
"""Replies that name the read-back's value again and agree to nothing: a no, a question, a condition, a quotation."""


def nothing_went_out(call, diary, said: list[str], before: list[tuple]) -> None:
    assert snapshot(diary.path) == before and not diary.complaints(), ('a value said again wrote', said)
    assert call.locale.restated not in said, said
    assert all(i.status == 'prepared' for i in call.intents), call.intents
    assert not call.approved(), 'a value said again approved the read-back'


async def a_value_said_again_is_not_a_yes():
    """V6 (session 4d, defect 1): recording an unchanged value never approves a booking, move, cancel or complaint.

    Each reply below says the read-back's value again, and the scripted agent
    only records it, as a model reading the words would, then looks again.
    Nothing is carried out, no `Locale.restated` is said, and the caller
    hears the salon's offer again (booking is rendered since session 4e). A
    heard read-back, an unheard one and one asked two turns ago are all tried.
    """
    for words in REPLIES:
        script, flow, call, diary = await at_booking()
        await read_back(flow, call, script)
        said = await turn(
            flow,
            call,
            script,
            words,
            ('collect', {'slot': slot(FRIDAY, '14:00')}),
            ('call', 'options'),
            ('call', 'wait'),
        )
        nothing_went_out(call, diary, said, [])
        assert said == ['2:00 PM on Friday the 2nd is free. I also have 1:30 PM or 2:30 PM.'], (words, said)

    script, flow, call, diary = await at_booking()
    await read_back(flow, call, script, heard=False)
    said = await turn(
        flow, call, script, 'Friday at two.', ('collect', {'slot': slot(FRIDAY, '14:00')}), ('say', 'Sure.')
    )
    nothing_went_out(call, diary, said, [])

    script, flow, call, diary = await at_booking()
    await read_back(flow, call, script)
    await turn(flow, call, script, 'How much is it?', ('say', 'It is 28 euros.'))
    said = await turn(
        flow, call, script, 'Friday at two.', ('collect', {'slot': slot(FRIDAY, '14:00')}), ('say', 'Sure.')
    )
    nothing_went_out(call, diary, said, [])

    for words in REPLIES:
        script, flow, call, diary = await at_booking()
        held = seed(diary)
        await ask_to_move(flow, call, script, held, '14:00')
        before = snapshot(diary.path)
        said = await turn(
            flow,
            call,
            script,
            words,
            ('collect', {'appointment': held, 'slot': slot(FRIDAY, '14:00')}),
            ('say', 'Sure.'),
        )
        nothing_went_out(call, diary, said, before)

        script, flow, call, diary = await at_booking()
        held = seed(diary)
        said = await turn(
            flow,
            call,
            script,
            'Cancel my Friday haircut.',
            ('call', 'options'),
            ('collect', {'appointment': held}),
            ('call', 'cancel_appointment'),
        )
        assert said[-1] == 'Shall I cancel your haircut Friday the 2nd at 9:00 AM?', said
        before = snapshot(diary.path)
        said = await turn(flow, call, script, words, ('collect', {'appointment': held}), ('say', 'Sure.'))
        nothing_went_out(call, diary, said, before)

    complaint = 'The colour came out patchy and uneven'
    script, flow, call, diary = await at_booking()
    call.commit(agent='complaints')
    said = await turn(
        flow,
        call,
        script,
        f'{complaint}.',
        ('collect', {'complaint': complaint}),
        ('call', 'record_complaint'),
    )
    assert said[-1].startswith('Shall I record your complaint in your words'), said
    said = await turn(
        flow,
        call,
        script,
        f'No, do not file it. {complaint}, that is all I said.',
        ('collect', {'complaint': complaint}),
        ('say', 'Understood, nothing is filed.'),
    )
    nothing_went_out(call, diary, said, [])


async def each_problem_says_its_own_next_step():
    """V7 (session 4d, defect 5, trace 5edecd4c): a missing pick, a day the diary cannot place, a time not free.

    Each tells the agent a different next step. A missing pick is not
    something to ask the caller for again; a day that cannot be placed is not
    a full day.
    """
    script, flow, call, diary = await at_booking()
    held = seed(diary)
    await turn(
        flow,
        call,
        script,
        'Move my Friday haircut to Tuesday.',
        ('call', 'options'),
        ('collect', {'appointment': held, 'service': 'haircut', 'booking_day': 'Tuesday'}),
        ('call', 'move_appointment'),
        ('call', 'wait'),
    )
    missing = refusals_in(script)[-1]
    assert missing.startswith('Not yet: no slot picked yet: call options'), missing
    assert 'ask the caller' not in missing, missing
    await turn(
        flow,
        call,
        script,
        'Whenever suits.',
        ('collect', {'booking_day': 'whenever suits'}),
        ('call', 'options'),
        ('say', 'Which day would you like?'),
    )
    [options] = [
        r for r in script.requests[-1].messages[-1].parts if getattr(r, 'tool_name', '') == 'options'
    ]
    note = str(options.content)
    assert 'Nothing was looked up' in note and 'says nothing about what is free' in note, note
    assert 'Nothing is free' not in note, note


async def a_contender_takes_the_time_the_bot_reads_back():
    """V8: the live check's second caller (`evals.contend`), offline: it waits for the move's read-back in the log.

    The bot's log is stood in for by a file the test writes the TTS line to,
    as Pipecat's TTS logs it; the booking goes through `Diary` on the same
    SQLite file, and the move then gets the diary's refusal.
    """
    import tempfile
    import threading
    from pathlib import Path

    from evals.contend import take, watch

    script, flow, call, diary = await at_booking()
    held = seed(diary)
    said = await ask_to_move(flow, call, script, held, '14:00')
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / 'salon-bot.log'
        log.write_text('')
        found: list[str] = []
        waiting = threading.Thread(
            target=lambda: found.append(watch(log, ['Shall I move your haircut', '2:00 PM'], timeout=5))
        )
        waiting.start()
        time.sleep(0.2)
        with log.open('a') as handle:
            handle.write(f'2026-09-28 08:30:00.000 | DEBUG | tts - Generating TTS [{said[-1]}]\n')
        waiting.join()
    assert found and found[0], 'the read-back line was not seen'
    taken = take(diary.path, OTHER, FRIDAY, '14:00', now=CLOCK)
    assert taken['status'] == 'booked', taken
    await turn(
        flow, call, script, 'Yes.', ('call', 'move_appointment'), ('say', 'Sorry, that time just went.')
    )
    assert the_move_intent(call) == 'rejected', call.intents
    assert snapshot(diary.path)[0][:4] == (held, PHONE, FRIDAY, '09:00'), snapshot(diary.path)


async def a_move_request_keeps_its_day():
    """V9 (live call CAc82c12, session 4d call 1): the move's day recorded beside an id not on offer yet.

    The id is refused and the day and time beside it are kept, so the times
    offered next are the new day's.
    """
    script, flow, call, diary = await at_booking()
    held = seed(diary)
    await turn(
        flow,
        call,
        script,
        'Can you move it to Wednesday, same time?',
        ('collect', {'booking_day': 'Wednesday', 'booking_time': '09:00', 'appointment': held}),
        ('call', 'options'),
        ('say', 'Wednesday at nine is free. Shall I move it?'),
    )
    facts = call.state.facts
    assert (facts.booking_day, facts.booking_time, facts.appointment) == ('2026-09-30', '09:00', ''), facts
    refusal = next(r for r in refusals_in(script) if 'on offer' in r)
    assert refusal.startswith('Recorded: booking_day, booking_time. Not recorded:'), refusal
    times = next(
        str(part.content)
        for request in script.requests
        for part in request.messages[-1].parts
        if getattr(part, 'tool_name', '') == 'options'
    )
    assert '2026-09-30T0900|haircut' in times, "the times offered were not the new day's"


async def a_day_said_two_ways_is_one_day():
    """V10 (live call CA4e5964, session 4d call 5): "thursday", then "2026-10-01" from concierge, is one request.

    Booked Tuesday, asked to move to Thursday, the times shown; booking then
    handed over to concierge, which recorded the day again as a date. The
    state holds the day as the date it means, so nothing changed: the time
    on offer stays pickable and the move is read back. A real change of day
    still takes the times away.
    """
    script, flow, call, diary = await at_booking()
    await read_back(flow, call, script, day='Tuesday', at='11:00', words='Tuesday at eleven.')
    await turn(flow, call, script, 'Yes.', ('call', 'book_appointment'))
    [held] = [b['booking_id'] for b in diary.bookings_for(PHONE, CLOCK.date())]
    await turn(
        flow,
        call,
        script,
        'Can you move it?',
        ('call', 'options'),
        ('collect', {'appointment': held}),
        ('say', 'Which day?'),
    )
    await turn(
        flow,
        call,
        script,
        'Thursday at three.',
        ('collect', {'booking_day': 'thursday', 'booking_time': '15:00'}),
        ('call', 'options'),
        ('say', 'Thursday at three is free.'),
    )
    thursday = slot('2026-10-01', '15:00')
    assert call.state.facts.booking_day == '2026-10-01' and thursday in call._offers['slot'], (
        call.state.facts,
        call._offers,
    )
    await turn(
        flow,
        call,
        script,
        'Thursday, yes.',
        ('collect', {'booking_day': 'Thursday'}),
        ('say', 'At three?'),
    )
    assert thursday in call._offers.get('slot', {}), 'the same day said again took the times away'
    await turn(
        flow,
        call,
        script,
        'Yes, Thursday the first.',
        ('call', 'hand_over', {'to': 'concierge', 'because': 'confirm the day'}),
        ('collect', {'booking_day': '2026-10-01'}),
        ('call', 'hand_over', {'to': 'booking', 'because': 'move to Thursday at three'}),
        ('collect', {'slot': thursday}),
        ('call', 'move_appointment'),
    )
    assert not [r for r in refusals_in(script) if 'on offer' in r], (
        'a day spelled again took the time on offer away',
        refusals_in(script),
    )
    assert call.state.facts.slot == thursday, call.state.facts
    await turn(
        flow,
        call,
        script,
        'Actually, Friday.',
        ('collect', {'booking_day': 'friday'}),
        ('say', 'Let me look at Friday.'),
    )
    facts = call.state.facts
    assert (facts.booking_day, facts.slot, facts.appointment) == ('2026-10-02', '', held), facts
    assert not [s for s in call._offers.get('slot', {}) if s.startswith('2026-10-01')], (
        'a real change of day kept the Thursday times'
    )


async def your_own_booking_is_not_taken():
    """V11 (session 4e, live call s4e-worker-bystander): the caller's own time was offered away as "taken".

    Moved to Tuesday at nine, the caller said "Tuesday at nine" again; the
    offer said "9:00 AM is taken. On Tuesday I have 10:00 AM ...", and they
    moved to ten. The time they asked for is theirs: the salon says so.
    """
    script, flow, call, diary = await at_booking()
    seed(diary, TUE, '09:00')
    said = await turn(
        flow,
        call,
        script,
        'Tuesday at nine, yes.',
        ('collect', {'service': 'haircut', 'booking_day': 'Tuesday', 'booking_time': '09:00'}),
        ('call', 'options'),
        ('call', 'wait'),
    )
    assert said == ['You already have a haircut tomorrow at 9:00 AM. Would you like a different time?'], said


async def the_booking_just_made_can_be_moved_without_looking_it_up():
    """V13 (session 4f, live call s4e-worker-affected, trace e67c9034, observation abbc0e2a).

    Booked BK0002, then asked to move it: `collect(appointment="BK0002")` was
    refused as not on offer, and `options` returned the same booking. Now a
    successful change offers the resource it touched again, by the salon's
    own `held_bookings`, so the same id goes from book to move to cancel with
    no "not on offer" retry. It is still only an offer: another caller's
    booking is refused, and with two bookings held nothing is picked for the
    caller.
    """
    script, flow, call, diary = await at_booking()
    await read_back_friday(flow, call, script)
    await turn(flow, call, script, 'Yes.', ('call', 'book_appointment'))
    [held] = [b['booking_id'] for b in diary.bookings_for(PHONE, CLOCK.date())]
    theirs = take(diary.path, OTHER, TUE, '11:00')
    before = len(script.requests)
    await turn(
        flow,
        call,
        script,
        'Can I move that one?',
        ('collect', {'appointment': theirs}),
        ('say', 'Which appointment?'),
    )
    assert call.state.facts.appointment == '', "another caller's booking was picked"
    await turn(
        flow,
        call,
        script,
        'The one I just booked.',
        ('collect', {'appointment': held}),
        ('say', 'Which day would you like instead?'),
    )
    assert call.state.facts.appointment == held, (
        'the booking just made was not on offer',
        dict(call._offers),
    )
    assert len(script.requests) - before == 4, ('a known booking cost extra requests', script.steps[before:])
    said = await turn(
        flow,
        call,
        script,
        'Tuesday at two.',
        (
            'calls',
            [('collect', {'values': {'booking_day': 'Tuesday', 'booking_time': '14:00'}}), ('options', {})],
        ),
        ('collect', {'slot': slot(TUE, '14:00')}),
        ('call', 'move_appointment'),
    )
    assert said[-1] == 'Shall I move your haircut from Friday the 2nd at 2:00 PM to tomorrow at 2:00 PM?', (
        said
    )
    await turn(flow, call, script, 'Yes.', ('call', 'move_appointment'))
    assert held in call._offers['appointment'], 'the booking just moved was not on offer to cancel'
    await turn(flow, call, script, 'Cancel it, actually.', ('call', 'cancel_appointment'))
    said = await turn(flow, call, script, 'Yes.', ('call', 'cancel_appointment'))
    assert said == ["That's cancelled for you. Anything else I can help with?"], said
    assert snapshot(diary.path) == [(theirs, OTHER, TUE, '11:00', 1)], snapshot(diary.path)
    assert [i.status for i in call.intents] == ['succeeded'] * 3, call.intents
    moved = [e.resource for e in call.evidence if e.changes]
    assert moved == [held] * 3, ('one booking should carry the whole call', moved)
    assert held not in call._offers.get('appointment', {}), 'a cancelled booking was offered back'
    assert not [r for r in refusals_in(script) if 'on offer' in r and held in r], refusals_in(script)

    script, flow, call, diary = await at_booking()
    seed(diary, FRIDAY, '09:00')
    await read_back_friday(flow, call, script)
    await turn(flow, call, script, 'Yes.', ('call', 'book_appointment'))
    assert len(call._offers['appointment']) == 2 and call.state.facts.appointment == '', (
        'with two bookings held, one was picked for the caller',
        dict(call._offers),
        call.state.facts,
    )


async def an_offer_changed_in_the_same_turn_is_not_said():
    """V12 (session 4f): the words for an offer go with its ids, on the salon as on the desk (core R15)."""
    script, flow, call, _ = await at_booking()
    said = ' '.join(
        await turn(
            flow,
            call,
            script,
            'A haircut on Thursday. No, sorry, Friday.',
            (
                'calls',
                [('collect', {'values': {'service': 'haircut', 'booking_day': 'Thursday'}}), ('options', {})],
            ),
            ('calls', [('collect', {'values': {'booking_day': 'Friday'}}), ('wait', {})]),
        )
    )
    assert 'Thursday' not in said and '1st' not in said, ("the old day's times were said", said)
    assert 'Friday' in said and all(s.startswith('2026-10-02') for s in call._offers.get('slot', {})), (
        said,
        dict(call._offers),
    )


async def main() -> None:
    await a_taken_destination_is_refused_and_another_time_moves()
    await a_booking_changed_elsewhere_is_refused_then_offered_fresh()
    refusals_name_the_request()
    await book_move_and_cancel(via_concierge=False)
    await book_move_and_cancel(via_concierge=True)
    await two_holdings_and_a_reference_not_on_offer()
    await a_value_said_again_is_not_a_yes()
    await each_problem_says_its_own_next_step()
    await a_contender_takes_the_time_the_bot_reads_back()
    await a_move_request_keeps_its_day()
    await a_day_said_two_ways_is_one_day()
    await your_own_booking_is_not_taken()
    await an_offer_changed_in_the_same_turn_is_not_said()
    await the_booking_just_made_can_be_moved_without_looking_it_up()
    print(
        'moves ok: V1-V13, refusals bound to their request, picks kept across a day change, no yes by repetition'
    )


if __name__ == '__main__':
    asyncio.run(main())
