"""Which load calls did not book, and whether their time was already taken in the shared diary when they rang.

Usage: `python unbooked.py <step folder>`. A fixture asks for one day and hour;
the salon has one chair. A call whose day and hour an earlier call already
booked cannot book it, by design: it hears the other times instead.
"""

import json
import sqlite3
import sys
from pathlib import Path

folder = Path(sys.argv[1])
db = sqlite3.connect(folder / 'diary.db')
booked = {
    row[0]: (row[1], row[2]) for row in db.execute('SELECT phone, day, at FROM bookings WHERE active = 1')
}
fixtures = [json.loads(p.read_text()) for p in sorted((folder / 'fixtures').glob('*.json'))]
taken: dict[tuple[str, str], str] = {}
for day, at, phone in db.execute('SELECT day, at, phone FROM bookings WHERE active = 1 ORDER BY id'):
    taken.setdefault((day, at), phone)
unbooked = [f for f in fixtures if f['phone'] not in booked]
wanted = {(f['day'], f['time']) for f in unbooked}
print(f'{len(fixtures)} calls, {len(booked)} booked, {len(unbooked)} not booked')
by_variant: dict[str, int] = {}
for f in unbooked:
    lane = f['name'].split('-l')[1].split('-')[0]
    by_variant[f'lane {lane}'] = by_variant.get(f'lane {lane}', 0) + 1
print('not booked, by lane:', by_variant)
print('distinct (day, hour) wanted by unbooked calls:', len(wanted))
dates = {f['day']: booked[f['phone']][0] for f in fixtures if f['phone'] in booked}
held = {(day, at[:5]) for day, at in taken}
free = [f for f in unbooked if (dates.get(f['day']), f['time']) not in held]
print(f'not booked although their time was never booked by anyone: {len(free)}')
for f in free:
    print('  ', f['name'], f['day'], f['time'])
