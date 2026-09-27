"""Offline evidence, after the live worker-failure pair: what a new call for the victim would find in the pair's diary.

    cd use-cases/salon-graph
    PYTHONPATH=. GOOGLE_API_KEY=offline uv run python ../../docs/research/reusable-core/session-4f/ringback_offline.py

No model, no network, no call placed. It opens a salon session for +34671540101 on a copy of
`live/state-pair/diary.db`, with every earlier call over (as `server.py` says once the dead worker's call is
closed as lost). The diary files a booking and its result in one transaction, so the booking the killed worker
committed is not owed: nothing is left unresolved, nothing is sent again, and the new session sees the booking
as held. This is a scripted check of the persisted backend, not a live reconnection: the killed call did not
continue.
"""

import shutil
import sqlite3
import tempfile
from pathlib import Path

from salon.app import FLOW, open_call
from salon.diary import Diary
from salon.functions import held_bookings

HERE = Path(__file__).resolve().parent
VICTIM = '+34671540101'

with tempfile.TemporaryDirectory() as tmp:
    copy = Path(tmp) / 'diary.db'
    shutil.copy(HERE / 'live' / 'state-pair' / 'diary.db', copy)
    diary = Diary(path=str(copy))
    call = open_call(
        FLOW,
        '',
        diary,
        key='offline-ringback',
        settle=lambda sid: True,
        verified_phone=VICTIM,
        customer_status='existing',
    )
    unresolved = diary.unresolved(VICTIM)
    db = sqlite3.connect(copy)
    bookings = db.execute(
        'SELECT booking_id, day, at, revision, active FROM bookings WHERE phone = ?', (VICTIM,)
    ).fetchall()
    results = db.execute(
        'SELECT o.result FROM intents i JOIN operations o ON o.token = i.id WHERE i.subject = ?', (VICTIM,)
    ).fetchall()
    db.close()
    held = held_bookings(call.services, call.state.facts)
    print('unresolved intents:', unresolved)
    print('restored into the new session:', [(i.operation, i.status) for i in call.intents])
    print('bookings for the victim:', bookings)
    print('result on file for its intent:', results)
    print('held, as the new session offers it:', dict(held.options), '|', held.say)
    assert unresolved == [] and not call.intents and len(bookings) == 1 and len(results) == 1, (
        'not as designed'
    )
    assert bookings[0][0] in held.options, 'the new session did not see the committed booking'
    print(
        'offline ring-back ok: the committed booking is on file once, owed nothing, and held for the caller'
    )
