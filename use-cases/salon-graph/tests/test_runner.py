"""The runner's admission record (`server.Admission`): bounded, claimed once, and never held by a dead worker.

* A1: five places over every worker; the sixth call is refused, and a place
  comes back when a call ends;
* A2: one CallSid is one call: admitted twice is one place, and its stream
  is claimed once;
* A3: a stream nobody admitted is refused;
* A4: an admitted call whose stream never came gives its place back after
  `ADMIT_SECONDS`, and a call whose worker died is closed as `lost`;
* A5: `over` lists only calls that are over, which is what a new call may
  settle (`salon.app.open_call(settle=...)`);
* A6: the memory diagnostic (`server.live_counts`) sees a live call, and
  stops seeing it once it is gone;
* A7: `LIVE_FAULT` kills its worker after the named caller's commit, and only
  once `LIVE_FAULT_ARM` exists.

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


def admission() -> None:
    """A1 to A5: what the N11 row of the native comparison runs."""
    main()


def the_memory_diagnostic_sees_a_live_call():
    """A6 (session 4e): `/debug/live` counts a call that is alive, and stops counting it once it is gone.

    A call's class is `FlowCall[SalonState]`: counted by its own class name,
    live calls were always 0, and a clean post-call count proved nothing.
    """
    import gc

    from salon.app import FLOW, open_call

    gc.collect()
    before = server.live_counts(gc.get_objects())['FlowCall']
    call = open_call(FLOW, '+34600000001')
    assert type(call).__name__ != 'FlowCall', type(call).__name__
    assert server.live_counts(gc.get_objects())['FlowCall'] == before + 1, 'a live call was not counted'
    del call
    gc.collect()
    assert server.live_counts(gc.get_objects())['FlowCall'] == before, 'a dropped call was still counted'


LIVE_FAULT_CHILD = """
import os, sys, types
sys.path.insert(0, os.getcwd())
os.environ.setdefault('GOOGLE_API_KEY', 'x')
import server
saved = []
diary = types.SimpleNamespace(save_booking=lambda phone, *rest: saved.append(phone) or {'ok': True})
server._arm_live_fault(diary, 'committed:+34600000009')
diary.save_booking('+34600000001', 'book')
diary.save_booking('+34600000009', 'book')
print('alive', saved)
"""


def the_live_fault_waits_to_be_armed(folder: str):
    """A7 (session 4e): `LIVE_FAULT` kills its worker after the named caller's commit, and only once armed.

    Other callers' commits never trigger it. With `LIVE_FAULT_ARM` naming a
    file that does not exist yet, the commit goes through and the worker
    lives; once the file exists, the worker dies with the commit on file.
    """
    arm = os.path.join(folder, 'armed')
    env = {**os.environ, 'LIVE_FAULT_ARM': arm, 'RUNNER_DB': os.path.join(folder, 'runner.db')}
    done = subprocess.run([sys.executable, '-c', LIVE_FAULT_CHILD], env=env, capture_output=True, text=True)
    assert done.returncode == 0 and "alive ['+34600000001', '+34600000009']" in done.stdout, done
    Path(arm).touch()
    done = subprocess.run([sys.executable, '-c', LIVE_FAULT_CHILD], env=env, capture_output=True, text=True)
    assert done.returncode == 9 and 'alive' not in done.stdout, done


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        for index, check in enumerate((ceiling_and_release, one_call_one_place, nothing_is_held_forever)):
            check(os.path.join(tmp, f'runner{index}.db'))
    the_memory_diagnostic_sees_a_live_call()
    with tempfile.TemporaryDirectory() as tmp:
        the_live_fault_waits_to_be_armed(tmp)
    print('runner ok: A1-A7, bounded admission, one claim, nothing held by the dead, live calls counted')


if __name__ == '__main__':
    main()
