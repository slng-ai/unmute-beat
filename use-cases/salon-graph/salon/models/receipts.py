"""What the salon's services are allowed to have said.

`Diary` stands in for an HTTP service and hands back plain JSON-shaped dicts,
so every one of them is validated into one of these before a single field is
read. A return annotation on the service function is not validation: Python
does not check it, and the one place a malformed response could reach the
state is the place that trusted the annotation.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from salon.models.shapes import Date, Id, Phone, Service, Time

STRICT = ConfigDict(validate_default=True, validate_assignment=True, revalidate_instances='always')
"""Defaults validated too, so an authored default that breaks its own type fails at import, not on a call."""


class Held(BaseModel):
    """One booking the caller holds, as the diary reports it."""

    model_config = STRICT
    booking_id: Id
    service: Service
    date: Date
    time: Time
    spoken: str


class Slot(BaseModel):
    """One free time the diary is offering."""

    model_config = STRICT
    slot_id: str
    service: Service
    time: Time
    spoken: str


class Slots(BaseModel):
    """The diary's answer to a read: what is free on the day named, and what the caller already holds."""

    model_config = STRICT
    status: Literal['ok', 'full', 'need_day', 'past', 'closed']
    bookings: list[Held] = []
    slots: list[Slot] = []
    day: str = ''
    date: Date | None = None
    note: str = ''
    price_euros: int | None = None


class Saved(BaseModel):
    """The diary's receipt for a write that landed."""

    model_config = STRICT
    status: Literal['booked', 'moved', 'cancelled']
    booking_id: Id
    service: Service
    date: Date
    time: Time
    spoken: str


class BookingCommand(BaseModel):
    """The one write a booking step may ask for, validated before the executor sees it."""

    model_config = STRICT
    action: Literal['book', 'move', 'cancel']
    slot_id: str
    booking_id: str


class CustomerLookup(BaseModel):
    """What the customer service says about a number.

    No '' status here: an empty status is what the state holds before a lookup
    has run, not something a lookup may come back with.
    """

    model_config = STRICT
    customer_status: Literal['existing', 'created']
    customer_phone: Phone


class ComplaintRecord(BaseModel):
    """The complaint book's receipt. `recorded` is read, not assumed: a well-formed refusal is still a refusal."""

    model_config = STRICT
    recorded: bool
    reference: Id
