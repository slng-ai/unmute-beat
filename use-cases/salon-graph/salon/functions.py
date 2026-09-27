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

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Annotated, Literal

from pydantic import BaseModel, StringConstraints, ValidationError

from core import Intent, OutcomeUnknownError
from salon.diary import AlreadyHoldsError, Diary, RefusedError, spoken_day, spoken_time
from salon.models.receipts import CustomerLookup, Held, Saved, Slots
from salon.models.shapes import Spoken, SubmittedPhone, grouped, phone_length
from salon.models.state import SalonState, ambiguous_day


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
    settle: Callable[[str], bool] | None = None
    """Which earlier calls are over, by call key: only their intents are owed to a new call (`owed`)."""

    def now(self) -> datetime:
        return self.diary.now()

    def owed(self, identity: str) -> list[Intent]:
        """This caller's intents that calls now over left with no answer: core settles them before anything new.

        Settled by the diary: found, they are said as done; not found, they
        are voided, so a worker still holding one can never land it. An
        intent of a call still running anywhere is left alone.
        """
        if self.settle is None:
            return []
        return [
            Intent(
                id=i['id'],
                operation=i['operation'],
                args=i['args'],
                subject=i['subject'],
                status='dispatched',
            )
            for i in self.diary.unresolved(identity)
            if i['session'] != self.call and self.settle(i['session'])
        ]

    async def note_intent(self, intent: Intent) -> None:
        """Core's durable record of an intent, before it is dispatched: what a new call settles if this one dies."""
        await asyncio.to_thread(
            self.diary.note_intent, intent.id, self.call, intent.operation, intent.args, intent.subject
        )

    def current_version(self, kind: str, resource: str) -> int | None:
        """A resource's version now, for a receipt core learnt by reconciliation. A complaint never changes."""
        if kind == 'booking':
            return self.diary.current_revision(resource)
        return 1 if kind == 'complaint' else None


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

    status: Literal['booked', 'moved', 'cancelled', 'refused', 'void']
    """`void`: nothing was done, and the diary will never do it: the write was fenced (`Diary.outcome_or_void`)."""
    phone: str
    booking_id: str = ''
    revision: int = 0
    """The booking's revision after this write: what orders a move after the booking it moved."""
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
    revision: int = 1
    """A complaint is written once and never changed, so its version is always 1."""


class Times(BaseModel):
    """The times on offer by slot id, what the agent should know that an id cannot say, and what the caller hears.

    `say` is the salon's own words for this offer: the booking step is
    rendered, so the model reads the caller and chooses, and never words an
    offer (session 4d: it spoke a copy of the options as JSON, call 4).
    """

    options: dict[str, str] = {}
    note: str = ''
    say: str = ''


def spoken_service(service: str) -> str:
    """The service as the voice should say it."""
    return service.replace('hair-color', 'hair colour')


def lookup_customer(salon: Salon, args: PhoneArgs) -> CustomerLookup:
    """Find the caller in the salon's books, or add them. Run only on their yes to the number read back."""
    return CustomerLookup.model_validate(salon.diary.find_or_create_customer(args.phone))


def number_words(args: PhoneArgs) -> str:
    return f'Is your number {Spoken.validate_python(args.phone)}?'


def slot_refusal(raw: str) -> str:
    """What the caller hears when the time recorded is not one on offer: booking's `refusals:`, said by code."""
    return "That time isn't one I have free."


def appointment_refusal(raw: str) -> str:
    """What the caller hears when the booking recorded is not one they hold on offer."""
    return "I can't find that booking."


def service_refusal(raw: str) -> str:
    """What the caller hears when the service recorded is not one the salon offers."""
    return 'We do haircuts, colour and blowouts. Which would you like?'


def time_refusal(raw: str) -> str:
    """What the caller hears when the time asked for is not a time the diary can read."""
    return 'What time would suit you?'


def phone_refusal(raw: str) -> str:
    """What the caller hears when the number recorded cannot be looked up: verify's `refusals:`, said by code.

    The same four questions the verify prompt used to give the model to say,
    now said by code, with a too-long number's digits grouped the way the
    read-back groups them. '' for a number with nothing wrong in its length:
    core then asks with the step's `asks:` line.
    """
    if not raw.startswith('+'):
        return 'Which country is that phone number from?'
    match phone_length(raw):
        case 'too long':
            return f'I have {grouped(raw)}. Which part is wrong?'
        case 'too short':
            return 'Please give me the rest of the digits.'
        case 'unknown country code':
            return 'Which phone number should I use? Please say it with the country code.'
    return ''


OFFER_AT_MOST = 6
"""How many times the agent is shown, nearest the caller's time first. The caller hears up to three of them."""
OFFER_SAID = 3


def _listed(items: list[str], last: str = 'or') -> str:
    return items[0] if len(items) == 1 else f'{", ".join(items[:-1])} {last} {items[-1]}'


def _on(day: str) -> str:
    """ "On Friday the 2nd", but "Tomorrow" and "Today" on their own."""
    return day.capitalize() if day in ('today', 'tomorrow') else f'On {day}'


def _times_said(state: SalonState, slots: list[dict], day: str, asked_free: bool | None) -> str:
    """The offer as the caller hears it: the nearest times on the day, or the time they asked for when it is free."""
    times = [
        spoken_time(time.fromisoformat(s['time']))
        + ('' if state.service else f' for a {spoken_service(s["service"])}')
        for s in slots[:OFFER_SAID]
    ]
    if not times:
        return f'{_on(day)} nothing is free. Would another day work?'
    if asked_free:
        free = (
            f'{times[0]} {day} is free.' if day in ('today', 'tomorrow') else f'{times[0]} on {day} is free.'
        )
        return f'{free} I also have {_listed(times[1:])}.' if times[1:] else free
    taken = f'{spoken_time(time.fromisoformat(state.booking_time))} is taken. ' if asked_free is False else ''
    return f'{taken}{_on(day)} I have {_listed(times)}. Which would suit you?'


def _discussed(salon: Salon, phone: str, state: SalonState, today: date) -> date | None:
    """The day already on the table: the times last offered to this call, else the booking being moved."""
    offered = salon.diary.offered_to(salon.call, phone)
    if offered:
        return date.fromisoformat(offered[0]['date'])
    for held in salon.diary.bookings_for(phone, today):
        if held['booking_id'] == state.appointment:
            return date.fromisoformat(held['date'])
    return None


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
        return Times(note='No day yet: ask which day they would like.', say='Which day would you like?')
    if (two := ambiguous_day(state.booking_day, today, _discussed(salon, phone, state, today))) is not None:
        first, second = (spoken_day(day, today) for day in two)
        return Times(
            note=f'"{state.booking_day}" could be {first} or {second}. Nothing is offered: ask which one, '
            'naming both dates, and record their answer as a date.',
            say=f'Do you mean {first} or {second}?',
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
    day = spoken_day(found.date, today) if found.date else found.day
    if found.status != 'ok':
        notes.append(
            {
                'need_day': f'Nothing was looked up: "{state.booking_day}" is not a day the diary can place, so '
                'this says nothing about what is free. Ask which day they mean: a weekday or a date.',
                'past': f'{found.day} has already gone past. Ask for a day that has not.',
                'closed': f'The salon is closed on {found.day}. Ask for another day.',
            }.get(found.status, f'Nothing is free on {found.day}. Ask about another day.')
        )
        return Times(
            note=' '.join(notes),
            say={
                'need_day': 'Which day do you mean? A weekday or a date is fine.',
                'past': f'{day.capitalize()} has already gone. Which other day would suit you?',
                'closed': f"We're closed {day if day in ('today', 'tomorrow') else f'on {day}'}. "
                'Which other day would suit you?',
            }.get(found.status, f'{_on(day)} nothing is free. Would another day work?'),
        )
    slots = _nearest([s.model_dump(mode='json') for s in found.slots], state.booking_time)[:OFFER_AT_MOST]
    free = None
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
        say=_times_said(state, slots, day, free),
    )


def held_bookings(salon: Salon, state: SalonState) -> Times:
    """The bookings this verified caller holds, by id, as they are said: what a move or a cancel can name.

    The caller hears them only while nothing else is on the table: no booking
    picked yet and no day asked for, which is how a move or a cancel starts.
    Once they are looking at times, the bookings they hold are the agent's to
    know, not a line to repeat with every offer.
    """
    if not state.verified_phone:
        return Times()
    today = salon.now().date()
    held = {
        h['booking_id']: f'{spoken_service(h["service"])} {h["spoken"]}'
        for h in salon.diary.show_holdings(salon.call, state.verified_phone, today)
    }
    if not held or state.appointment or state.booking_day:
        return Times(options=held)
    said = _listed([f'a {words}' for words in held.values()], 'and')
    return Times(options=held, say=f'You have {said}.' + (' Which one?' if len(held) > 1 else ''))


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
        revision=saved.revision,
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

    A refusal names the booking the request was about (session 4d, defect 3):
    core binds every receipt to its request by `matches`, and a move or cancel
    refusal with no booking id was about nothing, so it stayed unknown. That
    id is the request's own, not an effect: no revision, nothing moved.
    """
    asked = expected.get('booking_id', '')
    try:
        raw = attempt()
    except AlreadyHoldsError as refused:
        return Written(status='refused', phone=phone, booking_id=asked, reason=str(refused))
    except RefusedError as refused:
        return Written(status='refused', phone=phone, booking_id=asked, reason=str(refused))
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
            expected_revision=salon.diary.shown_revision(salon.call, args.booking_id),
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
            expected_revision=salon.diary.shown_revision(salon.call, args.booking_id),
        ),
        _expected(salon, args.phone, 'cancelled', booking_id=args.booking_id),
    )


def _found(salon: Salon, token: str, phone: str, expected: dict[str, str]) -> Written:
    """What an unanswered write did, by its token, checked against the request by the same rule as a first answer.

    Never "not yet": a write nobody has an answer for is fenced (`void`), so
    a worker still holding it can never land it later, and the caller hears
    that nothing was done, which is then true for good. The fence names the
    booking the request was about, as a refusal does.
    """
    raw = salon.diary.outcome_or_void(token)
    if raw.get('status') == 'void':
        return Written(status='void', phone=phone, booking_id=expected.get('booking_id', ''))
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


def find_complaint(salon: Salon, args: ComplaintArgs, intent: Intent) -> Noted:
    """What an unanswered complaint did, by its intent; fenced (`void`, not recorded) when the book never saw it."""
    return _noted(salon.diary.outcome_or_void(intent.id), args)


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
