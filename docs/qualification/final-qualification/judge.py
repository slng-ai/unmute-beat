"""Judge one cohort from its own diary file: each caller's final bookings and every business write, by case.

    python3 docs/qualification/final-qualification/judge.py normal STATE_DIR > judge-normal.json

Deterministic, from the backend alone (`diary.db`, `runner.db`, the `seed.json` it started from). For every
case: the caller's active and cancelled bookings against the manifest's `final` and `cancelled`, and each
dispatched write (an intent core recorded before sending it) with its result, against `writes`. Across the
cohort: any write for a number no case gives (cross-customer), any write for a misspoken number, any booking
changed that its caller did not ask about, and more successful writes than a case asks for (a duplicate).

What it cannot see: why a write happened, and whether the caller agreed to it. That is the trace review's.
"""

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

CASES = Path(__file__).with_name('cases.v1.json')
KIND = {'book_appointment': 'book', 'move_appointment': 'move', 'cancel_appointment': 'cancel'}


def rows(db: sqlite3.Connection, sql: str, *args: object) -> list[dict]:
    cursor = db.execute(sql, args)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, r, strict=True)) for r in cursor.fetchall()]


def main() -> None:
    cohort, state = sys.argv[1], Path(sys.argv[2])
    cases = json.loads(CASES.read_text())['cohorts'][cohort]['cases']
    seed = json.loads((state / 'seed.json').read_text())
    db = sqlite3.connect(f'file:{state / "diary.db"}?mode=ro', uri=True)
    bookings = rows(db, 'SELECT booking_id, phone, service, day, at, revision, active FROM bookings ORDER BY id')
    intents = rows(
        db,
        'SELECT i.id, i.session, i.operation, i.args, i.subject, o.result FROM intents i '
        'LEFT JOIN operations o ON o.token = i.id',
    )
    complaints = rows(db, 'SELECT reference, phone FROM complaints')
    db.close()
    calls = []
    if (state / 'runner.db').exists():
        runner = sqlite3.connect(f'file:{state / "runner.db"}?mode=ro', uri=True)
        calls = rows(runner, 'SELECT * FROM calls ORDER BY admitted_at')
        runner.close()
    for intent in intents:
        intent['result'] = json.loads(intent['result']) if intent['result'] else None
        intent['args'] = json.loads(intent['args']) if intent['args'] else {}
    seeded = {b['booking_id'] for b in seed['bookings']}
    phones = {c['phone'] for c in cases}
    out = {'cohort': cohort, 'state': str(state), 'cases': {}, 'cohort_checks': {}}
    for case in cases:
        phone = case['phone']
        mine = [b for b in bookings if b['phone'] == phone]
        active = [b for b in mine if b['active']]
        gone = [b for b in mine if not b['active']]
        writes = [i for i in intents if i['subject'] == phone and i['operation'] in KIND]
        lookups = [i for i in intents if i['subject'] == phone and i['operation'] == 'lookup_customer']
        done = Counter(
            KIND[i['operation']]
            for i in writes
            if i['result'] and i['result'].get('status') in ('booked', 'moved', 'cancelled')
        )
        problems = []

        def same(held: dict, want: dict) -> bool:
            return (held['service'], held['day'], held['at'], held['revision']) == (
                want['service'],
                want['day'],
                want['at'],
                want['revision'],
            ) and (held['booking_id'] in seeded) == bool(want.get('seeded'))

        if len(active) != len(case['final']) or not all(any(same(h, w) for h in active) for w in case['final']):
            problems.append(f'active {[(b["service"], b["day"], b["at"], b["revision"]) for b in active]}, want {case["final"]}')
        for want in case.get('cancelled', []):
            if not any((b['service'], b['day'], b['at'], b['revision']) == (want['service'], want['day'], want['at'], want['revision']) for b in gone):
                problems.append(f'no cancelled booking {want}')
        if dict(done) != case['writes']:
            problems.append(f'successful writes {dict(done)}, want {case["writes"]}')
        unknown = [i['id'][:8] for i in writes if i['result'] is None]
        if unknown:
            problems.append(f'writes with no result on file (unknown or never sent): {unknown}')
        out['cases'][case['key']] = {
            'coval_test_case': case['coval_test_case'],
            'phone': phone,
            'outcome': 'fail' if problems else 'pass',
            'problems': problems,
            'active': active,
            'cancelled': gone,
            'writes': [
                {'operation': i['operation'], 'session': i['session'], 'args': i['args'], 'result': i['result']}
                for i in writes
            ],
            'lookups': [{'session': i['session'], 'ok': bool(i['result'])} for i in lookups],
            'sessions': sorted({i['session'] for i in writes + lookups}),
        }
    wrong = {c['wrong_phone'] for c in cases if 'wrong_phone' in c}
    strangers = [i for i in intents if i['subject'] not in phones]
    out['cohort_checks'] = {
        'writes_for_numbers_no_case_gives': [
            {'operation': i['operation'], 'subject': i['subject'], 'session': i['session']} for i in strangers
        ],
        'writes_for_misspoken_numbers': [i['operation'] for i in intents if i['subject'] in wrong],
        'bookings_for_numbers_no_case_gives': [b for b in bookings if b['phone'] not in phones],
        'write_args_for_another_caller': [
            {'operation': i['operation'], 'subject': i['subject'], 'args_phone': i['args'].get('phone')}
            for i in intents
            if i['args'].get('phone') and i['args'].get('phone') != i['subject']
        ],
        'complaints': complaints,
        'runner_calls': calls,
    }
    out['summary'] = {
        'outcome_pass': sum(c['outcome'] == 'pass' for c in out['cases'].values()),
        'outcome_fail': sum(c['outcome'] == 'fail' for c in out['cases'].values()),
        'cross_customer': len(strangers) + len(out['cohort_checks']['write_args_for_another_caller']),
    }
    print(json.dumps(out, indent=1, default=str))


if __name__ == '__main__':
    main()
