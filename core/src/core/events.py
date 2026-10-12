"""What a call says about itself, in order: the events a listener gets, and the queue that carries them.

One optional listener per call (`CallContext.listen`), set by whoever opens it: the studio's runner,
a test, a log. Core calls it without waiting on it: every event goes on a bounded queue a background
task drains (`Events`), so a slow listener can never slow a turn, and a full queue drops the event and
counts it (`Events.dropped`). With no listener, nothing is built and nothing is queued: each place
that would emit checks `CallContext._events` first.

Every event is numbered per call (`seq`, from 0, no gaps among those delivered), stamped with the
caller turn it belongs to (0 is the greeting, before the caller first speaks) and with milliseconds
since the listener was attached, on the process's monotonic clock. They are what the studio's live
view, its store and a replay read; nothing in them changes what the call does.

Where each comes from:

    run.started      the opener (`Events.started`): agent, config version, channel, entry state
    caller.speech    `CallContext.turn_started`: the words a turn (or a continued piece) gets
    step.entered     `CallContext.commit`, whenever the step changes
    model.request    the turn engine's `before_model_request` hook, per provider request
    model.delta      each streamed piece: speech, or a tool call's arguments as they arrive
    model.decision   each tool or output the model called, and each one core sent back
    model.ended      the `after_model_request` hook: tokens, duration, time to first part
    state.committed  `CallContext.commit`, whenever the application's facts change
    intent.changed   `CallContext.intent_for`, `set_intent`, `restore`: a business action moved
    receipt          `CallContext.note_evidence`: a validated receipt, filed
    bot.speech       `note_said` and the closing of a streamed segment (queued), playback evidence
                     (delivered), a cut (cut)
    turn.finished    the end of every turn, as `core.turn.TurnFinished` says it
    turn.snapshot    after every turn of a flow call: `core.snapshot.TurnSnapshot`, as JSON
    run.ended        the opener (`Events.ended`), or `Events.close`

`EVENT.validate_json(line)` reads one event back; `EVENT.json_schema()` is what a web page's types
mirror. A listener may fall behind by `Events.QUEUE` events before new ones are dropped, and counted.
"""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any, Literal, Protocol

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Event(BaseModel):
    """_Event.

    Attributes:
        turn: The caller turn, from 1; 0 is before the caller first speaks (the greeting).
        t_ms: Milliseconds since the listener was attached, on the process's monotonic clock.
    """

    model_config = ConfigDict(extra='forbid', frozen=True)

    run: str
    seq: int = Field(ge=0)
    turn: int = Field(ge=0)
    t_ms: float = Field(ge=0)


class RunStarted(_Event):
    """RunStarted.

    Attributes:
        config: The config version it runs on: the studio's hash of the manifest and its files.
        forked_from: The run this one carries on from, when it went back to a turn of it
            (`core.snapshot`).
    """

    type: Literal['run.started'] = 'run.started'
    agent: str
    config: str = ''
    channel: Literal['voice', 'text', 'twilio']
    entry: str
    state: dict[str, Any]
    forked_from: str | None = None
    forked_turn: int | None = None


class CallerSpeech(_Event):
    """The words a caller turn gets: a new turn, or a piece that joins the one not yet answered.

    Attributes:
        continues_turn: The caller cut in before an answer played out: these words join the same
            turn (`CallContext.continues_turn`).
    """

    type: Literal['caller.speech'] = 'caller.speech'
    phase: Literal['started', 'partial', 'final', 'stopped'] = 'final'
    text: str = ''
    continues_turn: bool = False


class StepEntered(_Event):
    """StepEntered.

    Attributes:
        source: The step the caller came from; None on entry.
        via: Why, where core knows it on the spot (`exit`, `consult`, `handback`, `resume`); ''
            otherwise. The studio labels the rest from the engine's own moves (`studio.agents.catalog`),
            by source and step.
    """

    type: Literal['step.entered'] = 'step.entered'
    step: str
    source: str | None
    via: str = ''


class ModelRequest(_Event):
    """One provider request is about to go out, with its place in the turn's budget.

    Attributes:
        request: This turn's request number.
        budget: The turn's request limit; 0 when it has none.
    """

    type: Literal['model.request'] = 'model.request'
    step: str
    request: int = Field(ge=1)
    budget: int = Field(ge=0)


class ModelDelta(_Event):
    """A streamed piece of a response: speech, or a tool call's arguments as the model writes them.

    Attributes:
        name: The tool or output being called, for `args`.
    """

    type: Literal['model.delta'] = 'model.delta'
    step: str
    kind: Literal['text', 'args'] = 'text'
    name: str = ''
    text: str


class ModelDecision(_Event):
    """One tool or output the model called, as it called it; or core's refusal of one, which the model then retries.

    Attributes:
        name: The tool or output: `reply`, `record`, an operation, `hand_over`, `goodbye`, `done`, a
            read, `options`.
        refused: Why core sent it back, when it did.
    """

    type: Literal['model.decision'] = 'model.decision'
    step: str
    name: str
    args: dict[str, Any] = {}
    refused: str = ''


class ModelEnded(_Event):
    """That request is over, however it went: its tokens, how long it took, and when its first part came.

    Attributes:
        first_ms: From the request going out to its first part arriving; None when no part came.
    """

    type: Literal['model.ended'] = 'model.ended'
    step: str
    model: str = ''
    tokens_in: int = 0
    tokens_out: int = 0
    ms: float = 0
    first_ms: float | None = None


class StateCommitted(_Event):
    """The application state after a change committed: whole, as core replaces it."""

    type: Literal['state.committed'] = 'state.committed'
    state: dict[str, Any]
    changed: list[str]


class IntentChanged(_Event):
    """A business action moved: prepared, dispatched, then settled or not (`core.state.Intent`)."""

    type: Literal['intent.changed'] = 'intent.changed'
    intent: str
    operation: str
    status: Literal['prepared', 'dispatched', 'unknown', 'succeeded', 'rejected', 'withdrawn']
    args: dict[str, Any] = {}


class ReceiptFiled(_Event):
    """A validated receipt, as core filed it (`core.state.Evidence`)."""

    type: Literal['receipt'] = 'receipt'
    operation: str
    intent: str = ''
    receipt: dict[str, Any]
    succeeded: bool = True


class BotSpeech(_Event):
    """One spoken segment and what is known about whether it was heard (`core.state.Spoken`).

    Attributes:
        reason: The segment's origin as core records it: `entry`, `model`, `ask`, `confirmation`,
            `tool_filler`, ...
        evidence: What proved delivery, when it is `delivered`: `mark` (a transport's own report),
            `ack` (a text reader), `local`.
    """

    type: Literal['bot.speech'] = 'bot.speech'
    segment: str
    phase: Literal['queued', 'delivered', 'cut']
    text: str = ''
    source: Literal['model', 'code'] = 'model'
    reason: str = ''
    evidence: str = ''


class TurnFinished(_Event):
    """How the turn ended. Mirrors `core.turn.TurnFinished`."""

    type: Literal['turn.finished'] = 'turn.finished'
    status: Literal['answered', 'interrupted', 'exhausted', 'failed']
    step: str
    handoffs: list[str] = []
    requests: int = 0
    detail: str = ''


class TurnSnapshotted(_Event):
    """What going back to the end of this turn needs (`core.snapshot.TurnSnapshot`, as JSON)."""

    type: Literal['turn.snapshot'] = 'turn.snapshot'
    snapshot: dict[str, Any]


class RunEnded(_Event):
    """RunEnded.

    Attributes:
        dropped: Events dropped because the listener fell behind (`Events.QUEUE`).
    """

    type: Literal['run.ended'] = 'run.ended'
    outcome: Literal['goodbye', 'hangup', 'ceiling', 'failed', 'stopped']
    detail: str = ''
    dropped: int = 0


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
    | ReceiptFiled
    | BotSpeech
    | TurnFinished
    | TurnSnapshotted
    | RunEnded,
    Field(discriminator='type'),
]

EVENT: TypeAdapter[Event] = TypeAdapter(Event)


class CallListener(Protocol):
    """Whoever wants a call's events. Awaited off the turn's path, one event at a time, in order."""

    async def event(self, event: Event) -> None:
        """One event of the call, in order."""


class Events:
    """One call's events on their way to its listener: numbered, stamped, queued, and drained by a task of their own.

    `emit` never waits and never raises: it builds the event, puts it on the queue, and starts the
    draining task on first use (so a call opened outside a loop can still get a listener). A listener
    that raises is logged, and the next event goes on. `close` delivers what is queued, within a bound.
    """

    QUEUE = 2048

    def __init__(self, listener: CallListener, run: str, *, size: int = QUEUE) -> None:
        self.listener = listener
        self.run = run
        self.dropped = 0
        self._seq = 0
        self._origin = time.perf_counter()
        self._queue: asyncio.Queue[_Event | None] = asyncio.Queue(maxsize=size)
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def emit(self, kind: type[_Event], turn: int, /, **fields: Any) -> None:
        """Queue one event of this kind for this turn. Dropped, and counted, when the listener is too far behind."""
        if self._closed:
            return
        if self._queue.full():
            self.dropped += 1
            return
        try:
            event = kind(
                run=self.run,
                seq=self._seq,
                turn=turn,
                t_ms=round((time.perf_counter() - self._origin) * 1000, 3),
                **fields,
            )
        except ValueError as error:
            logger.warning('event {} not sent: {}', kind.__name__, error)
            return
        self._seq += 1
        self._queue.put_nowait(event)
        self._start()

    def started(
        self,
        *,
        agent: str,
        channel: Literal['voice', 'text', 'twilio'],
        entry: str,
        state: dict[str, Any],
        **fields: Any,
    ) -> None:
        """The opener's first event: what is running, on what, from where."""
        self.emit(RunStarted, 0, agent=agent, channel=channel, entry=entry, state=state, **fields)

    def ended(
        self,
        turn: int,
        outcome: Literal['goodbye', 'hangup', 'ceiling', 'failed', 'stopped'],
        detail: str = '',
    ) -> None:
        """The call's last event: how it ended, and how many events its listener missed."""
        self.emit(RunEnded, turn, outcome=outcome, detail=detail, dropped=self.dropped)

    def _start(self) -> None:
        """Start the task that drains the queue, once, when there is a loop to run it on."""
        if self._task is not None:
            return
        try:
            self._task = asyncio.get_running_loop().create_task(self._drain())
        except RuntimeError:
            return

    async def _drain(self) -> None:
        """Hand each event to the listener in order; a listener that fails on one is logged and given the next."""
        while (event := await self._queue.get()) is not None:
            try:
                await self.listener.event(event)  # ty: ignore[invalid-argument-type]
            except Exception:
                logger.exception('the call listener failed on {}', type(event).__name__)

    async def close(self, timeout: float = 5.0) -> None:
        """Deliver what is queued, then stop: no event is accepted after this. Bounded by `timeout`."""
        if self._closed:
            return
        self._closed = True
        self._start()
        if self._task is None:
            return
        await self._queue.put(None)
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout)
        except TimeoutError:
            logger.warning('the call listener did not take its last events in {:g}s', timeout)
            self._task.cancel()
