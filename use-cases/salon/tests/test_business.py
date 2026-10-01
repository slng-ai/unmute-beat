"""Run: uv run python -m tests.test_business

The new salon on the real flow, with scripted node agents and a fixed diary
clock (Monday 28 September 2026, Europe/Madrid), asserting the diary, the
receipts and the requests. Every step is `speech: structured` (manifest
version 2): the caller's update travels in the step's one decision, and there
is no `collect`. Scripted models prove runtime properties, never language
understanding: that is what the Coval run measures.

* verification: the caller ID is read back by code, a yes looks it up, the
  customer is the receipt, a corrected number is somebody else, and the
  caller's number said back is a yes;
* booking, moving and cancelling: one availability read with the request it
  rests on, read back, then done once on a heard yes; a repeat reads nothing;
* a booking the diary refuses after the yes: nothing booked, and the model
  told the diary's reason with the retry;
* a complaint in the caller's own words, read back in the same request;
* the locale's words for a number that cannot be right, and a past date;
* a stylist consulted mid-booking: it answers, the caller comes back, the
  read-back is asked again fresh, and a yes after it books; a change said to
  the stylist is handed back;
* the refund policy loaded once and kept;
* a whole call's requests per caller turn (1, 3, 1, 1), and every request's
  prompts naming only the tools it has. `--table` prints that call's requests
  in bytes, at the model boundary and at the Gemini adapter's mapping.
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart

from salon.app import SPEC
from salon.diary import Diary, RefusedError
from salon.receipts import Customer
from tests.script import CLOCK, PHONE, opened, slot, turn

TODAY = CLOCK.date()
FRIDAY, TUESDAY = '2026-10-02', '2026-09-29'
KNOWN = Customer(customer_status='existing', customer_phone=PHONE)
AT_TWO = {'service': 'haircut', 'booking_day': FRIDAY, 'booking_time': '14:00'}


def booked(diary: Diary) -> list[tuple[str, str, str]]:
    return [(b['service'], b['date'], b['time']) for b in diary.bookings_for(PHONE, TODAY)]


class Reads:
    """Every diary read of a call, by kind: availability (`find_slots`) apart from the bookings held (`show_holdings`)."""

    def __init__(self, diary: Diary) -> None:
        self.availability: list[str] = []
        self.holdings = 0
        find, show = diary.find_slots, diary.show_holdings

        def find_slots(phone, day, service, at_time, offered_to='', prefer=''):
            self.availability.append(f'{day} {service} {prefer}')
            return find(phone, day, service, at_time, offered_to=offered_to, prefer=prefer)

        def show_holdings(offered_to, phone, today):
            self.holdings += 1
            return show(offered_to, phone, today)

        diary.find_slots, diary.show_holdings = find_slots, show_holdings  # ty: ignore[invalid-assignment]


def at(step: str, **facts: Any):
    """A verified caller already on this step, with the diary's reads counted."""
    script, flow, call, diary = opened(customer=KNOWN, **facts)
    call.commit(agent=step)
    return script, flow, call, diary, Reads(diary)


def reply(say: str, **update: Any) -> tuple[str, str, dict[str, Any]]:
    return ('call', 'reply', {'say': say, **({'update': update} if update else {})})


async def read_back(flow, call, script, reads: Reads, heard: bool = True) -> list[str]:
    """A haircut on Friday at two, looked up with the request it rests on, then picked: three requests in all."""
    before = len(script.requests)
    said = await turn(
        flow,
        call,
        script,
        'A haircut on Friday at two.',
        ('call', 'options', {'update': AT_TWO}),
        reply('On Friday I have 2:00 PM. Does that work?'),
    )
    assert said == ['On Friday I have 2:00 PM. Does that work?'] and len(script.requests) - before == 2, said
    assert reads.availability == [f'{FRIDAY} haircut 14:00'] and reads.holdings == 0, (
        'one availability read; a new booking reads no bookings held'
    )
    said = await turn(
        flow,
        call,
        script,
        'Yes, two is great.',
        ('call', 'book_appointment', {'update': {'slot': slot(FRIDAY, '14:00')}}),
        heard=heard,
    )
    assert (
        said == ['Shall I book a haircut, Friday the 2nd at 2:00 PM?'] and len(script.requests) - before == 3
    )
    return said


def seed(diary: Diary, day: str = FRIDAY, at_time: str = '09:00', additional: bool = False) -> str:
    """A booking the caller already holds, made through the diary's own offer and write."""
    from datetime import date

    diary.find_slots(PHONE, date.fromisoformat(day), 'haircut', CLOCK, offered_to='seed', prefer=at_time)
    return diary.save_booking(PHONE, 'book', slot(day, at_time), '', additional, CLOCK, offered_to='seed')[
        'booking_id'
    ]


def retries(script) -> list[str]:
    return [
        str(part.content)
        for request in script.requests
        for message in request.messages
        for part in message.parts
        if isinstance(part, RetryPromptPart)
    ]


async def verification_reads_the_caller_id_back_and_looks_it_up():
    script, flow, call, diary = opened()
    said = await turn(
        flow,
        call,
        script,
        'A haircut Friday at two, please.',
        ('call', 'hand_over', {'to': 'booking', 'because': 'a haircut', 'update': AT_TWO}),
    )
    assert call.state.agent == 'verify' and said[-1] == 'Is your number +34 600 11 12 22?', said
    await turn(flow, call, script, 'Yes.', ('call', 'lookup_customer'))
    assert call.state.agent == 'booking' and call.state.facts.customer == KNOWN, call.state.facts
    call.commit(agent='verify')
    await turn(
        flow,
        call,
        script,
        'Sorry, use my other number.',
        ('call', 'record', {'update': {'customer_phone': '+34600999888'}}),
    )
    assert call.state.facts.customer is None, 'a corrected number is somebody else until it is looked up'
    assert booked(diary) == []


async def the_number_said_back_is_a_yes():
    """The caller says the number read back to them; code finds it unchanged and looks it up."""
    script, flow, call, _ = opened(clock=lambda: CLOCK)
    said = await turn(flow, call, script, WHOLE_CALL[0], *MOVES[0])
    assert said == ['Is your number +34 600 11 12 22?'], said
    before = len(script.requests)
    said = await turn(
        flow,
        call,
        script,
        'Plus three four, six hundred, eleven, twelve, twenty two.',
        ('call', 'record', {'update': {'customer_phone': '+34600111222'}}),
    )
    assert (
        said
        == [
            "Yes, that's the same as I read back.",
            'Thanks, I have you.',
            'On Friday the 2nd I have 2:00 PM. Which time works for you?',
        ]
        and call.state.facts.customer == KNOWN
    ), (said, call.state.facts.customer)
    assert len(script.requests) - before == 1, (
        'record; then code offers the times on arrival (`offers: {slot: [...]}`)'
    )
    assert script.requests[before].step == 'verify' and 'record' in script.requests[before].tools, (
        'verify offers `record`: a value said again confirms its read-back'
    )


async def a_closed_day_is_said_by_code():
    """`offers: [slot]` on arrival: a closed day offers nothing, and find_times' words ask for another day."""
    script, flow, call, diary = opened(customer=KNOWN)
    reads = Reads(diary)
    sunday = {'service': 'haircut', 'booking_day': '2026-10-04', 'booking_time': '10:00'}
    said = await turn(
        flow,
        call,
        script,
        'A haircut on Sunday at ten.',
        ('call', 'hand_over', {'to': 'booking', 'because': 'a haircut', 'update': sunday}),
    )
    assert said == ["We're closed on Sunday the 4th. Which other day would you like?"], said
    assert [r.step for r in script.requests] == ['concierge'] and len(reads.availability) == 1
    assert 'slot' not in call._offers, 'nothing stands on offer: the next read looks again'


async def a_number_that_cannot_be_right_gets_the_locales_words():
    script, flow, call, _ = opened(phone='')
    call.commit(agent='verify')
    await turn(
        flow,
        call,
        script,
        'Plus three four six seven one two seven three four five four four.',
        ('call', 'record', {'update': {'customer_phone': '+3467127345444'}}),
        reply('I have +34 671 27 34 54 44. Which part is wrong?'),
    )
    assert any('Which part is wrong?' in r and 'phone_too_long' not in r for r in retries(script)), retries(
        script
    )
    assert call.state.facts.customer_phone is None


async def a_heard_yes_books_once():
    script, flow, call, diary, reads = at('booking')
    said = await read_back(flow, call, script, reads, heard=False)
    assert booked(diary) == [], said
    await turn(flow, call, script, 'Yes.', ('call', 'book_appointment'))
    assert booked(diary) == [], 'a yes to a read-back nobody heard booked'
    said = await turn(flow, call, script, 'Yes.', ('call', 'book_appointment'))
    assert booked(diary) == [('haircut', FRIDAY, '14:00')], (said, booked(diary))
    assert said == ["You're all set for Friday the 2nd at 2:00 PM. Anything else I can help with?"], said
    await turn(flow, call, script, 'Yes, book it.', ('call', 'book_appointment'), reply('You are all set.'))
    assert booked(diary) == [('haircut', FRIDAY, '14:00')], 'a second yes booked a second time'
    assert reads.availability == [f'{FRIDAY} haircut 14:00'], 'nothing looked up again'


async def a_repeat_reads_nothing():
    script, flow, call, diary, reads = at('booking')
    await read_back(flow, call, script, reads)
    options = next(t for t in json.loads(script.requests[-1].payload.tools) if t['name'] == 'options')[
        'parameters_json_schema'
    ]
    assert options['required'] == ['update'], 'with the offer standing, a refetch needs something new'
    before = len(script.requests)
    said = await turn(
        flow,
        call,
        script,
        'What else is there?',
        ('call', 'options', {}),
        ('call', 'options', {'update': {'booking_day': '2026-10-03'}}),
        reply('On Saturday I have 10:00 AM.'),
    )
    assert len(script.requests) - before == 3 and reads.availability == [
        f'{FRIDAY} haircut 14:00',
        '2026-10-03 haircut 14:00',
    ], reads.availability
    assert any('still stands' in r for r in retries(script)), (
        'the repeat is a labelled retry, and read nothing'
    )
    assert said == ['On Saturday I have 10:00 AM.'] and booked(diary) == []


async def a_refused_booking_tells_the_model_why():
    """The diary's no after a heard yes: `Declined` is a `core.Rejection`, so its `reason` reaches the model."""
    script, flow, call, diary, reads = at('booking')
    await read_back(flow, call, script, reads)
    why = 'Somebody has just taken that time. Read the diary again and offer what is left.'

    def taken(*args: Any, **kwargs: Any) -> dict:
        raise RefusedError(why)

    diary.save_booking = taken
    said = await turn(
        flow, call, script, 'Yes.', ('call', 'book_appointment'), reply('That time has just gone.')
    )
    assert said == ['That time has just gone.'] and booked(diary) == [], said
    assert [i.status for i in call.intents] == ['rejected'], call.intents
    assert retries(script)[-1] == (
        f"Nothing was done: the book_appointment service answered {{'reason': {why!r}}}. Do not say it was "
        'done. slot is no longer on offer: get a fresh offer (`options`), and record what the caller picks.'
    ), retries(script)[-1]


async def a_past_date_is_refused():
    script, flow, call, _, _ = at('booking')
    await turn(
        flow,
        call,
        script,
        'Last Friday, please.',
        ('call', 'options', {'update': {'booking_day': '2026-09-25'}}),
        reply('That day has already gone.'),
    )
    assert call.state.facts.booking_day is None and any('already gone' in r for r in retries(script))


async def a_booking_is_moved_and_cancelled():
    """Moving and cancelling are steps of their own; what each says next is code's, one request a turn.

    Moving picks the one booking held, names it and asks for the new day, with
    no request. The new day is only recorded: code offers the times for the
    booking's own service. Cancelling picks the one booking held and reads its
    cancel back on arrival: the hand-over is the turn's only request.
    """
    script, flow, call, diary, _ = at('concierge')
    held = seed(diary)
    said = await turn(
        flow,
        call,
        script,
        'Can I move my Friday one?',
        ('call', 'hand_over', {'to': 'moving', 'because': 'a move'}),
    )
    assert said == [
        'You have a haircut Friday the 2nd at 9:00 AM. Which day would you like to move it to?'
    ], said
    assert call.state.agent == 'moving' and call.state.facts.booking_id == held and len(script.requests) == 1
    said = await turn(
        flow,
        call,
        script,
        'Tuesday at two instead.',
        ('call', 'record', {'update': {'booking_day': TUESDAY, 'booking_time': '14:00'}}),
    )
    assert said == ['Tomorrow I have 2:00 PM. Which time works for you?'] and len(script.requests) == 2, said
    said = await turn(
        flow,
        call,
        script,
        'Two is great.',
        ('call', 'move_appointment', {'update': {'slot': slot(TUESDAY, '14:00')}}),
    )
    assert said == ['Shall I move your haircut from Friday the 2nd at 9:00 AM to tomorrow at 2:00 PM?'], said
    said = await turn(flow, call, script, 'Yes.', ('call', 'move_appointment'))
    assert booked(diary) == [('haircut', TUESDAY, '14:00')] and said[0].startswith("That's moved to"), said
    before = len(script.requests)
    said = await turn(
        flow,
        call,
        script,
        'Actually, cancel it.',
        ('call', 'hand_over', {'to': 'cancelling', 'because': 'a cancellation'}),
    )
    assert (
        said == ['Shall I cancel your haircut tomorrow at 2:00 PM?'] and call.state.agent == 'cancelling'
    ), said
    assert len(script.requests) - before == 1, "the cancel's read-back is code's"
    said = await turn(flow, call, script, 'Yes.', ('call', 'cancel_appointment'))
    assert booked(diary) == [] and said == ["That's cancelled for you. Anything else I can help with?"], said


async def the_booking_just_made_moves_to_the_day_given():
    """A hand-over from booking with the new day in it: moving picks the booking, and code offers that day."""
    script, flow, call, diary, reads = at('booking')
    seed(diary)
    said = await turn(
        flow,
        call,
        script,
        'Actually, can you move it to Saturday?',
        ('call', 'hand_over', {'to': 'moving', 'because': 'a move', 'update': {'booking_day': '2026-10-03'}}),
    )
    assert said == ['On Saturday the 3rd I have 9:00 AM, 9:30 AM or 10:00 AM. Which time works for you?'], (
        said
    )
    assert len(script.requests) == 1 and reads.availability[1:] == ['2026-10-03 haircut '], (
        "after the seed's own read, one read: Saturday, for the booking's service"
    )


async def each_step_says_its_own_first_line():
    """On arrival, the step's choice function says what comes first: ask the day, or which booking to cancel."""
    script, flow, call, diary = opened()
    said = await turn(
        flow,
        call,
        script,
        'A haircut, please.',
        ('call', 'hand_over', {'to': 'booking', 'because': 'a haircut', 'update': {'service': 'haircut'}}),
    )
    said = await turn(flow, call, script, 'Yes.', ('call', 'lookup_customer'))
    assert said == ['Thanks, I have you.', 'Which day would you like?'] and len(script.requests) == 2, said
    assert call.state.agent == 'booking' and 'slot' not in call._offers, 'a question is no offer'
    said = await turn(
        flow, call, script, 'Friday, please.', ('call', 'record', {'update': {'booking_day': FRIDAY}})
    )
    assert said == ['On Friday the 2nd I have 9:00 AM, 9:30 AM or 10:00 AM. Which time works for you?'], said
    assert len(script.requests) == 3, 'the day is only recorded: code offers its times'

    script, flow, call, diary, _ = at('concierge')
    seed(diary)
    seed(diary, TUESDAY, '10:00', additional=True)
    said = await turn(
        flow,
        call,
        script,
        'I need to cancel a booking.',
        ('call', 'hand_over', {'to': 'cancelling', 'because': 'a cancellation'}),
    )
    assert said == [
        'You have a haircut tomorrow at 10:00 AM or a haircut Friday the 2nd at 9:00 AM. '
        'Which one would you like to cancel?'
    ], said
    assert len(script.requests) == 1 and booked(diary) == [
        ('haircut', TUESDAY, '10:00'),
        ('haircut', FRIDAY, '09:00'),
    ]


async def a_complaint_is_read_back_in_the_callers_words():
    """The complaint rides its read-back, one request; words the caller never said record nothing."""
    script, flow, call, diary, _ = at('complaints')
    words = 'The colour came out orange and I want my money back.'
    update = {
        'complaint': 'The colour came out orange',
        'wanted_outcome': 'I want my money back',
        'wants_refund': True,
    }
    before = len(script.requests)
    said = await turn(flow, call, script, words, ('call', 'record_complaint', {'update': update}))
    assert said[-1].startswith('Shall I record your complaint in your words: The colour came out orange.'), (
        said
    )
    assert len(script.requests) - before == 1, 'the update and the read-back in one request'
    said = await turn(flow, call, script, 'Yes.', ('call', 'record_complaint'))
    [complaint] = diary.complaints()
    assert complaint['summary'] == 'The colour came out orange', complaint
    assert 'goes to the manager' in said[0] and complaint['reference'] in said[0], said
    script, flow, call, diary, _ = at('complaints')
    await turn(
        flow,
        call,
        script,
        'It was not great.',
        ('call', 'record_complaint', {'update': {'complaint': 'the stylist was rude to me'}}),
        reply('What happened?'),
    )
    assert call.state.facts.complaint is None and diary.complaints() == [], (
        'words never said: nothing recorded'
    )
    assert any('Nothing was recorded' in r for r in retries(script)), retries(script)


async def false_is_a_value_and_null_clears_it():
    """In an update `false` is recorded, and an explicit `null` clears a field that may be None."""
    script, flow, call, _, _ = at('complaints')
    await turn(flow, call, script, 'No refund, no.', reply('Understood.', wants_refund=False))
    assert call.state.facts.wants_refund is False, call.state.facts.wants_refund
    await turn(flow, call, script, 'Actually, I am not sure.', reply('Understood.', wants_refund=None))
    assert call.state.facts.wants_refund is None, call.state.facts.wants_refund


async def a_stylist_answers_and_the_read_back_is_asked_again():
    script, flow, call, diary, reads = at('booking')
    readback = 'Shall I book a haircut, Friday the 2nd at 2:00 PM?'
    assert (await read_back(flow, call, script, reads))[-1] == readback
    before = len(script.requests)
    said = await turn(
        flow,
        call,
        script,
        'Wait, should I come with my hair washed?',
        ('call', 'hand_over', {'to': 'stylist', 'because': 'a hair question'}),
        ('call', 'done', {'answer': 'No need: a haircut includes a wash.'}),
    )
    assert said == ['No need: a haircut includes a wash.', readback], said
    assert len(script.requests) - before == 2 and call.state.agent == 'booking' and booked(diary) == []
    stylist = script.requests[-1]
    assert stylist.step == 'stylist' and 'done' in stylist.tools and 'book_appointment' not in stylist.tools
    assert '"customer"' not in stylist.instructions, (
        'the stylist is shown only the request, not who the caller is'
    )
    await turn(flow, call, script, 'Great, yes please.', ('call', 'book_appointment'))
    assert booked(diary) == [('haircut', FRIDAY, '14:00')]


async def a_stylist_can_ask_first_and_a_correction_is_read_back_fresh():
    script, flow, call, diary, reads = at('booking')
    await read_back(flow, call, script, reads)
    said = await turn(
        flow,
        call,
        script,
        'Could I get colour too? How long would that take?',
        ('call', 'hand_over', {'to': 'stylist', 'because': 'how long colour takes'}),
        reply('Is your hair short, shoulder length or long?'),
    )
    assert call.state.agent == 'stylist' and said == ['Is your hair short, shoulder length or long?'], said
    said = await turn(
        flow, call, script, 'Long.', ('call', 'done', {'answer': 'Up to three hours for long hair.'})
    )
    assert (
        call.state.agent == 'booking' and said[-1] == 'Shall I book a haircut, Friday the 2nd at 2:00 PM?'
    ), said
    said = await turn(
        flow,
        call,
        script,
        'Then make it three instead of two.',
        ('call', 'options', {'update': {'booking_time': '15:00'}}),
        ('call', 'book_appointment', {'update': {'slot': slot(FRIDAY, '15:00')}}),
    )
    assert said[-1] == 'Shall I book a haircut, Friday the 2nd at 3:00 PM?', said
    await turn(flow, call, script, 'Yes.', ('call', 'book_appointment'))
    assert booked(diary) == [('haircut', FRIDAY, '15:00')], booked(diary)


async def a_stylist_hands_the_change_back():
    """A mixed request to the stylist: its answer once, the change back to booking in the same turn, read back fresh."""
    script, flow, call, diary, reads = at('booking')
    await read_back(flow, call, script, reads)
    before = len(script.requests)
    said = await turn(
        flow,
        call,
        script,
        'How long does colour take? And make it three instead of two.',
        ('call', 'hand_over', {'to': 'stylist', 'because': 'how long colour takes'}),
        ('call', 'done', {'answer': 'Up to three hours for colour.', 'handback': 'Three instead of two.'}),
        ('call', 'options', {'update': {'booking_time': '15:00'}}),
        ('call', 'book_appointment', {'update': {'slot': slot(FRIDAY, '15:00')}}),
    )
    assert said == ['Up to three hours for colour.', 'Shall I book a haircut, Friday the 2nd at 3:00 PM?'], (
        said
    )
    assert [r.step for r in script.requests[before:]] == ['booking', 'stylist', 'booking', 'booking']
    assert 'make it three instead of two' in str(script.requests[-2].messages), (
        "the caller's words reach booking"
    )
    assert len(reads.availability) == 2 and booked(diary) == [], 'one read for the new time, nothing booked'
    await turn(flow, call, script, 'Yes.', ('call', 'book_appointment', {}))
    assert booked(diary) == [('haircut', FRIDAY, '15:00')], booked(diary)


async def a_side_question_and_a_correction_during_the_read_back():
    """A question during the read-back leaves it waiting; "yes, but at three" is read back fresh."""
    script, flow, call, diary, reads = at('booking')
    await read_back(flow, call, script, reads)
    said = await turn(flow, call, script, 'Do you take cards?', reply('Yes, cards are fine.'))
    assert said == ['Yes, cards are fine.'] and booked(diary) == [], said
    assert [i.status for i in call.intents if i.operation == 'book_appointment'] == ['prepared']
    before = len(script.requests)
    said = await turn(
        flow,
        call,
        script,
        'Yes, but at three.',
        ('call', 'options', {'update': {'booking_time': '15:00'}}),
        ('call', 'book_appointment', {'update': {'slot': slot(FRIDAY, '15:00')}}),
    )
    assert (
        said == ['Shall I book a haircut, Friday the 2nd at 3:00 PM?'] and len(script.requests) - before == 2
    )
    assert booked(diary) == [] and len(reads.availability) == 2, 'the old read-back was never carried out'
    await turn(flow, call, script, 'Yes.', ('call', 'book_appointment'))
    assert booked(diary) == [('haircut', FRIDAY, '15:00')], booked(diary)


async def the_refund_policy_is_loaded_once():
    script, flow, call, _ = opened()
    await turn(
        flow,
        call,
        script,
        'If I cancel a colour, do I get money back?',
        ('call', 'load_capability', {'id': 'refunds'}),
        reply('Here is how refunds work.'),
    )
    assert len(script.requests) == 2, 'load, then answer'
    await turn(flow, call, script, 'And for a haircut?', reply('The same rules apply.'))
    loads = [
        p
        for m in script.requests[-1].messages
        if isinstance(m, ModelResponse)
        for p in m.parts
        if isinstance(p, ToolCallPart) and p.tool_name == 'load_capability'
    ]
    assert len(script.requests) == 3 and len(loads) == 1, (
        'the follow-up was one request, and nothing was loaded again'
    )


async def a_local_answer_is_one_request():
    script, flow, call, _ = opened()
    said = await turn(flow, call, script, 'How much is a blowout?', reply('A blowout is 22 euros.'))
    assert said == ['A blowout is 22 euros.'] and len(script.requests) == 1


WHOLE_CALL = (
    'A haircut Friday at two, please.',
    'Yes, that is my number.',
    'Two is great.',
    'Yes please.',
)
"""One caller's turns, from the first words to a booking."""
MOVES = (
    (('call', 'hand_over', {'to': 'booking', 'because': 'a haircut', 'update': AT_TWO}),),
    (('call', 'lookup_customer'),),
    (('call', 'book_appointment', {'update': {'slot': slot(FRIDAY, '14:00')}}),),
    (('call', 'book_appointment'),),
)
"""The agents' decisions for `WHOLE_CALL`: each update inside its decision."""


async def whole_call() -> tuple[list[list], list[list[str]], Reads, Diary]:
    """`WHOLE_CALL`: each turn's requests and words, the diary's reads and the diary."""
    script, flow, call, diary = opened(clock=lambda: CLOCK)
    reads = Reads(diary)
    sent, said = [], []
    for words, moves in zip(WHOLE_CALL, MOVES, strict=True):
        before = len(script.requests)
        said.append(await turn(flow, call, script, words, *moves))
        sent.append(script.requests[before:])
    return sent, said, reads, diary


V1_TOOLS = re.compile(r'`(?:collect|wait)`|call `decline`')
"""Tools a structured step does not have, named the way the prompts name tools. `decline` is `reply`'s field."""


async def a_call_verifies_reads_and_books_once():
    """A whole call: code reads the number back, one availability read, code's read-back, one booking.

    Requests per caller turn: 1, 1, 1, 1. The second turn's one is the
    number's yes. Booking, which the prerequisite interposed, is reached with
    the service, day and time known, so code offers the times in the salon's
    words on arrival (`offers: {slot: [...]}`) with no request: the receipt and the
    offer are said together, with no silence between them for the caller to
    fill. No step's prompt names a tool a structured step does not have.
    """
    sent, said, reads, diary = await whole_call()
    assert [len(requests) for requests in sent] == [1, 1, 1, 1], [len(r) for r in sent]
    assert said == [
        ['Is your number +34 600 11 12 22?'],
        ['Thanks, I have you.', 'On Friday the 2nd I have 2:00 PM. Which time works for you?'],
        ['Shall I book a haircut, Friday the 2nd at 2:00 PM?'],
        ["You're all set for Friday the 2nd at 2:00 PM. Anything else I can help with?"],
    ], said
    assert reads.availability == [f'{FRIDAY} haircut 14:00'] and booked(diary) == [
        ('haircut', FRIDAY, '14:00')
    ]
    assert [r.step for r in sent[1]] == ['verify'], [r.step for r in sent[1]]
    for request in (r for turn_sent in sent for r in turn_sent):
        assert 'collect' not in request.tools, (request.step, request.tools)
    for step in SPEC.steps:
        prompt = str(SPEC.prompt(step.id))
        assert not V1_TOOLS.findall(prompt), (step.id, V1_TOOLS.findall(prompt))
    booking = sent[2][0].payload
    assert 'A haircut Friday at two, please.' in booking.history, 'the words that asked for booking reach it'
    assert 'Which time works for you?' in booking.history, "code's offer is booking's own line"
    assert '"customer_phone"' not in booking.instructions, 'booking does not see the number: it binds it'


async def adapter_bytes(request) -> dict[str, int]:
    """One request as the installed Gemini adapter maps it, offline: its system instruction, contents and tools.

    `GoogleModel._build_content_and_config` is the adapter's own mapping from
    the model boundary to what the google-genai client is handed; it sends
    nothing. The client's HTTP encoding of that (camelCase keys) is not in it.
    Bytes of canonical JSON (sorted keys, UTF-8), not tokens.
    """
    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.providers.google import GoogleProvider

    model = GoogleModel('gemini-3.5-flash-lite', provider=GoogleProvider(api_key='offline'))
    contents, config = await model._build_content_and_config(
        request.messages, request.info.model_settings or {}, request.info.model_request_parameters
    )

    def size(value: Any) -> int:
        return len(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode())

    found = {
        'system': size(config.get('system_instruction')),
        'contents': size(contents),
        'tools': size([config.get('tools'), config.get('tool_config')]),
    }
    return found | {'total': sum(found.values())}


async def table() -> None:
    """`--table`: `WHOLE_CALL`, request by request, in bytes (not tokens).

    The first columns are the Pydantic AI model boundary (`Payload`), the last
    four the Gemini adapter's mapping of the same request (`adapter_bytes`).
    """
    print(
        '| Turn | Req | Step | Instructions | History | of which results | Tools | Outputs | Total |'
        ' Adapter system | Adapter contents | Adapter tools | Adapter total |'
    )
    print('|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|')
    sent, *_ = await whole_call()
    for words, requests in zip(WHOLE_CALL, sent, strict=True):
        for number, request in enumerate(requests, 1):
            size, wire = request.payload.sizes(), await adapter_bytes(request)
            print(
                f'| {words} | {number} | {request.step} | {size["instructions"]} | {size["history"]} |'
                f' {request.payload.results()} | {size["tools"]} | {size["outputs"]} | {size["total"]} |'
                f' {wire["system"]} | {wire["contents"]} | {wire["tools"]} | {wire["total"]} |'
            )


GOLDEN = Path(__file__).parent / 'golden' / 'payloads.json'


async def requests_are_pinned():
    """The salon's requests whole (instructions, history, tool and output schemas), pinned."""
    requests = {}
    script, flow, call, _ = opened(clock=lambda: CLOCK)
    await turn(flow, call, script, 'How much is a blowout?', reply('A blowout is 22 euros.'))
    requests['concierge, a local answer'] = script.requests[-1].payload
    script, flow, call, _, reads = at('booking', clock=lambda: CLOCK)
    await read_back(flow, call, script, reads)
    requests['booking, the read-back request'] = script.requests[-1].payload
    await turn(
        flow,
        call,
        script,
        'Wait, should I come with my hair washed?',
        ('call', 'hand_over', {'to': 'stylist', 'because': 'a hair question'}),
        ('call', 'done', {'answer': 'No need: a haircut includes a wash.'}),
    )
    requests['stylist, consulted'] = script.requests[-1].payload
    text = json.dumps(
        {k: p.pinned() for k, p in requests.items()}, indent=2, sort_keys=True, ensure_ascii=False
    )
    if os.environ.get('BLESS_PAYLOADS'):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(text + '\n')
    assert GOLDEN.exists() and GOLDEN.read_text() == text + '\n', (
        'a request changed: review it, then BLESS_PAYLOADS=1'
    )


if __name__ == '__main__' and '--table' in sys.argv:
    asyncio.run(table())
elif __name__ == '__main__':
    asyncio.run(requests_are_pinned())
    asyncio.run(verification_reads_the_caller_id_back_and_looks_it_up())
    asyncio.run(the_number_said_back_is_a_yes())
    asyncio.run(a_closed_day_is_said_by_code())
    asyncio.run(a_number_that_cannot_be_right_gets_the_locales_words())
    asyncio.run(a_heard_yes_books_once())
    asyncio.run(a_repeat_reads_nothing())
    asyncio.run(a_refused_booking_tells_the_model_why())
    asyncio.run(a_past_date_is_refused())
    asyncio.run(a_booking_is_moved_and_cancelled())
    asyncio.run(the_booking_just_made_moves_to_the_day_given())
    asyncio.run(each_step_says_its_own_first_line())
    asyncio.run(a_complaint_is_read_back_in_the_callers_words())
    asyncio.run(false_is_a_value_and_null_clears_it())
    asyncio.run(a_stylist_answers_and_the_read_back_is_asked_again())
    asyncio.run(a_stylist_can_ask_first_and_a_correction_is_read_back_fresh())
    asyncio.run(a_stylist_hands_the_change_back())
    asyncio.run(a_side_question_and_a_correction_during_the_read_back())
    asyncio.run(the_refund_policy_is_loaded_once())
    asyncio.run(a_local_answer_is_one_request())
    asyncio.run(a_call_verifies_reads_and_books_once())
    print(
        'business ok: verify, book, move, cancel, complaint, refusal words, stylist detours, refunds, requests,'
        ' a whole call'
    )
