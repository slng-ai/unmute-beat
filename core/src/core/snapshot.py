"""Going back to the end of a turn and carrying on: what a turn leaves behind, saved, and put back in order.

Where the caller is lives on the call, not in the graph: each turn is one short `pydantic_graph` run
with no graph state (`core.flow`), so nothing of the graph is saved. What a turn leaves behind is all
on the `FlowCall`, and `TurnSnapshot` holds it as plain, validated data:

    step, pending, consulting, facts   `call.state`, the application's facts as JSON
    messages                           `call.messages`, Pydantic AI's own messages with core's tags
    speech                             the speech ledger: what the bot said and whether it was heard
    evidence, intents                  every validated receipt, every business action and its status
    turn, arrived, inputs, segments    the turn number, when each step was reached, the caller's words

The speech ledger matters: core builds the bot's side of the history from it, so a history restored
without it reaches the model with every bot line blank.

Putting it back (`restore`, through `StepFlow.open(resume=...)` or `StepFlow.resume`) goes in this
order. The call's clock is the snapshot's, moving on from it, so "Thursday" means the same day. The
history, the speech ledger (all of it in the past), the turn number and the receipts go back first;
only then are the facts, edited or not, validated whole with the context core always uses, the
snapshot's facts as `before` and the caller's words from the history (a `Quoted` field reads them).
An edit the application's types refuse raises `ValidationError` and nothing is opened. The step,
`pending` and `consulting` are set only when each names a step of this flow. Actions: one that was
sent and never settled comes back unknown and is reconciled with the business before anything new
is sent (`CallContext.restore`); one settled or withdrawn comes back as it was, so a read-back core
gave up on stays given up on; one only prepared is dropped, so its step asks again. Offers and a
waiting read-back are never restored: the step offers again for the state as it is now, and a
read-back is asked again, fresh. A yes never carries across.

What going back cannot undo is the business: a booking made after the turn stays made.

ponytail: which receipt set a state field (`FlowCall._assigned_by`) is not saved, so a field a
receipt set before the snapshot is not reset when what it rested on changes after it. Rebuild it
from `evidence` if a resumed call ever needs that reset.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.messages import ModelMessage

from core.state import Evidence, Intent, Spoken

if TYPE_CHECKING:
    from core.steps import FlowCall, StepFlow

SCHEMA = 1
"""Bumped when a field changes meaning: a snapshot of another schema is refused on restore."""


class SpokenRecord(BaseModel):
    """One segment of the speech ledger, without the in-process clock values: on restore it is all in the past."""

    kind: Literal['fixed', 'model']
    text: str
    origin: str
    id: str
    attempt: int = 0
    turn: int = 0
    step: str = ''
    response: str = ''
    generated: str = ''
    interrupted: bool = False
    delivery: Literal['submitted', 'runtime-estimate'] = 'submitted'
    playback: Literal['queued', 'playing', 'played'] = 'queued'
    heard_by: Literal['', 'signal', 'ack', 'estimate', 'mark', 'local'] = ''
    simulated: bool = False
    proposal: int = 0
    played: bool = False
    """It had played when the snapshot was taken (`Spoken.played_at` was set)."""
    cleared: bool = False
    lost: Literal['', 'cleared', 'silent', 'failed'] = ''

    @classmethod
    def of(cls, segment: Spoken) -> SpokenRecord:
        return cls(
            kind=segment.kind,
            text=segment.text,
            origin=segment.origin,
            id=segment.id,
            attempt=segment.attempt,
            turn=segment.turn,
            step=segment.step,
            response=segment.response,
            generated=segment.generated,
            interrupted=segment.interrupted,
            delivery=segment.delivery,
            playback=segment.playback,
            heard_by=segment.heard_by,
            simulated=segment.simulated,
            proposal=segment.proposal,
            played=segment.played_at is not None,
            cleared=segment.cleared,
            lost=segment.lost,
        )

    def spoken(self) -> Spoken:
        """The ledger's segment again, timed at the clock's zero: before anything this process says."""
        return Spoken(
            self.kind,
            self.text,
            origin=self.origin,  # ty: ignore[invalid-argument-type]
            id=self.id,
            attempt=self.attempt,
            at=0.0,
            interrupted=self.interrupted,
            turn=self.turn,
            step=self.step,
            response=self.response,
            generated=self.generated,
            delivery=self.delivery,
            playback=self.playback,
            heard_by=self.heard_by,
            simulated=self.simulated,
            proposal=self.proposal,
            played_at=0.0 if self.played else None,
            cleared=self.cleared,
            lost=self.lost,
        )


class EvidenceRecord(BaseModel):
    """One validated receipt (`core.state.Evidence`), field for field."""

    operation: str
    args: dict[str, Any]
    receipt: dict[str, Any]
    identity: str
    turn: int
    generation: int
    proposal: int = 0
    segments: tuple[str, ...] = ()
    intent: str = ''
    succeeded: bool = True
    changes: bool = True
    resource: str = ''
    kind: str = ''
    version: int | None = None

    @classmethod
    def of(cls, evidence: Evidence) -> EvidenceRecord:
        return cls.model_validate(evidence, from_attributes=True)

    def evidence(self) -> Evidence:
        return Evidence(**self.model_dump())


class TurnSnapshot(BaseModel):
    """Everything going back to the end of one turn needs, saved by whoever listens (`turn.snapshot`)."""

    model_config = ConfigDict(ser_json_bytes='base64', val_json_bytes='base64')

    schema_version: int = SCHEMA
    run_id: str
    turn: int = Field(ge=0)
    app_version: str = ''
    call_now: datetime
    """The call's clock when the snapshot was taken: a resumed call's clock moves on from it."""
    agent: str
    pending: str | None = None
    consulting: str | None = None
    facts: dict[str, Any]
    """The application's facts as JSON, validated against the flow's state type on restore."""
    messages: list[ModelMessage]
    speech: list[SpokenRecord]
    evidence: list[EvidenceRecord] = []
    intents: list[Intent] = []
    arrived: dict[str, int] = {}
    inputs: dict[int, str] = {}
    segments: int = 0
    """Segments said so far: a resumed call's segment ids carry on from it, so none is reused."""

    def said(self) -> list[dict[str, Any]]:
        """The conversation up to this turn, in order: the caller's words and each bot line, for a page."""
        lines: list[dict[str, Any]] = []
        for turn in sorted({*self.inputs, *(s.turn for s in self.speech)}):
            if text := self.inputs.get(turn):
                lines.append({'turn': turn, 'who': 'caller', 'text': text})
            lines.extend(
                {'turn': turn, 'who': 'bot', 'text': s.text, 'heard': s.played and not s.interrupted}
                for s in self.speech
                if s.turn == turn and s.text
            )
        return lines


def take(call: FlowCall[Any], run: str = '') -> TurnSnapshot:
    """The snapshot of this call as it stands: at the end of a turn, its last turn."""
    facts = call.state.facts
    return TurnSnapshot(
        run_id=run or (call.events.run if call.events is not None else call.id),
        turn=call.turn,
        app_version=call._app_version,
        call_now=call.now(),
        agent=call.state.agent,
        pending=call.state.pending,
        consulting=call.state.consulting,
        facts=facts.model_dump(mode='json'),
        messages=deepcopy(call.messages),
        speech=[SpokenRecord.of(s) for s in call.all_spoken],
        evidence=[EvidenceRecord.of(e) for e in call.evidence],
        intents=list(call.intents),
        arrived=dict(call._arrived),
        inputs=dict(call._inputs),
        segments=call._segments,
    )


def clock_from(snapshot: TurnSnapshot) -> Callable[[], datetime]:
    """The snapshot's clock, moving on in real time from the moment the call is resumed."""
    resumed = datetime.now(UTC)
    then = snapshot.call_now
    return lambda: then + (datetime.now(UTC) - resumed)


def restore(
    flow: StepFlow, call: FlowCall[Any], snapshot: TurnSnapshot, facts: Mapping[str, Any] | None = None
) -> FlowCall[Any]:
    """Put a snapshot back into a call just opened on `flow`, with `facts` in place of the snapshot's when given.

    Raises `ValueError` for a snapshot of another schema, and `pydantic.ValidationError` for facts the
    application's types refuse: the call is then not to be used.
    """
    if snapshot.schema_version != SCHEMA:
        raise ValueError(f'snapshot schema {snapshot.schema_version}; this core reads schema {SCHEMA}')
    call._clock = clock_from(snapshot)
    call._today = None
    call._turn = snapshot.turn
    call._inputs = dict(snapshot.inputs)
    call._arrived = dict(snapshot.arrived)
    call._segments = max(call._segments, snapshot.segments)
    call.messages = deepcopy(snapshot.messages)
    call._protocol = deepcopy(snapshot.messages)
    call._speech_history = [record.spoken() for record in snapshot.speech]
    call._spoken.clear()
    call._open = None
    call._evidence = [record.evidence() for record in snapshot.evidence]
    call._playback_known = any(s.played for s in snapshot.speech)

    state_type = type(call.state.facts)
    call.state = call.state.model_copy(update={'facts': state_type.model_construct(**snapshot.facts)})
    validated = call.validated(dict(snapshot.facts if facts is None else facts))
    steps = flow.by_id
    agent = snapshot.agent if snapshot.agent in steps else flow.start
    pending = snapshot.pending if snapshot.pending in steps else None
    consulting = snapshot.consulting if snapshot.consulting in steps else None
    call.state = type(call.state)(agent=agent, pending=pending, consulting=consulting, facts=validated)
    call._version += 1

    settled = [i for i in snapshot.intents if i.status in ('succeeded', 'rejected', 'withdrawn')]
    for intent in settled:
        call._intents[intent.id] = intent
    if open_ones := [i for i in snapshot.intents if i.status in ('dispatched', 'unknown')]:
        call.restore(open_ones, call._reconcile_with)
    call._asked_owed.clear()
    call.take_owed()
    return call


__all__ = ['SCHEMA', 'EvidenceRecord', 'SpokenRecord', 'TurnSnapshot', 'restore', 'take']
