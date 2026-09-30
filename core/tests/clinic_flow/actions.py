"""The clinic's two business actions written as classes (`classes.yaml`): the same backend and words as `clinic.py`.

What `agent.yaml` says in keys, these say in types: the arguments are
the class's fields, bound by name; `Booked | Full` is the outcome, `Full` a
`core.Rejection`; the reference is the `core.Subject` and `version` its
`core.Version`. `Remind` reads its number from inside the state's `contact`,
by `AliasPath`, to prove a nested source.
"""

from typing import Annotated, ClassVar, Literal

from pydantic import AliasPath, BaseModel, Field

import core
from tests.clinic_flow.clinic import TIMES, Backend, PatientId, Slot


class Booked(core.Receipt):
    reference: Annotated[str, core.Subject('booking')]
    patient: PatientId
    slot: Slot
    when: str
    status: Literal['booked']
    version: Annotated[int, core.Version()] = 0


class Full(core.Rejection):
    """The diary's no: that time is taken. Nothing was booked."""

    reference: str
    patient: PatientId
    slot: Slot
    when: str
    status: Literal['full']


class Cancelled(core.Receipt):
    reference: Annotated[str, core.Subject('booking')]
    status: Literal['cancelled']
    version: Annotated[int, core.Version()] = 0


class NotHeld(core.Rejection):
    reference: str
    status: Literal['unknown']
    version: int = 0


class Book(core.Write[Backend]):
    patient: PatientId
    slot: Slot
    say = 'Booked for {when}, reference {reference}.'
    matches = ('patient', 'slot')
    assign: ClassVar = {'reference': 'reference'}

    def run(
        self, clinic: Backend, intent: core.Intent
    ) -> Annotated[Booked | Full, Field(discriminator='status')]:
        """A diary that stops answering has an unknown outcome, never a failure."""
        try:
            answer = clinic.book(self.patient, self.slot)
        except TimeoutError as error:
            raise core.OutcomeUnknownError(str(error)) from error
        return Booked.model_validate(answer) if answer['status'] == 'booked' else Full.model_validate(answer)

    def readback(self, clinic: Backend) -> str:
        return f'Shall I book {TIMES[self.slot]} for {self.patient}?'

    def reconcile(self, clinic: Backend, intent: core.Intent) -> Booked | None:
        for reference, held in clinic.bookings.items():
            if held == (self.patient, self.slot):
                return Booked.model_validate(
                    {
                        'reference': reference,
                        'patient': held[0],
                        'slot': held[1],
                        'when': TIMES[held[1]],
                        'status': 'booked',
                        'version': 1,
                    }
                )
        return None


class Cancel(core.Write[Backend]):
    reference: str = Field(min_length=1)
    say = 'Booking {reference} is cancelled.'

    def run(
        self, clinic: Backend, intent: core.Intent
    ) -> Annotated[Cancelled | NotHeld, Field(discriminator='status')]:
        answer = clinic.cancel(self.reference)
        return (
            Cancelled.model_validate(answer)
            if answer['status'] == 'cancelled'
            else NotHeld.model_validate(answer)
        )

    def readback(self, clinic: Backend) -> str:
        return f'Shall I cancel booking {self.reference}?'


class Reminder(core.Receipt):
    phone: str
    sent: bool


class Remind(core.Lookup[Backend]):
    """Text the patient a reminder: its number comes from inside `contact`, never from a field named `phone`."""

    phone: str = Field(validation_alias=AliasPath('contact', 'phone'))

    def run(self, clinic: Backend, intent: core.Intent) -> Reminder:
        return Reminder(phone=self.phone, sent=True)


class CallerState(BaseModel):
    """A state whose identity is a phone number: what a carrier's caller ID fills (`tests.test_values`)."""

    phone: core.Phone | None = None
