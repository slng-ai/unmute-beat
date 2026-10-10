"""Kestrel, as every launcher needs it (`core.Entry`): its flow, its claims desk, what it says first."""

from pathlib import Path

import core
from kestrel import app
from kestrel.claims import Claims

CLAIMS = Claims()
"""The fake claims desk, shared by every call of this process."""

ENTRY = core.Entry(
    id=app.SPEC.id or 'kestrel',
    name='kestrel call',
    flow=lambda: app.FLOW,
    open=lambda flow, opening: app.open_call(flow, CLAIMS),
    greeting=app.greeting,
    manifest=Path(app.__file__).with_name('agent.yaml'),
)
