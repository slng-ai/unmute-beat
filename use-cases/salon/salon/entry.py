"""The salon, as every launcher needs it (`core.Entry`): its flow, its diary, how a call opens, what it says first.

`bot.py`, the bounded `server.py` and the studio's runner all run a call from this one entry.
"""

import os
from datetime import datetime

import core
from salon import app
from salon.diary import Diary

DIARY = Diary(
    path=os.getenv('DIARY_PATH') or None,
    clock=datetime.fromisoformat(os.environ['DIARY_CLOCK']) if os.getenv('DIARY_CLOCK') else None,
)
"""The salon: one SQLite file (`DIARY_PATH`) every worker process shares, so calls on two workers
contend for one chair. Unset: a private file for this process only, for development. `DIARY_CLOCK`
pins the salon's clock, for a load run whose days must not move."""


def open_call(flow: core.StepFlow, opening: core.Opening) -> core.FlowCall:
    """A call on the shared diary, keyed by the launcher's key, settling what earlier calls owe its caller."""
    return app.open_call(flow, opening.caller_id, diary=DIARY, key=opening.key, settle=opening.settle)


ENTRY = core.Entry(
    id=app.SPEC.id or 'salon',
    name='salon call',
    flow=lambda: app.FLOW,
    open=open_call,
    greeting=lambda flow, call: app.GREETING,
)
