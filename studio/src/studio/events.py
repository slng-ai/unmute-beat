"""What a run says, in order: the one format the live view, the store and a replay all read.

The engine publishes one `Event` per thing that happened, numbered per run (`seq`, from 0, no
gaps) and stamped with the caller turn it belongs to. The web page draws the live view from
the stream (Redis), the store keeps every event (Postgres `events`), and a replay is the same
events read back. Nothing in the studio is drawn from anything else.

Each event names where in core it comes from (`docs/studio/README.md`, "Events"). Some are
core's own already (`core.turn.TextDelta`, `TurnFinished`, the speech ledger's marks); the rest
need a small observer in core that the engine subscribes to. None of them changes what a call
does: the studio only listens.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Event(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    run: str
    seq: int = Field(ge=0)
    turn: int = Field(ge=0)
    """The caller turn, from 1; 0 is before the caller first speaks (the greeting)."""
    t_ms: float = Field(ge=0)
    """Milliseconds since `run.started`, on the engine's monotonic clock."""


class RunStarted(_Event):
    type: Literal['run.started'] = 'run.started'
    agent: str
    config: str
    """The config version it runs on: `agent_configs.version`."""
    channel: Literal['voice', 'text', 'twilio']
    entry: str
    state: dict[str, Any]
    forked_from: str | None = None
    forked_turn: int | None = None


class CallerSpeech(_Event):
    """The caller's voice: the detector's onset, the transcript as it grows, and the words a turn gets."""

    type: Literal['caller.speech'] = 'caller.speech'
    phase: Literal['started', 'partial', 'final', 'stopped']
    text: str = ''
    continues_turn: bool = False
    """The caller cut in before an answer played out: these words join the same turn (`CallContext.continues_turn`)."""


class StepEntered(_Event):
    type: Literal['step.entered'] = 'step.entered'
    step: str
    source: str | None
    """The step the caller came from; None on entry."""
    via: Literal['entry', 'exit', 'consult', 'handback', 'prerequisite', 'return', 'route', 'then']


class ModelRequest(_Event):
    """One provider request is about to start. Mirrors `core.turn.RunStarted`, with its place in the budget."""

    type: Literal['model.request'] = 'model.request'
    step: str
    request: int = Field(ge=1)
    """This turn's request number."""
    budget: int = Field(ge=1)


class ModelDelta(_Event):
    """The decision's words as they stream. Mirrors `core.turn.TextDelta`: speech, never a tool's arguments."""

    type: Literal['model.delta'] = 'model.delta'
    step: str
    text: str


class ModelDecision(_Event):
    """The one typed output a request returned, as committed (`core.structured`)."""

    type: Literal['model.decision'] = 'model.decision'
    step: str
    output: Literal['reply', 'record', 'operation', 'hand_over', 'goodbye', 'done', 'read', 'options']
    name: str = ''
    """The operation, read or destination, where the output has one."""
    args: dict[str, Any] = {}
    refused: str = ''
    """Why core sent it back, when it did; the model then retries in the same turn."""


class ModelEnded(_Event):
    """That request is over, however it went. Mirrors `core.turn.RunEnded`, with its cost."""

    type: Literal['model.ended'] = 'model.ended'
    step: str
    tokens_in: int = 0
    tokens_out: int = 0
    ms: float = 0


class StateCommitted(_Event):
    """The application state after an update committed: whole, as core replaces it."""

    type: Literal['state.committed'] = 'state.committed'
    state: dict[str, Any]
    changed: list[str]
    reset: list[str] = []
    """Fields cleared because what they rested on changed (choices' `after:`)."""


class IntentChanged(_Event):
    """A business action moved: prepared, dispatched, then settled or not (`core.state.Intent`)."""

    type: Literal['intent.changed'] = 'intent.changed'
    intent: str
    operation: str
    status: Literal['prepared', 'dispatched', 'unknown', 'succeeded', 'rejected', 'withdrawn']
    args: dict[str, Any] = {}
    receipt: dict[str, Any] | None = None


class BotSpeech(_Event):
    """One spoken segment and what is known about whether it was heard (`core.state.Spoken`)."""

    type: Literal['bot.speech'] = 'bot.speech'
    segment: str
    phase: Literal['queued', 'playing', 'delivered', 'cut', 'cleared']
    text: str = ''
    source: Literal['model', 'code'] = 'model'
    reason: Literal[
        'greeting', 'reply', 'readback', 'offer', 'receipt', 'recap', 'goodbye', 'fixed', 'hold'
    ] = 'reply'
    evidence: str = ''
    """What proved delivery, when it is `delivered`: a Twilio mark, a browser playout report, a text acknowledgement."""


class TurnFinished(_Event):
    """How the turn ended. Mirrors `core.turn.TurnFinished`."""

    type: Literal['turn.finished'] = 'turn.finished'
    status: Literal['answered', 'interrupted', 'exhausted', 'failed']
    step: str
    handoffs: list[str] = []
    requests: int = 0
    detail: str = ''


class RunEnded(_Event):
    type: Literal['run.ended'] = 'run.ended'
    outcome: Literal['goodbye', 'hangup', 'ceiling', 'failed', 'stopped']
    detail: str = ''


type Event = Annotated[
    RunStarted
    | CallerSpeech
    | StepEntered
    | ModelRequest
    | ModelDelta
    | ModelDecision
    | ModelEnded
    | StateCommitted
    | IntentChanged
    | BotSpeech
    | TurnFinished
    | RunEnded,
    Field(discriminator='type'),
]

EVENT: TypeAdapter[Event] = TypeAdapter(Event)
"""`EVENT.validate_json(line)` reads one event off the stream; `EVENT.json_schema()` is what the web types mirror."""
