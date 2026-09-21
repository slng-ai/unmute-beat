"""What a call to Sage and Stone knows.

Three of the original's six call variables are gone: the date, the weekday and
the salon clock. They existed so the model could work out which day "next
Friday" was and whether an afternoon slot had already passed. `salon.diary` does
both in code now, so nothing has to be told to the model and nothing can be got
wrong. What is left is what only the conversation can establish.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

import phonenumbers
from pydantic import AfterValidator, BaseModel, Field, TypeAdapter
from pydantic_extra_types.phone_numbers import PhoneNumberValidator

from voice import harness

AgentName = Literal['concierge', 'verify', 'booking', 'complaints']
Service = Literal['haircut', 'hair-color', 'blowout']
# The lookup outcome, and only ever one of these. It used to be a bare `str`,
# which meant a typo in a service response became a verified caller.
Verification = Literal['', 'existing', 'created']


def _shape(pattern: str, message: str):
    compiled = re.compile(pattern)

    def check(value: str) -> str:
        # Empty is not a wrong value, it is no value yet: it is what a field
        # holds before anything fills it. Refusing it deadlocks the one step
        # whose job is to fill it, because it has nothing else to send.
        if value and not compiled.match(value):
            raise ValueError(message)
        return value

    return AfterValidator(check)


# Spain only, and Google's libphonenumber rather than a regex of our own. The
# call that made this necessary offered +348340464 and the old E.164 pattern
# took it: eight national digits is a real-looking number and nothing here knew
# the Spanish plan has nine. libphonenumber knows, and it also knows which
# ranges are ordinary contact numbers -- +34500111222 is the right length and
# still not a number anybody has.
#
# No `default_region`. With one, a bare "600111222" would be silently completed
# to +34600111222, which is exactly the guess `verify.md` tells the model never
# to make. Without one it is refused and the agent asks which country.
_spanish = TypeAdapter(
    Annotated[
        str | phonenumbers.PhoneNumber,
        PhoneNumberValidator(number_format='E164', supported_regions=['ES']),
    ]
)


def _spanish_or_absent(value: str) -> str:
    # Empty is not a wrong value, it is no value yet -- the same sentinel the
    # other shapes keep, and the only absence this field has.
    return value if value == '' else _spanish.validate_python(value)


# AfterValidator over a plain `str`, never the `PhoneNumber` type itself and
# never a `pattern=` constraint: both put a keyword in the schema the model is
# sent (`format: phone`, `pattern`) and some providers reject it. The
# description is how the shape reaches the model instead -- it travels as prose,
# so no strict schema converter can strip it.
Phone = Annotated[
    str,
    AfterValidator(_spanish_or_absent),
    Field(description='A Spanish phone number in full international form, like +34600111222.'),
]
Date = Annotated[
    str, _shape(r'^\d{4}-\d{2}-\d{2}$', 'expected a day written year-month-day, like 2026-03-19')
]
Time = Annotated[str, _shape(r'^([01]\d|2[0-3]):[0-5]\d$', 'expected a 24-hour time, like 09:30')]
Id = Annotated[str, _shape(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$', 'expected an identifier')]


def unsupported_region(error: Exception) -> bool:
    """Whether a rejected number was well-formed but not Spanish.

    Two different conversations. A number from another country is a question --
    which country are you calling from, we can only look up Spanish numbers --
    and a malformed one is a re-ask. libphonenumber distinguishes them and the
    only carrier of that distinction is the message.
    """
    return 'not from a supported region' in str(error)


class Appointment(BaseModel):
    """The last booking action that actually succeeded.

    Every field comes from the diary, never from the model: `spoken` in
    particular is the one phrase to say this booking out loud, computed once so
    two agents cannot describe the same appointment two different ways.
    """

    model_config = harness.STRICT

    booking_id: Id
    service: Service
    date: Date
    time: Time
    spoken: str
    action: Literal['book', 'move', 'cancel']


class Handoff(harness.Handoff):
    to: AgentName


class SalonState(harness.VoiceState):
    agent: AgentName = 'concierge'
    # Narrowed from the base `str | None`: an unknown step name is now a
    # validation error rather than a `KeyError` on the next turn.
    pending: AgentName | None = None

    # Prefetched from the carrier, and a guess until the caller agrees to it.
    # Only `verify.md` renders it; no other prompt mentions it, so no other
    # agent can read somebody else's number back to them.
    customer_phone: Phone = ''
    # Three separate facts, kept apart on purpose. This one says only that the
    # digits are a real Spanish number; the next says the caller agreed to them
    # when they were read back; the third says what the customer lookup found.
    # They used to be one field, which let structural validity stand in for
    # identity -- and a valid-length wrong number is still the wrong customer.
    phone_confirmed: bool = False
    # '' until verification has run. Every prompt branches on this, and every
    # tool that touches a customer record refuses without it.
    customer_verified: Verification = ''
    # The day the diary was last read for, written by `read_diary` and never by
    # the model. A slot is only bookable on this day, so a caller who changes
    # their mind about the day cannot be put back onto the old one: the offer
    # died with the question. The consequences of a correction are computed, not
    # asked about, because no prompt sentence can hold an invariant.
    booking_day: Date = ''
    appointment: Appointment | None = None
    transfer: str = ''


# Keypad spellings of the words a carrier substitutes for a withheld number.
# They pass a shape check because they are the right shape: digits, about as
# many as a phone number has.
_WITHHELD = ('266696687', '7378742833', '8656696', '86282452253', '7748283')


def prefetch_number(raw: str) -> str:
    """The caller's number, or '' when the carrier sent a placeholder instead.

    A withheld caller ID arrives three ways: empty, as a word, or as the keypad
    digits of a word an upstream carrier sent. All three mean the same thing,
    and a value that reaches a prompt is one the agent reads back as their own.
    """
    text = (raw or '').strip()
    digits = text[1:] if text.startswith('+') else text
    if not (text.startswith('+') and digits.isascii() and digits.isdigit() and 8 <= len(digits) <= 15):
        return ''
    # Only this direction: a value that *begins* with a spelling is a real
    # number. "unknown" spells 8656696, and +86 5669 6xxx is an ordinary mobile.
    return '' if any(word.startswith(digits) for word in _WITHHELD) else text


class SalonCall(harness.CallContext):
    """This call's state, history and line. The agents' `deps`."""

    state: SalonState = Field(default_factory=SalonState)


def open_call(from_number: str = '') -> SalonCall:
    """A fresh call. Nothing here can fail it; a fact we cannot get is left unset.

    It does **not** reset the diary. It used to, and that made every new call
    erase the salon's bookings: call B opening deleted the appointment call A
    had just made. The diary is the business, shared by everyone who rings;
    only the conversation is per-call. Fixtures reset it explicitly in their own
    setup, which is the only place that should.
    """
    return SalonCall(state=SalonState(customer_phone=prefetch_number(from_number)))
