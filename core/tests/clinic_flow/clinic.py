"""A clinic's booking line, written on core's public boundary alone: models and plain functions, nothing else.

What the application writes: its state (`ClinicState`, plain data), the
arguments and receipts of its two business actions, its service
(`Backend`), and typed domain functions: `book` and `cancel` call the
service, `free_times` offers two appointment ids, and `booking_words` and
`cancel_words` word the read-backs. No call class, no operation class, no
question ledger, no speech filter. The manifests (`agent.yaml` and its two
variants) connect them, and core keeps the session.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from core import OutcomeUnknownError

TIMES = {'0900': 'Tuesday at 9:00 AM', '1000': 'Tuesday at 10:00 AM'}
"""The clinic's appointment times, by the id a caller picks."""

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
    slot: Slot | None = Field(
        None, description='The id of the time the caller chose, from the times offered.'
    )
    contact: Contact | None = None
    notes: list[str] = []
    reference: str = ''
    """The booking reference. Only a receipt sets it: no step collects it, so no model can."""


class Appointment(BaseModel):
    patient: PatientId
    slot: Slot


class Booked(BaseModel):
    reference: str
    patient: PatientId
    slot: Slot
    when: str
    status: Literal['booked', 'full']


class Cancellation(BaseModel):
    reference: Annotated[str, StringConstraints(min_length=1)]


class Cancelled(BaseModel):
    reference: str
    status: Literal['cancelled', 'unknown']


class Backend:
    """The clinic's diary, one per call (`services:` in the manifest). `calls` counts every request it gets.

    `failing` makes the next booking's outcome unknown after it lands, as a
    timeout would; `full` makes the diary answer that the time is taken.
    """

    def __init__(self) -> None:
        self.bookings: dict[str, tuple[str, str]] = {}
        self.calls = 0
        self.failing = False
        self.full = False

    def book(self, patient: str, slot: str) -> dict[str, str]:
        self.calls += 1
        if self.full:
            return {'reference': '', 'patient': patient, 'slot': slot, 'when': TIMES[slot], 'status': 'full'}
        reference = f'CL{len(self.bookings) + 1:03d}'
        self.bookings[reference] = (patient, slot)
        if self.failing:
            self.failing = False
            raise TimeoutError('the diary did not answer')
        return {
            'reference': reference,
            'patient': patient,
            'slot': slot,
            'when': TIMES[slot],
            'status': 'booked',
        }

    def cancel(self, reference: str) -> dict[str, str]:
        self.calls += 1
        found = self.bookings.pop(reference, None)
        return {'reference': reference, 'status': 'cancelled' if found else 'unknown'}


def book(clinic: Backend, appointment: Appointment) -> Booked:
    """Book the appointment. A diary that stops answering has an unknown outcome, never a failure."""
    try:
        return Booked.model_validate(clinic.book(appointment.patient, appointment.slot))
    except TimeoutError as error:
        raise OutcomeUnknownError(str(error)) from error


def find_booking(clinic: Backend, appointment: Appointment) -> Booked | None:
    """What the diary holds for this patient and time: how an unknown booking is settled before any retry."""
    for reference, held in clinic.bookings.items():
        if held == (appointment.patient, appointment.slot):
            return Booked.model_validate(
                {
                    'reference': reference,
                    'patient': held[0],
                    'slot': held[1],
                    'when': TIMES[held[1]],
                    'status': 'booked',
                }
            )
    return None


def cancel(clinic: Backend, cancellation: Cancellation) -> Cancelled:
    return Cancelled.model_validate(clinic.cancel(cancellation.reference))


class PatientRef(BaseModel):
    patient: PatientId


class Found(BaseModel):
    patient: PatientId
    reference: str
    status: Literal['found', 'none']


def look_up(clinic: Backend, ref: PatientRef) -> Found:
    """The patient's booking, if the diary has one: a read, so it needs no read-back (`code_node.yaml`)."""
    if clinic.failing:
        clinic.failing = False
        raise OutcomeUnknownError('the diary did not answer')
    held = next((code for code, (patient, _) in clinic.bookings.items() if patient == ref.patient), '')
    return Found(patient=ref.patient, reference=held, status='found' if held else 'none')


def free_times(clinic: Backend, state: ClinicState) -> dict[str, str]:
    """The two times offered, by id, as they are said. A taken time is not offered."""
    taken = {slot for _, slot in clinic.bookings.values()}
    return {slot: said for slot, said in TIMES.items() if slot not in taken}


def booking_words(appointment: Appointment) -> str:
    return f'Shall I book {TIMES[appointment.slot]} for {appointment.patient}?'


def cancel_words(cancellation: Cancellation) -> str:
    return f'Shall I cancel booking {cancellation.reference}?'
