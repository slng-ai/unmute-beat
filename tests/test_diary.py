"""Run: uv run --env-file .env python -m tests.test_diary

The fake salon service, which is the oracle every flow test is measured
against. A POC cannot prove a booking flow correct if its diary accepts
impossible bookings, so these are the checks that the diary can say no.

The clock is passed in everywhere, so every case here is a fixed instant and
nothing depends on when it runs. `AT` is Monday 21 September 2026, 10:00 in
Madrid.

Asserts, no pytest, no fixtures, same as the rest.
"""

import asyncio
from datetime import date, datetime, timedelta

from salon import diary
from salon.state import open_call

AT = datetime(2026, 9, 21, 10, 0, tzinfo=diary.TZ)  # a Monday morning
TODAY = AT.date()
TOMORROW = TODAY + timedelta(days=1)
ALICE, BOB = '+34600111222', '+34934567890'


def _refused(fn, *args, **kwargs) -> str:
    try:
        fn(*args, **kwargs)
    except diary.Refused as refused:
        return str(refused)
    raise AssertionError('the diary accepted something it must refuse')


def _slot(phone, when, service, at=AT, time_of_day=None):
    """One slot the diary really offered, optionally at a named time."""
    slots = diary.find_slots(phone, when, service, at)['slots']
    if time_of_day is None:
        return slots[0]
    return next(s for s in slots if s['time'] == time_of_day)


# --- slot identity is the service's own, and never parsed --------------------


def a_hair_colour_slot_can_be_booked():
    """The defect that made one of three services unbookable.

    `slot_id.rsplit('-', 2)` cut `2026-09-22T0900-hair-color` in the wrong
    place, because the service name has a hyphen in it and the split reserved
    only two. `Date.fromisoformat` then raised, a bare `except` caught it, and
    the caller was told the diary had never returned a slot it had offered
    thirty seconds earlier.
    """
    diary.reset()
    offered = _slot(ALICE, 'tomorrow', 'hair-color')
    saved = diary.save_booking(ALICE, 'book', offered['slot_id'], '', False, AT)
    assert saved['status'] == 'booked'
    assert saved['service'] == 'hair-color', saved


def every_offered_slot_can_be_taken():
    """The general form of it: whatever the diary offers, the diary accepts.

    One service at a time, because an offer for one is only still valid while
    nothing else has taken the chair.
    """
    for service in diary.SERVICES:
        diary.reset()
        count = len(diary.find_slots(ALICE, 'tomorrow', service, AT)['slots'])
        assert count, service
        for index in range(count):
            # Reset first, then re-read: a reset clears the offers too, so an
            # id captured before it names a slot the diary no longer knows.
            diary.reset()
            offered = diary.find_slots(ALICE, 'tomorrow', service, AT)['slots'][index]
            saved = diary.save_booking(ALICE, 'book', offered['slot_id'], '', False, AT)
            assert saved['status'] == 'booked', (service, offered)
            assert saved['service'] == service, saved


def a_fabricated_slot_is_refused():
    """03:07 on a day the salon opens at nine. Shaped right, offered never.

    It is refused because the diary has no record of issuing it, not because
    anybody checked the time: that check exists too, below, and this one holds
    whatever the id looks like.
    """
    diary.reset()
    for invented in (
        f'{TOMORROW.isoformat()}T0307|haircut',   # outside opening hours
        f'{TOMORROW.isoformat()}T0915|haircut',   # off the half-hour grid
        f'{TOMORROW.isoformat()}T0900|nails',     # not a service
        'have-a-slot',
        '',
    ):
        assert 'not a slot this diary returned' in _refused(
            diary.save_booking, ALICE, 'book', invented, '', False, AT
        ), invented
    assert diary.bookings_for(ALICE, TODAY) == [], 'and nothing was written'


def an_expired_offer_is_refused():
    """A slot offered yesterday is not a slot today, even if the day is still ahead."""
    diary.reset()
    offered = _slot(ALICE, '2026-09-30', 'haircut')
    assert 'expired' in _refused(
        diary.save_booking, ALICE, 'book', offered['slot_id'], '', False, AT + timedelta(days=1)
    )
    assert diary.bookings_for(ALICE, TODAY) == []


def a_slot_that_has_gone_past_is_refused():
    """Offered this morning, booked this afternoon, for this morning."""
    diary.reset()
    offered = _slot(ALICE, 'today', 'haircut', time_of_day='11:00')
    later = AT.replace(hour=14)
    assert 'gone past' in _refused(diary.save_booking, ALICE, 'book', offered['slot_id'], '', False, later)


def a_slot_taken_in_between_is_refused():
    """Free when it was offered is not free when it is written.

    The re-check at write time, which is the difference between a registry of
    what was offered and a diary that is actually right.
    """
    diary.reset()
    offered = _slot(ALICE, 'tomorrow', 'haircut', time_of_day='09:00')
    also_offered = _slot(BOB, 'tomorrow', 'haircut', time_of_day='09:00')
    diary.save_booking(ALICE, 'book', offered['slot_id'], '', False, AT)
    assert 'has the chair' in _refused(
        diary.save_booking, BOB, 'book', also_offered['slot_id'], '', False, AT
    )


# --- one chair, and a service takes time -------------------------------------


def a_long_appointment_blocks_an_overlapping_short_one():
    """The capacity rule, stated and then tested.

    # ponytail: one service resource. Two appointments clash when their
    # intervals overlap, not when their start times match -- which is all the
    # diary used to compare, so a 45-minute cut at 09:30 fitted inside a
    # 120-minute colour that started at 09:00 and both were confirmed.
    """
    diary.reset()
    colour = _slot(ALICE, 'tomorrow', 'hair-color', time_of_day='09:00')
    diary.save_booking(ALICE, 'book', colour['slot_id'], '', False, AT)

    free = diary.find_slots(BOB, 'tomorrow', 'haircut', AT)['slots']
    assert not any(s['time'] in ('09:00', '09:30', '10:00', '10:30') for s in free), free[:4]
    assert free[0]['time'] == '11:00', 'the chair is free when the colour finishes, not before'


def a_service_must_finish_before_closing():
    """A two-hour colour cannot start an hour before the doors shut."""
    diary.reset()
    saturday = diary.find_slots(ALICE, 'saturday', 'hair-color', AT)  # closes at 16:00
    assert saturday['slots'][-1]['time'] == '14:00', saturday['slots'][-1]


def asking_for_any_service_shows_more_than_one():
    """The cap must not starve a service.

    Built service by service and truncated as built, all twelve places went to
    whichever service was listed first, so `any` reported a salon that does
    haircuts and nothing else.
    """
    diary.reset()
    offered = diary.find_slots(ALICE, 'tomorrow', 'any', AT)['slots']
    assert len({s['service'] for s in offered}) > 1, offered
    assert offered == sorted(offered, key=lambda s: (s['time'], s['service'])), 'soonest first'


# --- a write has an identity -------------------------------------------------


def the_same_token_books_once():
    """Commit, lose the response, retry. One booking.

    Without an operation token the only options are to retry blind and risk
    two bookings, or not retry and lose one that succeeded.
    """
    diary.reset()
    offered = _slot(ALICE, 'tomorrow', 'haircut')
    first = diary.save_booking(ALICE, 'book', offered['slot_id'], '', False, AT, operation_token='op-1')
    # The caller never saw `first`: the response was lost. It asks again.
    again = diary.save_booking(ALICE, 'book', offered['slot_id'], '', False, AT, operation_token='op-1')
    assert first == again
    assert len(diary.bookings_for(ALICE, TODAY)) == 1, diary.bookings_for(ALICE, TODAY)


def an_unknown_outcome_stays_unknown():
    """`None` means "not started, or we cannot say", and never "it failed".

    A service that answers "failed" for a write it cannot find has invented a
    fact. The caller reconciles instead.
    """
    diary.reset()
    assert diary.outcome_of('never-sent') is None
    offered = _slot(ALICE, 'tomorrow', 'haircut')
    diary.save_booking(ALICE, 'book', offered['slot_id'], '', False, AT, operation_token='op-2')
    assert diary.outcome_of('op-2')['status'] == 'booked'


def a_refused_write_records_no_outcome():
    """A refusal is not an outcome to replay: the caller may legitimately retry."""
    diary.reset()
    _refused(diary.save_booking, ALICE, 'book', 'invented', '', False, AT, operation_token='op-3')
    assert diary.outcome_of('op-3') is None


# --- calls do not erase each other -------------------------------------------


def one_call_does_not_wipe_another():
    """`open_call` used to reset the process-global diary.

    Call A books, call B starts, and A's appointment was gone -- in a fixture
    whose whole job is to be the truth the flow tests are checked against.
    """
    diary.reset()
    offered = _slot(ALICE, 'tomorrow', 'haircut')
    diary.save_booking(ALICE, 'book', offered['slot_id'], '', False, AT)
    assert len(diary.bookings_for(ALICE, TODAY)) == 1

    second = open_call(BOB)
    assert len(diary.bookings_for(ALICE, TODAY)) == 1, "A's booking survived B arriving"
    # And B cannot act on A's record: the diary is shared, the conversation is not.
    assert diary.bookings_for(BOB, TODAY) == []
    assert second.state.customer_phone == BOB
    assert second.state.customer_verified == '', 'a new call knows nothing about the last one'


# --- the date policy, which is a policy and not understanding ----------------


def the_supported_phrases_resolve():
    assert diary.resolve_day('today', TODAY) == TODAY
    assert diary.resolve_day('tomorrow', TODAY) == TOMORROW
    assert diary.resolve_day('2026-10-01', TODAY) == date(2026, 10, 1)
    # Monday said on a Monday is next Monday, not this morning.
    assert diary.resolve_day('monday', TODAY) == TODAY + timedelta(days=7)
    assert diary.resolve_day('friday', TODAY) == date(2026, 9, 25)
    assert diary.resolve_day('next friday', TODAY) == date(2026, 10, 2)


def anything_outside_the_policy_is_asked_about():
    """Not guessed. The agent gets `need_day` and asks which day."""
    for outside in ('whenever suits', 'the 25th', 'in a fortnight', ''):
        assert diary.resolve_day(outside, TODAY) is None, outside
    assert diary.find_slots(ALICE, 'whenever suits', 'haircut', AT)['status'] == 'need_day'


def a_modifier_the_policy_cannot_do_is_asked_about():
    """"A week on Tuesday" is not next Tuesday, and must not be answered as it.

    A weekday match on its own found "Tuesday" here and returned the next one:
    seven days early, a real date, and silent. The phrases this policy does
    not implement are named and refused, so the agent asks.
    """
    for shifted in ('a week on Tuesday', 'the Tuesday after next', 'in two weeks on Friday',
                    'the following Monday', 'in 3 weeks'):
        assert diary.resolve_day(shifted, TODAY) is None, shifted
    # The plain forms it does implement still work.
    assert diary.resolve_day('tuesday', TODAY) == date(2026, 9, 22)
    assert diary.resolve_day('next tuesday', TODAY) == date(2026, 9, 29)


def a_weekday_must_be_a_whole_word():
    """"satur" inside another word used to resolve a real Saturday.

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
    diary.reset()
    past = diary.find_slots(ALICE, '2026-09-01', 'haircut', AT)
    assert past['status'] == 'past', past
    assert past.get('slots') is None, 'nothing offered for a day that has gone'


def sunday_is_closed():
    diary.reset()
    assert diary.find_slots(ALICE, 'sunday', 'haircut', AT)['status'] == 'closed'


def midnight_and_the_clock_edges():
    """The salon opens at nine, so nothing is offered around midnight either side."""
    diary.reset()
    just_before = datetime(2026, 9, 21, 23, 59, tzinfo=diary.TZ)
    tomorrow = diary.find_slots(ALICE, 'tomorrow', 'haircut', just_before)
    assert tomorrow['date'] == '2026-09-22', tomorrow
    assert tomorrow['slots'][0]['time'] == '09:00'
    # And "today", at one minute to midnight, has nothing left in it.
    assert diary.find_slots(ALICE, 'today', 'haircut', just_before)['status'] == 'full'


def the_clocks_changing_does_not_move_the_salon():
    """Spain changes the clocks at 02:00 on the last Sunday of October and March.

    The salon opens at 09:00, so no slot on the grid can land in a gap or a
    repeated hour. What this pins is that the day either side still reads as
    an ordinary day, which is what would break if any of this were done on
    naive datetimes.
    """
    diary.reset()
    # 25 October 2026 is a Sunday (closed); the Monday after is an ordinary day.
    after_autumn = datetime(2026, 10, 26, 10, 0, tzinfo=diary.TZ)
    monday = diary.find_slots(ALICE, 'today', 'haircut', after_autumn)
    assert monday['status'] == 'ok' and monday['date'] == '2026-10-26', monday
    # 29 March 2026 is the spring change, also a Sunday.
    assert diary.find_slots(ALICE, 'today', 'haircut',
                            datetime(2026, 3, 29, 10, 0, tzinfo=diary.TZ))['status'] == 'closed'
    # An hour's worth of booking still lands on the wall-clock time it names.
    spring_monday = datetime(2026, 3, 30, 10, 0, tzinfo=diary.TZ)
    offered = _slot(ALICE, 'today', 'haircut', at=spring_monday, time_of_day='11:00')
    saved = diary.save_booking(ALICE, 'book', offered['slot_id'], '', False, spring_monday)
    assert saved['time'] == '11:00' and saved['spoken'] == 'today at 11:00 AM', saved


# --- a correction does not destroy what was already agreed -------------------


def changing_the_day_leaves_a_confirmed_booking_alone():
    """They book Tuesday, then ask about Wednesday. Tuesday is still theirs.

    A proposed change is a question; only another write changes a record.
    """
    diary.reset()
    offered = _slot(ALICE, 'tomorrow', 'haircut')
    booked = diary.save_booking(ALICE, 'book', offered['slot_id'], '', False, AT)
    diary.find_slots(ALICE, 'wednesday', 'haircut', AT)  # just looking
    held = diary.bookings_for(ALICE, TODAY)
    assert len(held) == 1 and held[0]['booking_id'] == booked['booking_id'], held


def correcting_the_number_leaves_the_other_customers_record():
    """The caller was the wrong person. The right person still has their booking."""
    diary.reset()
    offered = _slot(ALICE, 'tomorrow', 'haircut')
    diary.save_booking(ALICE, 'book', offered['slot_id'], '', False, AT)
    # The call turns out to be Bob's. Alice's business record is untouched.
    assert len(diary.bookings_for(ALICE, TODAY)) == 1
    assert diary.bookings_for(BOB, TODAY) == []
    # And Bob cannot move a booking he does not hold.
    assert 'no booking under this number' in _refused(
        diary.save_booking, BOB, 'cancel', '', 'BK0001', False, AT
    )
    assert len(diary.bookings_for(ALICE, TODAY)) == 1, 'the refusal wrote nothing'


async def main():
    a_hair_colour_slot_can_be_booked()
    every_offered_slot_can_be_taken()
    a_fabricated_slot_is_refused()
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
    the_supported_phrases_resolve()
    anything_outside_the_policy_is_asked_about()
    a_modifier_the_policy_cannot_do_is_asked_about()
    a_weekday_must_be_a_whole_word()
    an_impossible_date_is_asked_about()
    a_past_day_is_refused_without_writing()
    sunday_is_closed()
    midnight_and_the_clock_edges()
    the_clocks_changing_does_not_move_the_salon()
    changing_the_day_leaves_a_confirmed_booking_alone()
    correcting_the_number_leaves_the_other_customers_record()
    print('ok')


asyncio.run(main())
