"""The booking step's tools: read the diary, offer times, ask for the yes, write.

`save_booking` is the only thing that changes an appointment, and it will not
run on the model's say-so. `_authorised` is what stands between the two: the
caller either agreed to a readback of this exact write, picked one of the
times the pending offer put to them, or answered the move-or-add question in
their own words. The model supplies none of those; the consent reading
interprets the reply, and this code checks it against what was on the table.

The step's opening is here too: resumed after verification with the
request the caller made at the front desk, it offers times for it without a
model re-reading that request, because the words are already typed.
"""

from __future__ import annotations

from typing import Literal

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.usage import RunUsage, UsageLimits

from core.graph import Opening
from core.operations import Operation
from core.policies import claim_or_refuse, pick
from core.state import present
from salon.classification import classify_reply
from salon.diary import (
    AlreadyHoldsError,
    RefusedError,
    day_phrases,
    resolve_day,
    resolve_time,
)
from salon.models.consent import Asked
from salon.models.receipts import BookingCommand, Held, Saved, Slots
from salon.models.shapes import Service
from salon.models.state import Appointment, SalonCall, Unresolved
from salon.tools.asking import Ask
from salon.tools.policy import ACTIONS
from salon.tools.writes import (
    SAVED,
    _answered,
    _disagrees,
    _settle,
    _unknown,
    close_with,
    operation_token,
    spoken_service,
)


async def read_diary(
    ctx: RunContext[SalonCall],
    when: str,
    service: Literal['any', 'haircut', 'hair-color', 'blowout'],
    at: str = '',
) -> Slots:
    """Read the diary: what this caller already holds, and what is free on the day they named.

    One call answers both, so you never need a second. It works the day and the
    time out from the caller's own words, never returns a time that has already
    gone past, and never writes anything.

    Args:
        when: The day in the caller's own words -- "tomorrow", "next Friday",
            "the 25th" -- or an empty string if they have not named one.
        service: Narrow the free times to one service, or `any` to see them all.
        at: The time or part of day they asked for, in their own words --
            "three", "half past two", "the afternoon" -- or empty if they said
            none. It decides which free times you are shown, so leaving it out
            when they named one is how a caller asking for the afternoon gets
            read the morning.
    """
    claim_or_refuse(ctx, ACTIONS)
    return await find_times(ctx.deps, when, service, at)


async def find_times(call: SalonCall, when: str, service: Service | Literal['any'], at: str = '') -> Slots:
    """The read itself. Shared by `read_diary`, `offer_times` and the booking step's opening.

    A new read replaces whatever was on the table, so it empties `offered`,
    and it is what the front desk's carried request was waiting for, so it
    consumes `wanted`.
    """
    if not present(call.state.customer_verified):
        raise ModelRetry(
            'The diary is read under a confirmed phone number and nobody is identified yet. Hand to the verify agent.'
        )
    _same_day_the_caller_named(call, when)
    found = Slots.model_validate(
        call.diary.find_slots(
            call.state.customer_phone,
            when,
            service,
            call.diary.now(),
            offered_to=call.id,
            prefer=resolve_time(at or call.heard),
        )
    )
    call.commit(booking_day=found.date, offered=[], wanted=None)
    return found


def _same_day_the_caller_named(call: SalonCall, when: str) -> None:
    """Refuse a lookup for a day the caller never said.

    The caller's words for the day, against the ones this call was given. The
    resolver is right; what it can be given is the wrong input: the caller
    says "Friday", the lookup asks for "next Friday", and under a Monday clock
    those are a week apart. The whole difference is a modifier the model added,
    so the check is on the words and not on the date they resolve to.

    The lookup is what records the offers and `save_booking` refuses a slot
    from any other one, so `offered_to` is this call, not this number, and
    asking again withdraws whatever the last search offered.
    """
    if (theirs := call.day_words) and (mine := day_phrases(when)):
        today = call.diary.now().date()
        wanted = {resolve_day(words, today) for words in theirs} - {None}
        asked_for = resolve_day(mine[-1], today)
        if wanted and asked_for not in wanted:
            said = '" or "'.join(theirs)
            raise ModelRetry(
                f'Nothing was looked up. The caller said "{said}" and this asked for "{mine[-1]}", '
                f'which is a different day. Call this again with their words. If you think they '
                'meant another day, ask them which one.'
            )


async def offer_times(
    ctx: RunContext[SalonCall],
    when: str,
    service: Service,
    at: str = '',
) -> str:
    """Read availability and speak up to three real choices, ending this reply.

    Use when the caller needs to choose a time. For an already authorized same-time
    move or clear selection, use read_diary then save_booking instead.

    Args:
        when: The caller's own day words.
        service: The requested service.
        at: The caller's time or daypart, if given.
    """
    claim_or_refuse(ctx, ACTIONS)
    return await offer(ctx.deps, when, service, at)


_OFFERED = 3
"""How many times one offer speaks. More is a list nobody on a phone remembers."""


async def offer(call: SalonCall, when: str, service: Service, at: str = '') -> str:
    """Read the diary and speak the first few free times. Shared by `offer_times` and the booking step's opening.

    What was spoken is recorded, ids and all, so the turn the caller picks one
    can go straight to `save_booking` rather than re-reading the diary to find
    the id of a time they have already been told. The offer is also the
    question the caller's next reply answers: the consent reading names which
    of these ids they picked, and only an id offered here can count.
    """
    found = await find_times(call, when, service, at)
    spoken = found.slots[:_OFFERED]
    if spoken:
        times = ', '.join(s.time.strftime('%I:%M %p').lstrip('0') for s in spoken)
        day = found.day if found.day in ('today', 'tomorrow') else f'on {found.day}'
        text = f'For a {spoken_service(service)} {day}, I have {times}. Which time works for you?'
    else:
        text = {
            'need_day': 'Which day would you like?',
            'past': 'That date has already passed. Which future day would you like?',
            'closed': 'The salon is closed that day. Which other day works for you?',
        }.get(found.status, 'There are no available times for that request. Which other day works for you?')
    call.commit(offered=spoken)
    if spoken:
        call.ask(
            Asked(
                about='offer',
                spoken=text,
                value=','.join(slot.slot_id for slot in spoken),
                owner=call.state.customer_phone,
            )
        )
    else:
        call.answered()
    await call.speak_now(text, origin='ask' if spoken else 'guidance')
    return text


async def booking_guidance(
    ctx: RunContext[SalonCall], topic: Literal['day', 'service', 'time', 'move_or_add', 'existing', 'retry']
) -> str:
    """Ask for missing booking details or recount the caller's actual appointments.

    Use offer_times for availability, ask_booking for an exact readback, and
    save_booking for an authorized change. This tool never claims a new write.
    """
    call = ctx.deps
    if topic == 'existing' and not call.state.customer_verified:
        raise ModelRetry('Identify the caller before discussing their bookings.')
    if topic == 'existing':
        held = [
            Held.model_validate(row)
            for row in call.diary.bookings_for(call.state.customer_phone, call.diary.now().date())
        ]
        text = (
            'You have '
            + '; '.join(f'a {spoken_service(b.service)} {b.spoken}' for b in held)
            + '. What would you like to do?'
            if held
            else 'There are no upcoming appointments under this number. Would you like to make one?'
        )
    elif topic == 'move_or_add':
        question = call._asked
        if (
            question is None
            or question.about != 'move_or_add'
            or question.turn not in (call.turn, call.turn - 1)
        ):
            raise ModelRetry(
                'Attempt the requested save first; its refusal identifies the booking and choice.'
            )
        text = 'Would you like to move the appointment you already have, or add another one?'
        call.ask(question.model_copy(update={'spoken': text, 'owner': call.state.customer_phone}))
    else:
        text = {
            'day': 'Which day would you like?',
            'service': 'Would you like a haircut, hair colour, or a blowout?',
            'time': 'Which time would you like?',
            'retry': "I couldn't complete that request. What would you like to change?",
        }[topic]
    if topic != 'move_or_add':
        call.answered()
    await call.speak_now(text, origin='guidance')
    return text


async def ask_booking(
    ctx: RunContext[SalonCall],
    action: Literal['book', 'move', 'cancel'],
    slot_id: str = '',
    booking_id: str = '',
) -> Ask:
    """Say back what you are about to write, and wait for their yes. Nothing is saved by this.

    The sentence is built here from the diary's own record of the slot, so it
    cannot describe a time the caller was never offered. Say nothing after
    calling it: their next answer is what decides.

    Args:
        action: book a new time, move an existing booking, or cancel one.
        slot_id: The slot exactly as the diary returned it. Empty when cancelling.
        booking_id: The booking exactly as the diary returned it. Empty when booking.
    """
    return booking_question(ctx.deps, action, slot_id, booking_id)


def booking_question(
    call: SalonCall, action: Literal['book', 'move', 'cancel'], slot_id: str, booking_id: str
) -> Ask:
    """The read-back of one booking, move or cancel, worded from the diary's own record.

    Shared by `ask_booking` and by the pick of an offered time (`salon.nodes`),
    which is read back like any other proposal: the caller's yes to it is what
    writes, never the pick.

    Raises:
        ModelRetry: If the slot is not one this caller is being offered, or the booking not one they hold.
    """
    phone = call.state.customer_phone
    if action in ('book', 'move'):
        offered = call.diary.offer_of(call.id, slot_id, phone)
        if not offered:
            raise ModelRetry(
                'Nothing was asked. That slot is not one this caller is currently being offered; '
                'read the diary again and offer a time it actually returned.'
            )
        service = spoken_service(offered['service'])
        words = (
            f'So that is a {service}, {offered["spoken"]}. Shall I book that in?'
            if action == 'book'
            else f'So I will move you to {offered["spoken"]}. Shall I do that?'
        )
    else:
        return cancel_question(call, booking_id)
    return Ask(about='booking', value=slot_id, action=action, booking_id=booking_id, spoken=words)


async def offer_cancel(ctx: RunContext[SalonCall]) -> Ask:
    """Offer to cancel the appointment booked earlier in this call, when the caller takes it back.

    For "leave it", "I don't want it after all", "cancel that one". It reads
    the cancellation back and waits for their yes; nothing is cancelled by this.
    """
    call = ctx.deps
    if not (booking_id := call.undo_booking):
        raise ModelRetry('Nothing was asked: nothing booked in this call is still held by this caller.')
    if call.classification.last_change == 'keep':
        raise ModelRetry(
            'Nothing was asked: the caller is keeping the appointment. Do not offer to cancel it.'
        )
    return cancel_question(call, booking_id)


def undo_ready(call: SalonCall) -> bool:
    """`offer_cancel` is offered only while a booking made in this call is held and the caller has not kept it.

    Coval run ad3n4Vxn, K5: asked whether to keep or cancel, the caller said
    "Fine, nine o'clock then", the reading said keep, and the model read the
    cancellation back anyway. The reading is the application's evidence, so
    it decides whether the offer is on the table at all.
    """
    return bool(call.undo_booking) and call.classification.last_change != 'keep'


def cancel_question(call: SalonCall, booking_id: str) -> Ask:
    """The read-back for cancelling one booking this caller holds, worded from the diary's own record.

    Shared by `ask_booking` and by the undo of a booking the caller has just
    taken back, so both put the same question and the same guard answers it.

    Raises:
        ModelRetry: If the caller holds no such booking.
    """
    phone = call.state.customer_phone
    held = {b['booking_id']: b for b in call.diary.bookings_for(phone, call.diary.now().date())}
    if booking_id not in held:
        raise ModelRetry(
            'Nothing was asked. That is not a booking this caller holds; read the diary to see '
            'what they actually have.'
        )
    one = held[booking_id]
    words = f'Shall I cancel your {spoken_service(one["service"])} {one["spoken"]}?'
    return Ask(about='booking', value='', action='cancel', booking_id=booking_id, spoken=words)


def _authorised(call: SalonCall, action: str, slot_id: str, booking_id: str) -> bool:
    """Whether the caller authorised this exact write, and whether it is a second appointment.

    Refuses by raising, so a write that reaches past here has caller evidence
    behind it. Two paths, checked against application state:

    * They agreed to an `ask_booking` readback of this same action, slot and
      booking, on the turn before this one.
    * They answered the move-or-add question in their own words.

    Picking one of the offered times is not one of them. It used to be: a
    plain choice of an offered slot booked it, one turn faster. The graph and
    LiveKit builds of this salon say "choosing a time is not a yes", and the
    Coval release check asks for a read-back and a yes before "booked", so a
    pick is now read back (`salon.nodes._picked`) and only the yes writes.

    The boolean it returns is the *second appointment* decision, and it comes
    from what the caller said, never from an argument: a flag the model could
    flip was its own permission slip, refused once and sent again a second
    later.
    """
    phone = call.state.customer_phone
    held = call.diary.bookings_for(phone, call.diary.now().date()) if phone else []

    if question := call.pending('booking', value=slot_id, action=action, booking_id=booking_id):
        if question.owner and question.owner != phone:
            raise ModelRetry('The caller identity changed. Ask for approval again under this number.')
        if call.classification.confirmation != 'accept':
            raise ModelRetry(
                'Nothing was saved: the caller did not agree to what was read back to them. They '
                f'said "{call.heard}". Answer what they actually asked.'
            )
        additional = action == 'book' and call.classification.appointment_choice == 'add'
        call.answered()
        return additional

    if action == 'cancel':
        raise ModelRetry(
            'Nothing was cancelled: nobody has agreed to cancel anything. Call `ask_booking` with '
            "action='cancel' and that booking, and wait for their answer."
        )

    if (decision := call.pending('move_or_add', value=slot_id)) is not None:
        if decision.owner and decision.owner != phone:
            raise ModelRetry('The caller identity changed. Ask again.')
        match call.classification.appointment_choice:
            case 'add':
                if action != 'book':
                    raise ModelRetry('The caller chose an additional booking. Ask again before moving one.')
                call.answered()
                return True
            case 'move':
                call.answered()
                if action != 'move' or not booking_id or booking_id != decision.booking_id:
                    raise ModelRetry(
                        'The caller asked to move the booking they already have, not to add one. '
                        f"Call this again with action='move' and booking_id={decision.booking_id!r}."
                    )
                return False
            case _:
                raise ModelRetry(
                    f'Nothing was saved. The caller said "{call.heard}", which does not answer '
                    'whether to move the appointment they already have or add another one. Ask '
                    'them again in those words.'
                )

    if held and action == 'book':
        one = held[0]
        call.ask(Asked(about='move_or_add', value=slot_id, action='move', booking_id=one['booking_id']))
        raise ModelRetry(
            f'Nothing was saved: this caller already has a {one["service"]} {one["spoken"]}. Ask '
            'them, in one question, whether to move that one or add another appointment on top of '
            'it, and call this again with their answer. Do not decide it for them.'
        )
    raise ModelRetry(
        'Nothing was saved: the caller has not said they want this. Read it back with '
        '`ask_booking` and call this again once they have agreed.'
    )


async def save_booking(
    ctx: RunContext[SalonCall],
    action: Literal['book', 'move', 'cancel'],
    slot_id: str,
    booking_id: str,
) -> str:
    """Write the diary, and finish this step. Nothing is booked, moved or cancelled until this runs.

    Only after the caller has agreed to an `ask_booking` readback of this
    exact appointment. Picking one of the offered times is not agreement yet:
    read it back first. It speaks the confirmation itself, so say nothing
    after calling it.

    Args:
        action: book a new time, move an existing booking, or cancel one.
        slot_id: The slot exactly as the diary returned it. Empty when cancelling.
        booking_id: The booking exactly as the diary returned it. Empty when booking.
    """
    claim_or_refuse(ctx, ACTIONS)
    return await write_booking(
        ctx.deps, action, slot_id, booking_id, usage=ctx.usage, usage_limits=ctx.usage_limits
    )


class WriteBooking(Operation[SalonCall, BookingCommand, Saved, str]):
    """Book, move or cancel in the diary, once the caller has authorised it.

    An outstanding earlier write is settled first, because until it is every
    other write is a second appointment waiting to be made. The slot is asked
    of the diary under this call *and* the caller's current identity, never
    read out of the id string: a number corrected mid-call makes the caller
    somebody else, and the slots the previous identity was shown are not theirs.

    The operation token names the action, not the attempt: the call, the
    number of writes it has had accepted, the caller and the arguments. A
    retry after a lost response derives the same one and the diary returns the
    outcome it already recorded. The whole request is kept beside the token,
    because what a recovery has to be checked against cannot be read back out
    of a hash.

    The diary refusing a booking the caller was authorised to make, because
    they already hold one, is recorded as the move-or-add question so the only
    way past it is the caller's answer. A legal-looking receipt is checked
    against the request before anything is committed or said, because both are
    irreversible in front of a caller. The appointment and the step the caller
    lands in commit together, and the confirmation is spoken here in the
    diary's own words rather than handed to a model to say.
    """

    name = 'save_booking'
    args_type = BookingCommand
    receipt_type = Saved

    async def _before(self, call: SalonCall, args: BookingCommand) -> str | None:
        return await _settle(call) if call.state.unresolved else None

    async def _authorise(
        self,
        call: SalonCall,
        args: BookingCommand,
        *,
        usage: RunUsage | None,
        usage_limits: UsageLimits | None,
    ) -> bool:
        """The three ways a caller authorises a write (see `_authorised`); the answer is whether it is an extra booking.

        The reply is read first whenever it could be the authority: a pending
        read-back or move-or-add question, or a time picked from what is on
        the table. The reading is cached per snapshot, so a turn that already
        read the same reply does not pay for it twice.
        """
        if args.action in ('book', 'move') or call.pending('booking') or call.pending('move_or_add'):
            await classify_reply(call, usage=usage, usage_limits=usage_limits)
        additional = _authorised(call, args.action, args.slot_id, args.booking_id)
        if not present(call.state.customer_verified):
            raise ModelRetry('Nothing was saved: nobody is identified yet. Hand to the verify agent.')
        return additional

    async def _execute(self, call: SalonCall, args: BookingCommand, grant: bool) -> Saved:
        action, slot_id, booking_id = args.action, args.slot_id, args.booking_id
        offered = call.diary.offer_of(call.id, slot_id, call.state.customer_phone)
        token = operation_token(
            call.id,
            call.state.writes,
            call.state.customer_phone,
            action,
            slot_id,
            booking_id,
            grant,
        )
        pending = Unresolved(token=token, action=action, booking_id=booking_id, offered=offered)
        try:
            written = _answered(
                call,
                pending,
                Saved,
                lambda: call.diary.save_booking(
                    call.state.customer_phone,
                    action,
                    slot_id,
                    booking_id,
                    grant,
                    call.diary.now(),
                    operation_token=token,
                    offered_to=call.id,
                ),
                'diary',
            )
        except AlreadyHoldsError as refused:
            call.ask(Asked(about='move_or_add', value=slot_id, action='move'))
            raise ModelRetry(str(refused)) from None
        except RefusedError as refused:
            raise ModelRetry(str(refused)) from None
        if why := _disagrees(action, booking_id, offered, written):
            _unknown(call, pending, why)
        return written

    async def _record(self, call: SalonCall, args: BookingCommand, receipt: Saved) -> str:
        appointment = Appointment(
            booking_id=receipt.booking_id,
            service=receipt.service,
            date=receipt.date,
            time=receipt.time,
            spoken=receipt.spoken,
            action=args.action,
        )
        going_back_to = call.resume('concierge')
        call.commit(
            appointment=appointment,
            agent=going_back_to,
            writes=call.state.writes + 1,
            unresolved=None,
            offered=[],
        )
        if args.action in ('book', 'move'):
            call.diary.withdraw_offers(call.id)
        call.retain_action('save_booking', receipt.model_dump(mode='json'))
        told = pick(f'saved:{receipt.status}', SAVED[receipt.status]).format(
            spoken=receipt.spoken,
            service=spoken_service(receipt.service),
        )
        if args.action == 'book':
            call.offer_undo(receipt.booking_id, told)
        await close_with(call, told)
        return f'{receipt.status}: {receipt.spoken}'


WRITE_BOOKING = WriteBooking()


async def write_booking(
    call: SalonCall,
    action: Literal['book', 'move', 'cancel'],
    slot_id: str,
    booking_id: str,
    *,
    usage: RunUsage | None = None,
    usage_limits: UsageLimits | None = None,
) -> str:
    """The write itself, once the caller has authorised it. Shared by the tool and by `advance`."""
    command = BookingCommand(action=action, slot_id=slot_id, booking_id=booking_id)
    return await WRITE_BOOKING.run(call, command, usage=usage, usage_limits=usage_limits)


def _resuming_a_request(call: SalonCall) -> bool:
    """Verified just now, for a booking request the front desk typed, with no question waiting on an answer."""
    return (
        present(call.state.customer_verified)
        and call.state.wanted is not None
        and call.pending_question() is None
    )


async def offer_wanted(call: SalonCall) -> str:
    """Offer times for the request the caller made before they were verified."""
    wanted = call.state.wanted
    assert wanted is not None
    return await offer(call, wanted.when, wanted.service, wanted.at)


OPENINGS = (Opening('offer_times', 'completed', _resuming_a_request, offer_wanted),)
"""The booking step's arrival back from verification: offer times for what the caller already asked for."""
