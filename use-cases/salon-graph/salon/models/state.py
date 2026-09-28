"""What a call to Sage and Stone knows: one validated model, shared by every step of the call.

Plain data. Core keeps it as the session's `state.facts` and replaces it
whole on every change; nothing here knows about calls, steps or consent.
Which fields a model may record from the caller, and which only a receipt
may set, is declared in `salon/agent.yaml` (`fields:` and `assign:`).

The caller's request (service, day, time) lives here, not in a hand-over
argument, so it survives the detour through verification exactly as the
caller left it, with any correction they made on the way (session 3's T3.v2
lost the requested 2 PM on that detour).

The date, the weekday and the salon clock are not state: the diary decides a
day in code (`salon.diary.resolve_day`), and every step's model is shown
today's date and the days ahead (`core.clock.Today`).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationInfo

from salon.diary import WEEKDAYS, day_phrases, resolve_day
from salon.models.shapes import Phone, Service, Verification

_WANTS = re.compile(r'^(?:\d{2}:\d{2}|morning|afternoon|evening)$')


def _time_wanted(value: str) -> str:
    if value and not _WANTS.match(value):
        raise ValueError(
            'write the time as HH:MM on a 24-hour clock (14:00 for 2 PM), or morning, afternoon or evening'
        )
    return value


_COMPLAINT_REQUEST_WORDS = (
    'a about an are be been caller can client complain complaining complaint complaints could customer '
    'er file files filing formal had has have he hello her hi his i id im in is it leave leaving like '
    'lodge lodges lodging make makes making may maybe me might my need official our please put raise '
    'raises raising register registering she someone something submit submitting the their they to uh '
    'um us want wants was we were wish would yeah yes you your '
)
_ASKING_TO_COMPLAIN = frozenset(_COMPLAINT_REQUEST_WORDS.split())
"""The words in "I'd like to raise a complaint" and nothing else.

A complaint made only of these describes wanting to complain, not what went
wrong, and reading it back asks the caller to confirm their own request.

ponytail: a closed word list, and it fails the same way: towards asking what
happened. Upgrade it with the salon's other languages.
"""


def _describes_something(value: str) -> str:
    words = [word.strip("'") for word in re.findall(r"[a-zA-Z']+", value.lower())]
    if value and all(word in _ASKING_TO_COMPLAIN for word in words):
        raise ValueError('that is the caller asking to complain, not what went wrong. Ask them what happened')
    return value


def bare_weekday(words: str) -> int | None:
    """The weekday these words name on their own ("Saturday", "on Saturday instead"), or None."""
    phrases = day_phrases(words)
    index = {name.casefold(): number for number, name in enumerate(WEEKDAYS)}
    return index.get(phrases[0]) if len(phrases) == 1 else None


def ambiguous_day(words: str, today: date, discussed: date | None) -> tuple[date, date] | None:
    """Two dates a bare weekday could mean beside the date under discussion, or None when it means one.

    "Saturday instead", said about "Friday next week", is the Saturday after
    that Friday to one caller and the coming Saturday to another (session 3:
    three live calls, two different dates). Read from today it is the coming
    one; read beside the date under discussion it is the one in that week.
    When those differ, neither is chosen: the caller is asked, with both dates.
    """
    weekday = bare_weekday(words)
    if weekday is None or discussed is None:
        return None
    coming = resolve_day(words, today)
    beside = discussed - timedelta(days=discussed.weekday()) + timedelta(days=weekday)
    if coming is None or beside < today or beside == coming:
        return None
    return coming, beside


_ISO_DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _as_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if _ISO_DAY.match(value) else None
    except ValueError:
        return None


def _day_meant(value: str, info: ValidationInfo) -> str:
    """The day asked for as the date it means (YYYY-MM-DD) once it can mean only one; else the caller's words.

    Read on the call's clock (`today` in the validation context core passes),
    so "Thursday" and "2026-10-01" are one value, and a change of spelling is
    no change of request (session 4d, live call 5: the day recorded again as
    a date took the times on offer away). A date is kept as it is, so it is
    never read again as the call goes on.

    A bare weekday is a date only when it cannot mean two: beside the day
    under discussion (the day asked for before, `before`) it may name
    another week (`ambiguous_day`), and then it stays words, so the times
    lookup asks the caller which one. So does one said about a booking being
    moved when no day was asked for yet: that booking's date is the diary's,
    and a validator reads no diary. Words the policy cannot place stay words.
    Without a context (a model built outside a call) nothing is read.
    """
    context = info.context or {}
    today = context.get('today')
    if not value or not isinstance(today, date) or _ISO_DAY.match(value):
        return value
    day = resolve_day(value, today)
    if day is None:
        return value
    before = context.get('before')
    discussed = _as_date(getattr(before, 'booking_day', ''))
    if bare_weekday(value) is not None:
        if discussed is None and getattr(before, 'appointment', ''):
            return value
        if ambiguous_day(value, today, discussed) is not None:
            return value
    return day.isoformat()


TimeWanted = Annotated[str, AfterValidator(_time_wanted)]
DayMeant = Annotated[str, AfterValidator(_day_meant)]
ComplaintWords = Annotated[str, AfterValidator(_describes_something)]


class SalonState(BaseModel):
    """The business facts of one salon call. Every field has a default: a call starts empty.

    Three facts about the caller's number are kept apart on purpose:
    `customer_phone` is the number the caller gave (or the carrier sent),
    `verified_phone` the number the customer lookup was run against, and
    `customer_status` what it found. Only a lookup's receipt sets the last
    two, and core clears them when `customer_phone` changes: a corrected number
    is somebody else until it is looked up.
    """

    model_config = ConfigDict(extra='forbid')

    customer_phone: Phone = ''
    verified_phone: Phone = ''
    """Set only by the lookup's receipt, for the number it looked up. Booking and complaints require it."""
    customer_status: Verification = ''

    service: Service | None = Field(
        None, description='The service the caller wants: haircut, hair-color or blowout.'
    )
    booking_day: DayMeant = Field(
        '',
        description=(
            'The day the caller asked for, in their own words ("Friday", "this Friday", "Saturday next week"), '
            'or as YYYY-MM-DD once they have agreed to an exact date.'
        ),
    )
    booking_time: TimeWanted = Field(
        '',
        description='The time the caller asked for: HH:MM on a 24-hour clock (14:00 for 2 PM), or morning, afternoon or evening.',
    )
    slot: str = Field('', description='The id of the time the caller picked, from the times on offer.')
    appointment: str = Field(
        '',
        description='The id of the booking they hold that they want moved or cancelled, from their bookings on offer.',
    )
    booked: str = ''
    """The booking the last write made, moved or cancelled. Set only by a receipt."""
    booked_spoken: str = ''
    """That booking as the diary says it out loud. Set only by a receipt."""

    complaint: ComplaintWords = Field(
        '',
        description="What went wrong, in the caller's own words: the shortest complete excerpt of what they said.",
    )
    wanted_outcome: str = Field(
        '',
        description='What the caller wants the salon to do about it, in their own words, or empty if they have not said.',
    )
    complaint_reference: str = ''
    """Set only by the complaint book's receipt."""


_WITHHELD = ('266696687', '7378742833', '8656696', '86282452253', '7748283')
"""Keypad spellings of the words a carrier substitutes for a withheld number. They pass a shape check because they are the right shape."""


def prefetch_number(raw: str) -> str:
    """The caller's number, or '' when the carrier sent a placeholder instead.

    A withheld caller ID arrives three ways: empty, as a word, or as the keypad
    digits of a word an upstream carrier sent. All three mean the same thing,
    and a value that reaches a prompt is one the agent reads back as theirs.
    Only a value that *begins* with a spelling is refused: "unknown" spells
    8656696, and +86 5669 6xxx is an ordinary mobile.
    """
    text = (raw or '').strip()
    digits = text[1:] if text.startswith('+') else text
    if not (text.startswith('+') and digits.isascii() and digits.isdigit() and 8 <= len(digits) <= 15):
        return ''
    return '' if any(word.startswith(digits) for word in _WITHHELD) else text
