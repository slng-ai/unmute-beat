"""The salon's five business actions, one class each: its arguments, what it does, and how it is read back.

Each class is a `core.Lookup` or `core.Write`: its fields are its arguments,
bound by name from the state (`SalonState`), or from inside the lookup's
receipt (`AliasPath('customer', 'customer_phone')`): a booking is only ever
for the number the lookup verified. The return type is the outcome: `Saved`
is a success, `Declined` (a `core.Rejection`) an authoritative no. Core runs
each write only on the caller's yes to its read-back, and settles one whose
answer was lost by its `reconcile` before anything is sent again.

What is salon, and stays here: how a slot, a move and a cancellation are
read back in the diary's own words, what a complaint's next step is under
the refunds policy, and how a write whose answer was lost is found again. A
write's diary token is the id of the `Intent` core issued for it.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from pydantic import AliasPath, Field, StringConstraints, ValidationError

import core
from salon.diary import AlreadyHoldsError, Diary, RefusedError
from salon.receipts import Customer, Declined, Held, Noted, NotNoted, Saved, Written


@dataclass
class Salon:
    """One call's services: the salon's diary, and the key the diary files this call's offers under."""

    diary: Diary
    call: str
    settle: Callable[[str], bool] | None = None
    """Which earlier calls are over, by call key: only their intents are owed to a new call (`owed`)."""

    def now(self) -> datetime:
        """The diary's clock: core reads the call's today from it once, when the call opens."""
        return self.diary.now()

    def owed(self, identity: str) -> list[core.Intent]:
        """This caller's intents that calls now over left with no answer: core settles them before anything new."""
        if self.settle is None:
            return []
        return [
            core.Intent(
                id=i['id'],
                operation=i['operation'],
                args=i['args'],
                subject=i['subject'],
                status='dispatched',
            )
            for i in self.diary.unresolved(identity)
            if i['session'] != self.call and self.settle(i['session'])
        ]

    async def note_intent(self, intent: core.Intent) -> None:
        """Core's durable record of an intent, before it is dispatched: what a new call settles if this one dies."""
        await asyncio.to_thread(
            self.diary.note_intent, intent.id, self.call, intent.operation, intent.args, intent.subject
        )

    def current_version(self, kind: str, resource: str) -> int | None:
        """A resource's version now, for a receipt core learnt by reconciliation. A complaint never changes."""
        if kind == 'booking':
            return self.diary.current_revision(resource)
        return 1 if kind == 'complaint' else None


Picked = Annotated[str, StringConstraints(min_length=1)]
"""An id the caller picked from an offer, or words they said. Empty is nothing yet: the action is not ready."""

Outcome = Annotated[Saved | Declined, Field(discriminator='status')]
Noting = Annotated[Noted | NotNoted, Field(discriminator='status')]


def spoken_service(service: str) -> str:
    """The service as the voice should say it."""
    return service.replace('hair-color', 'hair colour')


class LookupCustomer(core.Lookup[Salon]):
    """Find the caller in the salon's books, or add them. Run only on their yes to the number read back."""

    customer_phone: core.Phone
    say = 'Thanks, I have you.'
    matches = ('customer_phone',)
    restated_confirms = True
    """The user's rule (session 4c, finding 12): the same number said back is a yes to its read-back."""

    def run(self, salon: Salon, intent: core.Intent) -> Customer:
        return Customer.model_validate(salon.diary.find_or_create_customer(self.customer_phone))

    def readback(self, salon: Salon) -> str:
        return f'Is your number {self.customer_phone.spoken}?'


class _BookingWrite(core.Write[Salon]):
    """What booking, moving and cancelling share: the verified number, and one write that is found again if lost."""

    phone: core.Phone = Field(validation_alias=AliasPath('customer', 'customer_phone'))
    idempotent = True

    def _held(self, salon: Salon) -> list[Held]:
        return [Held.model_validate(h) for h in salon.diary.bookings_for(self.phone, salon.now().date())]

    def _expected(self, salon: Salon, status: str, slot: str = '', booking_id: str = '') -> dict[str, str]:
        """What a receipt for this request has to say: its status, the offered slot's day, time and service, the booking."""
        offered = salon.diary.offer_of(salon.call, slot, self.phone) if slot else {}
        return {'status': status, **{k: offered[k] for k in ('date', 'time', 'service') if k in offered}} | (
            {'booking_id': booking_id} if booking_id else {}
        )

    def _saved(self, raw: dict, expected: dict[str, str]) -> Saved:
        """The diary's answer as this request's receipt, or unknown when it is about something else.

        Pydantic says every field holds a legal value; it cannot say the
        answer is about the thing asked for. A `cancelled` to a booking, or a
        booking at another time, is in legal values and still not this receipt.
        """
        try:
            written = Written.model_validate(raw)
        except ValidationError as error:
            raise core.OutcomeUnknownError('the diary answered in a shape this cannot read') from error
        got = {k: str(raw.get(k, '')) for k in expected}
        if got != expected:
            raise core.OutcomeUnknownError(f'the diary answered {got}, not {expected}')
        return Saved(**written.model_dump(), phone=self.phone, service_spoken=spoken_service(written.service))

    def _write(
        self, salon: Salon, intent: core.Intent, attempt: Callable[[], dict], expected: dict[str, str]
    ):
        """One write, once: a refusal is an answer; a lost answer is asked about by its token before anything concludes."""
        asked = expected.get('booking_id', '')
        try:
            raw = attempt()
        except (AlreadyHoldsError, RefusedError) as refused:
            return Declined(status='refused', phone=self.phone, booking_id=asked, reason=str(refused))
        except Exception as error:
            found = salon.diary.outcome_of(intent.id)
            if found is None:
                raise core.OutcomeUnknownError(f'the diary did not answer: {error}') from error
            raw = found
        saved = self._saved(raw, expected)
        salon.diary.withdraw_offers(salon.call)
        return saved

    def _found(self, salon: Salon, intent: core.Intent, expected: dict[str, str]):
        """What an unanswered write did, by its token; fenced (`void`) when the diary never saw it."""
        raw = salon.diary.outcome_or_void(intent.id)
        if raw.get('status') == 'void':
            return Declined(status='void', phone=self.phone, booking_id=expected.get('booking_id', ''))
        saved = self._saved(raw, expected)
        salon.diary.withdraw_offers(salon.call)
        return saved

    def _slot_words(self, salon: Salon, slot: str) -> tuple[str, str]:
        offered = salon.diary.offer_of(salon.call, slot, self.phone)
        return spoken_service(offered.get('service', 'appointment')), offered.get('spoken', 'that time')


class BookAppointment(_BookingWrite):
    """Book the offered slot. A caller who already holds a booking was read back a second appointment."""

    slot: Picked
    matches = ('phone',)
    say = "You're all set for {spoken}. Anything else I can help with?"
    recap = "You're booked for {spoken}."

    def run(self, salon: Salon, intent: core.Intent) -> Outcome:
        additional = bool(self._held(salon))
        return self._write(
            salon,
            intent,
            lambda: salon.diary.save_booking(
                self.phone,
                'book',
                self.slot,
                '',
                additional,
                salon.now(),
                operation_token=intent.id,
                offered_to=salon.call,
            ),
            self._expected(salon, 'booked', self.slot),
        )

    def reconcile(self, salon: Salon, intent: core.Intent) -> Outcome:
        return self._found(salon, intent, self._expected(salon, 'booked', self.slot))

    def readback(self, salon: Salon) -> str:
        """Asking from its first word, and to a caller who holds one, as a second appointment.

        "So that is a hair colour, tomorrow at 9 AM" sounds like a
        confirmation: in Coval run RPH7TzmF (K5) the caller took it as one and
        hung up with nothing booked.
        """
        service, when = self._slot_words(salon, self.slot)
        held = self._held(salon)
        if held:
            also = (
                f'your {spoken_service(held[0].service)} {held[0].spoken}'
                if len(held) == 1
                else 'the appointments you already have'
            )
            return f'Shall I book you a second appointment, a {service} {when}, as well as {also}?'
        return f'Shall I book a {service}, {when}?'


class MoveAppointment(_BookingWrite):
    """Move the caller's own booking to the offered slot."""

    slot: Picked
    booking_id: Picked
    matches = ('phone', 'booking_id')
    say = "That's moved to {spoken}. Anything else I can help with?"
    recap = 'Your appointment is now {spoken}.'

    def run(self, salon: Salon, intent: core.Intent) -> Outcome:
        return self._write(
            salon,
            intent,
            lambda: salon.diary.save_booking(
                self.phone,
                'move',
                self.slot,
                self.booking_id,
                False,
                salon.now(),
                operation_token=intent.id,
                offered_to=salon.call,
                expected_revision=salon.diary.shown_revision(salon.call, self.booking_id),
            ),
            self._expected(salon, 'moved', self.slot, self.booking_id),
        )

    def reconcile(self, salon: Salon, intent: core.Intent) -> Outcome:
        return self._found(salon, intent, self._expected(salon, 'moved', self.slot, self.booking_id))

    def readback(self, salon: Salon) -> str:
        service, when = self._slot_words(salon, self.slot)
        held = next((h for h in self._held(salon) if h.booking_id == self.booking_id), None)
        return f'Shall I move your {service}{f" from {held.spoken}" if held else ""} to {when}?'


class CancelAppointment(_BookingWrite):
    """Cancel the caller's own booking: a real write with its own receipt, never an in-memory undo."""

    booking_id: Picked
    matches = ('phone', 'booking_id')
    say = "That's cancelled for you. Anything else I can help with?"
    recap = 'Your appointment is cancelled.'

    def run(self, salon: Salon, intent: core.Intent) -> Outcome:
        return self._write(
            salon,
            intent,
            lambda: salon.diary.save_booking(
                self.phone,
                'cancel',
                '',
                self.booking_id,
                False,
                salon.now(),
                operation_token=intent.id,
                offered_to=salon.call,
                expected_revision=salon.diary.shown_revision(salon.call, self.booking_id),
            ),
            self._expected(salon, 'cancelled', booking_id=self.booking_id),
        )

    def reconcile(self, salon: Salon, intent: core.Intent) -> Outcome:
        return self._found(salon, intent, self._expected(salon, 'cancelled', booking_id=self.booking_id))

    def readback(self, salon: Salon) -> str:
        held = next((h for h in self._held(salon) if h.booking_id == self.booking_id), None)
        return f'Shall I cancel {f"your {spoken_service(held.service)} {held.spoken}" if held else "that booking"}?'


NOTED = "That's on record, reference {reference}. {next_step}"
REPLY_SOON = 'The salon will get back to you within two working days.'
MANAGER_CALLS = 'That one goes to the manager, who will call you back.'


def acknowledgement(reference: str, requested_resolution: str, wants_refund: bool) -> str:
    """The one thing said when a complaint is written down: its reference, then the policy's next step.

    Money back goes to the manager, straight out of the refunds policy. The
    step's agent records whether the caller asks for it (`wants_refund`), so
    no word list reads their words here.
    """
    step = MANAGER_CALLS if wants_refund else REPLY_SOON
    pending = ' Your requested resolution is pending approval.' if requested_resolution else ''
    return NOTED.format(reference=reference, next_step=step) + pending + ' Anything else I can help with?'


class RecordComplaint(core.Write[Salon]):
    """Write the complaint down, in the caller's words, once. Twice with one intent writes it once."""

    phone: core.Phone = Field(validation_alias=AliasPath('customer', 'customer_phone'))
    summary: Picked = Field(validation_alias='complaint')
    requested_resolution: str | None = Field(None, validation_alias='wanted_outcome')
    wants_refund: bool | None = None
    matches = ('phone',)
    idempotent = True
    say = '{said}'
    recap = 'Your complaint is on record, reference {reference}.'

    def _noted(self, raw: dict) -> Noted | NotNoted:
        if not raw.get('recorded'):
            return NotNoted(status='void', phone=self.phone)
        reference = str(raw.get('reference', ''))
        said = acknowledgement(reference, self.requested_resolution or '', bool(self.wants_refund))
        return Noted(status='recorded', reference=reference, phone=self.phone, said=said)

    def run(self, salon: Salon, intent: core.Intent) -> Noting:
        try:
            raw = salon.diary.record_complaint(
                self.phone, self.summary, self.requested_resolution or '', operation_token=intent.id
            )
        except Exception as error:
            found = salon.diary.outcome_of(intent.id)
            if found is None:
                raise core.OutcomeUnknownError(f'the complaint book did not answer: {error}') from error
            raw = found
        return self._noted(raw)

    def reconcile(self, salon: Salon, intent: core.Intent) -> Noting:
        return self._noted(salon.diary.outcome_or_void(intent.id))

    def readback(self, salon: Salon) -> str:
        request = (
            f'Your request: {self.requested_resolution.strip().rstrip(".?!")}. '
            if self.requested_resolution
            else ''
        )
        return (
            f'Shall I record your complaint in your words: {self.summary.strip().rstrip(".?!")}. {request}'
            'The salon will review it; no refund or booking change is approved yet. Is that right?'
        )
