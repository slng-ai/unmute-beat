"""The runner's admission record (`server.Admission`): bounded, claimed once, and never held by a dead worker.

* A1: five places over every worker; the sixth call is refused, and a place
  comes back when a call ends;
* A2: one CallSid is one call: admitted twice is one place, and its stream
  is claimed once;
* A3: a stream nobody admitted is refused;
* A4: an admitted call whose stream never came gives its place back after
  `ADMIT_SECONDS`, and a call whose worker died is closed as `lost`;
* A5: `over` lists only calls that are over, which is what a new call may
  settle (`salon.app.open_call(settle=...)`).

The long checks (kills, drain, overload through the real runner) are
`python -m evals.load faults`.
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault('RUNNER_DB', os.path.join(tempfile.mkdtemp(prefix='runner-'), 'runner.db'))

import server
from server import Admission


def ceiling_and_release(path: str):
    """A1."""
    runner = Admission(path)
    assert all(runner.admit(f'CA{i}', 'c', '+34600000001', '') for i in range(server.MAX_CALLS))
    assert not runner.admit('CA-extra', 'c', '+34600000001', ''), 'a sixth call was admitted'
    assert runner.claim('CA0', 'MZ0') is not None
    runner.release('CA0', 'pipeline_end')
    assert runner.admit('CA-extra', 'c', '+34600000001', ''), 'a place did not come back'


def one_call_one_place(path: str):
    """A2 and A3."""
    runner = Admission(path)
    assert runner.admit('CA1', 'c', '', '') and runner.admit('CA1', 'c', '', '')
    assert len(runner.calls()) == 1
    assert runner.claim('CA1', 'MZ1') is not None
    assert runner.claim('CA1', 'MZ1') is None, 'one call was claimed twice'
    assert runner.claim('CA-never', 'MZ9') is None, 'a stream nobody admitted was run'


def nothing_is_held_forever(path: str):
    """A4 and A5."""
    runner = Admission(path)
    runner.admit('CA-slow', 'c', '', '')
    runner.admit('CA-dead', 'c', '', '')
    dead = subprocess.Popen([sys.executable, '-c', 'pass'])
    dead.wait()
    runner.claim('CA-dead', 'MZ')
    db = sqlite3.connect(Path(path))
    try:
        db.execute("UPDATE calls SET worker = ? WHERE call_sid = 'CA-dead'", (dead.pid,))
        db.execute("UPDATE calls SET admitted_at = ? WHERE call_sid = 'CA-slow'", (time.time() - 60,))
        db.commit()
    finally:
        db.close()
    rows = {c['call_sid']: c for c in runner.calls()}
    assert rows['CA-slow']['ended'] == 'never_connected', rows['CA-slow']
    assert rows['CA-dead']['ended'] == 'lost', rows['CA-dead']
    assert runner.over() == {'CA-slow', 'CA-dead'}
    runner.admit('CA-live', 'c', '', '')
    runner.claim('CA-live', 'MZ')
    assert 'CA-live' not in runner.over(), 'a live call was offered for settling'


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        for index, check in enumerate((ceiling_and_release, one_call_one_place, nothing_is_held_forever)):
            check(os.path.join(tmp, f'runner{index}.db'))
    print('runner ok: A1-A5, bounded admission, one claim, nothing held by the dead')


if __name__ == '__main__':
    main()
