"""The clinic with structured steps (`structured.yaml`): a visit's details, a price read and times on offer.

For `tests.test_structured`. The state has a nullable choice (`slot`), a
string, a bool, a bounded int and a nested model, plus one whole-state rule,
so every kind of update value is exercised. `Clinic` counts each kind of
backend read on its own: an availability lookup is not a price lookup.
"""

from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

import core
from tests.clinic_flow.clinic import TIMES, Backend, PatientId, Slot
from tests.clinic_flow.consult import CheckedIn

type Service = Literal['cleaning', 'checkup']


class Companion(BaseModel):
    """Who comes with the patient. Replaced whole by an update, never merged."""

    model_config = ConfigDict(extra='forbid')

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    relation: Literal['partner', 'parent', 'friend']


class Price(core.Receipt):
    status: Literal['priced']
    service: Service
    euros: int


class VisitState(BaseModel):
    model_config = ConfigDict(extra='forbid')

    patient: PatientId | None = Field(None, description='The patient number, like P-1234.')
    checked_in: CheckedIn | None = None
    service: Service | None = Field(None, description='What the visit is for.')
    slot: Slot | None = Field(
        None, description='The id of the time the caller chose, from the times offered.'
    )
    note: str = Field('', description='Anything else the caller mentioned.', examples=['uses a wheelchair'])
    wheelchair: bool = Field(False, description='Whether they come in a wheelchair.')
    visitors: int = Field(1, ge=0, le=3, description='How many people come with them.')
    companion: Companion | None = Field(None, description='Who comes with them.')
    reference: str = ''
    price: Price | None = None
    """Set whole by `quote`'s receipt: no step records it, and it rests on the service it was quoted for."""

    @model_validator(mode='after')
    def room(self) -> VisitState:
        """A rule over the whole state: the room fits two visitors beside a wheelchair."""
        if self.wheelchair and self.visitors > 2:
            raise ValueError('the room fits two visitors beside a wheelchair')
        return self


class Clinic(Backend):
    """The clinic's diary and price list. `availability` and `quotes` count each read apart from the bookings."""

    def __init__(self) -> None:
        super().__init__()
        self.availability: list[str | None] = []
        self.quotes: list[str] = []
        self.closed = False
        self.during_quote: Callable[[], None] | None = None


def free_times(clinic: Clinic, state: VisitState) -> dict[str, str]:
    """The times free for the service asked about, by id. A checkup is only at ten; nothing is free when closed."""
    clinic.availability.append(state.service)
    if clinic.closed:
        return {}
    taken = {slot for _, slot in clinic.bookings.values()}
    wanted = ['1000'] if state.service == 'checkup' else list(TIMES)
    return {slot: TIMES[slot] for slot in wanted if slot not in taken}


class Unpriced(core.Rejection):
    status: Literal['unpriced']
    service: Service


class Quote(core.Lookup[Clinic]):
    """What a visit costs."""

    service: Service

    def run(
        self, clinic: Clinic, intent: core.Intent
    ) -> Annotated[Price | Unpriced, Field(discriminator='status')]:
        clinic.quotes.append(self.service)
        if clinic.during_quote is not None:
            clinic.during_quote()
        if clinic.failing:
            clinic.failing = False
            raise TimeoutError('the price list did not answer')
        if self.service == 'checkup':
            return Unpriced(status='unpriced', service='checkup', reason='a checkup is priced at the visit')
        return Price(status='priced', service='cleaning', euros=40)
