"""What the salon's diary is allowed to have said: plain Pydantic models on `core.Receipt`.

`core.Receipt` is strict (defaults, assignments and nested models
validated, unknown fields refused), so a diary answer in a shape nobody
declared fails where it arrives. The field names are the ones the diary and
`concierge-tools/load_report.py` already use.
"""

from datetime import date, datetime, time
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field

import core

Service = Literal['haircut', 'hair-color', 'blowout']


def _day(value: object) -> object:
    """A date or its ISO text; a number is refused: lax Pydantic would take 1700000000 as a day in 2023."""
    if isinstance(value, int | float | datetime):
        raise ValueError('expected a day written year-month-day, like 2026-03-19')
    return value


def _on_the_grid(value: object) -> object:
    """A salon wall-clock time: no timezone, no seconds. Anything else is the diary disagreeing with itself."""
    at = time.fromisoformat(value) if isinstance(value, str) else value
    if isinstance(at, time) and (at.tzinfo is not None or at.second or at.microsecond):
        raise ValueError('expected a local salon time on the half hour, like 09:30')
    return at


Day = Annotated[date, BeforeValidator(_day)]
SlotTime = Annotated[time, BeforeValidator(_on_the_grid)]


class Customer(core.Receipt):
    """The customer service's answer about a number: who the caller is for the rest of the call."""

    customer_status: Literal['existing', 'created']
    customer_phone: core.Phone


class Held(core.Receipt):
    """One booking the caller holds, as the diary reports it."""

    booking_id: str
    service: Service
    date: Day
    time: SlotTime
    spoken: str
    revision: int = 1


class Slot(core.Receipt):
    """One free time the diary is offering."""

    slot_id: str
    service: Service
    time: SlotTime
    spoken: str


class Slots(core.Receipt):
    """The diary's answer to a read: what is free on the day asked for, and what the caller already holds."""

    status: Literal['ok', 'full', 'past', 'closed']
    bookings: list[Held] = Field(default_factory=list)
    slots: list[Slot] = Field(default_factory=list)
    day: str = ''
    date: Day | None = None
    note: str = ''
    price_euros: int | None = None


class Written(core.Receipt):
    """The diary's own answer to a write that landed, before the salon adds who it was for."""

    status: Literal['booked', 'moved', 'cancelled']
    booking_id: str
    service: Service
    date: Day
    time: SlotTime
    spoken: str
    revision: int


class Saved(core.Receipt):
    """A booking made, moved or cancelled: the booking is the resource, its revision orders two changes of it."""

    status: Literal['booked', 'moved', 'cancelled']
    booking_id: Annotated[str, core.Subject('booking')]
    phone: core.Phone
    service: Service
    date: Day
    time: SlotTime
    spoken: str
    service_spoken: str
    revision: Annotated[int, core.Version()]


class Declined(core.Rejection):
    """The diary's no to a booking write, or a write it fenced (`void`): nothing was done, and never will be.

    It names the booking the request was about, so the answer is bound to
    its request: a refusal about nothing would stay unknown.
    """

    status: Literal['refused', 'void']
    phone: core.Phone
    booking_id: str = ''


class Noted(core.Receipt):
    """The complaint book's receipt, and the one sentence the policy says comes next."""

    status: Literal['recorded']
    recorded: Literal[True] = True
    reference: Annotated[str, core.Subject('complaint')]
    phone: core.Phone
    said: str
    revision: Annotated[int, core.Version()] = 1
    """A complaint is written once and never changed, so its version is always 1."""


class NotNoted(core.Rejection):
    """A complaint write the book never saw, fenced so it can never land later."""

    status: Literal['void']
    recorded: Literal[False] = False
    phone: core.Phone
