"""Nosh's startup: its manifest, compiled once, and one session per call. Nothing else.

flow = nosh.app.FLOW                    # compiled from agent.yaml at import
call = nosh.app.open_call(flow)         # the place tools from TOOLS_URL and TOOLS_API_KEY
core.greet(call, nosh.app.greeting(flow, call))
"""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import core
from nosh.operations import Nosh
from nosh.state import NoshState
from nosh.tools import Tools

SPEC = core.load(Path(__file__).with_name('agent.yaml'))
"""The manifest, checked whole: an unknown key, a missing file or a prompt naming no field fails here, at import."""
FLOW = SPEC.compile()
"""The compiled flow every call of this process shares. Calls share nothing else."""


def open_call(
    flow: core.StepFlow,
    tools: Tools | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
    **facts: Any,
) -> core.FlowCall:
    """A new session with its own offers. Without `tools`, the Render place tools from the environment."""
    nosh = Nosh(tools if tools is not None else Tools(), clock)
    return flow.open(NoshState(**facts), services=nosh, clock=clock)


def greeting(flow: core.StepFlow, call: core.FlowCall) -> Any:
    """The opening line: Nosh's greeting, then the question for where to look."""
    return flow.greeting(call, SPEC.greeting)
