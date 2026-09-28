"""The salon's identity path on the public manifest: its real phone types, receipt and diary, and no salon runtime.

The salon's own `verify` step is hand-built (`salon/steps/verify.py`, a
`SalonCall`, the `SALON` conversation). This slice asks the same question
through `agent.yaml` alone: a collect step for the number, a read-back in
the salon's own words, and the lookup as a typed function. It exists to
catch an abstraction that fits the clinic and not the salon before session 4
migrates everything.
"""

from pydantic import BaseModel, ConfigDict

from salon.diary import Diary
from salon.models.receipts import CustomerLookup
from salon.models.shapes import Phone, Spoken, SubmittedPhone, Verification


class Caller(BaseModel):
    """Who is calling, as far as this call knows. The number is collected; the status only a receipt sets."""

    model_config = ConfigDict(extra='forbid')

    phone: Phone = ''
    verified: Verification = ''


class Number(BaseModel):
    """The one argument a lookup takes: a number the caller agreed to, never an empty one."""

    phone: SubmittedPhone


def look_up(diary: Diary, number: Number) -> CustomerLookup:
    return CustomerLookup.model_validate(diary.find_or_create_customer(number.phone))


def read_back(number: Number) -> str:
    return f'Let me read that back to you: {Spoken.validate_python(number.phone)}. Have I got that right?'
