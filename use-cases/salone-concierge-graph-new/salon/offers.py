"""What the salon offers a caller: the free times for their request, and the bookings they hold.

Each is a choice function (`choices:` in `agent.yaml`): it reads the state
and the diary, and returns the ids the caller may pick with the salon's own
words for them. A pick counts only if it was offered, so a model cannot
invent a slot or a booking id. Its `say` is also the step's first line when
the caller arrives there (`offers:`): the offer, or the question for what it
still needs.
"""

from datetime import time

from pydantic import BaseModel

from salon.diary import spoken_day, spoken_time
from salon.operations import Salon, spoken_service
from salon.receipts import Slots
from salon.state import SalonState

OFFER_AT_MOST = 6
"""How many times the agent is shown, nearest the caller's time first. The caller hears up to three of them."""
OFFER_SAID = 3


class Times(BaseModel):
    """The times on offer by slot id, what the agent should know that an id cannot say, and the offer in words."""

    options: dict[str, str] = {}
    note: str = ''
    say: str = ''


def _listed(items: list[str], last: str = 'or') -> str:
    return items[0] if len(items) == 1 else f'{", ".join(items[:-1])} {last} {items[-1]}'


def _on(day: str) -> str:
    """ "On Friday the 2nd", but "Tomorrow" and "Today" on their own."""
    return day.capitalize() if day in ('today', 'tomorrow') else f'On {day}'


def _minutes(at: time) -> int:
    return at.hour * 60 + at.minute


def find_times(salon: Salon, state: SalonState) -> Times:
    """The times the salon can offer for the request in the state: service, date, and time if one was asked for.

    The date is the state's: the model worked it out from the calendar, and
    the read-back confirms it. A closed day, a past day or a full day comes
    back as a note with nothing offered.
    """
    phone = state.customer.customer_phone if state.customer else state.customer_phone or ''
    now = salon.now()
    today = now.date()
    service = state.service or _service_of(salon, phone, state.booking_id)
    if not service:
        return Times(note='No service yet: ask which service.', say='Which service would you like?')
    if state.booking_day is None:
        return Times(note='No day yet: ask which day they would like.', say='Which day would you like?')
    wanted = state.booking_time
    prefer = wanted.strftime('%H:%M') if isinstance(wanted, time) else wanted or ''
    found = Slots.model_validate(
        salon.diary.find_slots(phone, state.booking_day, service, now, offered_to=salon.call, prefer=prefer)
    )
    held = ', '.join(f'{spoken_service(h.service)} {h.spoken} ({h.booking_id})' for h in found.bookings)
    notes = [f'They already hold: {held}.'] if held else []
    day = spoken_day(found.date, today) if found.date else found.day
    if found.status != 'ok':
        note, say = {
            'past': (
                f'{found.day} has already gone past. Ask for a day that has not.',
                f'{found.day} has already gone. Which day would you like?',
            ),
            'closed': (
                f'The salon is closed on {found.day}. Ask for another day.',
                f"We're closed on {found.day}. Which other day would you like?",
            ),
        }.get(
            found.status,
            (
                f'Nothing is free on {found.day}. Ask about another day.',
                f'Nothing is free on {found.day}. Would another day work?',
            ),
        )
        return Times(note=' '.join([*notes, note]), say=say)
    slots = found.slots
    if isinstance(wanted, time):
        slots = sorted(slots, key=lambda s: (abs(_minutes(s.time) - _minutes(wanted)), s.time))
    slots = slots[:OFFER_AT_MOST]
    free = None
    if isinstance(wanted, time):
        free = any(s.time == wanted for s in slots)
        notes.append(f'{wanted:%H:%M} is {"free" if free else "not free"}; the nearest times come first.')
        own = next((h for h in found.bookings if h.date == found.date and h.time == wanted), None)
        if not free and own is not None:
            notes.append(f"{wanted:%H:%M} is their own booking {own.booking_id}, not someone else's.")
    if found.price_euros is not None:
        notes.append(f'The price is {found.price_euros} euros.')
    said = [
        spoken_time(s.time) + ('' if state.service else f' for a {spoken_service(s.service)}') for s in slots
    ]
    return Times(
        options={s.slot_id: f'{spoken_service(s.service)} {s.spoken}' for s in slots},
        note=' '.join(notes),
        say=f'{_on(day)} I have {_listed(said[:1] if free else said[:OFFER_SAID])}. Which time works for you?'
        if said
        else '',
    )


def _service_of(salon: Salon, phone: str, booking_id: str | None) -> str:
    """The service of the booking being moved: a move keeps its service unless the caller changes it."""
    if not booking_id or not phone:
        return ''
    held = salon.diary.bookings_for(phone, salon.now().date())
    return next((h['service'] for h in held if h['booking_id'] == booking_id), '')


def _held(salon: Salon, state: SalonState) -> dict[str, str]:
    """The bookings this verified caller holds, by id, as they are said. Showing them records the revision seen."""
    if state.customer is None:
        return {}
    today = salon.now().date()
    return {
        h['booking_id']: f'a {spoken_service(h["service"])} {h["spoken"]}'
        for h in salon.diary.show_holdings(salon.call, state.customer.customer_phone, today)
    }


NONE_HELD = "I can't find an upcoming booking for this number. Would you like to make a new one?"


def bookings_to_move(salon: Salon, state: SalonState) -> Times:
    """What a move can name, with moving's first line: the booking they hold and the new day, or which one."""
    held = _held(salon, state)
    if not held:
        return Times(note='They hold no upcoming booking.', say=NONE_HELD)
    if len(held) == 1:
        [(_, words)] = held.items()
        return Times(options=held, say=f'You have {words}. Which day would you like to move it to?')
    return Times(
        options=held, say=f'You have {_listed(list(held.values()))}. Which one would you like to move?'
    )


def bookings_to_cancel(salon: Salon, state: SalonState) -> Times:
    """What a cancel can name. One booking has no words: its cancel read-back is the question, asked once."""
    held = _held(salon, state)
    if not held:
        return Times(note='They hold no upcoming booking.', say=NONE_HELD)
    if len(held) == 1:
        return Times(options=held, note='They hold one booking: read its cancel back.')
    return Times(
        options=held, say=f'You have {_listed(list(held.values()))}. Which one would you like to cancel?'
    )
