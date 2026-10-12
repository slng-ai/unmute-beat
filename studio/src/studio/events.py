"""What a run says, in order: the one format the live view, the store and a replay all read.

The events are core's own (`core.events`): a call's listener gets them, numbered per run (`seq`,
from 0), stamped with the caller turn they belong to, and the runner sends them on to the studio as
they come (`studio.runner`). The web page draws the live view from that stream, the store keeps every
event (Redis for a week, Postgres behind it), and a replay is the same events read back. Nothing in
the studio is drawn from anything else.

Core imports no studio code, so the models live in core and are only named here.
"""

from core.events import (
    EVENT,
    BotSpeech,
    CallerSpeech,
    Event,
    IntentChanged,
    ModelDecision,
    ModelDelta,
    ModelEnded,
    ModelRequest,
    ReceiptFiled,
    RunEnded,
    RunStarted,
    StateCommitted,
    StepEntered,
    TurnFinished,
    TurnSnapshotted,
)

__all__ = [
    'EVENT',
    'BotSpeech',
    'CallerSpeech',
    'Event',
    'IntentChanged',
    'ModelDecision',
    'ModelDelta',
    'ModelEnded',
    'ModelRequest',
    'ReceiptFiled',
    'RunEnded',
    'RunStarted',
    'StateCommitted',
    'StepEntered',
    'TurnFinished',
    'TurnSnapshotted',
]
