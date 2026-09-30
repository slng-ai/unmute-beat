"""The value types every application would otherwise write for itself: receipts, a phone number, a caller's words.

Native Pydantic comes first. A `date`, a `time`, a `Decimal`, a `Literal` and
Pydantic's own constraints already say most of what a state or a receipt
needs, so nothing here restates them. What is here is only what native
validation cannot say, or what every application repeated:

- `Receipt` and `Rejection`: the base of what a service answered, strict, so
  a malformed answer fails where it arrives. `Subject` and `Version` mark
  which receipt field names the resource and which its version.
- `NotPast`: a date the call's own clock has not left behind. Only core knows
  that clock (`CallContext.validation_context`).
- `Phone`: a real number from any country, with the country code the caller
  said. Its spoken form is what a read-back says.
- `Quoted`: words the caller actually said.

Each validator raises `PydanticCustomError` with a stable type
(`phone_too_long`, `not_quoted`, ...): the model reads the message, and the
locale can say the same problem to the caller in its own words
(`Locale.refusals`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any

import phonenumbers
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    GetCoreSchemaHandler,
    GetJsonSchemaHandler,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import PydanticCustomError, core_schema
from pydantic_extra_types.phone_numbers import PhoneNumberValidator

__all__ = ['NotPast', 'Phone', 'Quoted', 'Receipt', 'Rejection', 'Subject', 'Version']


class Receipt(BaseModel):
    """What a service answered, validated before any field of it is read.

    Defaults, assignments and nested instances are all validated (core's
    `STRICT`), and an unknown field is refused: a service that answers in a
    shape nobody declared has not answered this question. Every member of a
    receipt union subclasses this, so which member an answer is can always
    be decided.
    """

    model_config = ConfigDict(
        validate_default=True, validate_assignment=True, revalidate_instances='always', extra='forbid'
    )


class Rejection(Receipt):
    """An authoritative no: the service refused, and nothing was done. It settles the intent as `rejected`."""

    reason: str = ''


@dataclass(frozen=True)
class Subject:
    """Marks the receipt field naming the resource an operation acts on: `Annotated[str, Subject('booking')]`.

    `kind` is the kind of resource the id names (ids are unique only within
    one kind). Empty: the field's own name.
    """

    kind: str = ''


@dataclass(frozen=True)
class Version:
    """Marks the receipt field holding the resource's version after the change: `Annotated[int, Version()]`."""


def _not_past(value: date, info: ValidationInfo) -> date:
    today = (info.context or {}).get('today')
    if isinstance(today, date) and value < today:
        raise PydanticCustomError(
            'date_past',
            '{day} has already gone past: today is {today}. Ask for a day that has not.',
            {'day': value.isoformat(), 'today': today.isoformat()},
        )
    return value


NotPast = AfterValidator(_not_past)
"""A date on or after the call's own today: `Annotated[date, NotPast]`.

Without a call (a model built outside one) there is no today, and nothing is
refused. Only the past is refused: how far ahead a business takes bookings is
its own rule.
"""


def words(text: str) -> str:
    """The words of a text, lower case, punctuation gone: how a caller's words are compared, whatever STT added."""
    return ' '.join(re.findall(r'\w+', text.casefold()))


def _quoted(value: str, info: ValidationInfo) -> str:
    context = info.context or {}
    said = context.get('said')
    if said is None or not value:
        return value
    before = getattr(context.get('before'), info.field_name or '', None)
    wanted = words(value)
    if value == before or (wanted and any(f' {wanted} ' in f' {words(text)} ' for text in said)):
        return value
    raise PydanticCustomError(
        'not_quoted',
        "that must be the caller's own words, copied from what they said. Punctuation and case may differ; "
        'nothing else may.',
    )


Quoted = Annotated[str, AfterValidator(_quoted)]
"""Text that must be the caller's own words, found whole in something they said on this call.

An extractive record trades paraphrase for evidence anyone can check: a
complaint read back in the caller's words is theirs, not the model's summary.
A value already held passes unchanged. Without a call there is nothing to
check against, and nothing is refused.
"""


class Phone(str):
    """A real phone number from any country, in E.164 form: a plus, the country code, then the number.

    Validated by libphonenumber, never by a pattern, and never completed
    with a guessed country: a number is only what follows the caller's own
    "+". A wrong one is refused with the reason in its error type
    (`phone_no_country_code`, `phone_too_short`, `phone_too_long`,
    `phone_unknown_country`, `phone_invalid`), so the next question can be
    the right one.

    Its JSON schema is a plain string with a description: some providers
    reject a `pattern` or `format` keyword in a tool schema, and the shape
    reaches the model as prose instead. The description gives no example
    number, because a model with no digits from the caller copies one.
    """

    DESCRIPTION = 'A phone number the caller said, in full international form: a plus, the country code, then the number.'

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls.parse, core_schema.str_schema(), serialization=core_schema.to_string_ser_schema()
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return {'type': 'string', 'description': cls.DESCRIPTION}

    @classmethod
    def parse(cls, value: str) -> Phone:
        """The number as a `Phone`, or `PydanticCustomError` saying which way it is wrong."""
        if isinstance(value, Phone):
            return value
        text = value.strip()
        if not text.startswith('+'):
            raise PydanticCustomError(
                'phone_no_country_code',
                'that number has no country code. Ask which country the number is from; never guess a country code',
            )
        match _length(text):
            case 'too long':
                raise PydanticCustomError(
                    'phone_too_long',
                    'I heard {grouped}: that is {digits}, too many for its country, so a digit was misheard or '
                    'repeated. Say those digits back to the caller and ask which part is wrong. Nothing is '
                    'missing, so do not ask for the rest.',
                    {'grouped': grouped(text), 'digits': _digits_after_code(text)},
                )
            case 'too short':
                raise PydanticCustomError(
                    'phone_too_short',
                    'that is only {digits}, too few for its country. Ask for the rest of the digits.',
                    {'digits': _digits_after_code(text)},
                )
            case 'unknown country code':
                raise PydanticCustomError(
                    'phone_unknown_country',
                    'no country has that code. Ask for the whole number again, with the country code.',
                )
        try:
            return cls(_E164.validate_python(text))
        except ValidationError:
            raise PydanticCustomError(
                'phone_invalid', 'that is not a number anybody has. Ask for the whole number again.'
            ) from None

    @property
    def spoken(self) -> str:
        """The number as a person reads it out, grouped for its own country: "+34 671 27 34 54"."""
        number = phonenumbers.parse(self, None)
        return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.INTERNATIONAL)

    @classmethod
    def from_carrier(cls, raw: str | None) -> Phone | None:
        """The caller's number as the carrier sent it, or None when it is withheld or not a real number.

        A withheld caller ID arrives three ways: empty, as a word, or as the
        keypad digits of a word an upstream carrier sent. All three mean the
        same thing, and a value that reaches a prompt is one the agent reads
        back as theirs. Only a value that *begins* with a spelling is
        refused: "unknown" spells 8656696, and +86 5669 6xxx is an ordinary
        mobile.
        """
        text = (raw or '').strip()
        digits = text[1:] if text.startswith('+') else text
        if not (text.startswith('+') and digits.isascii() and digits.isdigit() and 8 <= len(digits) <= 15):
            return None
        if any(word.startswith(digits) for word in _WITHHELD):
            return None
        try:
            return cls.parse(text)
        except PydanticCustomError:
            return None


_E164 = TypeAdapter(Annotated[str, PhoneNumberValidator(number_format='E164')])
"""pydantic-extra-types' validator, with no `default_region`: a bare "600111222" is never completed to some country."""

_WITHHELD = ('266696687', '7378742833', '8656696', '86282452253', '7748283')
"""Keypad spellings of the words a carrier puts where a withheld number was (anonymous, restricted, unknown,
unavailable, private). They pass a shape check because they are the right shape."""


def _length(value: str) -> str:
    """What is wrong with the length of a number that starts with "+", in words, or '' when nothing is.

    libphonenumber's own reasons, so every country is counted by its own
    rule: too long is a digit misheard or repeated, too short is a caller not
    yet done. "Not a number" from the parser is a "+" with too few digits
    after it yet ("+34"): also too short.
    """
    try:
        number = phonenumbers.parse(value, None)
    except phonenumbers.NumberParseException as error:
        return {
            phonenumbers.NumberParseException.INVALID_COUNTRY_CODE: 'unknown country code',
            phonenumbers.NumberParseException.TOO_LONG: 'too long',
        }.get(error.error_type, 'too short' if value[1:].isdigit() else '')
    return {
        phonenumbers.ValidationResult.TOO_LONG: 'too long',
        phonenumbers.ValidationResult.TOO_SHORT: 'too short',
        phonenumbers.ValidationResult.INVALID_COUNTRY_CODE: 'unknown country code',
    }.get(phonenumbers.is_possible_number_with_reason(number), '')


def _digits_after_code(value: str) -> str:
    """How many digits follow the country code, in words ("10 digits after +34"), or all the digits when none parses."""
    try:
        number = phonenumbers.parse(value, None)
    except phonenumbers.NumberParseException:
        return f'{sum(c.isdigit() for c in value)} digits'
    return f'{len(phonenumbers.national_significant_number(number))} digits after +{number.country_code}'


def _heard(value: str) -> str:
    """The digits as heard, the country code apart and the rest in threes: "+34 671 273 454 4"."""
    digits = ''.join(c for c in value if c.isdigit())
    try:
        code = str(phonenumbers.parse(value, None).country_code)
    except phonenumbers.NumberParseException:
        code = ''
    code = code if code and digits.startswith(code) else ''
    rest = digits[len(code) :]
    return '+' + ' '.join(filter(None, [code, *(rest[i : i + 3] for i in range(0, len(rest), 3))]))


def grouped(value: str) -> str:
    """A number too long for its country, grouped the way its read-back is: "+34 680 84 06 46 4".

    The longest start of it that is a real number is grouped by
    libphonenumber, like `Phone.spoken`, and the digits left over follow, so
    what the caller hears about a wrong number and the read-back of a right
    one group the same digits the same way.
    """
    try:
        number = phonenumbers.parse(value, None)
    except phonenumbers.NumberParseException:
        return _heard(value)
    national = phonenumbers.national_significant_number(number)
    for cut in range(len(national) - 1, 0, -1):
        try:
            start = phonenumbers.parse(f'+{number.country_code}{national[:cut]}', None)
        except phonenumbers.NumberParseException:
            break
        if phonenumbers.is_valid_number(start):
            said = phonenumbers.format_number(start, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
            return f'{said} {national[cut:]}'
    return _heard(value)
