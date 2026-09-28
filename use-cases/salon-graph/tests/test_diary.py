"""Run: uv run --env-file .env python -m tests.test_diary

The fake salon service, which is the oracle every flow test is measured
against. A POC cannot prove a booking flow correct if its diary accepts
impossible bookings, so these are the checks that the diary can say no.

The clock is passed in everywhere, so every case here is a fixed instant and
nothing depends on when it runs. `AT` is Monday 21 September 2026, 10:00 in
Madrid.

Asserts, no pytest, no fixtures, same as the rest.

`tests/__main__.py` runs each module with `run_name='__main__'`, so this still runs there -- and a
debugging session can import one module without running every check in it.
"""

import asyncio
from datetime import date, datetime, timedelta

from salon import diary
from salon.app import FLOW
from salon.app import open_call as open_on
from salon.diary import Diary


def open_call(phone: str, diary: Diary):
    """A call on the salon's compiled flow, sharing this diary."""
    return open_on(FLOW, phone, diary)


AT = datetime(2026, 9, 21, 10, 0, tzinfo=diary.TZ)
TODAY = AT.date()
"""a Monday morning"""
TOMORROW = TODAY + timedelta(days=1)
ALICE, BOB = '+34600111222', '+34934567890'


books = Diary()
"""This module's salon, emptied by each case that writes to it."""


def _refused(fn, *args, **kwargs) -> str:
    try:
        fn(*args, **kwargs)
    except diary.RefusedError as refused:
        return str(refused)
    raise AssertionError('the diary accepted something it must refuse')


def _slot(phone, when, service, at=AT, time_of_day=None):
    """One slot the diary really offered, optionally at a named time."""
    slots = books.find_slots(phone, when, service, at)['slots']
    if time_of_day is None:
        return slots[0]
    return next(s for s in slots if s['time'] == time_of_day)


def a_hair_colour_slot_can_be_booked():
    """The defect that made one of three services unbookable.

    `slot_id.rsplit('-', 2)` cut `2026-09-22T0900-hair-color` in the wrong
    place, because the service name has a hyphen in it and the split reserved
    only two. `Date.fromisoformat` then raised, a bare `except` caught it, and
    the caller was told the diary had never returned a slot it had offered
    thirty seconds earlier.
    """
    books.reset()
    offered = _slot(ALICE, 'tomorrow', 'hair-color')
    saved = books.save_booking(ALICE, 'book', offered['slot_id'], '', False, AT)
    assert saved['status'] == 'booked'
    assert saved['service'] == 'hair-color', saved


def every_offered_slot_can_be_taken():
    """The general form of it: whatever the diary offers, the diary accepts.

    One service at a time, because an offer for one is only still valid while
    nothing else has taken the chair.

    Reset first, then re-read: a reset clears the offers too, so an id captured before it names a
    slot the diary no longer knows.
    """
    for service in diary.SERVICES:
        books.reset()
        count = len(books.find_slots(ALICE, 'tomorrow', service, AT)['slots'])
        assert count, service
        for index in range(count):
            books.reset()
            offered = books.find_slots(ALICE, 'tomorrow', service, AT)['slots'][index]
            saved = books.save_booking(ALICE, 'book', offered['slot_id'], '', False, AT)
            assert saved['status'] == 'booked', (service, offered)
            assert saved['service'] == service, saved


def a_fabricated_slot_is_refused():
    """03:07 on a day the salon opens at nine. Shaped right, offered never.

    It is refused because the diary has no record of issuing it, not because
    anybody checked the time: that check exists too, below, and this one holds
    whatever the id looks like.

    outside opening hours

    off the half-hour grid

    not a service
    """
    books.reset()
    for invented in (
        f'{TOMORROW.isoformat()}T0307|haircut',
        f'{TOMORROW.isoformat()}T0915|haircut',
        f'{TOMORROW.isoformat()}T0900|nails',
        'have-a-slot',
        '',
    ):
        assert 'not a slot this diary is currently offering this caller' in _refused(
            books.save_booking, ALICE, 'book', invented, '', False, AT
        ), invented
    assert books.bookings_for(ALICE, TODAY) == [], 'and nothing was written'


def an_expired_offer_is_refused():
    """A slot offered yesterday is not a slot today, even if the day is still ahead."""
    books.reset()
    offered = _slot(ALICE, '2026-09-30', 'haircut')
    assert 'expired' in _refused(
        books.save_booking, ALICE, 'book', offered['slot_id'], '', False, AT + timedelta(days=1)
    )
    assert books.bookings_for(ALICE, TODAY) == []


def a_slot_that_has_gone_past_is_refused():
    """Offered this morning, booked this afternoon, for this morning."""
    books.reset()
    offered = _slot(ALICE, 'today', 'haircut', time_of_day='11:00')
    later = AT.replace(hour=14)
    assert 'gone past' in _refused(books.save_booking, ALICE, 'book', offered['slot_id'], '', False, later)


def a_slot_taken_in_between_is_refused():
    """Free when it was offered is not free when it is written.

    The re-check at write time, which is the difference between a registry of
    what was offered and a diary that is actually right.
    """
    books.reset()
    offered = _slot(ALICE, 'tomorrow', 'haircut', time_of_day='09:00')
    also_offered = _slot(BOB, 'tomorrow', 'haircut', time_of_day='09:00')
    books.save_booking(ALICE, 'book', offered['slot_id'], '', False, AT)
    assert 'has the chair' in _refused(
        books.save_booking, BOB, 'book', also_offered['slot_id'], '', False, AT
    )


def a_long_appointment_blocks_an_overlapping_short_one():
    """The capacity rule, stated and then tested.

    ponytail: one service resource. Two appointments clash when their
    intervals overlap, not when their start times match -- which is all the
    diary used to compare, so a 45-minute cut at 09:30 fitted inside a
    120-minute colour that started at 09:00 and both were confirmed.
    """
    books.reset()
    colour = _slot(ALICE, 'tomorrow', 'hair-color', time_of_day='09:00')
    books.save_booking(ALICE, 'book', colour['slot_id'], '', False, AT)

    free = books.find_slots(BOB, 'tomorrow', 'haircut', AT)['slots']
    assert not any(s['time'] in ('09:00', '09:30', '10:00', '10:30') for s in free), free[:4]
    assert free[0]['time'] == '11:00', 'the chair is free when the colour finishes, not before'


def a_service_must_finish_before_closing():
    """A two-hour colour cannot start an hour before the doors shut.

    closes at 16:00
    """
    books.reset()
    saturday = books.find_slots(ALICE, 'saturday', 'hair-color', AT)
    assert saturday['slots'][-1]['time'] == '14:00', saturday['slots'][-1]


def asking_for_any_service_shows_more_than_one():
    """The cap must not starve a service.

    Built service by service and truncated as built, all twelve places went to
    whichever service was listed first, so `any` reported a salon that does
    haircuts and nothing else.
    """
    books.reset()
    offered = books.find_slots(ALICE, 'tomorrow', 'any', AT)['slots']
    assert len({s['service'] for s in offered}) > 1, offered
    assert offered == sorted(offered, key=lambda s: (s['time'], s['service'])), 'soonest first'


def the_same_token_books_once():
    """Commit, lose the response, retry. One booking.

    Without an operation token the only options are to retry blind and risk
    two bookings, or not retry and lose one that succeeded.

    The caller never saw `first`: the response was lost. It asks again.
    """
    books.reset()
    offered = _slot(ALICE, 'tomorrow', 'haircut')
    first = books.save_booking(ALICE, 'book', offered['slot_id'], '', False, AT, operation_token='op-1')
    again = books.save_booking(ALICE, 'book', offered['slot_id'], '', False, AT, operation_token='op-1')
    assert first == again
    assert len(books.bookings_for(ALICE, TODAY)) == 1, books.bookings_for(ALICE, TODAY)


def an_unknown_outcome_stays_unknown():
    """`None` means "not started, or we cannot say", and never "it failed".

    A service that answers "failed" for a write it cannot find has invented a
    fact. The caller reconciles instead.
    """
    books.reset()
    assert books.outcome_of('never-sent') is None
    offered = _slot(ALICE, 'tomorrow', 'haircut')
    books.save_booking(ALICE, 'book', offered['slot_id'], '', False, AT, operation_token='op-2')
    assert books.outcome_of('op-2')['status'] == 'booked'


def a_refused_write_records_no_outcome():
    """A refusal is not an outcome to replay: the caller may legitimately retry."""
    books.reset()
    _refused(books.save_booking, ALICE, 'book', 'invented', '', False, AT, operation_token='op-3')
    assert books.outcome_of('op-3') is None


def one_call_does_not_wipe_another():
    """`open_call` used to reset the process-global diary.

    Call A books, call B starts, and A's appointment was gone -- in a fixture
    whose whole job is to be the truth the flow tests are checked against.

    And B cannot act on A's record: the diary is shared, the conversation is not.
    """
    books.reset()
    offered = _slot(ALICE, 'tomorrow', 'haircut')
    books.save_booking(ALICE, 'book', offered['slot_id'], '', False, AT)
    assert len(books.bookings_for(ALICE, TODAY)) == 1

    second = open_call(BOB, diary=books)
    assert len(books.bookings_for(ALICE, TODAY)) == 1, "A's booking survived B arriving"
    assert books.bookings_for(BOB, TODAY) == []
    assert second.state.facts.customer_phone == BOB
    assert second.state.facts.verified_phone == '', 'a new call knows nothing about the last one'


def two_calls_from_one_phone_are_two_conversations():
    """And a fresh fixture is a decision somebody makes, not a side effect of the clock.

    `bot.py` used to empty the diary whenever `SALON_CLOCK` was set, which
    fixed one problem -- the second call of a benchmark run inheriting the
    first's booking -- by creating another: a pinned call could delete the
    records of a call still in progress. Pinning the clock makes two calls
    *comparable*; emptying the salon makes the second one destroy the first.
    Two different decisions, and `SALON_FIXTURE` is the second one.

    The same number rings again: a continuation, which is what a real salon sees and what the
    N1-to-N3 sequence is.

    What the first conversation was shown is not what the second was shown.

    Nor its pending question, nor its authority.

    And pinning the clock, on its own, changes nothing about the records.
    """
    books.reset()
    first = open_call(ALICE, diary=books)
    offered = books.find_slots(ALICE, 'tomorrow', 'haircut', AT, offered_to=first.services.call)['slots'][0]
    books.save_booking(ALICE, 'book', offered['slot_id'], '', False, AT, offered_to=first.services.call)

    second = open_call(ALICE, diary=books)
    assert len(books.bookings_for(ALICE, TODAY)) == 1, "the first call's booking is still there"
    assert second.services.call != first.services.call
    assert books.offer_of(second.services.call, offered['slot_id'], ALICE) == {}
    assert books.offer_of(first.services.call, offered['slot_id'], ALICE) != {}
    assert second._asked is None and second.state.facts.verified_phone == ''

    books.set_clock(AT)
    assert len(books.bookings_for(ALICE, TODAY)) == 1, 'a clock is not a reset'
    books.set_clock(None)


def calls_with_a_diary_each_share_nothing():
    """Two diaries on two files share nothing: a fixture per test is its own salon.

    The salon itself is one file every worker shares (`DIARY_PATH`), so calls
    running side by side contend for one chair (`tests.test_store`). Two
    files are two salons: a booking and a cancel in one are invisible to the
    other, and each one's first booking is BK0001.
    """
    first_diary, second_diary = Diary(), Diary()
    first, second = open_call(ALICE, diary=first_diary), open_call(ALICE, diary=second_diary)
    offered = first_diary.find_slots(ALICE, 'tomorrow', 'haircut', AT, offered_to=first.services.call)[
        'slots'
    ][0]
    booked = first_diary.save_booking(
        ALICE, 'book', offered['slot_id'], '', False, AT, offered_to=first.services.call
    )
    assert booked['booking_id'] == 'BK0001', booked
    assert (
        second_diary.bookings_for(ALICE, TODAY) == [] and second_diary.offered_to(second.services.call) == []
    )
    first_diary.save_booking(
        ALICE, 'cancel', '', booked['booking_id'], False, AT, offered_to=first.services.call
    )
    again = second_diary.find_slots(ALICE, 'tomorrow', 'haircut', AT, offered_to=second.services.call)[
        'slots'
    ][0]
    theirs = second_diary.save_booking(
        ALICE, 'book', again['slot_id'], '', False, AT, offered_to=second.services.call
    )
    assert theirs['booking_id'] == 'BK0001', 'a diary of its own numbers from the start'
    assert first_diary.bookings_for(ALICE, TODAY) == [], (
        "the second call's booking is not in the first's diary"
    )


def the_supported_phrases_resolve():
    """Monday said on a Monday is next Monday, not this morning."""
    assert diary.resolve_day('today', TODAY) == TODAY
    assert diary.resolve_day('tomorrow', TODAY) == TOMORROW
    assert diary.resolve_day('2026-10-01', TODAY) == date(2026, 10, 1)
    assert diary.resolve_day('monday', TODAY) == TODAY + timedelta(days=7)
    assert diary.resolve_day('friday', TODAY) == date(2026, 9, 25)
    assert diary.resolve_day('next friday', TODAY) == date(2026, 10, 2)


def the_phrases_of_trace_bbe42ad4_resolve():
    """Said on Friday 2026-09-25: "Saturday next week", then "October the 3rd". Both used to be need_day.

    Every form of a date by its month, "the 3rd" alone, and a weekday of next
    week or a week on. Monday-to-Sunday weeks, so on a Sunday "Saturday next
    week" is six days away and "a week on Saturday" thirteen.
    """
    friday, sunday = date(2026, 9, 25), date(2026, 9, 27)
    october_3rd = date(2026, 10, 3)
    for words in (
        'Saturday next week',
        'next week Saturday',
        'next week on Saturday',
        'next Saturday',
        'a week on Saturday',
        'a week from Saturday',
        'October the 3rd',
        'October 3rd',
        'Oct 3',
        'the 3rd of October',
        '3rd October',
        'the 3rd',
        '2026-10-03',
    ):
        assert diary.resolve_day(words, friday) == october_3rd, words
    assert diary.resolve_day('Saturday', friday) == date(2026, 9, 26)
    assert diary.resolve_day('this Saturday', friday) == date(2026, 9, 26)
    assert diary.resolve_day('January 5th', friday) == date(2027, 1, 5), 'a month already past is next year'
    assert diary.resolve_day('the 31st', friday) == date(2026, 10, 31), 'September has no 31st'
    assert diary.resolve_day('February 30th', friday) is None
    assert diary.resolve_day('at 3', friday) is None, 'a bare number is a time, not a day'
    assert diary.resolve_day('next Tuesday', friday) == date(2026, 9, 29), 'the Tuesday of next week'
    assert diary.resolve_day('Tuesday next week', friday) == date(2026, 9, 29)
    assert diary.resolve_day('the Tuesday after next', friday) == date(2026, 10, 6)
    assert diary.resolve_day('next Monday', sunday) == date(2026, 9, 28), 'next week starts tomorrow'
    assert diary.resolve_day('Saturday next week', sunday) == date(2026, 10, 3)
    assert diary.resolve_day('a week on Saturday', sunday) == date(2026, 10, 10)
    assert diary.day_phrases('Oh no, sorry, I meant Saturday next week, not this Saturday.') == [
        'saturday next week',
        'this saturday',
    ]
    assert diary.day_phrases('I think October the 3rd. Something like this?') == ['october the 3rd']


def an_exact_date_decides_and_a_day_after_counts_from_its_day():
    """Replay of 2026-09-25, on Friday the 25th: the model passed "Saturday 2026-10-03" and tomorrow was offered.

    The weekday rule read "Saturday" before the date. Now the date decides,
    and a weekday it does not fall on is refused. "The day after X" is X plus
    one; with no day after it, the words name none and the model uses its
    calendar.
    """
    friday = date(2026, 9, 25)
    for words, day in (
        ('Saturday 2026-10-03', date(2026, 10, 3)),
        ('October 3rd, Saturday', date(2026, 10, 3)),
        ('Saturday the 3rd', date(2026, 10, 3)),
        ('the day after Friday', date(2026, 10, 3)),
        ('the day before Saturday the 3rd', date(2026, 10, 2)),
        ('the day after tomorrow', date(2026, 9, 27)),
        ('the day after tomorrow at 3', date(2026, 9, 27)),
    ):
        assert diary.resolve_day(words, friday) == day, (words, diary.resolve_day(words, friday))
    for refused in (
        'Friday 2026-10-03',
        'Friday the 3rd',
        'the day after',
        'the next day',
        'the day after next',
    ):
        assert diary.resolve_day(refused, friday) is None, refused
        assert diary.names_a_day(refused), f'{refused!r} names a day, so the model is sent to its calendar'
    assert diary.day_phrases('Could we do the day after tomorrow?') == ['the day after tomorrow']
    assert diary.day_phrases('Can you look at the day after?') == ['the day after']
    on_the_table = date(2026, 10, 2)
    assert diary.anchored('the day after', on_the_table) == '2026-10-03'
    assert diary.anchored('the next day', on_the_table) == '2026-10-03'
    assert diary.anchored('the day before', on_the_table) == '2026-10-01'
    assert diary.anchored('the day after', None) == 'the day after', 'no day on the table: no day named'
    assert diary.anchored('friday', on_the_table) == 'friday'


def anything_outside_the_policy_is_asked_about():
    """Not guessed. The agent gets `need_day` and asks which day."""
    for outside in ('whenever suits', 'in a fortnight', ''):
        assert diary.resolve_day(outside, TODAY) is None, outside
    assert books.find_slots(ALICE, 'whenever suits', 'haircut', AT)['status'] == 'need_day'


def a_modifier_the_policy_cannot_do_is_asked_about():
    """ "In two weeks on Friday" is not next Friday, and must not be answered as it.

    A weekday match on its own found "Tuesday" in "a week on Tuesday" and
    returned the next one: seven days early, a real date, and silent. The
    phrases this policy does not implement are named and refused, so the agent
    asks. "A week on Tuesday" and "the Tuesday after next" are implemented now.

    The plain forms it does implement still work.
    """
    for shifted in (
        'in two weeks on Friday',
        'the following Monday',
        'in 3 weeks',
    ):
        assert diary.resolve_day(shifted, TODAY) is None, shifted
    assert diary.resolve_day('tuesday', TODAY) == date(2026, 9, 22)
    assert diary.resolve_day('next tuesday', TODAY) == date(2026, 9, 29)
    assert diary.resolve_day('a week on Tuesday', TODAY) == date(2026, 9, 29)
    assert diary.resolve_day('the Tuesday after next', TODAY) == date(2026, 10, 6)


def a_weekday_must_be_a_whole_word():
    """ "satur" inside another word used to resolve a real Saturday.

    The worst kind of wrong answer: it returns a day, so nothing downstream
    has any reason to doubt it and the caller is offered times for a day they
    never named.
    """
    assert diary.resolve_day('is that saturating?', TODAY) is None
    assert diary.resolve_day('sunday roast then?', TODAY) == date(2026, 9, 27), 'a real one still works'


def an_impossible_date_is_asked_about():
    """The right shape and not a day. Ask; never round to the 28th."""
    assert diary.resolve_day('2026-02-30', TODAY) is None
    assert diary.resolve_day('2026-13-01', TODAY) is None


def a_past_day_is_refused_without_writing():
    books.reset()
    past = books.find_slots(ALICE, '2026-09-01', 'haircut', AT)
    assert past['status'] == 'past', past
    assert past.get('slots') is None, 'nothing offered for a day that has gone'


def sunday_is_closed():
    books.reset()
    assert books.find_slots(ALICE, 'sunday', 'haircut', AT)['status'] == 'closed'


def a_day_more_than_a_week_off_is_read_as_an_ordinal():
    """ "Saturday the 3rd", never "Saturday the 3".

    A bare numeral is a written shape, and the voice reads it as one: on the
    live call of 2026-09-21 17:59 the caller was offered "a haircut, Saturday
    the 3 at 9:00 AM". Same reason `spoken_time` says "9:00 AM" and not
    "09:00" -- these strings are read out, not printed.

    Inside the week it carries its date too, since trace bbe42ad4, and today is still today.
    """
    monday = date(2026, 9, 21)
    assert diary.spoken_day(date(2026, 10, 3), monday) == 'Saturday the 3rd'
    assert diary.spoken_day(date(2026, 10, 1), monday) == 'Thursday the 1st'
    assert diary.spoken_day(date(2026, 10, 2), monday) == 'Friday the 2nd'
    assert diary.spoken_day(date(2026, 10, 12), monday) == 'Monday the 12th'
    assert diary.spoken_day(date(2026, 10, 21), monday) == 'Wednesday the 21st'
    assert diary.spoken_day(date(2026, 9, 26), monday) == 'Saturday the 26th'
    assert diary.spoken_day(monday, monday) == 'today'


def midnight_and_the_clock_edges():
    """The salon opens at nine, so nothing is offered around midnight either side.

    And "today", at one minute to midnight, has nothing left in it.
    """
    books.reset()
    just_before = datetime(2026, 9, 21, 23, 59, tzinfo=diary.TZ)
    tomorrow = books.find_slots(ALICE, 'tomorrow', 'haircut', just_before)
    assert tomorrow['date'] == '2026-09-22', tomorrow
    assert tomorrow['slots'][0]['time'] == '09:00'
    assert books.find_slots(ALICE, 'today', 'haircut', just_before)['status'] == 'full'


def the_clocks_changing_does_not_move_the_salon():
    """Spain changes the clocks at 02:00 on the last Sunday of October and March.

    The salon opens at 09:00, so no slot on the grid can land in a gap or a
    repeated hour. What this pins is that the day either side still reads as
    an ordinary day, which is what would break if any of this were done on
    naive datetimes.

    25 October 2026 is a Sunday (closed); the Monday after is an ordinary day.

    29 March 2026 is the spring change, also a Sunday.

    An hour's worth of booking still lands on the wall-clock time it names.
    """
    books.reset()
    after_autumn = datetime(2026, 10, 26, 10, 0, tzinfo=diary.TZ)
    monday = books.find_slots(ALICE, 'today', 'haircut', after_autumn)
    assert monday['status'] == 'ok' and monday['date'] == '2026-10-26', monday
    assert (
        books.find_slots(ALICE, 'today', 'haircut', datetime(2026, 3, 29, 10, 0, tzinfo=diary.TZ))['status']
        == 'closed'
    )
    spring_monday = datetime(2026, 3, 30, 10, 0, tzinfo=diary.TZ)
    offered = _slot(ALICE, 'today', 'haircut', at=spring_monday, time_of_day='11:00')
    saved = books.save_booking(ALICE, 'book', offered['slot_id'], '', False, spring_monday)
    assert saved['time'] == '11:00' and saved['spoken'] == 'today at 11:00 AM', saved


def changing_the_day_leaves_a_confirmed_booking_alone():
    """They book Tuesday, then ask about Wednesday. Tuesday is still theirs.

    A proposed change is a question; only another write changes a record.

    just looking
    """
    books.reset()
    offered = _slot(ALICE, 'tomorrow', 'haircut')
    booked = books.save_booking(ALICE, 'book', offered['slot_id'], '', False, AT)
    books.find_slots(ALICE, 'wednesday', 'haircut', AT)
    held = books.bookings_for(ALICE, TODAY)
    assert len(held) == 1 and held[0]['booking_id'] == booked['booking_id'], held


def correcting_the_number_leaves_the_other_customers_record():
    """The caller was the wrong person. The right person still has their booking.

    The call turns out to be Bob's. Alice's business record is untouched.

    And Bob cannot move a booking he does not hold.
    """
    books.reset()
    offered = _slot(ALICE, 'tomorrow', 'haircut')
    books.save_booking(ALICE, 'book', offered['slot_id'], '', False, AT)
    assert len(books.bookings_for(ALICE, TODAY)) == 1
    assert books.bookings_for(BOB, TODAY) == []
    assert 'no booking under this number' in _refused(
        books.save_booking, BOB, 'cancel', '', 'BK0001', False, AT
    )
    assert len(books.bookings_for(ALICE, TODAY)) == 1, 'the refusal wrote nothing'


def only_the_slots_it_returned_were_offered():
    """Twelve are shown and the rest were registered as offers anyway.

    A caller asking for `any` service gets the first twelve times across the
    three, and every eligible slot on the day used to be recorded as an offer
    before the cap was applied. A slot id nobody was ever read out therefore
    passed the "this diary offered that" check at write time -- which is the
    check that stops a fabricated time being booked, standing in for a claim
    it was not making.

    Learn a late blowout id, then throw the offers away with it.
    """
    books.reset()
    late = books.find_slots(ALICE, 'tomorrow', 'blowout', AT)['slots'][-1]
    books.reset()

    shown = books.find_slots(ALICE, 'tomorrow', 'any', AT)['slots']
    assert len(shown) == 12, len(shown)
    assert late['slot_id'] not in {slot['slot_id'] for slot in shown}, 'the fixture needs an unshown slot'
    assert 'not a slot this diary is currently offering' in _refused(
        books.save_booking, ALICE, 'book', late['slot_id'], '', False, AT
    )
    assert books.offer_of(ALICE, late['slot_id']) == {}, 'and it is not on record as offered'


def an_offer_is_made_to_one_caller():
    """Two callers, one slot id, and the offer is not the salon's to share.

    And ALICE, who was offered it, still has it.
    """
    books.reset()
    mine = _slot(ALICE, 'tomorrow', 'haircut')
    assert books.offer_of(ALICE, mine['slot_id'])['date'] == TOMORROW.isoformat()
    assert books.offer_of(BOB, mine['slot_id']) == {}, 'BOB was never offered it'
    assert 'not a slot this diary is currently offering this caller' in _refused(
        books.save_booking, BOB, 'book', mine['slot_id'], '', False, AT
    )
    assert books.save_booking(ALICE, 'book', mine['slot_id'], '', False, AT)['status'] == 'booked'


def a_preference_is_answered_before_the_list_is_cut_down():
    """F3: the twelve places went to times nobody asked about.

    Reproduced on all four live calls of 2026-09-21. On a day with nothing
    booked, `slots[:12]` is 09:00 to 14:30 -- so a caller asking for three in
    the afternoon was read the morning and told those were the free times.
    Nothing was wrong with the diary's answer; it was answering a question
    about the whole day.

    The same day, the same diary, the caller's own preference.

    And the alternatives are the ones either side of it, not the far end of the morning: an exact
    time ranks rather than filters, because the time they asked for may be taken and the near ones
    are the useful reply.

    Read out in the order of a day, whatever order they were chosen in.

    A daypart filters instead: being read the morning is not an answer to "the afternoon".

    Only the slots actually shown are offered, preference or not.
    """
    books.reset()
    plain = books.find_slots(ALICE, 'tomorrow', 'haircut', AT)['slots']
    assert len(plain) == 12 and plain[-1]['time'] <= '14:30', [s['time'] for s in plain]
    assert not [s for s in plain if s['time'] == '15:00'], 'the reproduction: three is not offered'

    wanted = books.find_slots(ALICE, 'tomorrow', 'haircut', AT, prefer='15:00')['slots']
    at_three = [s for s in wanted if s['time'] == '15:00']
    assert at_three, [s['time'] for s in wanted]
    times = [s['time'] for s in wanted]
    assert {'14:30', '15:00', '15:30'} <= set(times), times
    assert '09:00' not in times, 'the far end of the morning gave up its place'
    assert [s['time'] for s in wanted] == sorted(s['time'] for s in wanted)

    afternoon = books.find_slots(ALICE, 'tomorrow', 'haircut', AT, prefer='afternoon')['slots']
    assert afternoon and all('12:00' <= s['time'] <= '16:59' for s in afternoon), afternoon

    assert books.offer_of(ALICE, at_three[0]['slot_id'], ALICE)['time'] == '15:00'
    assert books.offer_of(ALICE, plain[0]['slot_id'], ALICE) == {}, 'the earlier search was withdrawn'


def a_preference_nothing_matches_is_not_obeyed_into_silence():
    """A daypart the salon cannot serve answers the day rather than nothing.

    Saturday closes at four, so there is no evening.

    Unreadable preferences are ignored, not guessed at.
    """
    books.reset()
    saturday = books.find_slots(ALICE, 'saturday', 'haircut', AT, prefer='evening')
    assert saturday['status'] == 'ok' and saturday['slots'], saturday
    assert books.find_slots(ALICE, 'tomorrow', 'haircut', AT, prefer='whenever')['slots']


def the_caller_s_own_words_choose_the_time():
    """`resolve_time` reads them, against the salon's own opening hours.

    A date is not a time.
    """
    assert diary.resolve_time('three') == '15:00', 'nothing is open at three in the morning'
    assert diary.resolve_time('nine') == '09:00'
    assert diary.resolve_time('half past two') == '14:30'
    assert diary.resolve_time('3 pm') == '15:00'
    assert diary.resolve_time('the afternoon') == 'afternoon'
    assert diary.resolve_time('whenever suits') == ''
    assert diary.resolve_time('') == ''
    assert diary.resolve_time('Friday the 25th') == ''


async def main():
    a_hair_colour_slot_can_be_booked()
    every_offered_slot_can_be_taken()
    a_fabricated_slot_is_refused()
    only_the_slots_it_returned_were_offered()
    a_preference_is_answered_before_the_list_is_cut_down()
    a_preference_nothing_matches_is_not_obeyed_into_silence()
    the_caller_s_own_words_choose_the_time()
    an_offer_is_made_to_one_caller()
    an_expired_offer_is_refused()
    a_slot_that_has_gone_past_is_refused()
    a_slot_taken_in_between_is_refused()
    a_long_appointment_blocks_an_overlapping_short_one()
    a_service_must_finish_before_closing()
    asking_for_any_service_shows_more_than_one()
    the_same_token_books_once()
    an_unknown_outcome_stays_unknown()
    a_refused_write_records_no_outcome()
    one_call_does_not_wipe_another()
    calls_with_a_diary_each_share_nothing()
    two_calls_from_one_phone_are_two_conversations()
    the_supported_phrases_resolve()
    the_phrases_of_trace_bbe42ad4_resolve()
    an_exact_date_decides_and_a_day_after_counts_from_its_day()
    anything_outside_the_policy_is_asked_about()
    a_modifier_the_policy_cannot_do_is_asked_about()
    a_weekday_must_be_a_whole_word()
    an_impossible_date_is_asked_about()
    a_past_day_is_refused_without_writing()
    sunday_is_closed()
    a_day_more_than_a_week_off_is_read_as_an_ordinal()
    midnight_and_the_clock_edges()
    the_clocks_changing_does_not_move_the_salon()
    changing_the_day_leaves_a_confirmed_booking_alone()
    correcting_the_number_leaves_the_other_customers_record()
    print('ok')


if __name__ == '__main__':
    asyncio.run(main())
