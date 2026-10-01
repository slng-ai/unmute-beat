"""Kestrel's startup: its manifest, compiled once, and one session per call. Nothing else.

flow = kestrel.app.FLOW
call = kestrel.app.open_call(flow, claims)
core.greet(call, kestrel.app.greeting(flow, call))   # "... Can I take your policy number, please?"
"""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import core
from kestrel.claims import Claims
from kestrel.operations import Kestrel
from kestrel.state import ClaimState

SPEC = core.load(Path(__file__).with_name('agent.yaml'))
"""The manifest, checked whole: an unknown key, a missing file or a prompt naming a hidden field fails here."""
FLOW = SPEC.compile()
"""The compiled flow every call of this process shares. Calls share nothing else."""


def open_call(
    flow: core.StepFlow, claims: Claims, *, clock: Callable[[], datetime] | None = None, **facts: Any
) -> core.FlowCall:
    """A new session on the claims desk, with its own safety-line flag."""
    return flow.open(ClaimState(**facts), services=Kestrel(claims, clock), clock=clock)


def greeting(flow: core.StepFlow, call: core.FlowCall) -> Any:
    """The opening line: Kestrel's greeting, then the question for the policy number."""
    return flow.greeting(call, SPEC.greeting)
