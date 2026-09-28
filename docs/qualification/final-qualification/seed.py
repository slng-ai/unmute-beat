"""Create one cohort's qualification diary, with only the bookings its cases say the caller already holds.

    cd use-cases/salon-graph && uv run python ../../docs/qualification/final-qualification/seed.py normal STATE_DIR

Refuses a folder that already has a diary: a diary is never reset while a call could still use it, and a
cohort always starts from this exact state. Prints the state it made, which `judge.py` later compares against.
"""

import json
import sqlite3
import sys
from datetime import date, time
from pathlib import Path

from salon.diary import SEED_CUSTOMERS, Diary, _minutes

CASES = Path(__file__).with_name('cases.v1.json')


def main() -> None:
    cohort, state = sys.argv[1], Path(sys.argv[2])
    path = state / 'diary.db'
    if path.exists() or (state / 'runner.db').exists():
        raise SystemExit(f'refused: {state} already holds a diary or a runner file')
    state.mkdir(parents=True, exist_ok=True)
    cases = json.loads(CASES.read_text())['cohorts'][cohort]['cases']
    held = [c for c in cases if 'seed' in c]
    Diary(customers=SEED_CUSTOMERS | {c['phone'] for c in held}, path=path)
    db = sqlite3.connect(path)
    with db:
        for case in held:
            seed = case['seed']
            day, at = date.fromisoformat(seed['day']), time.fromisoformat(seed['at'])
            starts, ends = _minutes(day, at, seed['service'])
            row = db.execute(
                'INSERT INTO bookings (phone, service, day, at, starts, ends) VALUES (?, ?, ?, ?, ?, ?)',
                (case['phone'], seed['service'], seed['day'], seed['at'], starts, ends),
            )
            db.execute('UPDATE bookings SET booking_id = ? WHERE id = ?', (f'BK{row.lastrowid:04d}', row.lastrowid))
    made = {
        'cohort': cohort,
        'diary': str(path),
        'customers': sorted(r[0] for r in db.execute('SELECT phone FROM customers')),
        'bookings': [
            dict(zip(('booking_id', 'phone', 'service', 'day', 'at', 'revision', 'active'), r, strict=True))
            for r in db.execute('SELECT booking_id, phone, service, day, at, revision, active FROM bookings')
        ],
        'counts': {t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in ('operations', 'intents', 'complaints')},
    }
    db.close()
    (state / 'seed.json').write_text(json.dumps(made, indent=1) + '\n')
    print(json.dumps(made, indent=1))


if __name__ == '__main__':
    main()
