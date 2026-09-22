"""The booking step's tools: read the diary, offer times, ask for the yes, write.

`save_booking` is the only thing that changes an appointment, and it will not
run on the model's say-so. `_authorised` is what stands between the two: the
caller either agreed to a readback of this exact write, named exactly one of
the times just offered to them, or answered the move-or-add question in their
own words. The model supplies none of those.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic_ai import ModelRetry, RunContext

from core.policies import claim_or_refuse, pick
from core.state import present
from salon.diary import (
    NEGATED_TIME,
    AlreadyHoldsError,
    RefusedError,
    day_phrases,
    resolve_day,
    resolve_time,
    times_named,
)
from salon.models.consent import Asked, agrees, move_or_add
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
    operation_token,
    spoken_service,
)

_SAME_TIME = re.compile(r'\b(?:same|usual)[\s\u2014\u2013-]+time\b', re.IGNORECASE)
""" "The same time" names a time exactly, without naming any digits: the one the caller already holds."""


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
    call = ctx.deps
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
    call.commit(booking_day=found.date)
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
    found = await read_diary(ctx, when, service, at)
    if found.slots:
        times = ', '.join(s.time.strftime('%I:%M %p').lstrip('0') for s in found.slots[:3])
        text = f'For a {spoken_service(service)} on {found.day}, I have {times}. Which time works for you?'
    else:
        text = {
            'need_day': 'Which day would you like?',
            'past': 'That date has already passed. Which future day would you like?',
            'closed': 'The salon is closed that day. Which other day works for you?',
        }.get(found.status, 'There are no available times for that request. Which other day works for you?')
    ctx.deps.answered()
    await ctx.deps.speak_now(text, origin='guidance')
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
        call.ask(question)
        text = 'Would you like to move the appointment you already have, or add another one?'
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
        held = {b['booking_id']: b for b in call.diary.bookings_for(phone, call.diary.now().date())}
        if booking_id not in held:
            raise ModelRetry(
                'Nothing was asked. That is not a booking this caller holds; read the diary to see '
                'what they actually have.'
            )
        one = held[booking_id]
        words = (
            f'So that is your {spoken_service(one["service"])} {one["spoken"]} cancelled. Shall I go ahead?'
        )
    return Ask(about='booking', value=slot_id, action=action, booking_id=booking_id, spoken=words)


def _authorised(call: SalonCall, action: str, slot_id: str, booking_id: str) -> bool:
    """Whether the caller authorised this exact write, and whether it is a second appointment.

    Refuses by raising, so a write that reaches past here has caller evidence
    behind it. Three ways in, and the model supplies none of them:

    * They agreed to an `ask_booking` readback of this same action, slot and
      booking, on the turn before this one.
    * They named a time, and exactly one of the times currently on the table
      is that time. This is the fast path the booking policy is built on: an
      unambiguous choice of an offered slot authorises that slot. Not
      available while they already hold something, because then the
      unanswered question is whether this replaces it. "The same time" names
      one time exactly without naming digits: the one they already hold.
    * They answered the move-or-add question in their own words.

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
        if not agrees(call.heard):
            raise ModelRetry(
                'Nothing was saved: the caller did not agree to what was read back to them. They '
                f'said "{call.heard}". Answer what they actually asked.'
            )
        call.answered()
        return action == 'book' and move_or_add(call.heard) == 'add'

    if action == 'cancel':
        raise ModelRetry(
            'Nothing was cancelled: nobody has agreed to cancel anything. Call `ask_booking` with '
            "action='cancel' and that booking, and wait for their answer."
        )

    if (decision := call.pending('move_or_add', value=slot_id)) is not None:
        match move_or_add(call.heard):
            case 'add':
                call.answered()
                return True
            case 'move':
                call.answered()
                if action != 'move' or not booking_id:
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

    offered = call.diary.offer_of(call.id, slot_id, phone)
    named = times_named(call.heard)
    on_the_table = call.diary.offered_to(call.id, phone)
    at_that_time = [slot for slot in on_the_table if slot['time'] in named]
    picked = bool(offered) and offered['time'] in named and len(at_that_time) == 1
    if action == 'book' and picked and not held:
        return False
    if action == 'move' and offered and move_or_add(call.heard) == 'move' and booking_id:
        moving = next((b for b in held if b['booking_id'] == booking_id), None)
        same = (
            bool(moving)
            and _SAME_TIME.search(call.heard)
            and not NEGATED_TIME.search(call.heard)
            and offered['time'] == moving['time']
        )
        if moving and (picked or same):
            return False

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

    Only after the caller has authorised this exact appointment -- by naming
    one of the times just offered to them, or by agreeing to an `ask_booking`
    readback of it. It speaks the confirmation itself, so say nothing after
    calling it.

    Args:
        action: book a new time, move an existing booking, or cancel one.
        slot_id: The slot exactly as the diary returned it. Empty when cancelling.
        booking_id: The booking exactly as the diary returned it. Empty when booking.
    """
    claim_or_refuse(ctx, ACTIONS)
    return await write_booking(ctx.deps, action, slot_id, booking_id)


async def write_booking(
    call: SalonCall, action: Literal['book', 'move', 'cancel'], slot_id: str, booking_id: str
) -> str:
    """The write itself, once the caller has authorised it. Shared by the tool and by `advance`.

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
    command = BookingCommand(action=action, slot_id=slot_id, booking_id=booking_id)
    action, slot_id, booking_id = command.action, command.slot_id, command.booking_id
    if call.state.unresolved:
        return await _settle(call)
    additional = _authorised(call, action, slot_id, booking_id)
    if not present(call.state.customer_verified):
        raise ModelRetry('Nothing was saved: nobody is identified yet. Hand to the verify agent.')
    offered = call.diary.offer_of(call.id, slot_id, call.state.customer_phone)
    token = operation_token(
        call.id,
        call.state.writes,
        call.state.customer_phone,
        action,
        slot_id,
        booking_id,
        additional,
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
                additional,
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
    appointment = Appointment(
        booking_id=written.booking_id,
        service=written.service,
        date=written.date,
        time=written.time,
        spoken=written.spoken,
        action=action,
    )
    going_back_to = call.resume('concierge')
    call.commit(appointment=appointment, agent=going_back_to, writes=call.state.writes + 1, unresolved=None)
    call.retain_action('save_booking', written.model_dump(mode='json'))
    await call.speak_now(
        pick(f'saved:{written.status}', SAVED[written.status]).format(
            spoken=written.spoken,
            service=spoken_service(written.service),
        )
    )
    return f'{written.status}: {written.spoken}'
