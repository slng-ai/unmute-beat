"""The salon: customers, the diary, and the complaint book.

`Diary` is the business, seeded, on one SQLite file that every worker process
shares, so two calls on two workers contend for the same chair and a booking
survives the process that made it. Nothing above this file knows it is a
database. One file is the salon; a `Diary()` with no path is a fixture.

Everything a model could get wrong and code can get right is done here: which
day "next Friday" is, whether a slot exists, what a booking sounds like when you
say it out loud. The agents are never asked to compute any of it.

This is also the oracle the agency is measured against, so it has to be able to
say no. Slot ids are opaque and looked up, never parsed; a slot is bookable only
if this diary issued it to this caller, and its eligibility is re-derived at
write time; appointments have durations, so two of them clash when their
intervals overlap; and every write takes an operation token so a lost response
can be reconciled instead of retried blind.

Nothing here is a scheduling platform. One chair, a fixed grid, a clock passed
in by the caller, and a written-down list of the date phrases it understands.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time as time_module
import weakref
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date as Date  # noqa: N812
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Madrid')

SERVICES: dict[str, tuple[int, int]] = {
    'haircut': (45, 28),
    'hair-color': (120, 75),
    'blowout': (30, 22),
}
"""Each service's duration in minutes and price in euros."""

HOURS: dict[int, tuple[int, int] | None] = {
    0: (9, 18),
    1: (9, 18),
    2: (9, 18),
    3: (9, 18),
    4: (9, 18),
    5: (9, 16),
    6: None,
}
"""Opening and closing hour by weekday, Monday first. `None` is closed."""

GRID_MINUTES = 30
WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
SEED_CUSTOMERS = frozenset({'+34600111222'})
"""The one customer every fresh diary knows, so a scripted call can be an existing one."""

SHOW_AT_MOST = 12
"""How many free times one answer offers.

Twelve is what a person can be read without losing track. It is also why a
preference has to be applied before the cut: whichever dozen survive are the
whole of what the caller hears.
"""


@dataclass
class Booking:
    booking_id: str
    phone: str
    service: str
    day: Date
    at: time
    revision: int = 1


@dataclass(frozen=True)
class Offer:
    """One slot this diary actually put in front of a caller.

    Held so a slot id never has to be parsed to find out what it means, and so
    a fabricated one is simply absent rather than plausible.

    Two things scope it, and they are not the same thing. The *call* is the
    offers key's first half, because two calls from one number are two
    conversations. `phone` is who that conversation had turned out to be about
    when the offer was made, and it is checked separately: a caller who
    corrects their number mid-call becomes somebody else, and slots put in
    front of the previous identity are not theirs.
    """

    day: Date
    at: time
    service: str
    issued_on: Date
    """The salon day the offer was made; a later day stales it."""
    phone: str = ''
    """The identity the offer was made under, '' for nobody in particular."""


class RefusedError(Exception):
    """The diary would not take it, and the caller needs to hear why."""


class AlreadyHoldsError(RefusedError):
    """A booking refused because this caller already has one.

    Its own type because it is the one refusal with a way past it, and the
    caller-facing rules need to tell it from the others without matching on
    the message text.
    """


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

    Computed rather than asked of the model, so there is only ever one string
    to say for a given day. Past tomorrow a day always carries its date, so
    "Saturday the 26th" and "Saturday the 3rd" can never sound alike: in
    trace bbe42ad4 the caller had to say "not this Saturday".
    """
    if day == today:
        return 'today'
    if day == today + timedelta(days=1):
        return 'tomorrow'
    return f'{WEEKDAYS[day.weekday()]} the {_ordinal(day.day)}'


def spoken(day: Date, at: time, today: Date) -> str:
    return f'{spoken_day(day, today)} at {spoken_time(at)}'


TODAY_WORDS = frozenset({'today', 'this morning', 'this afternoon', 'this evening', 'later', 'tonight'})
TOMORROW_WORDS = frozenset({'tomorrow', 'tomorrow morning', 'tomorrow afternoon', 'tomorrow evening'})
"""The supported date policy, written down rather than inferred from the code.

Anything outside these lists and the weekday rule below is `need_day` and the
agent asks; it is never guessed. A small deliberate policy, not language
understanding.
"""

_ISO = re.compile(r'\b(\d{4}-\d{2}-\d{2})\b')
"""A date as YYYY-MM-DD, anywhere in the words: a model passing "Saturday 2026-10-03" means the date."""
_WEEKDAY = {name: re.compile(rf'\b{name.casefold()}\b') for name in WEEKDAYS}
"""Whole words: a substring match would read "satur" out of the middle of another word."""
_DAYNAMES = '|'.join(w.casefold() for w in WEEKDAYS)
MONTHS = (
    'january',
    'february',
    'march',
    'april',
    'may',
    'june',
    'july',
    'august',
    'september',
    'october',
    'november',
    'december',
)
_MONTH_NUMBER = (
    {name: n for n, name in enumerate(MONTHS, 1)}
    | {name[:3]: n for n, name in enumerate(MONTHS, 1)}
    | {'sept': 9}
)
_MONTHS = '|'.join(sorted(_MONTH_NUMBER, key=len, reverse=True))
_NTH = r'(\d{1,2})(?:st|nd|rd|th)?'
_MONTH_DAY = re.compile(rf'\b({_MONTHS})\.?\s+(?:the\s+)?{_NTH}\b', re.IGNORECASE)
"""October 3rd, October the 3rd, Oct 3."""
_DAY_MONTH = re.compile(rf'\b(?:the\s+)?{_NTH}\s+(?:of\s+)?({_MONTHS})\b', re.IGNORECASE)
"""The 3rd of October, 3rd October, 3 Oct."""
_NEXT_WEEK = re.compile(
    rf'\b({_DAYNAMES})\s+next\s+week\b|\bnext\s+week\s+(?:on\s+)?({_DAYNAMES})\b', re.IGNORECASE
)
"""Saturday next week, next week on Saturday: that weekday in the Monday-to-Sunday week after this one."""
_WEEK_ON = re.compile(rf'\ba\s+week\s+(?:on|from)\s+({_DAYNAMES})\b', re.IGNORECASE)
"""A week on Saturday: seven days after the coming Saturday."""
_THE_NTH = re.compile(r'\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b', re.IGNORECASE)
"""The 3rd, with no month: the next 3rd from today. It needs "the" and the suffix, so "at 3" is never a day."""
_BESIDE = re.compile(r'\bthe\s+day\s+(after|before)\b(.*)$', re.IGNORECASE)
"""The day after (or before) another day: that day, plus or minus one. With no day after it, it names none."""
_UNHANDLED = re.compile(
    r'\b(week on|weeks?|fortnight|after next|the one after|following|in \d|in (two|three|four))\b'
)
"""Phrases that change which Tuesday is meant and that this policy does not implement.

Without them "a week on Tuesday" matched the weekday and returned the *next*
Tuesday, a confidently wrong day. Asking costs a turn; a wrong day costs an
appointment. What `resolve_day` does implement ("Saturday next week", "a week
on Saturday") is matched before this list is checked.
"""
_AFTER_NEXT = re.compile(rf'\bthe\s+({_DAYNAMES})\s+after\s+next\b', re.IGNORECASE)
"""The Tuesday after next: that weekday in the week after next week."""
_REFUSED = re.compile(
    rf'\bthe\s+following\s+(?:{_DAYNAMES})\b'
    rf'|\bin\s+(?:a\s+fortnight|(?:\d|two|three|four)\s+weeks?)(?:\s+on\s+(?:{_DAYNAMES}))?\b',
    re.IGNORECASE,
)
"""A day named in a form `resolve_day` refuses, captured whole so its weekday is not read on its own.

Without it "in two weeks on Friday" would reach the lookup check as "friday",
and the right date the model passed for it would be refused as a day never said.
"""
_ANCHOR = (
    '|'.join(p.pattern for p in (_MONTH_DAY, _DAY_MONTH, _AFTER_NEXT, _NEXT_WEEK, _WEEK_ON, _THE_NTH))
    + rf'|\b(?:(?:this|next)\s+)?(?:{_DAYNAMES})(?:\s+the\s+\d{{1,2}}(?:st|nd|rd|th))?\b'
    r'|\b(?:today|tomorrow)\b'
    r'|\b\d{4}-\d{2}-\d{2}\b'
)
_PHRASE = re.compile(
    rf'\bthe\s+day\s+(?:after|before)(?:\s+(?:{_ANCHOR}))?'
    r'|\bthe\s+(?:next|following)\s+day\b' + f'|{_REFUSED.pattern}|{_ANCHOR}',
    re.IGNORECASE,
)
"""The caller's own day words, lifted out of a whole utterance, longest form first.

"this"/"next" and "next week" are captured because they are the whole
difference between two Saturdays a week apart. "The day after tomorrow" is
captured whole for the same reason: read as "tomorrow" it is a day early.
A weekday with its date ("Saturday the 3rd") is one day, so one phrase.
"""


def day_phrases(heard: str) -> list[str]:
    """Every day the caller named in this utterance, in their own words.

    Kept beside `resolve_day` because they are two halves of one rule: this
    says what was said, that says which day it is. Comparing the two is how a
    lookup for "next Friday" is refused when the caller said "Friday".

    All of them, not the first: "Monday, actually make it Tuesday" names two
    days, and both are days the caller said.

    ponytail: it does not read negation. "Not Monday, Tuesday" yields both, so
    a lookup for Monday is permitted where it should be questioned. Deciding
    which of two named days they meant is the model's, and the readback before
    the write is where it is caught.
    """
    return [' '.join(hit.group(0).casefold().split()) for hit in _PHRASE.finditer(heard)]


_MONTH_WORD = re.compile(rf'\b({_MONTHS})\b', re.IGNORECASE)


def anchored(words: str, on: Date | None) -> str:
    """A day phrase, with "the day after" and the like fixed to the day the conversation was on when it was said.

    "The day after", "the next day" and "the day before" name a day only
    beside another one. Said while Friday the 2nd is on the table, "the day
    after" is Saturday the 3rd, and stays so when the table moves on. So it is
    kept as that date. With no day on the table it is kept as said, and names
    no day.
    """
    text = ' '.join(words.casefold().split())
    if on is None:
        return words
    if text in ('the day after', 'the next day', 'the following day'):
        return (on + timedelta(days=1)).isoformat()
    if text == 'the day before':
        return (on - timedelta(days=1)).isoformat()
    return words


def names_a_day(words: str) -> bool:
    """Whether these words try to name a day, resolvable or not: a weekday, a month, "next week", "the 3rd".

    "Whenever suits you" names none, so it is simply asked about. "The
    Tuesday after next" names one this policy refuses, so the model is told
    and can pass the date instead.
    """
    return bool(day_phrases(words) or _UNHANDLED.search(words.casefold()) or _MONTH_WORD.search(words))


def _dated(year: int, month: int, day: int) -> Date | None:
    try:
        return Date(year, month, day)
    except ValueError:
        return None


def _on(month: int, day: int, today: Date) -> Date | None:
    """That day of that month, this year, or next year when it has passed. February 30th is None."""
    this_year = _dated(today.year, month, day)
    if this_year is not None and this_year >= today:
        return this_year
    return _dated(today.year + 1, month, day)


def _nth(day: int, today: Date) -> Date | None:
    """The next day-of-month `day` from today: this month's if still to come, else next month's."""
    if day >= today.day and (found := _dated(today.year, today.month, day)) is not None:
        return found
    month, year = (1, today.year + 1) if today.month == 12 else (today.month + 1, today.year)
    return _dated(year, month, day)


def _exact(text: str, today: Date) -> Date | Literal[False] | None:
    """The date these words give exactly, False when they give one that is refused, None when they give none.

    Refused: a date that does not exist ("2026-02-30"), or one beside a
    weekday it does not fall on ("Friday the 3rd", when the 3rd is a
    Saturday). Two days that disagree are asked about, never picked from.
    """
    if hit := _ISO.search(text):
        try:
            day = Date.fromisoformat(hit.group(1))
        except ValueError:
            return False
    elif hit := _MONTH_DAY.search(text):
        day = _on(_MONTH_NUMBER[hit.group(1)], int(hit.group(2)), today)
    elif hit := _DAY_MONTH.search(text):
        day = _on(_MONTH_NUMBER[hit.group(2)], int(hit.group(1)), today)
    elif hit := _THE_NTH.search(text):
        day = _nth(int(hit.group(1)), today)
    else:
        return None
    if day is None or any(
        _WEEKDAY[name].search(text) for name in WEEKDAYS if name != WEEKDAYS[day.weekday()]
    ):
        return False
    return day


def resolve_day(words: str, today: Date) -> Date | None:
    """The day a caller named, or None when they named none this policy covers.

    Code, not the model: working out that "next Friday" is the 25th is
    arithmetic. "Monday" said on a Monday means the next one; "next Friday"
    is the Friday of next week, a Monday-to-Sunday week, the same day the
    calendar each step is shown labels "Friday next week" (`core.clock`);
    "2026-02-30" is the right shape and not a day, so it is refused rather
    than rounded.

    ponytail: "next Monday" said on a Sunday is tomorrow by this rule. Some
    callers mean the Monday after; the offer names the date they hear.

    Trace bbe42ad4 (2026-09-25): "Saturday next week" and "October the 3rd"
    were both outside the policy, and the caller was asked "Which day would
    you like?" twice. Both are in it now: a date by its month ("October the
    3rd", "the 3rd of October"), "the 3rd" alone, "Saturday next week",
    "a week on Saturday" and "the Tuesday after next". "In two weeks", "the
    following Monday" and the like are still refused.

    An exact date decides, wherever it is in the words. In the replay of
    2026-09-25 the model passed "Saturday 2026-10-03", the right day, and the
    weekday rule read it as tomorrow (3 of 8 runs). A weekday beside a date it
    does not fall on is refused. "The day after Friday" is the Saturday, and
    "the day after tomorrow" two days on. "The day after" on its own names no
    day in its words: the model works it out from the calendar.
    """
    text = ' '.join(words.strip().casefold().split())
    if not text:
        return None
    if hit := _BESIDE.search(text):
        rest = hit.group(2).strip(' ,.?!')
        anchor = day_phrases(rest)
        day = (resolve_day(rest, today) if rest else None) or (
            resolve_day(anchor[0], today) if anchor else None
        )
        if day is None:
            return None
        return day + timedelta(days=1 if hit.group(1) == 'after' else -1)
    if text in TODAY_WORDS:
        return today
    if text in TOMORROW_WORDS:
        return today + timedelta(days=1)
    if (exact := _exact(text, today)) is not None:
        return exact or None
    if hit := _AFTER_NEXT.search(text):
        index = WEEKDAYS.index(hit.group(1).capitalize())
        return today + timedelta(days=14 - today.weekday() + index)
    if hit := _NEXT_WEEK.search(text):
        index = WEEKDAYS.index((hit.group(1) or hit.group(2)).capitalize())
        return today + timedelta(days=7 - today.weekday() + index)
    if hit := _WEEK_ON.search(text):
        index = WEEKDAYS.index(hit.group(1).capitalize())
        return today + timedelta(days=((index - today.weekday()) % 7 or 7) + 7)
    if _UNHANDLED.search(text):
        return None
    for index, name in enumerate(WEEKDAYS):
        if _WEEKDAY[name].search(text):
            if 'next' in text:
                return today + timedelta(days=7 - today.weekday() + index)
            return today + timedelta(days=(index - today.weekday()) % 7 or 7)
    return None


_EARLIEST = f'{min(h[0] for h in HOURS.values() if h):02d}:00'
_LATEST = f'{max(h[1] for h in HOURS.values() if h):02d}:00'
"""The earliest and latest an appointment here can start, from `HOURS`.

What makes an unqualified hour unambiguous: "three" is 15:00 because nothing is
open at three in the morning.
"""

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
NEGATED_TIME = re.compile(r"\b(?:not|no|don't|instead|or|maybe|either|except)\b", re.IGNORECASE)
"""Words that make a time in the same utterance a rejection or an alternative, not a choice."""


def times_named(heard: str) -> set[str]:
    """Read an explicit time or a short selection; offers resolve AM/PM ambiguity.

    Negative or alternative selections yield nothing. A bare hour has to be
    the whole answer (possibly with "please") or follow a time preposition, so
    dates and party sizes cannot authorise a booking.
    """
    if NEGATED_TIME.search(heard):
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

    Code, like `resolve_day`, and for the same reason. It reads the salon's own
    hours: an unqualified "three" is 15:00 because the salon is shut at three
    in the morning. Ambiguity that survives that is returned as nothing and the
    diary answers the whole day. A looser reading than `times_named`, because
    the worst a wrong reading does here is order a list of free times badly.
    """
    text = words.strip().casefold()
    if not text:
        return ''
    for part in _DAYPARTS:
        if part in text:
            return part
    found = times_named(text)
    named = sorted(t for t in found if _EARLIEST <= t <= _LATEST)
    return named[0] if len(named) == 1 else ''


def _preferred(slots: list[dict], prefer: str) -> list[dict]:
    """The caller's preference first, before the list is cut down to what fits.

    A daypart filters: asking for the afternoon and being read the morning is
    not an answer. An exact time ranks, nearest first, because the time they
    asked for may be taken and the two either side of it are the useful reply.
    A preference nothing matches is ignored rather than obeyed into an empty
    answer.
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


def _interval(day: Date, at: time, service: str) -> tuple[datetime, datetime]:
    """When an appointment starts and ends, on the salon's own clock.

    `tzinfo=` on `combine` resolves the wall time against Europe/Madrid, so
    the two DST days a year are the zone's problem. The salon opens at 09:00
    and Spain changes the clocks at 02:00, so no slot on the grid can land in
    a gap.
    """
    start = datetime.combine(day, at, tzinfo=TZ)
    return start, start + timedelta(minutes=SERVICES[service][0])


def _slot_id(day: Date, at: time, service: str) -> str:
    """An identifier this service owns. Readable in a trace, never parsed.

    Nothing reads this string except the offers table, so its shape is a
    convenience and not a contract.
    """
    return f'{day.isoformat()}T{at.strftime("%H%M")}|{service}'


def _row(booking_id: str, service: str, day: Date, at: time, today: Date, revision: int = 1) -> dict:
    """One booking as the wire shape every read hands back, with its revision: 1 when made, one more per change."""
    return {
        'booking_id': booking_id,
        'service': service,
        'date': day.isoformat(),
        'time': at.strftime('%H:%M'),
        'spoken': spoken(day, at, today),
        'revision': revision,
    }


CLASH = 'Somebody has the chair from {at} for a {service}. Read the diary again and offer what is left.'


SCHEMA = """
PRAGMA journal_mode=DELETE;
CREATE TABLE IF NOT EXISTS customers (phone TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id TEXT UNIQUE,
    phone TEXT NOT NULL,
    service TEXT NOT NULL,
    day TEXT NOT NULL,
    at TEXT NOT NULL,
    starts INTEGER NOT NULL,
    ends INTEGER NOT NULL,
    resource TEXT NOT NULL DEFAULT 'chair',
    revision INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS bookings_by_phone ON bookings (phone, active);
CREATE INDEX IF NOT EXISTS bookings_by_time ON bookings (resource, active, starts);
CREATE TRIGGER IF NOT EXISTS one_per_chair_insert BEFORE INSERT ON bookings
WHEN NEW.active = 1 AND EXISTS (
    SELECT 1 FROM bookings
    WHERE active = 1 AND resource = NEW.resource AND starts < NEW.ends AND NEW.starts < ends)
BEGIN SELECT RAISE(ABORT, 'clash'); END;
CREATE TRIGGER IF NOT EXISTS one_per_chair_update BEFORE UPDATE OF starts, ends, active, resource ON bookings
WHEN NEW.active = 1 AND EXISTS (
    SELECT 1 FROM bookings
    WHERE active = 1 AND resource = NEW.resource AND id != NEW.id AND starts < NEW.ends AND NEW.starts < ends)
BEGIN SELECT RAISE(ABORT, 'clash'); END;
CREATE TABLE IF NOT EXISTS operations (token TEXT PRIMARY KEY, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intents (
    id TEXT PRIMARY KEY, session TEXT, operation TEXT, args TEXT, subject TEXT);
CREATE TABLE IF NOT EXISTS complaints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reference TEXT UNIQUE,
    phone TEXT,
    summary TEXT,
    requested_resolution TEXT
);
"""
"""The salon's books, as one SQLite file every worker opens.

The chair is the constraint, and the database holds it: a trigger refuses
any active booking whose interval overlaps another on the same resource, on
insert and on update. So two workers that both saw 09:00 free cannot both
book it, and a 45-minute cut cannot sit inside a colour by using another
slot id. A move is one `UPDATE` of its row: it takes the new time and gives
up the old one in one step, or, refused, leaves the booking as it was.

`operations` is the idempotency table: each write's result under its token
(the core `Intent` id), in the same transaction as the write. `intents` is
core's durable record of an intent before dispatch (`note_intent`).

The rollback journal (`journal_mode=DELETE`), not WAL. Every worker process
writes this file, and SQLite before 3.51.3 can corrupt a WAL database when
two processes write or checkpoint at the same instant (the WAL-reset bug,
https://www.sqlite.org/wal.html#walresetbug). This Python ships 3.50.4.
The rollback journal has no WAL to reset; writers still queue on
`BEGIN IMMEDIATE`, readers wait out a commit within the busy timeout, and
`synchronous=FULL` keeps each commit on disk before it returns.

ponytail: rollback journal while the runtime's SQLite is older than 3.51.3
(or the 3.50.7 backport); switch back to WAL, for readers that never wait,
once it is not.
"""

BUSY_SECONDS = 2.0
"""How long a write waits for another worker's transaction before it gives up: a bounded wait, never a queue."""


def create_schema(path: str, schema: str, seconds: float = 10.0) -> None:
    """Create the tables if they are missing, retrying while another process holds the file.

    Two workers starting together both run this. Changing a new file's
    journal mode takes the database's exclusive lock and does not wait on the
    busy timeout, so the second one is told `database is locked` at once. It
    is retried for a bounded time, never forever.
    """
    deadline = time_module.monotonic() + seconds
    while True:
        db = sqlite3.connect(path, timeout=BUSY_SECONDS)
        try:
            db.executescript(schema)
            return
        except sqlite3.OperationalError as error:
            if 'locked' not in str(error) or time_module.monotonic() > deadline:
                raise
        finally:
            db.close()
        time_module.sleep(0.05)


def _minutes(day: Date, at: time, service: str) -> tuple[int, int]:
    """An appointment's start and end, in minutes since the epoch on the salon's own clock."""
    start, end = _interval(day, at, service)
    return int(start.timestamp()) // 60, int(end.timestamp()) // 60


class Diary:
    """One salon's books: who its customers are, what is booked, what was offered, what was complained about.

    Everything that lasts is in one SQLite file (`path`), shared by every
    worker process on the host: customers, bookings, complaints, what each
    write token did, and the intents core records before dispatch. Faro's
    store is the pattern: `synchronous=FULL`, `BEGIN IMMEDIATE` for every
    write, a short connection per operation, and a bounded busy wait; the
    rollback journal rather than WAL, for the reason given at `SCHEMA`.
    Without a `path` the diary is a private temporary file, one per instance:
    a fixture.

    What is offered stays in this process, keyed by the call. A call lives on
    one worker; if that worker dies the call dies with it, and a slot never
    offered to the next call is not one it can book.

    ponytail: offers in process memory. Move them into the file if a call
    ever moves between workers.

    A `clock` pins the salon's idea of now, for a fixture or a demo that has to
    be comparable with another one; without it the wall clock in Madrid is
    used. Every write takes an operation token that names the business action
    rather than the attempt, so sending the same token twice returns the first
    outcome and writes nothing.
    """

    def __init__(
        self,
        customers: Iterable[str] = SEED_CUSTOMERS,
        clock: datetime | None = None,
        path: str | Path | None = None,
    ) -> None:
        if path is None:
            handle, name = tempfile.mkstemp(prefix='salon-', suffix='.db')
            os.close(handle)
            path = name
            weakref.finalize(self, _remove, name)
        self.path = str(path)
        self._seed = frozenset(customers)
        self._offers: dict[tuple[str, str], Offer] = {}
        """Keyed by the caller as well as the slot.

        Two callers can be offered the same time, and one dictionary keyed by
        slot alone let the second offer overwrite the first.
        """
        self._shown: dict[tuple[str, str], int] = {}
        """The revision of each booking a call was shown (`show_holdings`): a move or cancel of it acts on that one."""
        self._lock = threading.Lock()
        """Offers are read and written from the worker threads domain functions run in."""
        self._clock = clock
        create_schema(self.path, SCHEMA)
        with self._tx() as db:
            db.executemany('INSERT OR IGNORE INTO customers VALUES (?)', [(p,) for p in self._seed])

    @contextmanager
    def _tx(self, write: bool = True) -> Iterator[sqlite3.Connection]:
        """One transaction on its own connection: `BEGIN IMMEDIATE` for a write, so writers queue in SQLite."""
        db = sqlite3.connect(self.path, timeout=BUSY_SECONDS, isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    def reset(self, customers: Iterable[str] | None = None) -> None:
        """Empty the salon, back to its seed customers or the ones given. For fixtures that reuse one diary."""
        with self._tx() as db:
            for table in ('customers', 'bookings', 'operations', 'intents', 'complaints'):
                db.execute(f'DELETE FROM {table}')
            db.execute("DELETE FROM sqlite_sequence WHERE name IN ('bookings', 'complaints')")
            db.executemany(
                'INSERT INTO customers VALUES (?)',
                [(p,) for p in (self._seed if customers is None else customers)],
            )
        with self._lock:
            self._offers.clear()
            self._shown.clear()

    def set_clock(self, at: datetime | None) -> None:
        """Pin the salon's clock, or hand it back to the wall.

        A scenario run against the live clock resolves "Monday" to a different
        date every week, so its expected outcome cannot be written down.
        """
        self._clock = at

    def now(self) -> datetime:
        return self._clock or datetime.now(TZ)

    @property
    def customers(self) -> frozenset[str]:
        with self._tx(write=False) as db:
            return frozenset(row[0] for row in db.execute('SELECT phone FROM customers'))

    def _booked(self, db: sqlite3.Connection, where: str = '1', *params: object) -> list[Booking]:
        rows = db.execute(
            f'SELECT booking_id, phone, service, day, at, revision FROM bookings WHERE active = 1 AND {where}',
            params,
        ).fetchall()
        return [
            Booking(b, p, s, Date.fromisoformat(d), time.fromisoformat(a), r) for b, p, s, d, a, r in rows
        ]

    @staticmethod
    def _clashes_with(
        booked: list[Booking], day: Date, at: time, service: str, ignore: str = ''
    ) -> Booking | None:
        """The booking this one would sit on top of, if any: the friendly reason. The trigger is the guarantee.

        Intervals, not start times: comparing starts alone let a 45-minute cut
        at 09:30 sit inside a 120-minute colour that began at 09:00.

        ponytail: one chair, so any overlap is a clash. Per-stylist capacity
        means a resource per service and the trigger comparing it, as it
        already does.
        """
        start, end = _interval(day, at, service)
        for other in booked:
            if other.booking_id == ignore or other.day != day:
                continue
            other_start, other_end = _interval(other.day, other.at, other.service)
            if start < other_end and other_start < end:
                return other
        return None

    def _eligible(
        self, booked: list[Booking], day: Date, at: time, service: str, at_time: datetime, ignore: str = ''
    ) -> str:
        """Why this slot cannot be taken, or '' when it can.

        Re-derived from the salon's own rules at read and again at write. A
        slot that was free when it was offered is not free because it was
        offered: somebody else may have taken the chair in between, and the
        clock has moved on either way.
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
        start, end = _interval(day, at, service)
        if at < time(opens) or end > datetime.combine(day, time(closes), tzinfo=TZ):
            return 'The salon is not open then, or that service would not finish before closing.'
        if (at.hour * 60 + at.minute - opens * 60) % GRID_MINUTES:
            return 'Appointments start on the half hour.'
        if start <= at_time:
            return 'That time has already gone past. Offer one that has not.'
        if (clash := self._clashes_with(booked, day, at, service, ignore)) is not None:
            return CLASH.format(at=spoken_time(clash.at), service=clash.service)
        return ''

    def bookings_for(self, phone: str, today: Date) -> list[dict]:
        """What this caller holds, from today onwards, soonest first."""
        with self._tx(write=False) as db:
            held = self._booked(db, 'phone = ? AND day >= ?', phone, today.isoformat())
        held.sort(key=lambda b: (b.day, b.at))
        return [_row(b.booking_id, b.service, b.day, b.at, today, b.revision) for b in held]

    def show_holdings(self, offered_to: str, phone: str, today: Date) -> list[dict]:
        """What this caller holds, as shown to this call: a move or cancel later acts on these revisions only."""
        held = self.bookings_for(phone, today)
        with self._lock:
            for row in held:
                self._shown[offered_to, row['booking_id']] = row['revision']
        return held

    def shown_revision(self, offered_to: str, booking_id: str) -> int | None:
        """The revision of this booking this call was shown, None when it was never shown one."""
        with self._lock:
            return self._shown.get((offered_to, booking_id))

    def find_slots(
        self, phone: str, when: str, service: str, at_time: datetime, offered_to: str = '', prefer: str = ''
    ) -> dict:
        """Free times on the day the caller named, and everything they already hold.

        One call answers both questions, so the agent never needs a second one.
        Every slot **returned** is recorded as an offer, and only the returned
        ones: an offer is a thing this caller was told about, so it is recorded
        after the cap, not before it.

        Asking again **replaces** what was offered, whichever way the search
        goes: this search is the question now on the table and the last one is
        over. An offer belongs to one question, not to a day, so reading Friday
        for a haircut and then Friday for a colour cannot leave both standing.

        `offered_to` is who the offer belongs to, and it is the *call* rather
        than the number: two calls from one phone are two conversations. It
        defaults to the number so a direct caller gets its own namespace.

        `prefer` is applied before the cap and sorted chronologically after it:
        the preference decides *which* dozen, and a dozen times are read out in
        the order of a day. Soonest first across services before the cap, or
        asking for `any` would return one service and nothing else.
        """
        who = offered_to or phone
        self.withdraw_offers(who)
        today = at_time.date()
        held = self.bookings_for(phone, today)
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

        with self._tx(write=False) as db:
            booked = self._booked(db, 'day = ?', day.isoformat())
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
                if not self._eligible(booked, day, at, name, at_time):
                    slot_id = _slot_id(day, at, name)
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

        slots.sort(key=lambda slot: (slot['time'], slot['service']))
        shown = sorted(_preferred(slots, prefer)[:SHOW_AT_MOST], key=lambda s: (s['time'], s['service']))
        with self._lock:
            for slot in shown:
                self._offers[who, slot['slot_id']] = candidates[slot['slot_id']]

        return {
            'status': 'ok' if slots else 'full',
            'day': spoken_day(day, today),
            'date': day.isoformat(),
            'bookings': held,
            'slots': shown,
            'price_euros': SERVICES[service][1] if service in SERVICES else None,
        }

    def _offer_for(self, offered_to: str, slot_id: str, phone: str) -> Offer | None:
        """The offer this call holds under that id, if it is still this caller's.

        The one place an offer is matched, so the read and the write cannot
        disagree about what counts. An offer made before anybody was identified
        carries no identity and stays bookable by whoever this call turns out
        to be; one made under a number is void the moment that number changes.
        """
        with self._lock:
            offer = self._offers.get((offered_to, slot_id))
        if offer is None or (offer.phone and phone and offer.phone != phone):
            return None
        return offer

    def withdraw_offers(self, offered_to: str) -> None:
        """Take every time off this call's table: it has booked or moved, so the times offered for it are spent.

        Left on, every later reply was read as a possible pick (Coval run
        RPH7TzmF, K1: "No, that's all, thanks. Bye." after the booking ran two
        choice readings that could not decide anything). A caller who wants
        another time is offered times again.
        """
        with self._lock:
            for key in [k for k in self._offers if k[0] == offered_to]:
                del self._offers[key]

    def offer_of(self, offered_to: str, slot_id: str, phone: str = '') -> dict:
        """What this caller was offered under that id: the day, the time, the service and its one spoken phrasing.

        Empty when this diary never offered it to them, or offered it to who
        they were before they corrected their number. Exists so the rules can
        *ask* what a slot means instead of reading it out of the string.
        """
        offer = self._offer_for(offered_to, slot_id, phone)
        if offer is None:
            return {}
        return {
            'date': offer.day.isoformat(),
            'time': offer.at.strftime('%H:%M'),
            'service': offer.service,
            'spoken': spoken(offer.day, offer.at, self.now().date()),
        }

    def offered_to(self, offered_to: str, phone: str = '') -> list[dict]:
        """Every slot currently on the table for this call, soonest first.

        What "unambiguous" is measured against: a caller naming a time has
        chosen a slot only when exactly one of these is at it. Same matching
        rule as `offer_of`, because an offer the write would refuse is not one
        the caller can have chosen.

        A time this caller already holds is not on the table: it is theirs.
        Left on it, "Fine, nine o'clock then", said to keep the nine o'clock
        they had just booked, read as picking it again, and the caller was
        asked whether to move or add (Coval run TgByaegk, K5).
        """
        today = self.now().date()
        held = (
            {(b['date'], b['time'], b['service']) for b in self.bookings_for(phone, today)}
            if phone
            else set()
        )
        with self._lock:
            mine = [(slot_id, offer) for (who, slot_id), offer in self._offers.items() if who == offered_to]
        live = [
            (slot_id, offer)
            for slot_id, offer in mine
            if self._offer_for(offered_to, slot_id, phone) is not None
            and (offer.day.isoformat(), offer.at.strftime('%H:%M'), offer.service) not in held
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

    def outcome_of(self, operation_token: str) -> dict | None:
        """What a write with this token did, or None if this diary never saw it.

        None is "not started, or unknown", never "it failed". A caller whose
        response went missing asks this before retrying.
        """
        with self._tx(write=False) as db:
            row = db.execute('SELECT result FROM operations WHERE token = ?', (operation_token,)).fetchone()
        return json.loads(row[0]) if row else None

    def outcome_or_void(self, operation_token: str) -> dict:
        """What a write with this token did; if nothing yet, a `void` filed under it, so it never does anything.

        The fence for a write whose answer nobody is waiting for any more: a
        worker that died, or hung, with it in flight. Both this and the write
        run under `BEGIN IMMEDIATE`, so exactly one of them is first. Written
        first, the write is found. Void first, a late write finds the void and
        does nothing, so no stale worker can land an effect after it was
        settled as not done.
        """
        with self._tx() as db:
            row = db.execute('SELECT result FROM operations WHERE token = ?', (operation_token,)).fetchone()
            if row is not None:
                return json.loads(row[0])
            void = {'status': 'void'}
            db.execute('INSERT INTO operations VALUES (?, ?)', (operation_token, json.dumps(void)))
        return void

    def note_intent(self, intent_id: str, session: str, operation: str, args: str, subject: str) -> None:
        """Core's durable record of an intent before dispatch. The same id for other arguments is refused."""
        with self._tx() as db:
            db.execute(
                'INSERT OR IGNORE INTO intents VALUES (?, ?, ?, ?, ?)',
                (intent_id, session, operation, args, subject),
            )
            held = db.execute(
                'SELECT operation, args, subject FROM intents WHERE id = ?', (intent_id,)
            ).fetchone()
        if tuple(held) != (operation, args, subject):
            raise ValueError(f'intent {intent_id} is already recorded for different arguments')

    def unresolved(self, subject: str) -> list[dict]:
        """Every intent recorded for this caller with no result filed under its token: what a new call settles first."""
        with self._tx(write=False) as db:
            rows = db.execute(
                'SELECT id, session, operation, args, subject FROM intents '
                'WHERE subject = ? AND id NOT IN (SELECT token FROM operations)',
                (subject,),
            ).fetchall()
        return [{'id': i, 'session': c, 'operation': op, 'args': a, 'subject': s} for i, c, op, a, s in rows]

    def current_revision(self, booking_id: str) -> int | None:
        """A booking's revision now, active or cancelled; None for a booking this diary never made."""
        with self._tx(write=False) as db:
            row = db.execute('SELECT revision FROM bookings WHERE booking_id = ?', (booking_id,)).fetchone()
        return row[0] if row else None

    def save_booking(
        self,
        phone: str,
        action: str,
        slot_id: str,
        booking_id: str,
        additional: bool,
        at_time: datetime,
        operation_token: str = '',
        offered_to: str = '',
        expected_revision: int | None = None,
    ) -> dict:
        """Book, move or cancel, in one transaction. Raises `RefusedError` with something sayable.

        A slot is looked up among this caller's own current offers, never
        parsed: a fabricated id is simply absent, and a slot shown to another
        caller or to a previous identity is not this caller's to book. Its
        eligibility is checked again at the moment of writing, against the
        clock and the diary as they are now, and the chair trigger checks it
        once more as the row is written.

        A move or cancel acts only on the caller's own booking, and, when
        `expected_revision` is given, only on the revision they were shown: a
        booking changed since, by anyone, is refused, never overwritten.

        Sent twice with the same `operation_token`, the second call returns
        the first one's outcome and writes nothing, so an uncertain write
        cannot become two bookings. A token already voided does nothing.
        """
        who = offered_to or phone
        today = at_time.date()
        with self._tx() as db:
            if operation_token and (
                done := db.execute(
                    'SELECT result FROM operations WHERE token = ?', (operation_token,)
                ).fetchone()
            ):
                return json.loads(done[0])
            held = self._booked(db, 'phone = ? AND day >= ?', phone, today.isoformat())
            mine = {b.booking_id: b for b in held}
            if action in ('move', 'cancel') and booking_id not in mine:
                raise RefusedError(
                    'There is no booking under this number with that reference. '
                    f'They hold: {self._rows(held, today) or "nothing"}.'
                )
            if action in ('move', 'cancel') and expected_revision not in (None, mine[booking_id].revision):
                raise RefusedError(
                    'That booking has changed since it was read to the caller. Read their bookings again.'
                )
            if action == 'cancel':
                gone = mine[booking_id]
                db.execute(
                    'UPDATE bookings SET active = 0, revision = revision + 1 WHERE booking_id = ?',
                    (booking_id,),
                )
                result = {'status': 'cancelled'} | _row(
                    booking_id, gone.service, gone.day, gone.at, today, gone.revision + 1
                )
                return self._record(db, operation_token, result)

            offer = self._offer_for(who, slot_id, phone)
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
            booked = self._booked(db, 'day = ?', offer.day.isoformat())
            if why := self._eligible(
                booked,
                offer.day,
                offer.at,
                offer.service,
                at_time,
                ignore=booking_id if action == 'move' else '',
            ):
                raise RefusedError(why)
            if action == 'book' and held and not additional:
                raise AlreadyHoldsError(
                    f'This caller already holds {self._rows(held, today)}. Moving that one is almost '
                    'always what they mean; only pass additional when they asked for a second appointment.'
                )
            starts, ends = _minutes(offer.day, offer.at, offer.service)
            day, at = offer.day.isoformat(), offer.at.strftime('%H:%M')
            try:
                if action == 'book':
                    row = db.execute(
                        'INSERT INTO bookings (phone, service, day, at, starts, ends) VALUES (?, ?, ?, ?, ?, ?)',
                        (phone, offer.service, day, at, starts, ends),
                    )
                    booking_id = f'BK{row.lastrowid:04d}'
                    db.execute('UPDATE bookings SET booking_id = ? WHERE id = ?', (booking_id, row.lastrowid))
                    revision = 1
                else:
                    db.execute(
                        'UPDATE bookings SET service = ?, day = ?, at = ?, starts = ?, ends = ?, '
                        'revision = revision + 1 WHERE booking_id = ?',
                        (offer.service, day, at, starts, ends, booking_id),
                    )
                    revision = mine[booking_id].revision + 1
            except sqlite3.IntegrityError as clash:
                raise RefusedError(
                    'Somebody has just taken that time. Read the diary again and offer what is left.'
                ) from clash
            status = 'booked' if action == 'book' else 'moved'
            result = {'status': status} | _row(
                booking_id, offer.service, offer.day, offer.at, today, revision
            )
            return self._record(db, operation_token, result)

    @staticmethod
    def _rows(held: list[Booking], today: Date) -> list[dict]:
        return [_row(b.booking_id, b.service, b.day, b.at, today, b.revision) for b in held]

    @staticmethod
    def _record(db: sqlite3.Connection, operation_token: str, result: dict) -> dict:
        """Remember what this write did, in its own transaction, so the same token can only do it once."""
        if operation_token:
            db.execute('INSERT INTO operations VALUES (?, ?)', (operation_token, json.dumps(result)))
        return result

    def find_or_create_customer(self, phone: str) -> dict:
        with self._tx() as db:
            added = db.execute('INSERT OR IGNORE INTO customers VALUES (?)', (phone,)).rowcount
        return {'customer_status': 'created' if added else 'existing', 'customer_phone': phone}

    def record_complaint(
        self, phone: str, summary: str, requested_resolution: str, operation_token: str = ''
    ) -> dict:
        """Write a complaint down. Twice with one token writes it once.

        The same identity a booking write has, and for the same reason: a
        complaint book with the same grievance in it twice is a caller who gets
        phoned about it twice.
        """
        with self._tx() as db:
            if operation_token and (
                done := db.execute(
                    'SELECT result FROM operations WHERE token = ?', (operation_token,)
                ).fetchone()
            ):
                return json.loads(done[0])
            row = db.execute(
                'INSERT INTO complaints (phone, summary, requested_resolution) VALUES (?, ?, ?)',
                (phone, summary, requested_resolution),
            )
            reference = f'CX{row.lastrowid:04d}'
            db.execute('UPDATE complaints SET reference = ? WHERE id = ?', (reference, row.lastrowid))
            return self._record(db, operation_token, {'recorded': True, 'reference': reference})

    def complaints(self) -> list[dict]:
        with self._tx(write=False) as db:
            rows = db.execute(
                'SELECT reference, phone, summary, requested_resolution FROM complaints ORDER BY id'
            ).fetchall()
        return [{'reference': r, 'phone': p, 'summary': s, 'requested_resolution': q} for r, p, s, q in rows]


def _remove(path: str) -> None:
    """Delete a fixture diary's file and any journal beside it."""
    for suffix in ('', '-journal', '-wal', '-shm'):
        with contextlib.suppress(FileNotFoundError):
            os.remove(path + suffix)
