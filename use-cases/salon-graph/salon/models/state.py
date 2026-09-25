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
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

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


TimeWanted = Annotated[str, AfterValidator(_time_wanted)]
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
    booking_day: str = Field(
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
