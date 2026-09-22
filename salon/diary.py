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

    One caller's, not the salon's. Without `phone` an offer made to whoever
    rang first was bookable by everybody after them, for as long as the
    process lived: "this diary issued that id once" is not the same claim as
    "this caller was offered that time".

    Two things scope it, and they are not the same thing. The *call* is the
    key's first half, because two calls from one number are two
    conversations. `phone` is who that conversation had turned out to be
    about when the offer was made, and it is checked separately: a caller who
    corrects their number mid-call becomes somebody else, and slots put in
    front of the previous identity are not theirs. This field briefly held
    the call id, which made the identity check compare the call against
    itself and always agree.
    """

    day: Date
    at: time
    service: str
    issued_on: Date  # the salon day the offer was made; a later day stales it
    phone: str = ''  # the identity the offer was made under, '' for nobody in particular


_customers: set[str] = {'+34600111222'}
_bookings: dict[str, Booking] = {}
# Keyed by the caller as well as the slot: two callers can be offered the same
# time, and one dictionary keyed by slot alone let the second offer overwrite
# the first -- so the caller who was offered it first could no longer book it.
_offers: dict[tuple[str, str], Offer] = {}
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


_clock: datetime | None = None


def set_clock(at: datetime | None) -> None:
    """Pin the salon's clock, or hand it back to the wall.

    For a benchmark and for a fixture, and nowhere else. A scenario run
    against the live clock resolves "Monday" to a different date every week
    and "tomorrow" to a different day every night, so the expected outcome
    cannot be written down -- which is why the benchmark's only check was
    that *a* booking existed. A run nobody can state the right answer for
    cannot be scored, only watched.
    """
    global _clock
    _clock = at


def now() -> datetime:
    return _clock or datetime.now(TZ)


# --- saying things out loud --------------------------------------------------


def spoken_time(at: time) -> str:
    """ "9:00 AM", never "09:00": the voice reads the written shape."""
    return f'{(at.hour - 1) % 12 + 1}:{at.minute:02d} {"AM" if at.hour < 12 else "PM"}'


def _ordinal(day: int) -> str:
    """ "3rd", never "3": a bare numeral is read out as "the three"."""
    if 11 <= day <= 13:
        return f'{day}th'
    return f'{day}{ {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th") }'


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
    return f'{WEEKDAYS[day.weekday()]} the {_ordinal(day.day)}'


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


# The caller's own day words, lifted out of a whole utterance. `resolve_day`
# takes a phrase somebody hands it; this finds the phrase in what was actually
# said, so the two can be compared. "this"/"next" are captured because they are
# the whole difference between two Fridays a week apart.
_PHRASE = re.compile(
    r'\b((?:this|next)\s+)?(' + '|'.join(w.casefold() for w in WEEKDAYS) + r')\b'
    r'|\b(today|tomorrow)\b'
    r'|\b(\d{4}-\d{2}-\d{2})\b',
    re.IGNORECASE,
)


def day_phrases(heard: str) -> list[str]:
    """Every day the caller named in this utterance, in their own words.

    Kept beside `resolve_day` because they are two halves of one rule: this
    says what was said, that says which day it is. On the live call of
    2026-09-21 12:58 the caller said "Friday" and the diary was asked for
    "next Friday" -- a week out under that clock, and booked. The resolver was
    right; it was given the wrong words.

    All of them, not the first: "Monday, actually make it Tuesday" is one
    utterance naming two days, and both are days the caller said.

    ponytail: it does not read negation. "Not Monday, Tuesday" yields both, so
    a lookup for Monday is permitted where it should be questioned. This is
    the guard against a day the caller never mentioned, which is what the live
    call got wrong; deciding which of two named days they meant is the
    model's, and the readback before the write is where it is caught.
    """
    found = []
    for hit in _PHRASE.finditer(heard):
        if hit.group(2):
            found.append(f'{(hit.group(1) or "").strip()} {hit.group(2)}'.strip().casefold())
        else:
            found.append((hit.group(3) or hit.group(4) or '').casefold())
    return found


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


# The earliest and latest an appointment here can start, from `HOURS`. What
# makes an unqualified hour unambiguous: "three" is 15:00 because nothing is
# open at three in the morning.
_EARLIEST = f'{min(h[0] for h in HOURS.values() if h):02d}:00'
_LATEST = f'{max(h[1] for h in HOURS.values() if h):02d}:00'

# How many free times one answer offers. Twelve is what a person can be read
# without losing track, and it is also why `prefer` has to be applied before
# it: whichever dozen survive the cut are the whole of what the caller hears.
SHOW_AT_MOST = 12

_DAYPARTS = {
    'morning': (time(0, 0), time(11, 59)),
    'afternoon': (time(12, 0), time(16, 59)),
    'evening': (time(17, 0), time(23, 59)),
}


_HOURS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6,
    'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12,
}  # fmt: skip
_HOUR_WORD = '|'.join(_HOURS)
_OCLOCK_WORD = r"o\s?'?\s?clock"
_CLOCK = re.compile(
    rf'\b(2[0-3]|1[0-9]|0?[1-9]|0|{_HOUR_WORD})'
    rf'(?::([0-5]\d)|\s*{_OCLOCK_WORD})?\s*([ap]\.?m\.?)?\b',
    re.IGNORECASE,
)
_NEGATED_TIME = re.compile(r"\b(?:not|no|don't|instead|or|maybe|either|except)\b", re.IGNORECASE)


def times_named(heard: str) -> set[str]:
    """Read an explicit time or a short selection; offers resolve AM/PM ambiguity.

    Reject negative or alternative selections. Bare hours must be the whole
    answer (possibly with 'please') or follow a time preposition, so dates and
    party sizes cannot authorize a booking.
    """
    if _NEGATED_TIME.search(heard):
        return set()
    found: set[str] = set()
    for hit in _CLOCK.finditer(heard):
        word, minute, period = hit.groups()
        hour = _HOURS.get(word.lower(), int(word) if word.isdigit() else 0)
        prefix, suffix = heard[: hit.start()].strip(), heard[hit.end() :].strip(' .,!?')
        explicit = ':' in hit.group() or 'clock' in hit.group().lower() or period
        half = bool(re.search(r'half past$', prefix, re.IGNORECASE))
        bare = (not prefix and suffix.lower() in ('', 'please', 'works', 'works for me')) or bool(
            re.search(r'\b(?:at|around|about)$', prefix, re.IGNORECASE)
        )
        if not (explicit or half or bare):
            continue
        minute = minute or ('30' if half else '00')
        if period:
            if not 1 <= hour <= 12:
                continue
            hours = [hour % 12 + (12 if period.lower().startswith('p') else 0)]
        else:
            hours = [hour % 12, hour % 12 + 12] if 1 <= hour <= 12 else [hour]
        found.update(f'{h:02d}:{minute}' for h in hours)
    return found


def resolve_time(words: str) -> str:
    """The time or part of day a caller named, as `HH:MM` or a daypart, or ''.

    Code, like `resolve_day`, and for the same reason. It reads the salon's
    own hours: an unqualified "three" is 15:00 here because the salon is shut
    at three in the morning, and "nine" is 09:00 because it is shut at nine at
    night. Ambiguity that survives that is returned as nothing, and the diary
    answers the whole day.
    """
    text = words.strip().casefold()
    if not text:
        return ''
    for part in _DAYPARTS:
        if part in text:
            return part
    # A looser reading than `times_named`, which will not take a bare number
    # without "at" in front of it. That caution is for authorising a booking,
    # where a wrong reading books something; here the worst a wrong reading
    # does is order a list of free times badly.
    found = times_named(text)
    named = sorted(t for t in found if _EARLIEST <= t <= _LATEST)
    return named[0] if len(named) == 1 else ''


def _preferred(slots: list[dict], prefer: str) -> list[dict]:
    """The caller's preference first, before the list is cut down to what fits.

    A daypart filters: asking for the afternoon and being read the morning is
    not an answer. An exact time ranks, nearest first, because the time they
    asked for may be taken and the two either side of it are the useful
    reply -- filtering there would turn "three is gone" into "nothing is
    free". A preference nothing matches is ignored rather than obeyed into an
    empty answer.
    """
    if not prefer:
        return slots
    if window := _DAYPARTS.get(prefer):
        opens, closes = window
        return [slot for slot in slots if opens <= time.fromisoformat(slot['time']) <= closes] or slots
    try:
        wanted = time.fromisoformat(prefer)
    except ValueError:
        return slots

    def distance(slot: dict) -> tuple[int, str]:
        at = time.fromisoformat(slot['time'])
        minutes = abs((at.hour * 60 + at.minute) - (wanted.hour * 60 + wanted.minute))
        return minutes, slot['time']

    return sorted(slots, key=distance)


def find_slots(
    phone: str, when: str, service: str, at_time: datetime, offered_to: str = '', prefer: str = ''
) -> dict:
    """Free times on the day the caller named, and everything they already hold.

    One call answers both questions, so the agent never needs a second one.
    Every slot **returned** is recorded as an offer, which is the only way one
    can later be booked -- and only the returned ones. Registering every
    eligible slot and then returning the first twelve meant an id could be
    accepted at write time that this caller was never shown: "issued
    somewhere in the process" standing in for "currently offered to you".

    Asking again **replaces** what was offered: this search is the question
    now on the table, and the last one is over. Without that, reading Friday
    for a haircut and then Friday for a colour left both batches standing, so
    the caller could be booked for the service they had just corrected --
    same date, so no date check could see it. An offer belongs to one
    question, not to a day.

    `offered_to` is who the offer belongs to, and it is the *call* rather than
    the number: two calls from one phone are two conversations, and a slot put
    in front of one of them was never put in front of the other. It defaults
    to the number so a direct caller -- a test, a fixture -- gets its own
    namespace without inventing an identity.
    """
    who = offered_to or phone
    # Whatever was on the table is off it, whichever way this search goes: a
    # day with nothing free, a day the salon is shut and a day nobody could
    # work out are all the previous offer being withdrawn.
    for key in [k for k in _offers if k[0] == who]:
        del _offers[key]
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
    candidates: dict[str, Offer] = {}
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
                # `phone`, never `who`: the identity, not the conversation.
                candidates[slot_id] = Offer(day=day, at=at, service=name, issued_on=today, phone=phone)
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
    # And the caller's preference before the cap, not after it. `slots[:12]`
    # on a day whose morning is free returns 09:00 to 14:30, so a caller
    # asking for three o'clock was told there was nothing at three on a day
    # with an empty afternoon: the twelve places had gone to times nobody
    # asked about. Reproduced on all four live calls of 2026-09-21.
    # Chronological again after the cut: the preference decides *which* dozen,
    # and a dozen times are read out in the order of a day.
    shown = sorted(_preferred(slots, prefer)[:SHOW_AT_MOST], key=lambda s: (s['time'], s['service']))
    # Recorded after the cap, not before it: an offer is a thing this caller
    # was told about.
    for slot in shown:
        _offers[who, slot['slot_id']] = candidates[slot['slot_id']]

    return {
        'status': 'ok' if slots else 'full',
        'day': spoken_day(day, today),
        'date': day.isoformat(),
        'bookings': held,
        'slots': shown,
        'price_euros': SERVICES[service][1] if service in SERVICES else None,
    }


# --- writing to it -----------------------------------------------------------


class RefusedError(Exception):
    """The diary would not take it, and the caller needs to hear why."""


class AlreadyHoldsError(RefusedError):
    """A booking refused because this caller already has one.

    Its own type because it is the one refusal with a way past it. The
    wording names `additional`, and on the live calls of 2026-09-21 the model
    read that as the retry knob: refused with `additional=False`, sent again
    one second later with `additional=True`, same slot, and the caller who
    asked for one appointment got two. The caller-facing rules need to tell
    that refusal from the others, and matching on the message text is not a
    way to do it.
    """


def _offer_for(offered_to: str, slot_id: str, phone: str) -> Offer | None:
    """The offer this call holds under that id, if it is still this caller's.

    The one place an offer is matched, so the read and the write cannot
    disagree about what counts. An offer made before anybody was identified
    carries no identity and stays bookable by whoever this call turns out to
    be; one made under a number is void the moment that number changes.
    """
    offer = _offers.get((offered_to, slot_id))
    if offer is None or (offer.phone and phone and offer.phone != phone):
        return None
    return offer


def offer_of(offered_to: str, slot_id: str, phone: str = '') -> dict:
    """What this caller was offered under that id: the day, the time and the service.

    Empty when this diary never offered it to them, or offered it to who they
    were before they corrected their number. Exists so the caller-facing
    rules can *ask* what a slot means instead of reading it out of the
    string, which is the parsing this module stopped doing -- and so a result
    can be checked against the slot it claims to be about.
    """
    offer = _offer_for(offered_to, slot_id, phone)
    if offer is None:
        return {}
    return {
        'date': offer.day.isoformat(),
        'time': offer.at.strftime('%H:%M'),
        'service': offer.service,
        # The one phrasing of this slot, computed here like every other spoken
        # date, so the question the caller is asked and the confirmation they
        # get afterwards cannot word the same appointment two ways.
        'spoken': spoken(offer.day, offer.at, now().date()),
    }


def offered_to(offered_to: str, phone: str = '') -> list[dict]:
    """Every slot currently on the table for this call, soonest first.

    What "unambiguous" is measured against: a caller naming a time has chosen
    a slot only when exactly one of these is at it. Same matching rule as
    `offer_of`, because an offer the write would refuse is not one the caller
    can have chosen.
    """
    today = now().date()
    live = [
        (slot_id, offer)
        for (who, slot_id), offer in _offers.items()
        if who == offered_to and _offer_for(offered_to, slot_id, phone) is not None
    ]
    live.sort(key=lambda pair: (pair[1].day, pair[1].at, pair[1].service))
    return [
        {
            'slot_id': slot_id,
            'date': offer.day.isoformat(),
            'time': offer.at.strftime('%H:%M'),
            'service': offer.service,
            'spoken': spoken(offer.day, offer.at, today),
        }
        for slot_id, offer in live
    ]


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
    offered_to: str = '',
) -> dict:
    """Book, move or cancel. Raises RefusedError with something sayable.

    `operation_token` names the business action rather than the attempt. Sent
    twice with the same token -- a retry after a timeout, a reconciliation
    after a lost response -- the second call returns the first one's outcome
    and writes nothing, so an uncertain write cannot become two bookings.
    """
    if operation_token and (already := _operations.get(operation_token)) is not None:
        return already

    who = offered_to or phone
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
    # This caller's own offers, never the salon's. A slot id is the same
    # string whoever it was shown to, so a single dictionary keyed by the id
    # made every offer bookable by everybody for as long as the process lived.
    offer = _offer_for(who, slot_id, phone)
    if offer is None:
        raise RefusedError(
            'That is not a slot this diary is currently offering this caller. Read the diary again '
            'and use one of the slots it returns now.'
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
            raise AlreadyHoldsError(
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


def record_complaint(phone: str, summary: str, requested_resolution: str, operation_token: str = '') -> dict:
    """Write a complaint down. Twice with one token writes it once.

    The same identity a booking write has, and for the same reason: a caller
    whose response went missing is retried, and a complaint book with the
    same grievance in it twice is a caller who gets phoned about it twice.
    """
    if operation_token and (already := _operations.get(operation_token)) is not None:
        return already

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
    return _record(operation_token, {'recorded': True, 'reference': reference})


def complaints() -> list[dict]:
    return list(_complaints)
