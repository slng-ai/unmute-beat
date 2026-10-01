"""What a Kestrel claim call knows: one plain Pydantic model. Everything else is core's.

`policy` is the policy lookup's receipt, set whole and only by it. The
incident's facts are typed: a day that is not in the future, a time or a
part of the day, the caller's own words for where and what happened, and
plain yes or no answers. `vehicle` is a plate from the cars on the policy, so
the model cannot invent one.
"""

from datetime import date, time
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, ValidationInfo
from pydantic_core import PydanticCustomError

import core
from kestrel.receipts import PolicyFound


def _not_future(value: date, info: ValidationInfo) -> date:
    """A day on or before the call's today. Without a call there is no today: nothing refused."""
    today = (info.context or {}).get('today')
    if isinstance(today, date) and value > today:
        raise PydanticCustomError(
            'date_future',
            '{day} has not happened yet: today is {today}. Ask on which day it happened.',
            {'day': value.isoformat(), 'today': today.isoformat()},
        )
    return value


PolicyNumber = Annotated[str, StringConstraints(pattern=r'^KP\d{6}$')]
WhenInDay = time | Literal['morning', 'afternoon', 'evening', 'night']


class ClaimState(BaseModel):
    """The facts of one claim call. Every field has a default: a call starts empty."""

    model_config = ConfigDict(extra='forbid')

    policy_number: PolicyNumber | None = Field(
        None, description='The policy number: KP and six digits, like KP482913. Never guess a digit.'
    )
    birth_date: date | None = Field(
        None, description="The policy holder's date of birth, as YYYY-MM-DD. Never guess a part."
    )
    policy: PolicyFound | None = None
    """Set only by the policy lookup. The incident step requires it."""

    incident_day: Annotated[date, AfterValidator(_not_future)] | None = Field(
        None, description='The day it happened, as YYYY-MM-DD, worked out from the calendar you are shown.'
    )
    incident_time: WhenInDay | None = Field(
        None,
        description='When it happened: HH:MM on a 24-hour clock, or morning, afternoon, evening or night.',
    )
    place: core.Quoted | None = Field(
        None, description='Where it happened, in their own words: a road, a junction, a car park, a town.'
    )
    what_happened: core.Quoted | None = Field(
        None, description='What happened, in their own words: the shortest complete excerpt.'
    )
    injuries: bool | None = Field(None, description='Whether anyone was hurt.')
    police_report: str | None = Field(
        None, description='The police report reference they gave, or "none" when there is no report.'
    )
    other_party: bool | None = Field(None, description='Whether another vehicle or driver was involved.')
    vehicle_drivable: bool | None = Field(None, description='Whether their car can still be driven.')
    vehicle: str | None = Field(
        None, description='The plate of the car it happened to, from the cars on offer.'
    )
