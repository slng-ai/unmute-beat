"""The salon's startup: its manifest, compiled once, and one session per call. Nothing else.

    flow = salon.app.FLOW                          # compiled from agent.yaml at import
    call = salon.app.open_call(flow, '+34600111222', diary)

What the salon wrote is `agent.yaml`, `models/`, `prompts/`, `functions.py`
and `diary.py`. How a turn runs, how a reply is read, what authorises a
write and what is said about it are core's, the same for every application.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import core
from salon.diary import Diary
from salon.functions import Salon
from salon.models.state import SalonState, prefetch_number

SPEC = core.load(Path(__file__).with_name('agent.yaml'))
"""The manifest, checked whole: an unknown key, a missing file or a prompt naming no field fails here, at import."""
GREETING = SPEC.greeting
FLOW = SPEC.compile()
"""The compiled flow every call of this process shares: agents, operations and graph. Calls share nothing else."""


def open_call(
    flow: core.StepFlow,
    from_number: str = '',
    diary: Diary | None = None,
    *,
    key: str = '',
    settle: Callable[[str], bool] | None = None,
    **facts: Any,
) -> core.FlowCall:
    """A new session on the salon's flow: the carrier's number if it is a real one, the diary, this call's key.

    It does **not** empty the diary. The diary is the business, shared by
    everyone who rings; only the conversation is per call. Without a diary the
    call gets its own empty one, which is what a test wants and a bot must
    never rely on. `key` names the call in the diary (its offers, the intents
    it records); a runner passes the carrier's call id, so the diary's records
    and the call's trace share one name.

    `settle` says which earlier calls are over (`server.py` knows from its
    call record). Their intents for this caller that never got an answer
    are restored into the new call, so they are settled, by the diary, before
    anything new is sent: found, they are said as done; not found, they are
    voided, so a worker still holding one can never land it. An intent of a
    call still running somewhere is left alone. Without `settle`, none.
    """
    state = SalonState(customer_phone=prefetch_number(from_number), **facts)
    salon = Salon(diary if diary is not None else Diary(), key or uuid4().hex[:12])
    owed: list[core.Intent] = []
    if settle is not None and state.customer_phone:
        owed = [
            core.Intent(
                id=i['id'],
                operation=i['operation'],
                args=i['args'],
                subject=i['subject'],
                status='dispatched',
            )
            for i in salon.diary.unresolved(state.customer_phone)
            if i['session'] != salon.call and settle(i['session'])
        ]
    return flow.open(state, services=salon, clock=salon.now, intents=owed)
