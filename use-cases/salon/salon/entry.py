"""The salon, as every launcher needs it (`core.Entry`): its flow, its diary, how a call opens, what it says first.

`bot.py`, the bounded `server.py` and the studio's runner all run a call from this one entry.

`DIARY`: The salon: one SQLite file (`DIARY_PATH`) every worker process shares, so calls on two workers
contend for one chair. Unset: a fresh file, named in this process's environment as `DIARY_PATH`, so the
processes it starts share it too (a LiveKit worker runs each call in a process of its own), and removed
when this process ends. `DIARY_CLOCK` pins the salon's clock, for a load run whose days must not move.
"""

import atexit
import contextlib
import os
import tempfile
from datetime import datetime
from pathlib import Path

import core
from salon import app
from salon.diary import Diary


def shared_path() -> str:
    """`DIARY_PATH`, or a fresh file named there, so every process this one starts opens the same diary."""
    if path := os.getenv('DIARY_PATH'):
        return path
    handle, path = tempfile.mkstemp(prefix='salon-', suffix='.db')
    os.close(handle)
    os.environ['DIARY_PATH'] = path
    owner = os.getpid()

    def remove() -> None:
        """Remove the file in the process that made it, never in one that only inherited its name."""
        if os.getpid() == owner:
            for leftover in (path, f'{path}-journal', f'{path}-wal', f'{path}-shm'):
                with contextlib.suppress(OSError):
                    os.remove(leftover)

    atexit.register(remove)
    return path


DIARY = Diary(
    path=shared_path(),
    clock=datetime.fromisoformat(os.environ['DIARY_CLOCK']) if os.getenv('DIARY_CLOCK') else None,
)


def open_call(flow: core.StepFlow, opening: core.Opening) -> core.FlowCall:
    """A call on the shared diary, keyed by the launcher's key, settling what earlier calls owe its caller."""
    return app.open_call(flow, opening.caller_id, diary=DIARY, key=opening.key, settle=opening.settle)


ENTRY = core.Entry(
    id=app.SPEC.id or 'salon',
    name='salon call',
    flow=lambda: app.FLOW,
    open=open_call,
    greeting=lambda flow, call: app.GREETING,
    manifest=Path(app.__file__).with_name('agent.yaml'),
)
