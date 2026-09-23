"""Sage and Stone: four agents, one call, one shared state, compiled from `salon/flow.yaml`.

The steps themselves live in `salon/steps/`, and what they are built from in
`salon/nodes.py`. This module is the table the rest of the code runs, and the
names callers of the salon have always imported from here.
"""

from __future__ import annotations

from typing import cast

from core.graph import Node
from salon.classification import MODEL, SETTINGS
from salon.instructions import GREETING, SPEC
from salon.models.state import SalonState
from salon.nodes import advance, build, complaint_ready, decided, solve, verified
from salon.tools.booking import ask_booking, read_diary, save_booking
from salon.tools.complaints import ask_complaint, record_complaint
from salon.tools.identity import ask_number, confirm_customer, stop_verifying, verification_guidance
from salon.tools.policy import ACTIONS

__all__ = [
    'ACTIONS',
    'AGENTS',
    'GREETING',
    'MODEL',
    'SETTINGS',
    'advance',
    'ask_booking',
    'ask_complaint',
    'ask_number',
    'build',
    'complaint_ready',
    'confirm_customer',
    'decided',
    'read_diary',
    'record_complaint',
    'save_booking',
    'solve',
    'stop_verifying',
    'verification_guidance',
    'verified',
]

AGENTS = cast(dict[str, Node[SalonState]], SPEC.compile(model=MODEL, settings=SETTINGS).nodes)
"""The graph. If an agent's words should be heard, it is a step in here; if not, it is a delegation from inside a tool.

`booking` appears twice among the front desk's exits in `flow.yaml`, and the pair is the
point: unguarded, it is a `hand_over` the model may choose whoever is calling,
and whether anyone has to be identified first is the graph's business;
guarded on `verified`, it also lends `read_diary`, so an identified caller
moves into `booking` by the diary being read rather than by a request that
exists only to say "booking". `verify` is an exit only once somebody has been
identified, because before that `requires` interposes it. The verify step's
own exit is guarded too: an incomplete number is a reason to ask again, not a
reason to leave.
"""
