"""What every agency shares: the validated state of one call, and the call itself.

`VoiceState` is the business facts of a call and nothing else. `CallContext`
holds that state, the model protocol, the ledger of everything the caller was
sent, and the line to the caller. An agency subclasses both; the turn engine
knows only what is here.
"""

from __future__ import annotations

import contextvars
import json
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from pydantic_ai.messages import ModelMessage, ModelRequest, SystemPromptPart, UserPromptPart

SayFn = Callable[[str], Awaitable[None]]
EndFn = Callable[[], Awaitable[None]]

turn_generation: contextvars.ContextVar[tuple[str, int] | None] = contextvars.ContextVar(
    'turn_generation', default=None
)
"""Which call, and which accepted caller turn of it, the code running right now belongs to.

A context variable rather than an argument because the thing that has to know
is `CallContext.commit`, and every tool in an agency would otherwise carry a
number it never reads. Tool tasks inherit the context they were spawned in, so
a tool still running after its turn was cancelled still reports that turn's
number while the call has moved on to the next. The call id is part of it
because the number only means anything within one call.
"""

STRICT = ConfigDict(validate_default=True, validate_assignment=True, revalidate_instances='always')
"""What every model holding business facts opts into.

Defaults are validated, so an authored default that breaks its own type fails
at import and not on the call that first reads it; assignments are validated;
and a nested model handed in is revalidated rather than trusted because it is
already an instance of the right class. None of the three is on by default,
and each one alone leaves a hole the other two do not cover.
"""


def present(value: object) -> bool:
    """Whether a prerequisite is filled, under one written-down rule.

    `None` is absent and a permitted empty string is absent. A legitimate
    `False` or `0` is **present**, which plain truthiness gets wrong: a field
    holding `False` because the caller said no has been answered, and a step
    waiting on it should not be interposed a second time.
    """
    return value is not None and value != ''


@dataclass
class Spoken:
    """One segment of speech the caller was actually sent, and who wrote it.

    A turn's delivered speech is not one string. An entry line, a question and
    a computed confirmation are written by this code and never appear in the
    model's history, so a record built from the history alone reports a turn
    that said three things as one that said one. `kind` keeps the two apart:
    "the agency answered" and "the model answered" are different claims, and
    only one of them costs a provider request.
    """

    kind: Literal['fixed', 'model']
    text: str
    origin: Literal[
        'entry', 'tool_filler', 'ask', 'confirmation', 'model', 'goodbye', 'error', 'guidance'
    ] = 'model'
    """Where this segment came from, so a trace can tell a question from a confirmation."""
    id: str = ''
    """Stable within the call: turn, then position in it. Matches a submitted segment to the audio that played it."""
    attempt: int = 0
    """Which run of the agency asked for it. A turn can have more than one."""
    tool_call_id: str = ''
    """The tool call it was covering, where there was one."""
    at: float = field(default_factory=time.perf_counter)
    """When this segment started going out, on the same clock as `Request.started`.

    Text handed to TTS, not audio the caller heard: playout is a boundary
    nothing here can see. Every segment is timed and not just the model's,
    because "let me check" going out quickly is not an answer going out quickly.
    """
    interrupted: bool = False
    """Cut down to what the caller actually heard, once the runtime says so."""
    turn: int = 0
    response: str = ''
    generated: str = ''
    delivery: Literal['submitted', 'runtime-estimate'] = 'submitted'


@dataclass
class Request:
    """One provider request, timed at the model boundary.

    `first_output` is the first part that came back, whatever it was, and
    `kind` says which: a tool call answers the request without the caller
    hearing anything, and a time to first token that cannot say which of the
    two it measured is not a number anybody can compare.
    """

    started: float
    first_output: float | None = None
    kind: Literal['', 'text', 'tool', 'classifier'] = ''
    duration: float | None = None
    fallback: bool = False

    @property
    def ttft(self) -> float | None:
        """Seconds from the request going out to its first part, or None if none came."""
        return None if self.first_output is None else round(self.first_output - self.started, 3)


@dataclass(frozen=True)
class Checkpoint:
    """Where a step that needed a prerequisite was, so it can be resumed when the prerequisite is met.

    One owner at a time: the graph forbids a satisfier with prerequisites of
    its own, so checkpoints never nest.
    """

    owner: str
    messages: list[ModelMessage]
    speech_ids: frozenset[str]
    heard: str


class Handoff(BaseModel):
    """Where the caller goes next. Agencies return this from their `hand_over` action and their finishers."""

    to: str
    because: str = Field(default='', description='One short sentence: what the caller wants. Never spoken.')
    status: Literal['handoff', 'completed', 'unserved'] = 'handoff'
    """A plain handoff, or a prerequisite step reporting that it finished, or that it gave up."""


@runtime_checkable
class Question(Protocol):
    """An agent output that is a question for the caller, worded by the application.

    The turn engine says it and ends the turn. It exists so the one sentence the
    next consequential action will be checked against is the sentence the
    caller actually heard, not a paraphrase the model wrote and then treated as
    having been agreed to.
    """

    def put(self, call: CallContext) -> str:
        """Record the question as pending, and hand back the words to say."""
        ...


class VoiceState(BaseModel):
    """The business facts of one call, and nothing else.

    Every supported write path is validated: authored defaults on the way in,
    assignments on the way back, and nested models every time they are handed
    over rather than only when first built. That is three separate opt-ins in
    Pydantic and none of them is on by default.

    Conversation history and the runtime's callbacks are deliberately **not**
    here. They live on `CallContext`, which holds this object and replaces it
    wholesale. Keeping them out is what makes a whole-candidate commit possible:
    a state that carried a message list and two bound methods could not be
    rebuilt from its own values.

    This is also the scope the prompt templates render against, so a
    `{{state.placeholder}}` naming no field fails when the prompt is compiled,
    at import, rather than rendering as nothing on a live call.
    """

    model_config = STRICT

    @classmethod
    def flow_fields(cls) -> frozenset[str]:
        """The fields that say where the caller is, rather than what is true of them.

        Read by `CallContext.commit` to decide what a superseded turn may still
        write. A business fact it established really happened; the step it was
        going to send the caller to next is an answer to a question they have
        moved on from.
        """
        return frozenset({'agent', 'pending'})

    agent: str
    pending: str | None = None
    """The step waiting to resume.

    A step whose prerequisite was not met is recorded here and the graph enters
    the step that satisfies it. One slot, not a list: no satisfier has
    prerequisites of its own, so `route` cannot chain, and a list invited
    in-place mutation that no validator would ever see.
    """


class CallContext(BaseModel):
    """One call: its validated state, its history, and the line to the caller.

    The agents' `deps`. This object is stable for the whole call and `state` is
    replaced inside it, so a tool holding `ctx.deps` always reads the current
    state; there is no way to capture a snapshot and write to it later.

    Everything below `heard` is a private attribute rather than a field: the
    prompt templates are compiled against this model's JSON schema, and a
    callable or a ledger has no place in it.
    """

    model_config = ConfigDict(validate_assignment=True)

    state: VoiceState
    messages: list[ModelMessage] = Field(default_factory=list)
    heard: str = ''
    """What the caller actually said to open this turn, as the runtime accepted it.

    A field, because the prompts render it, and because everything an action
    claims the caller agreed to has to be checkable against something the
    model did not write. Every consent fact used to enter as a model-authored
    argument, and a model supplied all of them for things the caller never said.
    """

    _say: SayFn | None = PrivateAttr(default=None)
    _end: EndFn | None = PrivateAttr(default=None)
    _covered: bool = PrivateAttr(default=False)
    """Whether the current silence already has a fixed line in it. `say` allows one per gap."""
    _generation: int = PrivateAttr(default=0)
    """Which accepted caller turn is current.

    Bumped once per turn and read back before a move is committed: a turn that
    was cancelled and then finished its unwinding must not still move the
    caller, and a late result is not evidence about where the caller is now.
    """
    _claim: str | None = PrivateAttr(default=None)
    """The one tool call in the current model response allowed to change the flow or write to the business.

    Set by `one_action_per_reply` from the whole response before any tool runs;
    read by every action that could be the second one. `None` means this
    response proposed no such action.
    """
    _requests: list[Request] = PrivateAttr(default_factory=list)
    """Every provider request of the turn now running, timed at the model boundary by `request_clock`."""
    _spoken: list[Spoken] = PrivateAttr(default_factory=list)
    """Everything the caller was sent this turn, in order. Fixed lines are here and nowhere else."""
    _open: Spoken | None = PrivateAttr(default=None)
    """The model segment currently streaming, held open until something else speaks or the run ends.

    Kept open so that a fixed line spoken by a tool mid-run lands after the
    words that preceded it and before the words that follow: the order of the
    ledger is the order the caller heard, and a timestamp cannot put back a
    boundary that was never kept.
    """
    _spoke: bool = PrivateAttr(default=False)
    """Whether the *model* streamed text this turn."""
    _id: str = PrivateAttr(default_factory=lambda: uuid4().hex[:12])
    """This call, told apart from every other one sharing the process.

    Part of the name of every business operation it starts, so one call's
    write can never be answered out of another call's cache.
    """
    _turn: int = PrivateAttr(default=0)
    """How many caller turns this call has accepted.

    A question put to the caller is stamped with it, so "they answered the
    thing we asked" is a comparison and not an assumption.
    """
    _ended: str = PrivateAttr(default='')
    """Why this call ended, set by whoever established it. Empty while it has not."""
    _segments: int = PrivateAttr(default=0)
    _attempt: int = PrivateAttr(default=0)
    """Which run of the agency is going on inside this turn. More than one when a caller speaks again before the agency answers."""
    _speech_history: list[Spoken] = PrivateAttr(default_factory=list)
    _inputs: dict[int, str] = PrivateAttr(default_factory=dict)
    _response: str = PrivateAttr(default='')
    _protocol: list[ModelMessage] = PrivateAttr(default_factory=list)
    """Every message that executed, retained for audit and never projected away."""
    _hidden_speech: set[str] = PrivateAttr(default_factory=set)
    _checkpoint: Checkpoint | None = PrivateAttr(default=None)
    _return_status: str = PrivateAttr(default='')
    _retained_turns: set[int] = PrivateAttr(default_factory=set)

    def retain_input(self) -> None:
        """Accept a model-free turn once, without inventing a provider exchange."""
        if self.turn in self._retained_turns:
            return
        message = ModelRequest(parts=[UserPromptPart(self.heard)], metadata={'caller_turn': self.turn})
        self.messages.append(message)
        self._protocol.append(deepcopy(message))
        self._retained_turns.add(self.turn)

    def retain_action(self, name: str, receipt: dict) -> None:
        """Retain a validated application result before speech can be cancelled.

        Model tools already retain their SDK exchanges. A local action has no
        model tool call, so its receipt is explicitly an application instruction.
        """
        if not self._response.endswith(':application'):
            return
        message = ModelRequest(
            parts=[SystemPromptPart(content=f'Application result {name}: {json.dumps(receipt)}')],
            metadata={'application_action': name, 'caller_turn': self.turn},
        )
        self.messages.append(message)
        self._protocol.append(deepcopy(message))

    @property
    def all_spoken(self) -> list[Spoken]:
        """Every segment of the call so far, including old turns that can still receive late playback."""
        return [*self._speech_history, *self.spoken]

    @property
    def id(self) -> str:
        """This call's own identity, stable for its whole life."""
        return self._id

    @property
    def turn(self) -> int:
        """Which accepted caller turn this is, counting from one."""
        return self._turn

    @property
    def requests(self) -> list[Request]:
        """This turn's provider requests, oldest first."""
        return self._requests

    @property
    def spoken(self) -> list[Spoken]:
        """This turn's delivered speech segments, in the order they went out.

        The segment still streaming is included, so a reader does not have to
        know whether a run has finished to see what it said.
        """
        return self._spoken if self._open is None else [*self._spoken, self._open]

    def note_said(
        self,
        kind: Literal['fixed', 'model'],
        text: str,
        at: float | None = None,
        origin: str = '',
        tool_call_id: str = '',
    ) -> None:
        """One more segment the caller was sent, in the order it went out.

        Every route to the caller comes through here, so the ledger holds the
        model's half as well as the fixed lines: a transcript missing either
        cannot show whether consent was asked for. `at` is when the segment
        *started* going out, which for a streamed answer is its first token;
        the default is only for a line written and sent in one go. Whatever the
        model was in the middle of saying is closed first, because it went out
        first.
        """
        if text:
            self.close_model_text()
            self._spoken.append(
                Spoken(
                    kind,
                    text,
                    at=time.perf_counter() if at is None else at,
                    origin=origin or ('model' if kind == 'model' else 'confirmation'),  # ty: ignore[invalid-argument-type]
                    id=self.next_segment(),
                    attempt=self._attempt,
                    turn=self._turn,
                    response=self._response,
                    generated=text,
                    tool_call_id=tool_call_id,
                )
            )

    def next_segment(self) -> str:
        """One id for the next stretch of speech, stable within this call."""
        self._segments += 1
        return f'{self._id}:{self._turn}:{self._segments}'

    def attempt_started(self) -> int:
        """One more run of the agency inside this caller turn. Returns which."""
        self._attempt += 1
        return self._attempt

    @property
    def attempt(self) -> int:
        """Which run of the agency is going on, counting from one within the turn."""
        return self._attempt

    def note_model_text(self, text: str) -> None:
        """One more piece of the model segment now streaming, opening it if it is the first.

        Kept open rather than appended per delta, because a segment is what
        the caller hears as one stretch of speech and `at` has to be its
        first token. Closed by the next fixed line or by the end of the run,
        whichever comes first.
        """
        if not text:
            return
        if self._open is not None and self._open.response != self._response:
            self.close_model_text()
        if self._open is None:
            self._open = Spoken(
                'model',
                '',
                id=self.next_segment(),
                attempt=self._attempt,
                turn=self._turn,
                response=self._response,
            )
        self._open.text += text
        self._open.generated += text

    def close_model_text(self) -> None:
        """End the model segment now streaming, if there is one."""
        if self._open is not None and self._open.text:
            self._spoken.append(self._open)
        self._open = None

    def note_heard(self, delivered: str, *, segment_ids: tuple[str, ...] = ()) -> bool:
        """Apply a runtime-estimated delivered prefix to the identified speech segments only.

        Returns False, and changes nothing, when the segments cannot be matched
        against the text: an estimate that does not line up is not evidence.
        """
        if not segment_ids:
            segment_ids = tuple(s.id for s in self.spoken[-1:])
        by_id = {s.id: s for s in self.all_spoken}
        if not segment_ids or any(i not in by_id for i in segment_ids):
            return False
        selected = [by_id[i] for i in segment_ids]
        if len(set(segment_ids)) != len(segment_ids):
            return False
        remaining = delivered.strip()
        updates = []
        for segment in selected:
            text = segment.generated or segment.text
            stripped = text.strip()
            if remaining.startswith(stripped):
                updates.append(text)
                remaining = remaining[len(stripped) :].lstrip()
            elif stripped.startswith(remaining):
                updates.append(remaining)
                remaining = ''
            else:
                return False
        if remaining:
            return False
        if any(
            s.delivery == 'runtime-estimate' and not s.text.strip().startswith(text.strip())
            for s, text in zip(selected, updates, strict=True)
        ):
            return False
        if self._open is not None and self._open.id in segment_ids:
            self.close_model_text()
        for segment, text in zip(selected, updates, strict=True):
            segment.text = text
            segment.interrupted = text != segment.generated
            segment.delivery = 'runtime-estimate'
        return True

    def unbacked_claim(self) -> str:
        """Model speech this turn asserting something only the business can assert. '' when clean.

        The claim itself, when there is one, so it can be logged and traced.
        This base has no business to write to and so nothing to check against;
        an agency that does overrides it. The turn engine says a correction out
        loud rather than leaving the caller with it.
        """
        return ''

    def wrote_this_turn(self) -> bool:
        """Whether this turn has already changed the business. Base: it has no business."""
        return False

    def delivered(self, heard: str, excluding: frozenset[int] = frozenset()) -> bool:
        """Whether this utterance reached a model and is in the history it keeps.

        Input that never entered an SDK run still needs carrying into the next
        turn; identity checks distinguish a newly accepted repeated utterance
        from the same words in an older turn.
        """
        return any(
            isinstance(part, UserPromptPart) and part.content == heard and id(part) not in excluding
            for message in self.messages
            for part in message.parts
        )

    def transcript(self) -> list[dict[str, str]]:
        """What was actually said on this call, in order, as a record rather than a protocol.

        `messages` is the model and tool protocol that executed. This is the
        conversation the *caller* had: their own utterances, and every segment
        that went out to them, fixed lines and model speech alike, cut down to
        what they heard where the runtime could tell us.
        """
        result = []
        for turn in sorted(set(self._inputs) | {s.turn for s in self.all_spoken}):
            if text := self._inputs.get(turn):
                result.append({'said': 'caller', 'text': text})
            result.extend(
                {'said': s.kind, 'text': s.text} for s in self.all_spoken if s.turn == turn and s.text
            )
        return result

    def claims_completion(self, clause: str) -> bool:
        """Whether this clause, on its own, tells the caller the business has changed.

        Asked of each clause **before** it goes out, on a step that can write,
        so the answer can be to not say it. `unbacked_claim` asks the same
        question of a whole turn afterwards, which is all that can be done once
        the words are at TTS. This base has no business to check against.
        """
        return False

    def note_first_output(self, kind: Literal['text', 'tool']) -> None:
        """The newest provider request has produced its first part.

        A second part in the same response does not overwrite the first: time
        to *first* output is the number, and `kind` says whether that first
        thing was speech the caller can hear or a tool call they cannot.
        """
        if self._requests and self._requests[-1].first_output is None:
            self._requests[-1].first_output = time.perf_counter()
            self._requests[-1].kind = kind

    def begin_turn(self) -> int:
        """Open a new accepted caller turn, and say which one it is.

        The number only has to differ from the last one; nothing reads it as a
        count. Work started under an older turn can tell that it is stale
        without knowing why.
        """
        self._generation += 1
        return self._generation

    def is_current(self, generation: int) -> bool:
        """Whether work from that turn may still change where the caller is."""
        return generation == self._generation

    def commit(self, **updates) -> None:
        """Replace the whole state with one validated candidate, or change nothing.

        Built from values rather than `model_copy(update=...)`, which copies
        updates in without validating them, and rather than a sequence of
        assignments, which commits the first field before the second is
        checked. A tool writing a phone number and a verification status
        together gets both or neither.

        A keyword naming no field is refused rather than dropped: Pydantic
        ignores an extra keyword by default, so a typo in a tool would become a
        state write that silently did not happen.

        A commit from a turn the caller has moved on from keeps its business
        facts and loses its flow fields. Where the caller *is* belongs to the
        current turn and a stale answer about it is wrong by definition; a
        booking that really happened is a fact whichever turn made it, and
        refusing to record it would leave the diary holding an appointment the
        call would book again.
        """
        if unknown := set(updates) - set(type(self.state).model_fields):
            raise ValueError(f'no such field on {type(self.state).__name__}: {", ".join(sorted(unknown))}')
        running = turn_generation.get()
        if running is not None and running[0] == self._id and running[1] != self._generation:
            if late := set(updates) & self.state.flow_fields():
                logger.info('fenced a late commit of {} from turn {}', ', '.join(sorted(late)), running[1])
                updates = {name: value for name, value in updates.items() if name not in late}
            if not updates:
                return
        self.state = type(self.state)(**(self.state.model_dump() | updates))

    async def say(self, text: str, origin: str = 'entry', tool_call_id: str = '') -> None:
        """Speak one fixed line now, without waiting for the model.

        At most one per gap: an entry line and a tool's line cover the same
        silence, and a caller should not hear both back to back. The turn
        engine opens a new gap at the start of each turn and closes this one as
        soon as the model's own words start.
        """
        if self._covered or self._say is None:
            return
        self._covered = True
        self.note_said('fixed', text, origin=origin, tool_call_id=tool_call_id)
        await self._say(text)

    def resume(self, fallback: str) -> str:
        """Where the caller goes now this step is done: back to what they were doing.

        A finisher calls this instead of asking the model where to go next. The
        step that was waiting recorded itself on the way in, so the application
        already knows the answer.

        Never the step that is finishing. A step entered and then left without
        finishing leaves its record behind, and resuming into it would hand the
        caller to the agent they are already with: a whole model request that
        says nothing.
        """
        step = self.state.pending
        if step is not None:
            self.commit(pending=None)
        return step if step is not None and step != self.state.agent else fallback

    def turn_started(self, heard: str = '') -> None:
        """The caller has said something new. Age out anything that was only news.

        Last turn's speech moves from the per-turn ledger to the per-call
        record first. `heard` is kept for the whole turn, because an action
        three hops later still has to be able to ask what the caller actually
        asked for rather than take the model's word for it.
        """
        self.close_model_text()
        self._speech_history.extend(self._spoken)
        self.heard = heard
        self._turn += 1
        self._inputs[self._turn] = heard
        self._attempt = 0
        self._response = ''
        self._requests.clear()
        self._spoken.clear()
        self._open = None
        self._spoke = False

    async def speak_now(self, text: str, origin: str = 'confirmation') -> None:
        """Say this whatever else was said. For a confirmation, a question or a goodbye, never a filler."""
        if self._say is not None:
            self.note_said('fixed', text, origin=origin)
            await self._say(text)

    def end_requested(self, why: Literal['caller', 'abandoned', 'transport', 'error']) -> None:
        """Record *why* the call is ending, before anything acts on it.

        Queuing a goodbye and observing a disconnect are different facts, and a
        record that cannot tell "the caller rang off" from "the agency hung up
        on them" scores the second as answered.
        """
        self._ended = why

    @property
    def ended(self) -> str:
        """Why this call is ending, or '' while it is not."""
        return self._ended

    async def end_call(self) -> None:
        """Hang up, once the caller has said the call is over."""
        if not self._ended:
            self._ended = 'caller'
        if self._end is not None:
            await self._end()
