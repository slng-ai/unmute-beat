"""The booking step's tools: read the diary, offer times, ask for the yes, write.

`save_booking` is the only thing that changes an appointment, and it will not
run on the model's say-so. `_authorised` is what stands between the two: the
caller either agreed to a readback of this exact write, or answered the
move-or-add question in their own words. A plain pick of an offered time is
read back first. The model supplies none of those; the readings interpret the
reply, and this code checks it against what was on the table.

The step's opening is here too: resumed after verification with the
request the caller made at the front desk, it offers times for it without a
model re-reading that request, because the words are already typed.
"""

from __future__ import annotations

from typing import Literal

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.usage import RunUsage, UsageLimits

from core.graph import Opening
from core.operations import Operation, operation_token
from core.policies import claim_or_refuse, pick
from core.state import present
from salon.diary import (
    AlreadyHoldsError,
    RefusedError,
    day_phrases,
    names_a_day,
    resolve_day,
    resolve_time,
    spoken_day,
    times_named,
)
from salon.models.consent import Asked
from salon.models.receipts import BookingCommand, Held, Saved, Slots
from salon.models.shapes import Date, Service
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
            "Saturday next week", "October the 3rd" -- or its date as
            YYYY-MM-DD from the calendar in your instructions, or an empty
            string if they have not named one.
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

    A day the caller named that the diary cannot work out goes back to the
    model once a turn, with the reason: it has the calendar (`core.clock`)
    and can pass the date. In trace bbe42ad4 the offer spoke "Which day would
    you like?" straight to a caller who had just named one, twice, and the
    model never learned why.
    """
    if not present(call.state.customer_verified):
        raise ModelRetry(
            'The diary is read under a confirmed phone number and nobody is identified yet. Hand to the verify agent.'
        )
    theirs = _day_they_said(call, when)
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
    if found.status == 'need_day' and names_a_day(when) and call._day_retried != call.turn:
        call._day_retried = call.turn
        raise ModelRetry(
            f'Nothing was read: the diary could not work out which day "{when}" is. Find that day in the '
            'calendar in your instructions and call this again with its date as YYYY-MM-DD. If it is not '
            'there, ask the caller which date they mean.'
        )
    call._said_day = ''
    if theirs is not None and found.date is not None and found.date != theirs:
        call._said_day = spoken_day(theirs, call.diary.now().date())
        contrast = f'The caller said {call._said_day}; this is {found.day}. Say both when you offer it.'
        found = found.model_copy(update={'note': f'{found.note} {contrast}'.strip()})
    call.commit(booking_day=found.date, offered=[], wanted=None)
    return found


def _day_they_said(call: SalonCall, when: str) -> Date | None:
    """The day the caller named, when this lookup is for another one. None when it is theirs, or cannot be told.

    Live call N2: the caller said "Friday", the model looked up "next Friday",
    and the booking landed a week late. The lookup used to be refused. Now the
    model has the calendar and works days out from context ("the day after
    the one you proposed"), and in trace 9195116 the refusal sent three right
    dates back until the caller said "Saturday the 3rd". So the lookup goes
    ahead, and the offer says both days: "That's Saturday the 3rd, not
    tomorrow". Heard side by side, a week's difference is not missed the way
    one date on its own was.

    The last of the caller's days that differs is the one named. A day they
    named that the code cannot work out ("in two weeks on Tuesday") could be
    the one the model's date is for, so then nothing is said.
    """
    if not (theirs := call.day_words) or not (mine := day_phrases(when)):
        return None
    today = call.diary.now().date()
    wanted = [resolve_day(words, today) for words in theirs]
    asked_for = resolve_day(mine[-1], today)
    if None in wanted or asked_for is None or asked_for in wanted:
        return None
    return wanted[-1]


async def offer_times(
    ctx: RunContext[SalonCall],
    when: str,
    service: Service,
    at: str = '',
) -> str:
    """Read availability and speak up to three real choices, ending this reply.

    Use when the caller needs to choose a time. For a same-time move or a
    time they already picked plainly, use read_diary then ask_booking instead.

    Args:
        when: The caller's own day words, or the day's date as YYYY-MM-DD from the calendar in your instructions.
        service: The requested service.
        at: The caller's time or daypart, if given.
    """
    claim_or_refuse(ctx, ACTIONS)
    return await offer(ctx.deps, when, service, at)


_OFFERED = 3
"""How many times one offer speaks. More is a list nobody on a phone remembers."""


async def offer(
    call: SalonCall, when: str, service: Service, at: str = '', *, move: str = '', found: Slots | None = None
) -> str:
    """Read the diary and speak the first few free times. Shared by `offer_times` and the booking step's opening.

    What was spoken is recorded, ids and all, so the turn the caller picks one
    can go straight to its read-back rather than re-reading the diary to find
    the id of a time they have already been told. The offer is also the
    question the caller's next reply answers: the consent reading names which
    of these ids they picked, and only an id offered here can count.

    `move` is the booking the caller asked to move, so a pick of one of these
    times is read back as that move (`salon.classification._picked`), not
    asked about again. `found` is a diary read already made for this request.
    """
    if found is None:
        found = await find_times(call, when, service, at)
    spoken = found.slots[:_OFFERED]
    if spoken:
        times = ', '.join(s.time.strftime('%I:%M %p').lstrip('0') for s in spoken)
        day = found.day if found.day in ('today', 'tomorrow') else f'on {found.day}'
        text = f'For a {spoken_service(service)} {day}, I have {times}. Which time works for you?'
        if call._said_day:
            text = f"That's {found.day}, not {call._said_day}: for a {spoken_service(service)}, I have {times}. Which time works for you?"
    else:
        text = {
            'need_day': 'Sorry, which date do you mean?'
            if names_a_day(when)
            else 'Which day would you like?',
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
                action='move' if move else '',
                booking_id=move,
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
    call = ctx.deps
    names = call.conversation.offer('chosen_offer').named
    if action in ('book', 'move') and (names or _names_the_time(call, slot_id)):
        await call.read_reply(usage=ctx.usage, usage_limits=ctx.usage_limits)
    if action in ('book', 'move') and _named_but_not_picked(call, slot_id):
        raise ModelRetry(
            f'Nothing was asked: the caller named that time but did not simply pick it. They said '
            f'"{call.heard}". Answer their question or condition first; read it back once they pick it plainly.'
        )
    return booking_question(call, action, slot_id, booking_id)


def _named_but_not_picked(call: SalonCall, slot_id: str) -> bool:
    """The caller named this slot's time, and the pick reading still found no plain pick: a condition or a doubt.

    Coval run L5RWCS7b, K5: "09:00. But only if it costs less than 30 euros"
    read as no pick, and the booking model read 9:00 back anyway, leaving the
    price question unanswered. A yes to a time the model proposed in its own
    words names no time, and is read back as before. `times_named` only ever
    refuses here; it never authorises anything.

    With the named pick on (`salon.classification.NAMED_PICK`), the reading
    itself says which option was named that way (`Offer.refuses`), in any
    language and with no time said: "the first one, if it is under 30 euros".
    The word list stays as a second check. It can only refuse, so keeping it
    costs nothing but English-only coverage.
    """
    offer = call.conversation.offer('chosen_offer')
    if offer.named and offer.refuses(call, slot_id):
        return True
    return _names_the_time(call, slot_id) and offer.reading.ran(call) and not call.picked_offer()


def _names_the_time(call: SalonCall, slot_id: str) -> bool:
    offered = call.diary.offer_of(call.id, slot_id, call.state.customer_phone)
    return bool(offered) and offered['time'] in times_named(call.heard)


def booking_question(
    call: SalonCall, action: Literal['book', 'move', 'cancel'], slot_id: str, booking_id: str
) -> Ask:
    """The read-back of one booking, move or cancel, worded from the diary's own record.

    Shared by `ask_booking` and by the pick of an offered time (`salon.nodes`),
    which is read back like any other proposal: the caller's yes to it is what
    writes, never the pick.

    It asks from its first word. "So that is a hair colour, tomorrow at 9 AM"
    sounds like a confirmation, and in Coval run RPH7TzmF (K5) the caller who
    had picked a time and said goodbye took it as one and hung up with nothing
    booked. A goodbye read this turn is answered before the question.

    To a caller who already holds a booking, a booking is read back as a
    second appointment, and the yes to it is the yes to that (`Asked.additional`).
    In trace fbdd81b6 a plain "Shall I book you in for a haircut, Friday at
    11:00?" went to a caller holding the 9:00. The yes could never be
    carried out: the diary refused a second booking nobody had asked for,
    and the caller heard "going round in circles".

    Raises:
        ModelRetry: If the slot is not one this caller is being offered, or the booking not one they hold.
    """
    phone = call.state.customer_phone
    held: list[dict] = []
    if action in ('book', 'move'):
        offered = call.diary.offer_of(call.id, slot_id, phone)
        if not offered:
            raise ModelRetry(
                'Nothing was asked. That slot is not one this caller is currently being offered; '
                'read the diary again and offer a time it actually returned.'
            )
        service = spoken_service(offered['service'])
        held = call.diary.bookings_for(phone, call.diary.now().date()) if action == 'book' else []
        if len(held) == 1:
            also = f'your {spoken_service(held[0]["service"])} {held[0]["spoken"]}'
        else:
            also = 'the appointments you already have'
        if action == 'move':
            words = f'Shall I move your appointment to {offered["spoken"]}?'
        elif held:
            words = (
                f'Shall I book you a second appointment, a {service} {offered["spoken"]}, as well as {also}?'
            )
        else:
            words = f'Shall I book you in for a {service}, {offered["spoken"]}?'
        if call.classification.conversation == 'end':
            words = f'Before you go: {words[0].lower()}{words[1:]}'
    else:
        return cancel_question(call, booking_id)
    return Ask(
        about='booking',
        value=slot_id,
        action=action,
        booking_id=booking_id,
        additional=bool(held),
        spoken=words,
    )


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
    pick is now read back (`salon.classification._picked`) and only the yes writes.

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
        additional = action == 'book' and (
            question.additional or call.classification.appointment_choice == 'add'
        )
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
            await call.read_reply(usage=usage, usage_limits=usage_limits)
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


def _handed_a_request(call: SalonCall) -> bool:
    """Handed over by the front desk with a request typed from the caller's words, the caller already verified."""
    return present(call.state.customer_verified) and call.state.wanted is not None


def _only_booking(call: SalonCall) -> str:
    """The one booking this caller holds, by id, or '' when they hold none or more than one."""
    held = call.diary.bookings_for(call.state.customer_phone, call.diary.now().date())
    return held[0]['booking_id'] if len(held) == 1 else ''


async def offer_wanted(call: SalonCall) -> str | Ask:
    """Act on the request the front desk carried: offer times, or read a move back when its time is free.

    A move of the one booking the caller holds, to a time the diary has free
    exactly as they named it, is read back at once: "Shall I move your
    appointment to Saturday at 9:00 AM?". Otherwise the free times are
    offered, marked as a move, so the time they pick is read back as one.
    The user's call of 24 September spent six requests in a row on "same
    time" after "move it to Saturday"; this is none.
    """
    wanted = call.state.wanted
    assert wanted is not None
    move = _only_booking(call) if wanted.change == 'move' else ''
    if not move:
        return await offer(call, wanted.when, wanted.service, wanted.at)
    found = await find_times(call, wanted.when, wanted.service, wanted.at)
    named = resolve_time(wanted.at)
    exact = next((slot for slot in found.slots if slot.time.strftime('%H:%M') == named), None)
    if exact is not None:
        return booking_question(call, 'move', exact.slot_id, move)
    return await offer(call, wanted.when, wanted.service, wanted.at, move=move, found=found)


OPENINGS = (
    Opening('offer_times', 'completed', _resuming_a_request, offer_wanted),
    Opening('offer_times', 'handoff', _handed_a_request, offer_wanted),
)
"""The booking step's arrival with a request already known: back from verification, or handed over by the front desk."""
