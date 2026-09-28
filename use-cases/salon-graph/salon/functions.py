"""The salon's typed domain functions: what `agent.yaml` connects to the conversation.

Each one takes the call's services (`Salon`: the diary, this call's key and
its write counter) and a Pydantic argument model, and returns a Pydantic
receipt. Core validates the arguments before the call and the receipt after
it, binds every argument from the state rather than from a model's words,
and runs a write only on the caller's yes to its read-back. Nothing here
knows about calls, steps, consent or speech.

What is salon, and stays here: which day "Saturday" is next to the date the
caller is discussing (`find_times`), how a slot, a move and a cancellation
are read back in the diary's own words, what a complaint's next step is
under the refunds policy, and how a write whose answer was lost is found
again (`find_booking` and the others). A write's diary token is the id of the
`Intent` core issued for it: the same intent sent twice writes once, and a
new intent with the same arguments is a new write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, StringConstraints, ValidationError

from core import Intent, OutcomeUnknownError
from salon.diary import WEEKDAYS, AlreadyHoldsError, Diary, RefusedError, day_phrases, resolve_day, spoken_day
from salon.models.receipts import CustomerLookup, Held, Saved, Slots
from salon.models.shapes import Spoken, SubmittedPhone
from salon.models.state import SalonState


@dataclass
class Salon:
    """One call's services: the salon's diary, and the key the diary files this call's offers under.

    No write counter: each write is named by its `Intent`, so booking a slot,
    cancelling it and booking the same slot again are three intents and three
    writes, while a retry of one write whose answer was lost is the same
    intent and asks about the same token, in this process or a fresh one.
    """

    diary: Diary
    call: str

    def now(self) -> datetime:
        return self.diary.now()


class PhoneArgs(BaseModel):
    phone: SubmittedPhone


Picked = Annotated[str, StringConstraints(min_length=1)]
"""An id the caller picked from the offer. Empty is no pick: the operation is not ready, and says so."""


class Booking(BaseModel):
    phone: SubmittedPhone
    slot: Picked


class Move(BaseModel):
    phone: SubmittedPhone
    slot: Picked
    booking_id: Picked


class Cancel(BaseModel):
    phone: SubmittedPhone
    booking_id: Picked


class Written(BaseModel):
    """What a booking, move or cancellation did, as the diary reported it; `refused` carries the diary's reason."""

    status: Literal['booked', 'moved', 'cancelled', 'refused']
    phone: str
    booking_id: str = ''
    service: str = ''
    spoken: str = ''
    service_spoken: str = ''
    reason: str = ''


class ComplaintArgs(BaseModel):
    phone: SubmittedPhone
    summary: Picked
    requested_resolution: str = ''


class Noted(BaseModel):
    """The complaint book's receipt, and the one sentence the policy says comes next."""

    recorded: bool
    reference: str
    phone: str
    said: str


class Times(BaseModel):
    """The times on offer by slot id, and what the agent should know that an id cannot say."""

    options: dict[str, str] = {}
    note: str = ''


def spoken_service(service: str) -> str:
    """The service as the voice should say it."""
    return service.replace('hair-color', 'hair colour')


def lookup_customer(salon: Salon, args: PhoneArgs) -> CustomerLookup:
    """Find the caller in the salon's books, or add them. Run only on their yes to the number read back."""
    return CustomerLookup.model_validate(salon.diary.find_or_create_customer(args.phone))


def number_words(args: PhoneArgs) -> str:
    return f'Is your number {Spoken.validate_python(args.phone)}?'


OFFER_AT_MOST = 6
"""How many times the agent is shown, nearest the caller's time first. It offers up to three of them."""


def _bare_weekday(words: str) -> int | None:
    """The weekday these words name on their own ("Saturday", "on Saturday instead"), or None."""
    phrases = day_phrases(words)
    index = {name.casefold(): number for number, name in enumerate(WEEKDAYS)}
    return index.get(phrases[0]) if len(phrases) == 1 else None


def _discussed(salon: Salon, phone: str, state: SalonState, today: date) -> date | None:
    """The day already on the table: the times last offered to this call, else the booking being moved."""
    offered = salon.diary.offered_to(salon.call, phone)
    if offered:
        return date.fromisoformat(offered[0]['date'])
    for held in salon.diary.bookings_for(phone, today):
        if held['booking_id'] == state.appointment:
            return date.fromisoformat(held['date'])
    return None


def _ambiguous(words: str, today: date, discussed: date | None) -> tuple[date, date] | None:
    """Two dates a bare weekday could mean beside the date under discussion, or None when it means one.

    "Saturday instead", said about "Friday next week", is the Saturday after
    that Friday to one caller and the coming Saturday to another (session 3:
    three live calls, two different dates). Read from today it is the coming
    one; read beside the date under discussion it is the one in that week.
    When those differ, neither is chosen: the caller is asked, with both dates.
    """
    weekday = _bare_weekday(words)
    if weekday is None or discussed is None:
        return None
    coming = resolve_day(words, today)
    beside = discussed - timedelta(days=discussed.weekday()) + timedelta(days=weekday)
    if coming is None or beside < today or beside == coming:
        return None
    return coming, beside


def _nearest(slots: list[dict], wanted: str) -> list[dict]:
    """Slots nearest an exact time first; a daypart or no time keeps the diary's own order."""
    if not re.fullmatch(r'\d{2}:\d{2}', wanted):
        return slots
    minutes = int(wanted[:2]) * 60 + int(wanted[3:])
    return sorted(
        slots, key=lambda s: (abs(int(s['time'][:2]) * 60 + int(s['time'][3:5]) - minutes), s['time'])
    )


def find_times(salon: Salon, state: SalonState) -> Times:
    """The times the salon can offer for the request in the state: service, day, and time if one was asked for.

    The day is the caller's own words, resolved by the diary's written policy
    (`resolve_day`); the time orders the answer, nearest first, and never
    comes from anything but the state (in session 3 it fell back to the
    caller's latest words, which after verification were "yes, that's my
    number"). A day the policy cannot place, a closed day, a past day or a
    full day comes back as a note with nothing offered.
    """
    phone = state.verified_phone or state.customer_phone
    now = salon.now()
    today = now.date()
    if not state.booking_day:
        return Times(note='No day yet: ask which day they would like.')
    if (two := _ambiguous(state.booking_day, today, _discussed(salon, phone, state, today))) is not None:
        first, second = (spoken_day(day, today) for day in two)
        return Times(
            note=f'"{state.booking_day}" could be {first} or {second}. Nothing is offered: ask which one, '
            'naming both dates, and record their answer as a date.'
        )
    found = Slots.model_validate(
        salon.diary.find_slots(
            phone,
            state.booking_day,
            state.service or '',
            now,
            offered_to=salon.call,
            prefer=state.booking_time,
        )
    )
    held = ', '.join(f'{spoken_service(h.service)} {h.spoken} ({h.booking_id})' for h in found.bookings)
    notes = [f'They already hold: {held}.'] if held else []
    if found.status != 'ok':
        notes.append(
            {
                'need_day': f'The diary cannot place "{state.booking_day}" as a day. Ask for a weekday or a date.',
                'past': f'{found.day} has already gone past. Ask for a day that has not.',
                'closed': f'The salon is closed on {found.day}. Ask for another day.',
            }.get(found.status, f'Nothing is free on {found.day}. Ask about another day.')
        )
        return Times(note=' '.join(notes))
    slots = _nearest([s.model_dump(mode='json') for s in found.slots], state.booking_time)[:OFFER_AT_MOST]
    if state.booking_time and re.fullmatch(r'\d{2}:\d{2}', state.booking_time):
        free = any(s['time'][:5] == state.booking_time for s in slots)
        notes.append(
            f'{state.booking_time} is {"free" if free else "not free"}; the nearest times come first.'
        )
    if found.price_euros is not None:
        notes.append(f'The price is {found.price_euros} euros.')
    return Times(
        options={s['slot_id']: f'{spoken_service(s["service"])} {s["spoken"]}' for s in slots},
        note=' '.join(notes),
    )


def held_bookings(salon: Salon, state: SalonState) -> dict[str, str]:
    """The bookings this verified caller holds, by id, as they are said: what a move or a cancel can name."""
    if not state.verified_phone:
        return {}
    today = salon.now().date()
    return {
        h['booking_id']: f'{spoken_service(h["service"])} {h["spoken"]}'
        for h in salon.diary.bookings_for(state.verified_phone, today)
    }


def _held(salon: Salon, phone: str) -> list[Held]:
    return [Held.model_validate(h) for h in salon.diary.bookings_for(phone, salon.now().date())]


def _written(raw: dict, phone: str) -> Written:
    saved = Saved.model_validate(raw)
    return Written(
        status=saved.status,
        phone=phone,
        booking_id=saved.booking_id,
        service=saved.service,
        spoken=saved.spoken,
        service_spoken=spoken_service(saved.service),
    )


def _expected(salon: Salon, phone: str, status: str, slot: str = '', booking_id: str = '') -> dict[str, str]:
    """What a receipt for this request has to say: its status, the offered slot's day, time and service, the booking."""
    offered = salon.diary.offer_of(salon.call, slot, phone) if slot else {}
    return {'status': status, **{k: offered[k] for k in ('date', 'time', 'service') if k in offered}} | (
        {'booking_id': booking_id} if booking_id else {}
    )


def _disagrees(raw: dict, expected: dict[str, str]) -> str:
    """Why a well-formed diary answer is not this request's answer, or ''.

    Pydantic says every field holds a legal value; it cannot say the answer is
    about the thing asked for. A `cancelled` to a booking, or a booking at
    another time, is in perfectly legal values, and is still not this receipt.
    """
    got = {k: str(raw.get(k, '')) for k in expected}
    return '' if got == expected else f'the diary answered {got}, not {expected}'


def _write(salon: Salon, token: str, phone: str, attempt, expected: dict[str, str]) -> Written:
    """One write, once: a refusal is an answer; a lost answer is asked about by its token before anything concludes.

    Found: the write landed and this is its receipt. Not found, unreadable, or
    about something else: nobody knows what happened, so it is an unknown
    outcome, never "nothing happened", and core will not let it be tried again
    until its `find_*` reconciliation has been asked.
    """
    try:
        raw = attempt()
    except AlreadyHoldsError as refused:
        return Written(status='refused', phone=phone, reason=str(refused))
    except RefusedError as refused:
        return Written(status='refused', phone=phone, reason=str(refused))
    except Exception as error:
        found = salon.diary.outcome_of(token)
        if found is None:
            raise OutcomeUnknownError(f'the diary did not answer: {error}') from error
        raw = found
    try:
        written = _written(raw, phone)
    except ValidationError as error:
        raise OutcomeUnknownError('the diary answered in a shape this cannot read') from error
    if why := _disagrees(raw, expected):
        raise OutcomeUnknownError(why)
    salon.diary.withdraw_offers(salon.call)
    return written


def book(salon: Salon, args: Booking, intent: Intent) -> Written:
    """Book the offered slot. A caller who already holds a booking was read back a second appointment, and said yes to that."""
    token = intent.id
    additional = bool(_held(salon, args.phone))
    return _write(
        salon,
        token,
        args.phone,
        lambda: salon.diary.save_booking(
            args.phone,
            'book',
            args.slot,
            '',
            additional,
            salon.now(),
            operation_token=token,
            offered_to=salon.call,
        ),
        _expected(salon, args.phone, 'booked', args.slot),
    )


def move(salon: Salon, args: Move, intent: Intent) -> Written:
    """Move the caller's own booking to the offered slot."""
    token = intent.id
    return _write(
        salon,
        token,
        args.phone,
        lambda: salon.diary.save_booking(
            args.phone,
            'move',
            args.slot,
            args.booking_id,
            False,
            salon.now(),
            operation_token=token,
            offered_to=salon.call,
        ),
        _expected(salon, args.phone, 'moved', args.slot, args.booking_id),
    )


def cancel(salon: Salon, args: Cancel, intent: Intent) -> Written:
    """Cancel the caller's own booking: a real write with its own receipt, never an in-memory undo."""
    token = intent.id
    return _write(
        salon,
        token,
        args.phone,
        lambda: salon.diary.save_booking(
            args.phone,
            'cancel',
            '',
            args.booking_id,
            False,
            salon.now(),
            operation_token=token,
            offered_to=salon.call,
        ),
        _expected(salon, args.phone, 'cancelled', booking_id=args.booking_id),
    )


def _found(salon: Salon, token: str, phone: str, expected: dict[str, str]) -> Written | None:
    """What an unanswered write did, by its token, checked against the request by the same rule as a first answer."""
    raw = salon.diary.outcome_of(token)
    if raw is None:
        return None
    written = _written(raw, phone)
    if why := _disagrees(raw, expected):
        raise OutcomeUnknownError(why)
    salon.diary.withdraw_offers(salon.call)
    return written


def find_booking(salon: Salon, args: Booking, intent: Intent) -> Written | None:
    """What an unanswered booking did, asked by its own intent: the reconciliation before any retry."""
    return _found(
        salon,
        intent.id,
        args.phone,
        _expected(salon, args.phone, 'booked', args.slot),
    )


def find_move(salon: Salon, args: Move, intent: Intent) -> Written | None:
    return _found(
        salon,
        intent.id,
        args.phone,
        _expected(salon, args.phone, 'moved', args.slot, args.booking_id),
    )


def find_cancel(salon: Salon, args: Cancel, intent: Intent) -> Written | None:
    return _found(
        salon,
        intent.id,
        args.phone,
        _expected(salon, args.phone, 'cancelled', booking_id=args.booking_id),
    )


def _slot_words(salon: Salon, phone: str, slot: str) -> tuple[str, str]:
    offered = salon.diary.offer_of(salon.call, slot, phone)
    return spoken_service(offered.get('service', 'appointment')), offered.get('spoken', 'that time')


def booking_words(salon: Salon, args: Booking) -> str:
    """The read-back of a booking, asking from its first word; to a caller who holds one, as a second appointment.

    "So that is a hair colour, tomorrow at 9 AM" sounds like a confirmation:
    in Coval run RPH7TzmF (K5) the caller took it as one and hung up with
    nothing booked. And in trace fbdd81b6 a plain "Shall I book you in" went
    to a caller holding the 9:00, whose yes the diary then refused.
    """
    service, when = _slot_words(salon, args.phone, args.slot)
    held = _held(salon, args.phone)
    if held:
        also = (
            f'your {spoken_service(held[0].service)} {held[0].spoken}'
            if len(held) == 1
            else 'the appointments you already have'
        )
        return f'Shall I book you a second appointment, a {service} {when}, as well as {also}?'
    return f'Shall I book a {service}, {when}?'


def move_words(salon: Salon, args: Move) -> str:
    service, when = _slot_words(salon, args.phone, args.slot)
    held = next((h for h in _held(salon, args.phone) if h.booking_id == args.booking_id), None)
    was = f' from {held.spoken}' if held else ''
    return f'Shall I move your {service}{was} to {when}?'


def cancel_words(salon: Salon, args: Cancel) -> str:
    held = next((h for h in _held(salon, args.phone) if h.booking_id == args.booking_id), None)
    what = f'your {spoken_service(held.service)} {held.spoken}' if held else 'that booking'
    return f'Shall I cancel {what}?'


NOTED = "That's on record, reference {reference}. {next_step}"
REPLY_SOON = 'The salon will get back to you within two working days.'
MANAGER_CALLS = 'That one goes to the manager, who will call you back.'
_WANTS_MONEY = re.compile(r'\b(?:refund|money back|reimburse|compensat\w*|my money)\b', re.IGNORECASE)
"""What sends a complaint to the manager, straight out of the policy document: a half or a full refund."""


def acknowledgement(reference: str, requested_resolution: str) -> str:
    """The one thing said when a complaint is written down: its reference, then the policy's next step."""
    step = MANAGER_CALLS if _WANTS_MONEY.search(requested_resolution) else REPLY_SOON
    pending = ' Your requested resolution is pending approval.' if requested_resolution else ''
    return NOTED.format(reference=reference, next_step=step) + pending + ' Anything else I can help with?'


def _noted(raw: dict, args: ComplaintArgs) -> Noted:
    recorded = bool(raw.get('recorded'))
    reference = str(raw.get('reference', ''))
    return Noted(
        recorded=recorded,
        reference=reference,
        phone=args.phone,
        said=acknowledgement(reference, args.requested_resolution),
    )


def record_complaint(salon: Salon, args: ComplaintArgs, intent: Intent) -> Noted:
    """Write the complaint down, in the caller's words, once. Twice with one intent writes it once."""
    token = intent.id
    try:
        raw = salon.diary.record_complaint(
            args.phone, args.summary, args.requested_resolution, operation_token=token
        )
    except Exception as error:
        found = salon.diary.outcome_of(token)
        if found is None:
            raise OutcomeUnknownError(f'the complaint book did not answer: {error}') from error
        raw = found
    return _noted(raw, args)


def find_complaint(salon: Salon, args: ComplaintArgs, intent: Intent) -> Noted | None:
    raw = salon.diary.outcome_of(intent.id)
    if raw is None:
        return None
    return _noted(raw, args)


def complaint_words(args: ComplaintArgs) -> str:
    request = (
        f'Your request: {args.requested_resolution.strip().rstrip(".?!")}. '
        if args.requested_resolution
        else ''
    )
    return (
        f'Shall I record your complaint in your words: {args.summary.strip().rstrip(".?!")}. {request}'
        'The salon will review it; no refund or booking change is approved yet. Is that right?'
    )
