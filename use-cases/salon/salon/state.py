"""What a call to Sage and Stone knows: one plain Pydantic model. Everything else is core's.

The caller's number is `core.Phone`, validated by libphonenumber, and the
carrier's caller ID fills it (`StepFlow.open(caller_id=...)`). `customer` is
the lookup's receipt, set whole and only by it; core clears it when the
number changes, because a corrected number is somebody else until it is
looked up. A date is a `date`, picked by the model from the calendar core
shows every step (`current_datetime:`), and never in the past.
"""

from datetime import date, time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

import core
from salon.receipts import Customer, Service

TimeWanted = time | Literal['morning', 'afternoon', 'evening']


class SalonState(BaseModel):
    """The business facts of one salon call. Every field has a default: a call starts empty."""

    model_config = ConfigDict(extra='forbid')

    customer_phone: core.Phone | None = None
    customer: Customer | None = None
    """Set only by the lookup's receipt, for the number it looked up. Booking and complaints require it."""

    service: Service | None = Field(
        None, description='The service the caller wants: haircut, hair-color or blowout.'
    )
    booking_day: Annotated[date, core.NotPast] | None = Field(
        None,
        description='The date the caller asked for, as YYYY-MM-DD, worked out from the calendar you are shown.',
    )
    booking_time: TimeWanted | None = Field(
        None,
        description='The time the caller asked for: HH:MM on a 24-hour clock (14:00 for 2 PM), or morning, afternoon or evening.',
    )
    slot: str | None = Field(
        None, description='The id of the time the caller picked, from the times on offer.'
    )
    booking_id: str | None = Field(
        None,
        description='The id of the booking they hold that they want moved or cancelled, from their bookings on offer.',
    )

    complaint: core.Quoted | None = Field(
        None,
        description="What went wrong, in the caller's own words: the shortest complete excerpt of what they said.",
    )
    wanted_outcome: core.Quoted | None = Field(
        None, description='What the caller wants the salon to do about it, in their own words.'
    )
    wants_refund: bool | None = Field(
        None,
        description='Whether what they want includes money back: a refund, compensation or reimbursement.',
    )
