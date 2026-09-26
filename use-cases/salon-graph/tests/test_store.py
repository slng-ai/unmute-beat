"""The shared salon diary under contention: several worker processes on one SQLite file.

Every check starts real processes (`multiprocessing`, spawn) that open the
same file, and judges the outcome from the file itself, read with plain
`sqlite3` (`snapshot`), never from any process's own answer or state:

* B1: five callers booking five different times keep all five;
* B2: five callers racing for one time: exactly one booking, four refusals,
  and each refused caller is offered other times;
* B3: an overlapping time under another slot id cannot share the chair, and
  the database refuses an overlapping row written past the Python check;
* B4: one caller cannot move or cancel another caller's booking;
* B5: a move takes the new time and frees the old one at once; a refused
  move leaves the booking as it was;
* B6: one intent delivered twice, from two processes at once, is one
  effect with one answer; a new intent with the same arguments is new;
* B7: a write committed by a process killed before it answered is found
  after a restart, and its intent is no longer unresolved;
* B8: a stale worker's write, fenced by reconciliation first, does nothing;
  a move of a revision that changed since it was shown is refused;
* B9: booking ids are unique in the file and every booking belongs to the
  caller that made it.

Synthetic callers only: +44 7700 900xxx is Ofcom's drama range.
"""

import multiprocessing
import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from salon import diary
from salon.diary import Diary, RefusedError

AT = datetime(2026, 9, 21, 10, 0, tzinfo=diary.TZ)
"""Monday 21 September 2026, 10:00 in Madrid. 'tomorrow' is Tuesday the 22nd."""
CALLERS = tuple(f'+44770090000{n}' for n in range(1, 6))
TIMES = ('09:00', '10:00', '11:00', '12:00', '13:00')


def snapshot(path: str) -> list[tuple]:
    """The active bookings, straight from the file: (booking_id, phone, service, day, at, revision)."""
    db = sqlite3.connect(path)
    try:
        return db.execute(
            'SELECT booking_id, phone, service, day, at, revision FROM bookings WHERE active = 1 ORDER BY id'
        ).fetchall()
    finally:
        db.close()


def _slot(books: Diary, phone: str, at: str, service: str = 'haircut', who: str = '') -> str:
    slots = books.find_slots(phone, 'tomorrow', service, AT, offered_to=who)['slots']
    return next(s['slot_id'] for s in slots if s['time'] == at)


def _book(path: str, phone: str, at: str, start, token: str = '', service: str = 'haircut') -> dict:
    """One worker: read the diary, wait for the others, book. Returns what it was told."""
    books = Diary(path=path, clock=AT)
    slot = _slot(books, phone, at, service)
    start.wait()
    try:
        return books.save_booking(phone, 'book', slot, '', False, AT, operation_token=token)
    except RefusedError as refused:
        free = [s['time'] for s in books.find_slots(phone, 'tomorrow', service, AT)['slots']]
        return {'status': 'refused', 'reason': str(refused), 'offered': free}


def _race(path: str, jobs: list[tuple]) -> list[dict]:
    """Run each job in its own process, all released at once."""
    context = multiprocessing.get_context('spawn')
    with context.Manager() as manager:
        start = manager.Barrier(len(jobs))
        with context.Pool(len(jobs)) as pool:
            return pool.starmap(_workers()._book, [(path, *job[:2], start, *job[2:]) for job in jobs])


def _die_after_commit(path: str, phone: str, at: str, token: str) -> None:
    """A worker that commits a booking and is killed before it can answer anyone."""
    books = Diary(path=path, clock=AT)
    books.note_intent(token, 'call-dead', 'book_appointment', '{}', phone)
    slot = _slot(books, phone, at)
    books.save_booking(phone, 'book', slot, '', False, AT, operation_token=token)
    os._exit(3)


def _workers():
    """This module under its import name: `python -m tests` runs it as `__main__`, which a spawned worker cannot import."""
    import tests.test_store

    return tests.test_store


def fresh(folder: Path, name: str) -> str:
    path = str(folder / f'{name}.db')
    Diary(path=path, clock=AT)
    return path


def five_distinct_callers(folder: Path):
    """B1."""
    path = fresh(folder, 'distinct')
    told = _race(path, [(phone, at) for phone, at in zip(CALLERS, TIMES, strict=True)])
    assert [t['status'] for t in told] == ['booked'] * 5, told
    rows = snapshot(path)
    assert sorted((r[1], r[4]) for r in rows) == sorted(zip(CALLERS, TIMES, strict=True)), rows


def five_callers_one_time(folder: Path):
    """B2."""
    path = fresh(folder, 'contended')
    told = _race(path, [(phone, '09:00') for phone in CALLERS])
    booked = [t for t in told if t['status'] == 'booked']
    refused = [t for t in told if t['status'] == 'refused']
    assert len(booked) == 1 and len(refused) == 4, told
    assert all('09:00' not in t['offered'] and t['offered'] for t in refused), (
        'a refused caller got no alternatives'
    )
    rows = snapshot(path)
    assert [(r[4]) for r in rows] == ['09:00'] and rows[0][0] == booked[0]['booking_id'], rows


def overlaps_share_no_chair(folder: Path):
    """B3."""
    path = fresh(folder, 'overlap')
    told = _race(path, [(CALLERS[0], '09:00', '', 'hair-color'), (CALLERS[1], '10:00', '', 'haircut')])
    assert sorted(t['status'] for t in told) == ['booked', 'refused'], told
    assert len(snapshot(path)) == 1
    db = sqlite3.connect(path)
    try:
        (won,) = db.execute('SELECT starts FROM bookings WHERE active = 1').fetchone()
        db.execute(
            'INSERT INTO bookings (phone, service, day, at, starts, ends) VALUES (?, ?, ?, ?, ?, ?)',
            (CALLERS[2], 'blowout', '2026-09-22', 'any', won + 15, won + 45),
        )
    except sqlite3.IntegrityError as error:
        assert 'clash' in str(error), error
    else:
        raise AssertionError('the database took a booking on top of another')
    finally:
        db.close()
    assert len(snapshot(path)) == 1


def only_the_owner_changes_it(folder: Path):
    """B4."""
    path = fresh(folder, 'owner')
    books = Diary(path=path, clock=AT)
    mine = books.save_booking(CALLERS[0], 'book', _slot(books, CALLERS[0], '09:00'), '', False, AT)
    other = Diary(path=path, clock=AT)
    for action, slot in (('cancel', ''), ('move', _slot(other, CALLERS[1], '11:00'))):
        try:
            other.save_booking(CALLERS[1], action, slot, mine['booking_id'], False, AT)
        except RefusedError:
            continue
        raise AssertionError(f'another caller could {action} a booking')
    assert snapshot(path) == [(mine['booking_id'], CALLERS[0], 'haircut', '2026-09-22', '09:00', 1)]


def a_move_is_one_step(folder: Path):
    """B5."""
    path = fresh(folder, 'move')
    books = Diary(path=path, clock=AT)
    held = books.save_booking(CALLERS[0], 'book', _slot(books, CALLERS[0], '09:00'), '', False, AT)
    moved = books.save_booking(
        CALLERS[0], 'move', _slot(books, CALLERS[0], '11:00'), held['booking_id'], False, AT
    )
    assert moved['revision'] == 2 and snapshot(path) == [
        (held['booking_id'], CALLERS[0], 'haircut', '2026-09-22', '11:00', 2)
    ]
    other = Diary(path=path, clock=AT)
    taken = other.save_booking(CALLERS[1], 'book', _slot(other, CALLERS[1], '09:00'), '', False, AT)
    assert taken['status'] == 'booked', 'the old time was not freed by the move'
    blocked = _slot(books, CALLERS[0], '13:00')
    other.save_booking(CALLERS[1], 'book', _slot(other, CALLERS[1], '13:00'), '', True, AT)
    try:
        books.save_booking(CALLERS[0], 'move', blocked, held['booking_id'], False, AT)
    except RefusedError:
        pass
    else:
        raise AssertionError('a move onto a taken time went through')
    assert (held['booking_id'], CALLERS[0], 'haircut', '2026-09-22', '11:00', 2) in snapshot(path), (
        'a refused move changed the booking'
    )


def one_intent_one_effect(folder: Path):
    """B6."""
    path = fresh(folder, 'intent')
    told = _race(path, [(CALLERS[0], '09:00', 'intent-A'), (CALLERS[0], '09:00', 'intent-A')])
    assert told[0] == told[1] and told[0]['status'] == 'booked', told
    assert len(snapshot(path)) == 1
    books = Diary(path=path, clock=AT)
    books.save_booking(CALLERS[0], 'cancel', '', told[0]['booking_id'], False, AT, operation_token='intent-B')
    again = books.save_booking(
        CALLERS[0], 'book', _slot(books, CALLERS[0], '09:00'), '', False, AT, operation_token='intent-C'
    )
    assert again['status'] == 'booked' and again['booking_id'] != told[0]['booking_id'], again
    assert [r[0] for r in snapshot(path)] == [again['booking_id']]


def a_lost_answer_survives_a_restart(folder: Path):
    """B7."""
    path = fresh(folder, 'lost')
    context = multiprocessing.get_context('spawn')
    worker = context.Process(
        target=_workers()._die_after_commit, args=(path, CALLERS[0], '09:00', 'intent-L')
    )
    worker.start()
    worker.join(60)
    assert worker.exitcode == 3, worker.exitcode
    books = Diary(path=path, clock=AT)
    found = books.outcome_of('intent-L')
    assert found is not None and found['status'] == 'booked', found
    assert [r[0] for r in snapshot(path)] == [found['booking_id']]
    assert books.unresolved(CALLERS[0]) == [], 'a committed intent is still listed as unresolved'


def stale_work_is_fenced(folder: Path):
    """B8."""
    path = fresh(folder, 'stale')
    books = Diary(path=path, clock=AT)
    books.note_intent('intent-S', 'call-hung', 'book_appointment', '{}', CALLERS[0])
    assert [i['id'] for i in books.unresolved(CALLERS[0])] == ['intent-S']
    slot = _slot(books, CALLERS[0], '09:00')
    assert Diary(path=path, clock=AT).outcome_or_void('intent-S') == {'status': 'void'}
    late = books.save_booking(CALLERS[0], 'book', slot, '', False, AT, operation_token='intent-S')
    assert late == {'status': 'void'} and snapshot(path) == [], ('a fenced write landed', late)
    assert books.unresolved(CALLERS[0]) == []

    held = books.save_booking(CALLERS[0], 'book', _slot(books, CALLERS[0], '09:00'), '', False, AT)
    shown = books.show_holdings('call-1', CALLERS[0], AT.date())
    other = Diary(path=path, clock=AT)
    other.save_booking(CALLERS[0], 'move', _slot(other, CALLERS[0], '11:00'), held['booking_id'], False, AT)
    try:
        books.save_booking(
            CALLERS[0],
            'cancel',
            '',
            held['booking_id'],
            False,
            AT,
            expected_revision=books.shown_revision('call-1', held['booking_id']),
        )
    except RefusedError:
        pass
    else:
        raise AssertionError('a cancel of a revision changed since it was shown went through')
    assert shown[0]['revision'] == 1 and snapshot(path)[0][4:] == ('11:00', 2), snapshot(path)


def ids_are_unique_and_owned(folder: Path):
    """B9."""
    path = fresh(folder, 'ids')
    _race(path, [(phone, at) for phone, at in zip(CALLERS, TIMES, strict=True)])
    later = tuple(f'+4477009000{n:02d}' for n in range(6, 11))
    _race(path, [(phone, '15:00') for phone in later])
    rows = snapshot(path)
    assert len({r[0] for r in rows}) == len(rows) == 6, rows
    db = sqlite3.connect(path)
    try:
        owners = dict(db.execute('SELECT token, result FROM operations').fetchall())
    finally:
        db.close()
    assert not owners, 'these writes carried no token, and filed one'
    assert set(CALLERS) <= {r[1] for r in rows} <= set(CALLERS) | set(later), rows
    assert [r[4] for r in rows].count('15:00') == 1, rows


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        five_distinct_callers(folder)
        five_callers_one_time(folder)
        overlaps_share_no_chair(folder)
        only_the_owner_changes_it(folder)
        a_move_is_one_step(folder)
        one_intent_one_effect(folder)
        a_lost_answer_survives_a_restart(folder)
        stale_work_is_fenced(folder)
        ids_are_unique_and_owned(folder)
    print('store ok: B1-B9, several processes on one SQLite file, judged from the file')


if __name__ == '__main__':
    main()
