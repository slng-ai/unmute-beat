"""Run: uv run python -m tests.test_business

The new salon on the real flow, with scripted node agents and a fixed diary
clock (Monday 28 September 2026, Europe/Madrid), asserting the diary, the
receipts and the requests. Scripted models prove runtime properties, never
language understanding: that is what the Coval run measures.

* verification: the caller ID is read back by code, a yes looks it up, the
  customer is the receipt, and a corrected number is somebody else;
* booking, moving and cancelling: read back, then done once on a heard yes;
* a complaint in the caller's own words, and the refund policy's next step;
* the locale's words for a number that cannot be right, and a past date;
* a stylist consulted mid-booking: it answers, the caller comes back, the
  read-back is asked again fresh, and a yes after it books;
* the refund policy loaded once and kept;
* request counts on the common paths.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart

from salon.diary import Diary
from salon.receipts import Customer
from tests.script import CLOCK, PHONE, Script, opened, slot, turn

TODAY = CLOCK.date()
FRIDAY, TUESDAY = '2026-10-02', '2026-09-29'
KNOWN = Customer(customer_status='existing', customer_phone=PHONE)


def booked(diary: Diary) -> list[tuple[str, str, str]]:
    return [(b['service'], b['date'], b['time']) for b in diary.bookings_for(PHONE, TODAY)]


def at(step: str, **facts: Any):
    """A verified caller already on this step."""
    script, flow, call, diary = opened(customer=KNOWN, **facts)
    call.commit(agent=step)
    return script, flow, call, diary


async def read_back(flow, call, script, day: str = FRIDAY, at_time: str = '14:00', heard: bool = True):
    """Ask for a haircut on `day`, then pick the time at `at_time`: the booking is read back."""
    await turn(
        flow,
        call,
        script,
        'A haircut on Friday at two.',
        ('collect', {'service': 'haircut', 'booking_day': day, 'booking_time': at_time}),
        ('say', 'On Friday I have 2:00 PM. Does that work?'),
    )
    return await turn(
        flow,
        call,
        script,
        'Yes, two is great.',
        ('collect', {'slot': slot(day, at_time)}),
        ('call', 'book_appointment'),
        heard=heard,
    )


def seed(diary: Diary, day: str = FRIDAY, at_time: str = '09:00') -> str:
    """A booking the caller already holds, made through the diary's own offer and write."""
    from datetime import date

    diary.find_slots(PHONE, date.fromisoformat(day), 'haircut', CLOCK, offered_to='seed', prefer=at_time)
    return diary.save_booking(PHONE, 'book', slot(day, at_time), '', False, CLOCK, offered_to='seed')[
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
        ('collect', {'service': 'haircut', 'booking_day': FRIDAY, 'booking_time': '14:00'}),
        ('call', 'hand_over', {'to': 'booking', 'because': 'a haircut'}),
    )
    assert call.state.agent == 'verify' and said[-1] == 'Is your number +34 600 11 12 22?', said
    await turn(flow, call, script, 'Yes.', ('call', 'lookup_customer'), ('say', 'Which time suits you?'))
    assert call.state.agent == 'booking' and call.state.facts.customer == KNOWN, call.state.facts
    call.commit(agent='verify')
    await turn(
        flow, call, script, 'Sorry, use my other number.', ('collect', {'customer_phone': '+34600999888'})
    )
    assert call.state.facts.customer is None, 'a corrected number is somebody else until it is looked up'
    assert booked(diary) == []


async def a_number_that_cannot_be_right_gets_the_locales_words():
    script, flow, call, _ = opened(phone='')
    call.commit(agent='verify')
    await turn(
        flow,
        call,
        script,
        'Plus three four six seven one two seven three four five four four.',
        ('collect', {'customer_phone': '+3467127345444'}),
        ('say', 'I have +34 671 27 34 54 44. Which part is wrong?'),
    )
    assert any('Which part is wrong?' in r and 'phone_too_long' not in r for r in retries(script)), retries(
        script
    )
    assert call.state.facts.customer_phone is None


async def a_heard_yes_books_once():
    script, flow, call, diary = at('booking')
    said = await read_back(flow, call, script, heard=False)
    assert said[-1] == 'Shall I book a haircut, Friday the 2nd at 2:00 PM?' and booked(diary) == [], said
    await turn(flow, call, script, 'Yes.', ('call', 'book_appointment'))
    assert booked(diary) == [], 'a yes to a read-back nobody heard booked'
    said = await turn(flow, call, script, 'Yes.', ('call', 'book_appointment'))
    assert booked(diary) == [('haircut', FRIDAY, '14:00')], (said, booked(diary))
    assert said == ["You're all set for Friday the 2nd at 2:00 PM. Anything else I can help with?"], said
    await turn(flow, call, script, 'Yes, book it.', ('call', 'book_appointment'), ('say', 'You are all set.'))
    assert booked(diary) == [('haircut', FRIDAY, '14:00')], 'a second yes booked a second time'


async def a_past_date_is_refused():
    script, flow, call, _ = at('booking')
    await turn(
        flow,
        call,
        script,
        'Last Friday, please.',
        ('collect', {'booking_day': '2026-09-25'}),
        ('say', 'That day has already gone.'),
    )
    assert call.state.facts.booking_day is None and any('already gone' in r for r in retries(script))


async def a_booking_is_moved_and_cancelled():
    script, flow, call, diary = at('booking')
    held = seed(diary)
    await turn(flow, call, script, 'Can I move my Friday one?', ('call', 'options'), ('say', 'To when?'))
    said = await turn(
        flow,
        call,
        script,
        'Tuesday at two instead.',
        ('collect', {'booking_id': held, 'booking_day': TUESDAY, 'booking_time': '14:00'}),
        ('calls', [('collect', {'values': {'slot': slot(TUESDAY, '14:00')}}), ('move_appointment', {})]),
    )
    assert said[-1] == 'Shall I move your haircut from Friday the 2nd at 9:00 AM to tomorrow at 2:00 PM?', (
        said
    )
    said = await turn(flow, call, script, 'Yes.', ('call', 'move_appointment'))
    assert booked(diary) == [('haircut', TUESDAY, '14:00')] and said[0].startswith("That's moved to"), said
    said = await turn(
        flow,
        call,
        script,
        'Actually, cancel it.',
        ('collect', {'booking_id': held}),
        ('call', 'cancel_appointment'),
    )
    assert said[-1] == 'Shall I cancel your haircut tomorrow at 2:00 PM?', said
    said = await turn(flow, call, script, 'Yes.', ('call', 'cancel_appointment'))
    assert booked(diary) == [] and said == ["That's cancelled for you. Anything else I can help with?"], said


async def a_complaint_is_recorded_in_the_callers_words():
    script, flow, call, diary = at('complaints')
    words = 'The colour came out orange and I want my money back.'
    said = await turn(
        flow,
        call,
        script,
        words,
        (
            'collect',
            {
                'complaint': 'The colour came out orange',
                'wanted_outcome': 'I want my money back',
                'wants_refund': True,
            },
        ),
        ('call', 'record_complaint'),
    )
    assert said[-1].startswith('Shall I record your complaint in your words: The colour came out orange.'), (
        said
    )
    said = await turn(flow, call, script, 'Yes.', ('call', 'record_complaint'))
    [complaint] = diary.complaints()
    assert complaint['summary'] == 'The colour came out orange', complaint
    assert 'goes to the manager' in said[0] and complaint['reference'] in said[0], said

    script, flow, call, diary = at('complaints')
    await turn(
        flow,
        call,
        script,
        'It was not great.',
        ('collect', {'complaint': 'the stylist was rude to me'}),
        ('say', 'What happened?'),
    )
    assert call.state.facts.complaint is None, 'a complaint in words the caller never said was recorded'


async def a_stylist_answers_and_the_read_back_is_asked_again():
    script, flow, call, diary = at('booking')
    readback = 'Shall I book a haircut, Friday the 2nd at 2:00 PM?'
    assert (await read_back(flow, call, script))[-1] == readback
    before = len(script.requests)
    said = await turn(
        flow,
        call,
        script,
        'Wait, should I come with my hair washed?',
        ('call', 'hand_over', {'to': 'stylist', 'because': 'a hair question'}),
        ('answer', 'No need: a haircut includes a wash.'),
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
    script, flow, call, diary = at('booking')
    await read_back(flow, call, script)
    said = await turn(
        flow,
        call,
        script,
        'Could I get colour too? How long would that take?',
        ('call', 'hand_over', {'to': 'stylist', 'because': 'how long colour takes'}),
        ('say', 'Is your hair short, shoulder length or long?'),
    )
    assert call.state.agent == 'stylist' and said == ['Is your hair short, shoulder length or long?'], said
    said = await turn(flow, call, script, 'Long.', ('answer', 'Up to three hours for long hair.'))
    assert (
        call.state.agent == 'booking' and said[-1] == 'Shall I book a haircut, Friday the 2nd at 2:00 PM?'
    ), said
    said = await turn(
        flow,
        call,
        script,
        'Then make it three instead of two.',
        ('collect', {'booking_time': '15:00'}),
        ('calls', [('collect', {'values': {'slot': slot(FRIDAY, '15:00')}}), ('book_appointment', {})]),
    )
    assert said[-1] == 'Shall I book a haircut, Friday the 2nd at 3:00 PM?', said
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
        ('say', 'Here is how refunds work.'),
    )
    assert len(script.requests) == 2, 'load, then answer'
    await turn(flow, call, script, 'And for a haircut?', ('say', 'The same rules apply.'))
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
    said = await turn(flow, call, script, 'How much is a blowout?', ('say', 'A blowout is 22 euros.'))
    assert said == ['A blowout is 22 euros.'] and len(script.requests) == 1


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


def structured_at(step: str, **facts: Any):
    """A verified caller on this step of the salon compiled with `speech: structured` steps (`tests.script.STRUCTURED`)."""
    script, flow, call, diary = opened(Script(structured=True), customer=KNOWN, **facts)
    call.commit(agent=step)
    return script, flow, call, diary, Reads(diary)


def reply(say: str, **update: Any) -> tuple[str, str, dict[str, Any]]:
    return ('call', 'reply', {'say': say, **({'update': update} if update else {})})


async def structured_read_back(flow, call, script, reads: Reads) -> None:
    """A haircut on Friday at two, looked up with the request it rests on, then picked: three requests in all."""
    before = len(script.requests)
    said = await turn(
        flow,
        call,
        script,
        'A haircut on Friday at two.',
        (
            'call',
            'options',
            {'update': {'service': 'haircut', 'booking_day': FRIDAY, 'booking_time': '14:00'}},
        ),
        reply('On Friday I have 2:00 PM. Does that work?'),
    )
    assert said == ['On Friday I have 2:00 PM. Does that work?'] and len(script.requests) - before == 2, said
    assert reads.availability == [f'{FRIDAY} haircut 14:00'] and reads.holdings == 1, (
        'one availability read; the bookings held are a read of their own'
    )
    said = await turn(
        flow,
        call,
        script,
        'Yes, two is great.',
        ('call', 'book_appointment', {'update': {'slot': slot(FRIDAY, '14:00')}}),
    )
    assert (
        said == ['Shall I book a haircut, Friday the 2nd at 2:00 PM?'] and len(script.requests) - before == 3
    )


async def a_structured_booking_reads_once_and_books_once():
    """The booking step as `speech: structured`: no `collect`, one read with the request it rests on, exact read-back."""
    script, flow, call, diary, reads = structured_at('booking')
    await structured_read_back(flow, call, script, reads)
    tools = script.requests[0].tools
    assert 'collect' not in tools and 'record' not in tools, 'the read-back rides book_appointment: no record'
    assert {'options', 'reply', 'book_appointment'} <= set(tools), tools
    said = await turn(flow, call, script, 'Yes please.', ('call', 'book_appointment', {}))
    assert booked(diary) == [('haircut', FRIDAY, '14:00')], booked(diary)
    assert said == ["You're all set for Friday the 2nd at 2:00 PM. Anything else I can help with?"], said
    assert reads.availability == [f'{FRIDAY} haircut 14:00'], 'nothing looked up again'


async def a_structured_repeat_reads_nothing():
    script, flow, call, diary, reads = structured_at('booking')
    await structured_read_back(flow, call, script, reads)
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


async def a_structured_stylist_hands_the_change_back():
    """A mixed request to the stylist: its answer once, the change back to booking in the same turn, read back fresh."""
    script, flow, call, diary, reads = structured_at('booking')
    await structured_read_back(flow, call, script, reads)
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


GOLDEN = Path(__file__).parent / 'golden' / 'payloads.json'


async def v1_requests_are_pinned():
    """The salon's requests whole (instructions, history, tool and output schemas), pinned before the scoped view."""
    requests = {}
    script, flow, call, _ = opened(clock=lambda: CLOCK)
    await turn(flow, call, script, 'How much is a blowout?', ('say', 'A blowout is 22 euros.'))
    requests['concierge, a local answer'] = script.requests[-1].payload
    script, flow, call, _ = at('booking', clock=lambda: CLOCK)
    await read_back(flow, call, script)
    requests['booking, the read-back request'] = script.requests[-1].payload
    await turn(
        flow,
        call,
        script,
        'Wait, should I come with my hair washed?',
        ('call', 'hand_over', {'to': 'stylist', 'because': 'a hair question'}),
        ('answer', 'No need: a haircut includes a wash.'),
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


async def false_is_a_value_and_null_leaves_one():
    """Legacy `collect`, frozen for Session 2: `false` is recorded, and an explicit `null` changes nothing."""
    script, flow, call, _ = at('complaints')
    await turn(
        flow, call, script, 'No refund, no.', ('collect', {'wants_refund': False}), ('say', 'Understood.')
    )
    assert call.state.facts.wants_refund is False, call.state.facts.wants_refund
    await turn(flow, call, script, 'Hmm.', ('collect', {'wants_refund': None}), ('say', 'Understood.'))
    assert call.state.facts.wants_refund is False, 'legacy collect cannot clear: null is left out'


if __name__ == '__main__':
    asyncio.run(v1_requests_are_pinned())
    asyncio.run(false_is_a_value_and_null_leaves_one())
    asyncio.run(verification_reads_the_caller_id_back_and_looks_it_up())
    asyncio.run(a_number_that_cannot_be_right_gets_the_locales_words())
    asyncio.run(a_heard_yes_books_once())
    asyncio.run(a_past_date_is_refused())
    asyncio.run(a_booking_is_moved_and_cancelled())
    asyncio.run(a_complaint_is_recorded_in_the_callers_words())
    asyncio.run(a_stylist_answers_and_the_read_back_is_asked_again())
    asyncio.run(a_stylist_can_ask_first_and_a_correction_is_read_back_fresh())
    asyncio.run(the_refund_policy_is_loaded_once())
    asyncio.run(a_local_answer_is_one_request())
    asyncio.run(a_structured_booking_reads_once_and_books_once())
    asyncio.run(a_structured_repeat_reads_nothing())
    asyncio.run(a_structured_stylist_hands_the_change_back())
    print(
        'business ok: verify, book, move, cancel, complaint, refusal words, stylist detours, refunds, requests,'
        ' structured booking and stylist'
    )
