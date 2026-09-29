"""The salon's startup: its manifest, compiled once, and one session per call. Nothing else.

    flow = salon.app.FLOW                          # compiled from agent.yaml at import
    call = salon.app.open_call(flow, '+34600111222', diary)

What the salon wrote is `agent.yaml`, `state.py`, `receipts.py`,
`operations.py`, `offers.py`, `capabilities.py` and the prompts; `diary.py`
stands in for the business's own backend.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import core
from salon.diary import Diary
from salon.operations import Salon
from salon.state import SalonState

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
    """A new session: the carrier's number when it is a real one (core checks), the diary, this call's key.

    It does **not** empty the diary: the diary is the business, shared by
    everyone who rings. Without one the call gets its own empty diary, which
    is what a test wants and a bot must never rely on. `settle` says which
    earlier calls are over, so what they still owe this caller is settled first.
    """
    salon = Salon(diary if diary is not None else Diary(), key or uuid4().hex[:12], settle)
    return flow.open(SalonState(**facts), services=salon, caller_id=from_number)
