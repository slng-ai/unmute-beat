"""The salon: customers, the diary, and the complaint book.

`Diary` is the business, seeded, on one SQLite file that every worker process
shares, so two calls on two workers contend for the same chair and a booking
survives the process that made it. Nothing above this file knows it is a
database. One file is the salon; a `Diary()` with no path is a fixture.

Everything a model could get wrong and code can get right is done here: whether
a slot exists, and what a booking sounds like when you say it out loud. Which
date the caller means is theirs and the model's, from the calendar core shows it
(`current_datetime:`); the read-back confirms it. This copy of salon-graph's
diary keeps no day grammar of its own.

This is also the oracle the agency is measured against, so it has to be able to
say no. Slot ids are opaque and looked up, never parsed; a slot is bookable only
if this diary issued it to this caller, and its eligibility is re-derived at
write time; appointments have durations, so two of them clash when their
intervals overlap; and every write takes an operation token so a lost response
can be reconciled instead of retried blind.

Nothing here is a scheduling platform. One chair, a fixed grid and a clock
passed in by the caller.
"""

from __future__ import annotations

import contextlib
import json
import os
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


_DAYPARTS = {
    'morning': (time(0, 0), time(11, 59)),
    'afternoon': (time(12, 0), time(16, 59)),
    'evening': (time(17, 0), time(23, 59)),
}


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
        self, phone: str, day: Date, service: str, at_time: datetime, offered_to: str = '', prefer: str = ''
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
            if action == 'move':
                self._moved_here(offered_to, booking_id, revision)
            return self._record(db, operation_token, result)

    def _moved_here(self, offered_to: str, booking_id: str, revision: int) -> None:
        """The call that moved a booking it was shown has seen the revision it made (session 4d, trace ce40c124).

        Without this, the cancel that follows a move in the same call was
        refused as stale: the call was shown revision 1 and made revision 2.
        A revision someone else makes is still never taken as seen.
        ponytail: set before the transaction commits; a failed commit leaves
        a revision marked seen that is not, and the next write is refused as
        stale by the database's own check, which is the safe side.
        """
        with self._lock:
            if (offered_to, booking_id) in self._shown:
                self._shown[offered_to, booking_id] = revision

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
