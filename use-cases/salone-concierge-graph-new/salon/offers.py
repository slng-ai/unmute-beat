"""What the salon offers a caller: the free times for their request, and the bookings they hold.

Each is a choice function (`choices:` in `agent.yaml`): it reads the state
and the diary, and returns the ids the caller may pick with the salon's own
words for them. A pick counts only if it was offered, so a model cannot
invent a slot or a booking id.
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
    if state.booking_day is None:
        return Times(note='No day yet: ask which day they would like.', say='Which day would you like?')
    wanted = state.booking_time
    prefer = wanted.strftime('%H:%M') if isinstance(wanted, time) else wanted or ''
    found = Slots.model_validate(
        salon.diary.find_slots(
            phone, state.booking_day, state.service or '', now, offered_to=salon.call, prefer=prefer
        )
    )
    held = ', '.join(f'{spoken_service(h.service)} {h.spoken} ({h.booking_id})' for h in found.bookings)
    notes = [f'They already hold: {held}.'] if held else []
    day = spoken_day(found.date, today) if found.date else found.day
    if found.status != 'ok':
        notes.append(
            {
                'past': f'{found.day} has already gone past. Ask for a day that has not.',
                'closed': f'The salon is closed on {found.day}. Ask for another day.',
            }.get(found.status, f'Nothing is free on {found.day}. Ask about another day.')
        )
        return Times(note=' '.join(notes))
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
        say=f'{_on(day)} I have {_listed(said[:OFFER_SAID])}.' if said else '',
    )


def held_bookings(salon: Salon, state: SalonState) -> Times:
    """The bookings this verified caller holds, by id, as they are said: what a move or a cancel can name."""
    if state.customer is None:
        return Times()
    today = salon.now().date()
    held = {
        h['booking_id']: f'{spoken_service(h["service"])} {h["spoken"]}'
        for h in salon.diary.show_holdings(salon.call, state.customer.customer_phone, today)
    }
    return Times(options=held)
