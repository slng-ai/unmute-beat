"""The validated value types a salon call is made of.

Each one refuses what a plain `str` would accept: a phone number that is the
right length and still not a Spanish number, a date that exists as a shape and
not on a calendar, a time with a timezone or a stray second on it. A tool
argument or a state field typed with one of these cannot hold a wrong value,
so nothing downstream has to check.
"""

from __future__ import annotations

import re
from datetime import date as _date
from datetime import datetime
from datetime import time as _time
from typing import Annotated, Literal

import phonenumbers
from pydantic import AfterValidator, BeforeValidator, Field, TypeAdapter
from pydantic_extra_types.phone_numbers import PhoneNumberValidator

AgentName = Literal['concierge', 'verify', 'booking', 'complaints']
Service = Literal['haircut', 'hair-color', 'blowout']
Verification = Literal['', 'existing', 'created']
"""The lookup outcome, and only ever one of these. A bare `str` let a typo in a service response verify a caller."""


def _shape(pattern: str, message: str):
    """An after-validator that refuses a non-empty value not matching the pattern.

    Empty is not a wrong value, it is no value yet: it is what a field holds
    before anything fills it, and refusing it would deadlock the one step
    whose job is to fill it.
    """
    compiled = re.compile(pattern)

    def check(value: str) -> str:
        if value and not compiled.match(value):
            raise ValueError(message)
        return value

    return AfterValidator(check)


_spanish = TypeAdapter(
    Annotated[
        str | phonenumbers.PhoneNumber,
        PhoneNumberValidator(number_format='E164', supported_regions=['ES']),
    ]
)
"""Spain only, and Google's libphonenumber rather than a regex of our own.

A regex cannot know that +348340464 is eight national digits short of a real
Spanish number, or that a range of the right length is not one anybody has. No
`default_region`: with one, a bare "600111222" would be silently completed to
+34600111222, which is exactly the guess the verify step must never make.
"""


def _spanish_or_absent(value: str) -> str:
    """A real Spanish number, or '' for no number yet, which is the field's only absence."""
    return value if value == '' else _spanish.validate_python(value)


def _spanish_and_present(value: str) -> str:
    """A number somebody actually gave us. Absence is not one of them.

    The state's `Phone` has to permit '', because that is what the field holds
    before anybody has said a number. A tool *argument* is the opposite case:
    it is a number being submitted for a lookup, and an empty one submitted as
    if it were a number would create an empty customer record and verify the
    caller as nobody. Same digits, different question, so a different type.

    A Spanish number with the wrong count of digits says which way it is
    wrong. Coval run RPH7TzmF, K3: speech-to-text heard "six seven one, two
    seven three" as "672. 1. 273.", ten digits, and the model answered the
    bare validation error with "Please give me the rest of the digits".
    """
    if not value:
        raise ValueError('no number was given. Ask the caller for one; never look up an empty number')
    national = re.sub(r'\D', '', value)[2:] if value.startswith('+34') else ''
    if national and len(national) > 9:
        raise ValueError(
            f'that is {len(national)} digits after +34, and a Spanish number has 9: a digit was misheard or '
            "repeated. Nothing is missing, so do not ask for the rest; use verification_guidance 'correction' "
            'to ask for the whole number again.'
        )
    if national and len(national) < 9:
        raise ValueError(
            f'that is only {len(national)} digits after +34, and a Spanish number has 9. Use '
            "verification_guidance 'incomplete' to ask for the rest."
        )
    return _spanish.validate_python(value)


def _one_day(value: object) -> _date:
    """A day, as a `date` or the ISO string a service sends. Nothing else is a day.

    Pydantic's ordinary lax mode would take `1700000000` here and call it a
    day in 2023, so a number that reached a date field by accident would become
    a plausible, wrong appointment. `datetime` is refused too: a day carrying a
    time of its own is two answers to one question. `date.fromisoformat` is the
    calendar check as well as the shape one: it refuses 2026-02-30.
    """
    if isinstance(value, datetime):
        raise ValueError('expected a day, not a moment in time')
    if isinstance(value, _date):
        return value
    if isinstance(value, str):
        return _date.fromisoformat(value)
    raise ValueError('expected a day written year-month-day, like 2026-03-19')


def _one_time(value: object) -> _time:
    """A wall-clock time in this salon's own day: no timezone, no seconds.

    The salon has one timezone and a half-hour grid. A time arriving with an
    offset is asserting something about where it is, and a time with seconds
    on it did not come off that grid; both are a service disagreeing with the
    diary rather than answering it.
    """
    if isinstance(value, str):
        value = _time.fromisoformat(value)
    if not isinstance(value, _time) or isinstance(value, datetime):
        raise ValueError('expected a 24-hour time, like 09:30')
    if value.tzinfo is not None:
        raise ValueError('expected a local salon time, with no timezone on it')
    if value.second or value.microsecond:
        raise ValueError('expected a time on the half hour, with no seconds on it')
    return value


Phone = Annotated[
    str,
    AfterValidator(_spanish_or_absent),
    Field(
        description='A Spanish phone number the caller said, in full international form: +34 then nine digits.'
    ),
]
"""A state field: a Spanish number, or '' before anybody has given one.

An `AfterValidator` over a plain `str`, never the `PhoneNumber` type itself and
never a `pattern=` constraint: both put a keyword in the schema the model is
sent and some providers reject it. The description is how the shape reaches
the model instead; it travels as prose, so no schema converter can strip it.

It states the format and gives no example number: a model with no digits from
the caller copied the example, which was also the diary's seeded customer.
"""
Date = Annotated[_date, BeforeValidator(_one_day)]
Time = Annotated[_time, BeforeValidator(_one_time)]
"""Native `date` and `time`, validated by Pydantic, so a comparison is of days and not of spellings."""
Id = Annotated[str, _shape(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$', 'expected an identifier')]
SubmittedPhone = Annotated[
    str,
    AfterValidator(_spanish_and_present),
    Field(
        description='A Spanish phone number the caller said, in full international form: +34 then nine digits.'
    ),
]
"""A tool argument: a number being submitted for a lookup. Never for state; see `_spanish_and_present`."""


def unsupported_region(error: Exception) -> bool:
    """Whether a rejected number was well-formed but not Spanish.

    A number from another country is a question, which country are you calling
    from; a malformed one is a re-ask. libphonenumber distinguishes them and
    the only carrier of that distinction is the message.
    """
    return 'not from a supported region' in str(error)
