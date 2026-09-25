"""A clinic's booking line, written on core's public boundary alone: `import core`, nothing deeper.

What the application writes: its state (`ClinicState`, plain data), its
service (`Backend`), the arguments and receipt of its one business action,
and that action's read-back and result (`Book`). No call class, no question
ledger, no history, no speech filter: the flow file (`flow.yaml`) connects
them, and core keeps the session.

ponytail: `Book` is still one `Operation` subclass per action. Session 3
replaces it with a typed function declared in the flow file.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

import core

TIMES = {'0900': 'Tuesday at 9:00 AM', '1000': 'Tuesday at 10:00 AM'}
"""The clinic's free times, by id."""

type Slot = Literal['0900', '1000']
type PatientId = Annotated[str, StringConstraints(pattern=r'^P-\d{4}$')]


class Contact(BaseModel):
    """How to reach the patient. No `extra` setting of its own: the state's commit refuses unknown keys anyway."""

    phone: Annotated[str, StringConstraints(min_length=6)]
    texts: bool = False


class ClinicState(BaseModel):
    """What the clinic knows about one call. Every field has a default: a call starts empty."""

    model_config = ConfigDict(extra='forbid')

    patient: PatientId | None = Field(None, description='The patient number, like P-1234.')
    slot: Slot | None = Field(None, description='The time the caller chose: 0900 or 1000.')
    contact: Contact | None = None
    notes: list[str] = []
    reference: str = ''
    """The booking reference. Only a receipt sets it: no collect step names it, so no model can."""


class Appointment(BaseModel):
    patient: PatientId
    slot: Slot


class Booked(BaseModel):
    reference: str
    patient: PatientId
    slot: Slot
    status: Literal['booked']


class Backend:
    """The clinic's diary, a service passed to each session. `calls` counts every request it gets."""

    def __init__(self) -> None:
        self.bookings: dict[str, tuple[str, str]] = {}
        self.calls = 0

    def book(self, patient: str, slot: str) -> dict[str, str]:
        self.calls += 1
        reference = f'CL{len(self.bookings) + 1:03d}'
        self.bookings[reference] = (patient, slot)
        return {'reference': reference, 'patient': patient, 'slot': slot, 'status': 'booked'}


class Book(core.ConfirmedOperation[Appointment, Booked]):
    """Book the chosen time for the patient, once they agreed to its read-back."""

    name = 'book'
    args_type = Appointment
    receipt_type = Booked

    def proposal(self, args: Appointment) -> core.BaseProposal:
        return core.BaseProposal(
            about='book',
            spoken=f'Shall I book {TIMES[args.slot]} for {args.patient}?',
            value=f'{args.patient}|{args.slot}',
        )

    async def _execute(self, call, args: Appointment, grant) -> dict[str, str]:
        return call.services.book(args.patient, args.slot)

    async def _record(self, call, args: Appointment, receipt: Booked) -> Booked:
        call.update(reference=receipt.reference)
        return receipt


OPERATION = Book
