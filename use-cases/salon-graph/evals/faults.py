"""The offline worker-failure checks of Stage C, on the real runner with two workers (`evals.load faults`).

* F0: a worker is killed after the caller's yes, before the booking intent
  is recorded. Nothing was sent, so the ring-back finds no old intent and
  books once.
* F1: a worker is killed after a booking intent is recorded and before it
  is sent. A bystander call on the other worker goes on and books; the dead
  call is closed; a replacement worker comes up; the caller rings back, the
  new call voids the dead intent first, and books once.
* F2: the same, killed after the diary committed and before anyone was told.
  The ring-back finds the booking by its intent and books nothing more.
* F3: a stale worker. The write hangs in its thread, the caller hangs up and
  rings back; the new call voids the old intent while the thread still
  sleeps; when the thread wakes, its write finds the void and does nothing.
* F4: six calls at once against a ceiling of five: one is refused as busy,
  none waits, and a later call is admitted once places are free.
* F5: graceful drain. SIGTERM to the worker carrying a call: that call
  finishes and books, a call placed during the drain is served in full
  (the workers share one socket, so it may land on the draining worker,
  which then waits for it too), and the drained worker exits and is
  replaced.
* F6 (session 4f): the live worker-failure pair, offline. Two runner
  processes share the diary and the runner file; the webhook places the
  bystander on one and the victim on the other (`MEDIA_HOSTS`); placement
  is read from where each stream was claimed (`evals.placement`), and only
  then is `LIVE_FAULT` armed, exactly as the live pair arms it. The victim's
  worker dies right after its booking commits. The bystander books on the
  other process; the dead call is closed; its runner replaces the worker;
  admission comes back to empty; the fault fired once; the caller rings back
  and books nothing more; a contender asking for the victim's time does not
  get it.

Limits, fixed before running (the brief): a dead session closed within 10 s,
a replacement ready within 30 s, and a worker's tasks and descriptors back
to their idle baseline within 30 s. Judged from the diary file, the runner
file and the workers' own stats files.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sqlite3
import time
from pathlib import Path
from typing import Any

from evals.doubles import Caller, Utterance, booking
from evals.load import Phone, Record, Runner, _write_fixture, snapshot
from evals.placement import watch

DEAD_SESSION_S = 10.0
REPLACEMENT_S = 30.0
IDLE_S = 30.0
NUMBER, YES, BYE = "Yes, that's my number.", 'Yes please.', "No, that's all. Bye."


def request(day: str) -> Utterance:
    return Utterance((f"Hi, I'd like a haircut on {day} at 9 am.",))


def plain(name: str, phone: str, day: str) -> Caller:
    return booking(name, phone, day, '09:00')


def calls_of(runner: Runner) -> dict[str, dict[str, Any]]:
    db = sqlite3.connect(runner.out / 'runner.db')
    db.row_factory = sqlite3.Row
    try:
        return {row['call_sid']: dict(row) for row in db.execute('SELECT * FROM calls')}
    finally:
        db.close()


def active_for(state: dict[str, list], phone: str) -> list:
    return [b for b in state['bookings'] if b[1] == phone and b[7] == 1]


async def ring(runner: Runner, caller: Caller, lane: int = 0) -> Record:
    _write_fixture(runner.out / 'fixtures', caller)
    return await Phone(runner.base, caller, lane, runner.out / 'audio').run()


async def until(check, limit: float, every: float = 0.2) -> float | None:
    """Seconds until `check()` holds, or None if it never did within `limit`."""
    started = time.perf_counter()
    while time.perf_counter() - started < limit:
        if check():
            return time.perf_counter() - started
        await asyncio.sleep(every)
    return None


def idle_ok(before: dict[int, dict[str, Any]], after: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Each surviving worker's tasks and descriptors against its own idle baseline."""
    found = {}
    for pid, stats in after.items():
        base = before.get(pid)
        if base is None:
            found[str(pid)] = {'replacement': True, 'tasks': stats['tasks'], 'fds': stats['fds']}
            continue
        found[str(pid)] = {
            'tasks': (base['tasks'], stats['tasks']),
            'fds': (base['fds'], stats['fds']),
            'ok': stats['tasks'] <= base['tasks'] and stats['fds'] <= base['fds'],
        }
    return found


async def kill_case(folder: Path, fault: str, port: int, attempts: int = 3) -> dict[str, Any]:
    """F0 (`before`), F1 (`noted`) and F2 (`committed`): a worker killed in a write, a bystander on the other worker."""
    report: dict[str, Any] = {}
    for attempt in range(attempts):
        report = await _kill_once(folder, fault, port, attempt)
        if report['bystanders_on_other_worker']:
            break
    return report


async def _kill_once(folder: Path, fault: str, port: int, attempt: int) -> dict[str, Any]:
    """One try of F0, F1 or F2. Which worker a call lands on is the kernel's choice, so a try may put no bystander on the other."""
    out = folder / f'try{attempt}'
    runner = Runner(out, 2, port + attempt)
    runner.start()
    baseline = runner.workers_now()
    victim = booking(f'{fault}-victim', f'+3469800000{attempt}', 'Tuesday', '09:00', fault=fault)
    bystanders = [
        plain(f'{fault}-by{i}', f'+346980010{attempt}{i}', day)
        for i, day in enumerate(('Wednesday', 'Thursday', 'Friday'))
    ]
    try:
        tasks = [asyncio.create_task(ring(runner, c, i + 1)) for i, c in enumerate(bystanders)]
        await asyncio.sleep(0.5)
        hit = await ring(runner, victim, 0)
        carried = calls_of(runner)
        dead_worker = carried.get(hit.call_sid, {}).get('worker')
        closed = await until(lambda: calls_of(runner).get(hit.call_sid, {}).get('state') == 'ended', 30)
        replaced = await until(
            lambda: (
                len(runner.workers_now(fresh=1.5)) == 2
                and any(p not in baseline for p in runner.workers_now(fresh=1.5))
            ),
            60,
        )
        done = await asyncio.gather(*tasks)
        again = await ring(runner, plain(f'{fault}-again', victim.phone, 'Tuesday'))
        await asyncio.sleep(3)
        after = runner.workers_now()
        await until(
            lambda: all(
                w['tasks'] <= baseline.get(pid, w)['tasks'] for pid, w in runner.workers_now().items()
            ),
            IDLE_S,
        )
        settled = runner.workers_now()
    finally:
        runner.stop()
    carried = calls_of(runner)
    survivors = [r for r in done if carried.get(r.call_sid, {}).get('worker') != dead_worker]
    state = snapshot(out / 'diary.db')
    mine = active_for(state, victim.phone)
    operations = dict(state['operations'])
    victim_intents = [i for i in state['intents'] if i[1] == hit.call_sid]
    report = {
        'attempt': attempt,
        'dead_worker': dead_worker,
        'victim_closed_by': hit.closed_by,
        'dead_session_closed_s': closed,
        'replacement_ready_s': replaced,
        'bystanders_on_other_worker': len(survivors),
        'bystanders_on_other_worker_booked': sum(1 for r in survivors if active_for(state, r.phone)),
        'bystanders_on_dead_worker': len(done) - len(survivors),
        'victim_bookings_after_ringing_back': len(mine),
        'victim_old_intents': [
            (i[0][:8], json.loads(operations[i[0]]) if i[0] in operations else None) for i in victim_intents
        ],
        'ring_back_closed_by': again.closed_by,
        'idle_after': idle_ok(baseline, settled),
        'raw_after': {str(k): {x: v[x] for x in ('tasks', 'fds')} for k, v in after.items()},
        'duplicates': sum(1 for b in state['bookings'] if b[7] == 1 and len(active_for(state, b[1])) > 1),
    }
    expected_old = {'before': None, 'noted': 'void', 'committed': 'booked'}[fault]
    report['verdict'] = {
        'bystander_went_on': report['bystanders_on_other_worker'] > 0
        and report['bystanders_on_other_worker_booked'] == report['bystanders_on_other_worker'],
        'dead_session_closed_in_time': closed is not None and closed <= DEAD_SESSION_S,
        'replacement_in_time': replaced is not None and replaced <= REPLACEMENT_S,
        'one_booking_for_the_victim': len(mine) == 1,
        'old_intent_settled': not report['victim_old_intents']
        if expected_old is None
        else bool(report['victim_old_intents'])
        and all(r is not None and r.get('status') == expected_old for _, r in report['victim_old_intents']),
        'no_duplicates': report['duplicates'] == 0,
        'idle_restored': all(v.get('ok', True) for v in report['idle_after'].values()),
    }
    return report


async def stale_case(folder: Path, port: int) -> dict[str, Any]:
    """F3: a worker hangs inside the write; the caller rings back; the late write finds its intent voided."""
    runner = Runner(folder, 2, port, HANG_SECONDS='30')
    runner.start()
    phone = '+34698002000'
    hung = booking(
        'hang-victim',
        phone,
        'Tuesday',
        '09:00',
        fault='hang',
        leave_after=1.0,
        script=(request('Tuesday'), Utterance((NUMBER,)), Utterance((YES,))),
    )
    try:
        first = await ring(runner, hung)
        ended = await until(lambda: calls_of(runner).get(first.call_sid, {}).get('state') == 'ended', 30)
        again = await ring(runner, plain('hang-again', phone, 'Tuesday'))
        await asyncio.sleep(max(0.0, 35 - (time.perf_counter() - first.started)))
    finally:
        runner.stop()
    state = snapshot(folder / 'diary.db')
    operations = dict(state['operations'])
    old = [i for i in state['intents'] if i[1] == first.call_sid]
    mine = active_for(state, phone)
    report = {
        'first_call_ended_s': ended,
        'ring_back_closed_by': again.closed_by,
        'old_intents': [
            (i[0][:8], json.loads(operations[i[0]]) if i[0] in operations else None) for i in old
        ],
        'bookings': mine,
        'waited_for_the_thread_s': 35,
    }
    report['verdict'] = {
        'old_intent_voided': bool(old)
        and all(operations.get(i[0]) and json.loads(operations[i[0]]).get('status') == 'void' for i in old),
        'one_booking': len(mine) == 1,
    }
    return report


async def overload_case(folder: Path, port: int) -> dict[str, Any]:
    """F4: six at once against five places."""
    runner = Runner(folder, 2, port)
    runner.start()
    days = ('Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Tuesday')
    try:
        started = time.perf_counter()
        callers = [
            booking(
                f'over{i}',
                f'+3469800300{i}',
                days[i],
                '09:00' if i < 5 else '11:00',
                script=(request(days[i]), Utterance((NUMBER,)), Utterance((YES,)), Utterance((BYE,))),
            )
            for i in range(6)
        ]
        records = await asyncio.gather(*(ring(runner, c, i) for i, c in enumerate(callers)))
        rejected_in = (
            max((r.ended or time.perf_counter()) - started for r in records if r.closed_by == 'rejected')
            if any(r.closed_by == 'rejected' for r in records)
            else None
        )
        later = await ring(runner, booking('over-later', '+34698003009', 'Saturday', '11:00'))
    finally:
        runner.stop()
    report = {
        'admitted': sum(r.admitted for r in records),
        'rejected': sum(r.closed_by == 'rejected' for r in records),
        'later_admitted': later.admitted,
        'rejection_answer_s': rejected_in,
    }
    report['verdict'] = {
        'five_admitted_one_refused': report['admitted'] == 5 and report['rejected'] == 1,
        'places_freed': later.admitted,
    }
    return report


async def drain_case(folder: Path, port: int) -> dict[str, Any]:
    """F5: SIGTERM the worker carrying a call; the call finishes; new calls go to the other worker."""
    runner = Runner(folder, 2, port)
    runner.start()
    baseline = set(runner.workers_now())
    first = plain('drain-first', '+34698004001', 'Tuesday')
    second = plain('drain-second', '+34698004002', 'Wednesday')
    try:
        task = asyncio.create_task(ring(runner, first))
        await until(lambda: any(c['state'] == 'active' for c in calls_of(runner).values()), 30)
        carrier = next(c['worker'] for c in calls_of(runner).values() if c['state'] == 'active')
        os.kill(carrier, signal.SIGTERM)
        drained_at = time.perf_counter()
        await asyncio.sleep(1.0)
        other = await ring(runner, second)
        done = await task
        exited = await until(lambda: not _alive(carrier), 60)
        replaced = await until(lambda: len(set(runner.workers_now(fresh=1.5)) - baseline) >= 1, 60)
    finally:
        runner.stop()
    carried = calls_of(runner)
    state = snapshot(folder / 'diary.db')
    report = {
        'carrier': carrier,
        'first_closed_by': done.closed_by,
        'first_booked': bool(active_for(state, first.phone)),
        'second_worker': carried.get(other.call_sid, {}).get('worker'),
        'second_booked': bool(active_for(state, second.phone)),
        'carrier_exited_after_s': exited,
        'drain_to_exit_s': (time.perf_counter() - drained_at) if exited is None else None,
        'replacement_ready_s': replaced,
    }
    report['verdict'] = {
        'drained_call_finished': report['first_booked'] and done.closed_by in ('caller', 'bot'),
        'new_call_served_not_dropped': report['second_booked'],
        'drained_worker_exited': exited is not None,
        'replaced': replaced is not None and replaced <= REPLACEMENT_S + 60,
    }
    return report


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def placed_case(folder: Path, port: int) -> dict[str, Any]:
    """F6: two runner processes, the pair placed by the webhook, the live fault armed only after placement."""
    victim_phone, bystander_phone, contender_phone = '+34698006000', '+34698006001', '+34698006002'
    armed = folder / 'armed'
    runner = Runner(folder, 2, port, 2, LIVE_FAULT=f'committed:{victim_phone}', LIVE_FAULT_ARM=str(armed))
    runner.start()
    baseline = runner.workers_now()
    bystander = booking('placed-bystander', bystander_phone, 'Wednesday', '09:00')
    victim = booking('placed-victim', victim_phone, 'Tuesday', '09:00')
    try:
        arming = asyncio.create_task(
            asyncio.to_thread(watch, runner.ports[1], armed, folder / 'placement.json', 60.0)
        )
        standing = asyncio.create_task(ring(runner, bystander, 1))
        await asyncio.sleep(0.3)
        hit = await ring(runner, victim, 0)
        was_armed = await arming
        carried = calls_of(runner)
        dead_worker = carried.get(hit.call_sid, {}).get('worker')
        closed = await until(lambda: calls_of(runner).get(hit.call_sid, {}).get('state') == 'ended', 30)
        replaced = await until(
            lambda: (
                len(runner.workers_now(fresh=1.5)) == 4
                and any(p not in baseline for p in runner.workers_now(fresh=1.5))
            ),
            60,
        )
        survivor = await standing
        empty = await until(
            lambda: not [r for r in calls_of(runner).values() if r['state'] in ('admitted', 'active')], 30
        )
        again = await ring(runner, plain('placed-again', victim_phone, 'Tuesday'))
        contender = await ring(runner, plain('placed-contender', contender_phone, 'Tuesday'))
        await asyncio.sleep(3)
        await until(
            lambda: all(
                w['tasks'] <= baseline.get(pid, w)['tasks'] for pid, w in runner.workers_now().items()
            ),
            IDLE_S,
        )
        settled = runner.workers_now()
    finally:
        runner.stop()
    carried = calls_of(runner)
    state = snapshot(folder / 'diary.db')
    operations = dict(state['operations'])
    victim_intents = [i for i in state['intents'] if i[1] == hit.call_sid]
    at_nine = [b for b in state['bookings'] if b[2] == '2026-09-29' and b[3] == '09:00' and b[7] == 1]
    placed = [carried.get(r.call_sid, {}) for r in (survivor, hit)]
    report = {
        'placement': json.loads((folder / 'placement.json').read_text()),
        'media': [survivor.media, hit.media],
        'claimed': [(row.get('worker'), row.get('served_port'), row.get('media_host')) for row in placed],
        'armed': was_armed,
        'fault_fired_once': not armed.exists(),
        'dead_worker': dead_worker,
        'victim_closed_by': hit.closed_by,
        'victim_ended': carried.get(hit.call_sid, {}).get('ended'),
        'dead_session_closed_s': closed,
        'replacement_ready_s': replaced,
        'bystander': {'closed_by': survivor.closed_by, 'bookings': active_for(state, bystander_phone)},
        'admission_empty_s': empty,
        'victim_bookings': active_for(state, victim_phone),
        'victim_old_intents': [
            (i[0][:8], json.loads(operations[i[0]]) if i[0] in operations else None) for i in victim_intents
        ],
        'ring_back_closed_by': again.closed_by,
        'contender': {'closed_by': contender.closed_by, 'bookings': active_for(state, contender_phone)},
        'tuesday_nine': at_nine,
        'idle_after': idle_ok(baseline, settled),
    }
    report['verdict'] = {
        'media_followed_the_webhook': all(
            row.get('media_host', '').endswith(f':{row.get("served_port")}/ws') for row in placed
        ),
        'placed_on_two_processes': placed[0].get('served_port') != placed[1].get('served_port')
        and placed[0].get('worker') != placed[1].get('worker'),
        'armed_only_after_placement': was_armed and report['placement']['verdict'] == 'armed',
        'victim_worker_died_after_commit': report['victim_ended'] == 'lost'
        and len(report['victim_bookings']) == 1,
        'fault_fired_once': report['fault_fired_once'],
        'bystander_booked_on_the_other_process': len(report['bystander']['bookings']) == 1
        and placed[0].get('worker') != dead_worker,
        'dead_session_closed_in_time': closed is not None and closed <= DEAD_SESSION_S,
        'replacement_in_time': replaced is not None and replaced <= REPLACEMENT_S,
        'admission_recovered': empty is not None,
        'old_intent_settled_booked': bool(victim_intents)
        and all(r is not None and r.get('status') == 'booked' for _, r in report['victim_old_intents']),
        'ring_back_booked_nothing_more': len(report['victim_bookings']) == 1,
        'contender_did_not_get_the_time': len(at_nine) == 1 and at_nine[0][1] == victim_phone,
        'idle_restored': all(v.get('ok', True) for v in report['idle_after'].values()),
    }
    return report


async def faults(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    port = args.port
    cases = {
        'F0_kill_before_intent_recorded': lambda: kill_case(out / 'f0', 'before', port + 20),
        'F1_kill_after_intent_recorded': lambda: kill_case(out / 'f1', 'noted', port),
        'F2_kill_after_commit': lambda: kill_case(out / 'f2', 'committed', port + 5),
        'F3_stale_worker_fenced': lambda: stale_case(out / 'f3', port + 10),
        'F4_overload': lambda: overload_case(out / 'f4', port + 11),
        'F5_graceful_drain': lambda: drain_case(out / 'f5', port + 12),
        'F6_placed_pair_live_fault': lambda: placed_case(out / 'f6', port + 30),
    }
    only = set(filter(None, getattr(args, 'only', '').split(',')))
    results: dict[str, Any] = {}
    for name, case in cases.items():
        if only and name.split('_')[0] not in only:
            continue
        results[name] = await case()
        print(json.dumps({name: results[name].get('verdict')}))
        (out / 'faults.json').write_text(json.dumps(results, indent=1, default=str))
    failed = [f'{n}:{k}' for n, r in results.items() for k, ok in r.get('verdict', {}).items() if not ok]
    if failed:
        print('failed:', ', '.join(failed))
    return 1 if failed else 0
