"""The write boundary: how a booking or a complaint reaches the diary and comes back validated.

A booking and a complaint fail the same three ways: the service does not
answer, it answers in a shape nothing can read, or it answers about something
else. `_answered` handles the first two for both and `_disagrees` the third,
which is specific to a booking. A lost answer is never "nothing happened":
the write may well have committed, and the operation token is asked what it
did before anything concludes otherwise.

What the caller hears when a write lands is also decided here, in the diary's
own words, so a second model request cannot describe one appointment two ways
or promise a policy step the salon has not written down.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import NoReturn

from pydantic import BaseModel, ValidationError
from pydantic_ai import ModelRetry

from core.policies import pick
from salon.diary import RefusedError
from salon.models.consent import Asked
from salon.models.receipts import ComplaintRecord, Saved
from salon.models.state import Appointment, SalonCall, Unresolved

DID = {'book': 'booked', 'move': 'moved', 'cancel': 'cancelled'}
"""What the diary calls the outcome of each action. A response reporting one of the other two is not this request's answer."""

OUTSTANDING = (
    'There is a write on this call whose outcome is still unknown, so nothing else may be '
    'written until somebody checks it. Tell the caller you will confirm what happened, and '
    'do not try to book, move or cancel anything.'
)
"""Said by every entry point while an earlier write is outstanding. One string, so two copies cannot drift."""
UNKNOWN = (
    'It is not clear whether that saved: {why}. Nothing else can be written on this call until '
    'somebody checks. Tell the caller you will confirm it and not to book again, and do not call '
    'this a second time.'
)

SAVED: dict[str, tuple[str, ...]] = {
    'booked': (
        "You're all set for {spoken}. Anything else I can help with?",
        "Lovely, that's your {service} booked for {spoken}. What else can I do?",
        'Done, {spoken}. Anything else?',
    ),
    'moved': (
        "That's moved to {spoken}. Anything else I can help with?",
        "Right, you're down for {spoken} now. What else can I do?",
        'Moved to {spoken}. Anything else?',
    ),
    'cancelled': (
        "That's cancelled for you. Anything else I can help with?",
        "Right, that's cancelled. What else can I do?",
        'Cancelled. Anything else?',
    ),
}
"""What the caller hears the moment the diary is written, said by this code.

`{spoken}` is the diary's own phrase, computed once. Alternatives, because a
caller who books and then moves it hears this twice inside a minute.
"""

NOTED = "That's on record, reference {reference}. {next_step}"
REPLY_SOON = 'The salon will get back to you within two working days.'
MANAGER_CALLS = 'That one goes to the manager, who will call you back.'
"""The two next steps the refunds policy actually states, and nothing else.

Said here rather than by the model, for the same reason a booking confirmation
is: there is one right sentence, and a round trip spent asking for it is a
round trip that can weld two commitments together.
"""
_WANTS_MONEY = re.compile(r'\b(?:refund|money back|reimburse|compensat\w*|my money)\b', re.IGNORECASE)
"""What sends a complaint to the manager, straight out of the policy document: a half or a full refund."""


def acknowledgement(reference: str, requested_resolution: str) -> str:
    """The one thing said when a complaint is written down: its reference, then the policy's next step."""
    step = MANAGER_CALLS if _WANTS_MONEY.search(requested_resolution) else REPLY_SOON
    pending = ' Your requested resolution is pending approval.' if requested_resolution else ''
    return NOTED.format(reference=reference, next_step=step) + pending + ' Anything else I can help with?'


async def close_with(call: SalonCall, text: str) -> None:
    """Speak a receipt and record its offer of further help as a decision point.

    To a caller who is leaving (`CallContext.leaving`), only the receipt: the
    goodbye comes next, and "Anything else?" right before it asks nothing.

    ponytail: the offer of help is the last sentence of every receipt here,
    all of them this module's own templates, so it is cut off by position.
    A receipt that ends otherwise would need the offer passed apart.
    """
    if call.leaving:
        receipt, _, offer = text.rpartition('. ')
        await call.speak_now(f'{receipt}.' if receipt and offer.endswith('?') else text)
        return
    call.ask(Asked(about='closing', spoken=text, owner=call.state.customer_phone))
    await call.speak_now(text)


def spoken_service(service: str) -> str:
    """The service as the voice should say it."""
    return service.replace('hair-color', 'hair colour')


def _disagrees(action: str, booking_id: str, offered: dict, written: Saved) -> str:
    """Why a well-formed result is not an answer to the request, or '' when it is.

    Pydantic says every field holds a legal value. It cannot say the result is
    about the thing that was asked for, and a service answering `cancelled` to
    a booking request answers in perfectly legal values. The offered slot is
    compared in the wire shape the offer was recorded in, on the three fields
    that identify it; its spoken phrasing is presentation and not something a
    result has to agree with.
    """
    if written.status != DID[action]:
        return f'a {written.status} result came back from a {action} request'
    if action in ('move', 'cancel') and booking_id and written.booking_id != booking_id:
        return f'it is about booking {written.booking_id}, not {booking_id}'
    if action in ('book', 'move') and offered:
        got = {
            'date': written.date.isoformat(),
            'time': written.time.strftime('%H:%M'),
            'service': written.service,
        }
        was = {key: offered[key] for key in got if key in offered}
        if got != was:
            return f'it is {got}, not the slot that was offered: {was}'
    return ''


def _unknown(call: SalonCall, pending: Unresolved, why: str) -> NoReturn:
    """Record this write as outstanding and refuse, which is the only thing that blocks the next one.

    On the state, not in a sentence: retry text asking the model not to try
    again is not a guard, and changing one argument changes the token, so the
    service's own idempotency cache never sees the second attempt.
    """
    call.commit(unresolved=pending)
    raise ModelRetry(UNKNOWN.format(why=why))


def _answered[Result: BaseModel](
    call: SalonCall,
    pending: Unresolved,
    shape: type[Result],
    attempt: Callable[[], object],
    book: str,
) -> Result:
    """Perform one write and read its answer, or record the outcome as unknown and refuse.

    The service saying no is an answer, and a clear one, so a `RefusedError`
    passes straight through. Anything else that stops the attempt, and any
    answer that does not parse, sends the token back to the service to ask what
    actually happened: the service may have recorded a perfectly good outcome
    and merely told us about it badly.
    """
    try:
        raw = attempt()
    except RefusedError:
        raise
    except Exception:
        recovered = call.diary.outcome_of(pending.token)
        if recovered is None:
            _unknown(call, pending, f'the {book} did not answer')
        raw = recovered
    try:
        return shape.model_validate(raw)
    except ValidationError:
        recovered = call.diary.outcome_of(pending.token)
        if recovered is None or recovered is raw:
            _unknown(call, pending, f'the {book} answered in a shape this cannot read')
        try:
            return shape.model_validate(recovered)
        except ValidationError:
            _unknown(call, pending, f'the {book} answered in a shape this cannot read')


async def _settle(call: SalonCall) -> str:
    """Finish an earlier write whose outcome was never established, or refuse to move past it.

    Until it is settled, a second write is how one caller gets two
    appointments. What comes back is checked against what was *asked for*, by
    the same rule the first answer was checked by: the request, not the reply,
    says what this was. Returns what to say when the earlier write turns out to
    have landed, and raises when it is still unknown or still does not answer
    the question.
    """
    pending = call.state.unresolved
    assert pending is not None, 'only reached while a write is outstanding'
    outcome = call.diary.outcome_of(pending.token)
    if outcome is None:
        raise ModelRetry(OUTSTANDING)
    if pending.action == 'complaint':
        try:
            record = ComplaintRecord.model_validate(outcome)
        except ValidationError:
            raise ModelRetry(UNKNOWN.format(why='the complaint book answered unreadably')) from None
        if not record.recorded:
            call.commit(unresolved=None)
            raise ModelRetry(
                'The complaint was never taken -- the book refused it. Tell the caller it is not '
                'logged and that someone will call them back.'
            )
        call.commit(writes=call.state.writes + 1, unresolved=None)
        call.retain_action('record_complaint', record.model_dump(mode='json'))
        await close_with(call, acknowledgement(record.reference, pending.wanted))
        return f'complaint recorded: {record.reference}'
    try:
        written = Saved.model_validate(outcome)
    except ValidationError:
        raise ModelRetry(UNKNOWN.format(why='the diary answered in a shape this cannot read')) from None
    action = pending.action
    if why := _disagrees(action, pending.booking_id, pending.offered, written):
        raise ModelRetry(UNKNOWN.format(why=why)) from None
    call.commit(
        appointment=Appointment(
            booking_id=written.booking_id,
            service=written.service,
            date=written.date,
            time=written.time,
            spoken=written.spoken,
            action=action,
        ),
        agent=call.resume('concierge'),
        writes=call.state.writes + 1,
        unresolved=None,
    )
    call.retain_action('save_booking', written.model_dump(mode='json'))
    await close_with(
        call,
        pick(f'saved:{written.status}', SAVED[written.status]).format(
            spoken=written.spoken, service=spoken_service(written.service)
        ),
    )
    return f'{written.status}: {written.spoken}'
