"""The salon: customers, the diary, and the complaint book.

In memory and seeded, so the harness runs offline and a benchmark run is
repeatable. Swap the four module-level dicts for a database and nothing above
this file changes.

Everything a model could get wrong and code can get right is done here: which
day "next Friday" is, whether a slot exists, what a booking sounds like when you
say it out loud. The agents are never asked to compute any of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Madrid')

SERVICES: dict[str, tuple[int, int]] = {  # minutes, euros
    'haircut': (45, 28),
    'hair-color': (120, 75),
    'blowout': (30, 22),
}

# Monday..Sunday. None is closed.
HOURS: dict[int, tuple[int, int] | None] = {
    0: (9, 18), 1: (9, 18), 2: (9, 18), 3: (9, 18), 4: (9, 18), 5: (9, 16), 6: None,
}
GRID_MINUTES = 30

WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')


@dataclass
class Booking:
    booking_id: str
    phone: str
    service: str
    day: Date
    at: time


_customers: set[str] = {'+34600111222'}
_bookings: dict[str, Booking] = {}
_complaints: list[dict] = []
_counter = {'booking': 0, 'complaint': 0}


def reset(customers: set[str] | None = None) -> None:
    """Empty the salon. For tests and for a fresh benchmark run."""
    _customers.clear()
    _customers.update({'+34600111222'} if customers is None else customers)
    _bookings.clear()
    _complaints.clear()
    _counter.update(booking=0, complaint=0)


def now() -> datetime:
    return datetime.now(TZ)


# --- saying things out loud --------------------------------------------------


def spoken_time(at: time) -> str:
    """"9:00 AM", never "09:00": the voice reads the written shape."""
    return f'{(at.hour - 1) % 12 + 1}:{at.minute:02d} {"AM" if at.hour < 12 else "PM"}'


def spoken_day(day: Date, today: Date) -> str:
    """The way a person names a day, which depends on how far away it is.

    Computed rather than asked of the model. The original prompt had to spend a
    paragraph on "name a day once, and one way" because the model was writing
    this string; here there is only ever one string to say.
    """
    if day == today:
        return 'today'
    if day == today + timedelta(days=1):
        return 'tomorrow'
    if (day - today).days < 7:
        return WEEKDAYS[day.weekday()]
    return f'{WEEKDAYS[day.weekday()]} the {day.day}'


def spoken(day: Date, at: time, today: Date) -> str:
    return f'{spoken_day(day, today)} at {spoken_time(at)}'


# --- which day did they mean -------------------------------------------------

_ISO = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def resolve_day(words: str, today: Date) -> Date | None:
    """The day a caller named, or None when they named none.

    Code, not the model. Working out that "next Friday" is the 25th is
    arithmetic, and a model doing arithmetic mid-conversation gets it wrong in
    exactly the way nobody notices until somebody misses an appointment.
    """
    text = words.strip().casefold()
    if not text:
        return None
    if _ISO.match(text):
        try:
            return Date.fromisoformat(text)
        except ValueError:
            return None
    if text in ('today', 'this afternoon', 'this morning', 'later', 'tonight'):
        return today
    if text in ('tomorrow', 'tomorrow morning', 'tomorrow afternoon'):
        return today + timedelta(days=1)
    for index, name in enumerate(WEEKDAYS):
        if name.casefold() in text:
            ahead = (index - today.weekday()) % 7
            # "Monday" said on a Monday means the next one, not this morning.
            ahead = ahead or 7
            # "next Friday" means the week after, when this Friday is still to come.
            if 'next' in text and ahead < 7:
                ahead += 7
            return today + timedelta(days=ahead)
    return None


# --- reading the diary -------------------------------------------------------


def _slot_id(day: Date, at: time, service: str) -> str:
    return f'{day.isoformat()}-{at.strftime("%H%M")}-{service}'


def _taken(day: Date) -> set[time]:
    return {b.at for b in _bookings.values() if b.day == day}


def bookings_for(phone: str, today: Date) -> list[dict]:
    """What this caller holds, from today onwards, soonest first."""
    held = sorted(
        (b for b in _bookings.values() if b.phone == phone and b.day >= today),
        key=lambda b: (b.day, b.at),
    )
    return [
        {
            'booking_id': b.booking_id,
            'service': b.service,
            'date': b.day.isoformat(),
            'time': b.at.strftime('%H:%M'),
            'spoken': spoken(b.day, b.at, today),
        }
        for b in held
    ]


def find_slots(phone: str, when: str, service: str, at_time: datetime) -> dict:
    """Free times on the day the caller named, and everything they already hold.

    One call answers both questions, so the agent never needs a second one.
    """
    today = at_time.date()
    held = bookings_for(phone, today)
    day = resolve_day(when, today)
    if day is None:
        return {'status': 'need_day', 'bookings': held,
                'note': 'No day was named. Ask the caller which day before calling again.'}
    if day < today:
        return {'status': 'past', 'bookings': held, 'day': spoken_day(day, today)}

    hours = HOURS[day.weekday()]
    if hours is None:
        return {'status': 'closed', 'bookings': held, 'day': spoken_day(day, today),
                'note': 'The salon is closed on Sunday.'}

    wanted = [service] if service in SERVICES else list(SERVICES)
    opens, closes = hours
    taken = _taken(day)
    slots = []
    for name in wanted:
        minutes, _price = SERVICES[name]
        start = datetime.combine(day, time(opens), tzinfo=TZ)
        last = datetime.combine(day, time(closes), tzinfo=TZ) - timedelta(minutes=minutes)
        while start <= last:
            at = start.time()
            # A slot that has already gone past on the salon clock is not free,
            # however empty the diary looks.
            if at not in taken and start > at_time:
                slots.append({
                    'slot_id': _slot_id(day, at, name),
                    'service': name,
                    'time': at.strftime('%H:%M'),
                    'spoken': spoken(day, at, today),
                })
            start += timedelta(minutes=GRID_MINUTES)

    return {
        'status': 'ok' if slots else 'full',
        'day': spoken_day(day, today),
        'date': day.isoformat(),
        'bookings': held,
        'slots': slots[:12],
        'price_euros': SERVICES[service][1] if service in SERVICES else None,
    }


# --- writing to it -----------------------------------------------------------


class Refused(Exception):
    """The diary would not take it, and the caller needs to hear why."""


def save_booking(
    phone: str, action: str, slot_id: str, booking_id: str, additional: bool, at_time: datetime
) -> dict:
    """Book, move or cancel. Raises Refused with something sayable."""
    today = at_time.date()
    held = {b['booking_id'] for b in bookings_for(phone, today)}

    if action in ('move', 'cancel'):
        if booking_id not in held:
            raise Refused(
                'There is no booking under this number with that reference. '
                f'They hold: {bookings_for(phone, today) or "nothing"}.'
            )
    if action == 'cancel':
        gone = _bookings.pop(booking_id)
        return {'status': 'cancelled', 'booking_id': booking_id, 'service': gone.service,
                'date': gone.day.isoformat(), 'time': gone.at.strftime('%H:%M'),
                'spoken': spoken(gone.day, gone.at, today)}

    try:
        day_text, hhmm, service = slot_id.rsplit('-', 2)
        day, at = Date.fromisoformat(day_text), time(int(hhmm[:2]), int(hhmm[2:]))
        assert service in SERVICES
    except Exception:
        raise Refused('That is not a slot this diary returned. Read the diary again and use one of its slots.') from None
    if day < today or datetime.combine(day, at, tzinfo=TZ) <= at_time:
        raise Refused('That time has already gone past. Offer one that has not.')
    if at in _taken(day) - ({_bookings[booking_id].at} if action == 'move' else set()):
        raise Refused('Somebody took that slot. Read the diary again and offer what is left.')

    if action == 'book':
        if held and not additional:
            raise Refused(
                f'This caller already holds {bookings_for(phone, today)}. Moving that one is almost '
                'always what they mean; only pass additional when they asked for a second appointment.'
            )
        _counter['booking'] += 1
        booking_id = f'BK{_counter["booking"]:04d}'

    _bookings[booking_id] = Booking(booking_id, phone, service, day, at)
    return {'status': 'booked' if action == 'book' else 'moved', 'booking_id': booking_id,
            'service': service, 'date': day.isoformat(), 'time': at.strftime('%H:%M'),
            'spoken': spoken(day, at, today)}


def find_or_create_customer(phone: str) -> dict:
    existing = phone in _customers
    _customers.add(phone)
    return {'customer_status': 'existing' if existing else 'created', 'customer_phone': phone}


def record_complaint(phone: str, summary: str, requested_resolution: str) -> dict:
    _counter['complaint'] += 1
    reference = f'CX{_counter["complaint"]:04d}'
    _complaints.append({'reference': reference, 'phone': phone, 'summary': summary,
                        'requested_resolution': requested_resolution})
    return {'recorded': True, 'reference': reference}


def complaints() -> list[dict]:
    return list(_complaints)
