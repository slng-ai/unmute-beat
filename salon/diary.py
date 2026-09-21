"""The salon: customers, the diary, and the complaint book.

In memory and seeded, so the harness runs offline and a benchmark run is
repeatable. Swap the module-level dicts for a database and nothing above this
file changes.

Everything a model could get wrong and code can get right is done here: which
day "next Friday" is, whether a slot exists, what a booking sounds like when you
say it out loud. The agents are never asked to compute any of it.

This is also the oracle the whole POC is measured against, so it has to be able
to say no. Four things it could not say no to before, each of which made a flow
test meaningless:

* A slot id was *parsed* to find out what it meant, with `rsplit('-', 2)`. Every
  service whose name contains a hyphen came apart in the wrong place, so a
  `hair-color` slot the diary had itself just offered was refused as one it had
  never returned. Ids are now opaque and looked up.
* Anything of the right shape was accepted, so a time nobody was ever offered --
  03:07, outside opening hours -- booked cleanly. A slot is now only bookable if
  this diary issued it, and its eligibility is re-derived at write time.
* Two appointments collided only if they started at the same minute, though the
  services have durations. A 120-minute colour and a 45-minute cut half an hour
  later were both accepted for one chair.
* A write had no identity, so a caller who lost the response could only be
  retried blind. Every write now takes an operation token and its outcome is
  recallable.

Nothing here is a scheduling platform. One chair, a fixed grid, a fixed clock
passed in by the caller, and a written-down list of the date phrases it
understands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as Date  # noqa: N812
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
    0: (9, 18),
    1: (9, 18),
    2: (9, 18),
    3: (9, 18),
    4: (9, 18),
    5: (9, 16),
    6: None,
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


@dataclass(frozen=True)
class Offer:
    """One slot this diary actually put in front of a caller.

    Held so a slot id never has to be parsed to find out what it means, and so
    a fabricated one is simply absent rather than plausible.
    """

    day: Date
    at: time
    service: str
    issued_on: Date  # the salon day the offer was made; a later day stales it


_customers: set[str] = {'+34600111222'}
_bookings: dict[str, Booking] = {}
_offers: dict[str, Offer] = {}
_operations: dict[str, dict] = {}
_complaints: list[dict] = []
_counter = {'booking': 0, 'complaint': 0}


def reset(customers: set[str] | None = None) -> None:
    """Empty the salon. For tests and for a fresh benchmark run, and only there.

    Never on ordinary call creation. `open_call` used to do exactly that, which
    meant the second caller of the day erased the first one's booking.
    """
    _customers.clear()
    _customers.update({'+34600111222'} if customers is None else customers)
    _bookings.clear()
    _offers.clear()
    _operations.clear()
    _complaints.clear()
    _counter.update(booking=0, complaint=0)


def now() -> datetime:
    return datetime.now(TZ)


# --- saying things out loud --------------------------------------------------


def spoken_time(at: time) -> str:
    """ "9:00 AM", never "09:00": the voice reads the written shape."""
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

# The supported date policy, written down rather than inferred from the code
# that happens to implement it. Anything outside this list is `need_day` and
# the agent asks; it is not guessed. This is a small deliberate policy, not
# language understanding, and calling it the latter is how a caller ends up
# booked on a day nobody said.
TODAY_WORDS = frozenset({'today', 'this morning', 'this afternoon', 'this evening', 'later', 'tonight'})
TOMORROW_WORDS = frozenset({'tomorrow', 'tomorrow morning', 'tomorrow afternoon', 'tomorrow evening'})

_ISO = re.compile(r'^\d{4}-\d{2}-\d{2}$')
# Whole words. A substring match reads "satur" out of the middle of another
# word, and `resolve_day` is the one place a wrong answer is silent: it returns
# a real day, so nothing downstream has any reason to doubt it.
_WEEKDAY = {name: re.compile(rf'\b{name.casefold()}\b') for name in WEEKDAYS}
# Phrases that change which Tuesday is meant, and that this policy does not
# implement. Without them "a week on Tuesday" matched the weekday and returned
# the *next* Tuesday -- a confidently wrong day, seven days early, with nothing
# downstream able to tell. Ask instead: a question costs a turn, a wrong day
# costs an appointment. Anything added to `resolve_day` comes off this list.
_UNHANDLED = re.compile(
    r'\b(week on|weeks?|fortnight|after next|the one after|following|in \d|in (two|three|four))\b'
)


def resolve_day(words: str, today: Date) -> Date | None:
    """The day a caller named, or None when they named none this policy covers.

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
            # "2026-02-30" is the right shape and not a day. Ask, never round.
            return None
    if text in TODAY_WORDS:
        return today
    if text in TOMORROW_WORDS:
        return today + timedelta(days=1)
    if _UNHANDLED.search(text):
        return None
    for index, name in enumerate(WEEKDAYS):
        if _WEEKDAY[name].search(text):
            ahead = (index - today.weekday()) % 7
            # "Monday" said on a Monday means the next one, not this morning.
            ahead = ahead or 7
            # "next Friday" means the week after, when this Friday is still to come.
            if 'next' in text and ahead < 7:
                ahead += 7
            return today + timedelta(days=ahead)
    return None


# --- what the chair can actually take ----------------------------------------


def _interval(day: Date, at: time, service: str) -> tuple[datetime, datetime]:
    """When an appointment starts and ends, on the salon's own clock.

    `tzinfo=` on `combine` resolves the wall time against Europe/Madrid, so the
    two DST days a year are the zone's problem and not this file's. The salon
    opens at 09:00 and Spain changes the clocks at 02:00, so no slot on the
    grid can land in a gap; the arithmetic below is between aware datetimes
    either way.
    """
    start = datetime.combine(day, at, tzinfo=TZ)
    return start, start + timedelta(minutes=SERVICES[service][0])


def _clashes_with(day: Date, at: time, service: str, ignore: str = '') -> Booking | None:
    """The booking this one would sit on top of, if any.

    # ponytail: one service resource -- the salon has one chair, so any overlap
    # is a clash. Per-stylist capacity means a resource on `Booking` and this
    # becoming a per-resource scan; nothing else here changes.

    Intervals, not start times. Comparing starts alone let a 45-minute cut at
    09:30 sit inside a 120-minute colour that began at 09:00, which the diary
    called free and one chair cannot do.
    """
    start, end = _interval(day, at, service)
    for booked in _bookings.values():
        if booked.booking_id == ignore or booked.day != day:
            continue
        other_start, other_end = _interval(booked.day, booked.at, booked.service)
        if start < other_end and other_start < end:
            return booked
    return None


def _eligible(day: Date, at: time, service: str, at_time: datetime, ignore: str = '') -> str:
    """Why this slot cannot be taken, or '' when it can.

    Re-derived from the salon's own rules every time, at read and again at
    write. A slot that was free when it was offered is not free because it was
    offered -- somebody else may have taken the chair in between, and the clock
    has moved on either way.
    """
    if service not in SERVICES:
        return 'That is not a service the salon offers.'
    today = at_time.date()
    if day < today:
        return 'That day has already gone past. Offer one that has not.'
    hours = HOURS[day.weekday()]
    if hours is None:
        return 'The salon is closed that day.'
    opens, closes = hours
    _minutes, _price = SERVICES[service]
    start, end = _interval(day, at, service)
    if at < time(opens) or end > datetime.combine(day, time(closes), tzinfo=TZ):
        return 'The salon is not open then, or that service would not finish before closing.'
    if (at.hour * 60 + at.minute - opens * 60) % GRID_MINUTES:
        return 'Appointments start on the half hour.'
    if start <= at_time:
        return 'That time has already gone past. Offer one that has not.'
    if (clash := _clashes_with(day, at, service, ignore)) is not None:
        return (
            f'Somebody has the chair from {spoken_time(clash.at)} for a {clash.service}. '
            'Read the diary again and offer what is left.'
        )
    return ''


# --- reading the diary -------------------------------------------------------


def _slot_id(day: Date, at: time, service: str) -> str:
    """An identifier this service owns. Readable in a trace, never parsed.

    It used to be taken apart with `rsplit('-', 2)` to recover the day, the
    time and the service, which put the seam in the middle of `hair-color` and
    made every colour booking impossible. Nothing reads this string now except
    `_offers`, so its shape is a convenience and not a contract.
    """
    return f'{day.isoformat()}T{at.strftime("%H%M")}|{service}'


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
    Every slot returned is recorded as an offer, which is the only way one can
    later be booked.
    """
    today = at_time.date()
    held = bookings_for(phone, today)
    day = resolve_day(when, today)
    if day is None:
        return {
            'status': 'need_day',
            'bookings': held,
            'note': 'No day was named, or not in words the diary understands. '
            'Ask the caller which day, and offer a weekday or a date.',
        }
    if day < today:
        return {'status': 'past', 'bookings': held, 'day': spoken_day(day, today)}

    hours = HOURS[day.weekday()]
    if hours is None:
        return {
            'status': 'closed',
            'bookings': held,
            'day': spoken_day(day, today),
            'note': 'The salon is closed on Sunday.',
        }

    wanted = [service] if service in SERVICES else list(SERVICES)
    opens, closes = hours
    slots = []
    for name in wanted:
        minutes, _price = SERVICES[name]
        start = datetime.combine(day, time(opens), tzinfo=TZ)
        last = datetime.combine(day, time(closes), tzinfo=TZ) - timedelta(minutes=minutes)
        while start <= last:
            at = start.time()
            # One rule, asked here and asked again at write time, so a slot can
            # never be offered under one set of checks and written under another.
            if not _eligible(day, at, name, at_time):
                slot_id = _slot_id(day, at, name)
                _offers[slot_id] = Offer(day=day, at=at, service=name, issued_on=today)
                slots.append(
                    {
                        'slot_id': slot_id,
                        'service': name,
                        'time': at.strftime('%H:%M'),
                        'spoken': spoken(day, at, today),
                    }
                )
            start += timedelta(minutes=GRID_MINUTES)

    # Soonest first, across services, and only then capped. Built service by
    # service and capped as built, the twelve places all went to whichever
    # service `SERVICES` happened to list first, so asking for `any` returned
    # haircuts and nothing else -- the diary saying the salon does one thing.
    slots.sort(key=lambda slot: (slot['time'], slot['service']))

    return {
        'status': 'ok' if slots else 'full',
        'day': spoken_day(day, today),
        'date': day.isoformat(),
        'bookings': held,
        'slots': slots[:12],
        'price_euros': SERVICES[service][1] if service in SERVICES else None,
    }


# --- writing to it -----------------------------------------------------------


class RefusedError(Exception):
    """The diary would not take it, and the caller needs to hear why."""


def offered_day(slot_id: str) -> str:
    """The day a slot sits on, as this diary recorded when it offered it.

    '' when this diary never issued that id. Exists so the caller-facing
    stale-day rule can ask instead of reading the day out of the string, which
    is the parsing this module just stopped doing.
    """
    offer = _offers.get(slot_id)
    return offer.day.isoformat() if offer else ''


def outcome_of(operation_token: str) -> dict | None:
    """What a write with this token did, or None if this diary never saw it.

    None is "not started, or unknown", never "it failed". A caller whose
    response went missing asks this before retrying, and a real service that
    could not answer would leave the outcome genuinely unknown rather than
    reporting a failure it cannot prove.
    """
    return _operations.get(operation_token)


def save_booking(
    phone: str,
    action: str,
    slot_id: str,
    booking_id: str,
    additional: bool,
    at_time: datetime,
    operation_token: str = '',
) -> dict:
    """Book, move or cancel. Raises RefusedError with something sayable.

    `operation_token` names the business action rather than the attempt. Sent
    twice with the same token -- a retry after a timeout, a reconciliation
    after a lost response -- the second call returns the first one's outcome
    and writes nothing, so an uncertain write cannot become two bookings.
    """
    if operation_token and (already := _operations.get(operation_token)) is not None:
        return already

    today = at_time.date()
    held = {b['booking_id'] for b in bookings_for(phone, today)}

    if action in ('move', 'cancel') and booking_id not in held:
        raise RefusedError(
            'There is no booking under this number with that reference. '
            f'They hold: {bookings_for(phone, today) or "nothing"}.'
        )
    if action == 'cancel':
        gone = _bookings.pop(booking_id)
        result = {
            'status': 'cancelled',
            'booking_id': booking_id,
            'service': gone.service,
            'date': gone.day.isoformat(),
            'time': gone.at.strftime('%H:%M'),
            'spoken': spoken(gone.day, gone.at, today),
        }
        return _record(operation_token, result)

    # Looked up, never parsed. A slot this diary did not offer is not a slot,
    # whatever it looks like: a fabricated 03:07 is absent here and stops.
    offer = _offers.get(slot_id)
    if offer is None:
        raise RefusedError(
            'That is not a slot this diary returned. Read the diary again and use one of its slots.'
        )
    if offer.issued_on != today:
        raise RefusedError(
            'That slot was offered on an earlier day and the offer has expired. '
            'Read the diary again and offer what it returns now.'
        )
    # Checked again at the moment of writing, against the clock and the diary
    # as they are now. Being in `_offers` says it was free when it was offered.
    if why := _eligible(
        offer.day, offer.at, offer.service, at_time, ignore=booking_id if action == 'move' else ''
    ):
        raise RefusedError(why)

    if action == 'book':
        if held and not additional:
            raise RefusedError(
                f'This caller already holds {bookings_for(phone, today)}. Moving that one is almost '
                'always what they mean; only pass additional when they asked for a second appointment.'
            )
        _counter['booking'] += 1
        booking_id = f'BK{_counter["booking"]:04d}'

    _bookings[booking_id] = Booking(booking_id, phone, offer.service, offer.day, offer.at)
    result = {
        'status': 'booked' if action == 'book' else 'moved',
        'booking_id': booking_id,
        'service': offer.service,
        'date': offer.day.isoformat(),
        'time': offer.at.strftime('%H:%M'),
        'spoken': spoken(offer.day, offer.at, today),
    }
    return _record(operation_token, result)


def _record(operation_token: str, result: dict) -> dict:
    """Remember what this write did, so the same token can only do it once."""
    if operation_token:
        _operations[operation_token] = result
    return result


def find_or_create_customer(phone: str) -> dict:
    existing = phone in _customers
    _customers.add(phone)
    return {'customer_status': 'existing' if existing else 'created', 'customer_phone': phone}


def record_complaint(phone: str, summary: str, requested_resolution: str) -> dict:
    _counter['complaint'] += 1
    reference = f'CX{_counter["complaint"]:04d}'
    _complaints.append(
        {
            'reference': reference,
            'phone': phone,
            'summary': summary,
            'requested_resolution': requested_resolution,
        }
    )
    return {'recorded': True, 'reference': reference}


def complaints() -> list[dict]:
    return list(_complaints)
