"""What every agency on this harness shares: state, handoffs, and the step table.

An agency -- a set of agents that take one call between them -- supplies its own
subclasses of `VoiceState` and `Handoff` and its own `AGENTS` table. The
processor knows only what is here.
"""

from __future__ import annotations

import contextvars
import json
import random
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, NamedTuple, Protocol, runtime_checkable
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, TypeAdapter, create_model
from pydantic_ai import Agent, ModelRetry, Tool
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.messages import ModelMessage, ModelRequest, SystemPromptPart, ToolCallPart, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage, UsageLimits

SayFn = Callable[[str], Awaitable[None]]
EndFn = Callable[[], Awaitable[None]]

# Which accepted caller turn the code running right now belongs to. A context
# variable rather than an argument because the thing that has to know is
# `commit`, and every tool in the agency would otherwise have to carry a
# number it never reads. Tool tasks inherit the context they were spawned in,
# so a tool still running after its turn was cancelled still reports that
# turn's number while the call has moved on to the next.
#
# The call as well as the number, because the number only means anything
# within one call: two calls in one process each count their own turns from
# one, and comparing across them fences a perfectly current write.
_turn_generation: contextvars.ContextVar[tuple[str, int] | None] = contextvars.ContextVar(
    'turn_generation', default=None
)


# Every model that holds business facts opts into all three. Defaults are
# validated (an authored default that breaks its own type fails at import, not
# on the call that first reads it), assignments are validated, and a nested
# model handed in is revalidated rather than trusted because it is already an
# instance of the right class. None of the three is on by default, and each one
# alone leaves a hole the other two do not cover.
STRICT = ConfigDict(validate_default=True, validate_assignment=True, revalidate_instances='always')


def present(value: object) -> bool:
    """Whether a prerequisite is filled, under one written-down rule.

    `None` is absent and a permitted empty string is absent. A legitimate
    `False` or `0` is **present**, which plain truthiness gets wrong: a field
    holding `False` because the caller said no has been answered, and a step
    waiting on it should not be interposed a second time.
    """
    return value is not None and value != ''


@dataclass(frozen=True)
class TurnBudget:
    """Everything one accepted caller turn is allowed to spend.

    One object for the whole turn, not one per agent run: a turn that hands
    the caller between four agents has spent four agents' worth of a caller's
    patience, and budgets that reset per run bound nothing. The usage object
    built by `account` is threaded through every run in the turn.

    These numbers are **proposals to tune and record**, not SDK defaults and
    not values anybody has yet shown to be right for production. They are
    written down here so a change to them is a change to a file rather than a
    different number in three places. `bench/baseline/budget.md` is where the
    reasoning lives.

    `repair_retries` counts semantic repair only -- a tool refusing and the
    model correcting itself. Provider SDK retries and HTTP attempts are a
    different layer with its own counter, and adding them together would hide
    a model that cannot get a tool call right behind a flaky network. It is
    also supplied to each agent run separately, so it bounds repairs *per run*
    and is not an aggregate allowance across the turn the way the other four
    are. `step_runs` is what bounds how many times it can be spent.

    `tool_calls` is read against `RunUsage.tool_calls`, which the SDK
    increments for tools that **succeeded**. A tool that refused with a
    `ModelRetry` costs a model request and does not cost this, so the two
    numbers answer different questions and a write-up that calls this "tool
    invocations" is overstating what it bounds.
    """

    step_runs: int = 4
    model_requests: int = 8
    tool_calls: int = 8
    seconds: float = 20.0
    repair_retries: int = 2

    def account(self) -> tuple[RunUsage, UsageLimits]:
        """A fresh tally and the limits to read it against, for one turn."""
        return RunUsage(), UsageLimits(request_limit=self.model_requests, tool_calls_limit=self.tool_calls)


@dataclass
class Spoken:
    """One segment of speech the caller was actually sent, and who wrote it.

    A turn's delivered speech is not one string. A filler, a handoff line and
    a computed confirmation are written by this code and never appear in the
    model's history at all, so a benchmark row built from the history reports
    a turn that said three things as a turn that said one -- or, when the
    model spoke nothing, as a silent turn. `kind` keeps the two apart,
    because "the agency answered" and "the model answered" are different
    claims and only one of them costs a provider request.
    """

    kind: Literal['fixed', 'model']
    text: str
    # Where this segment came from, which the trace could not otherwise say.
    # Every TTS observation on the live calls of 2026-09-21 hung off the
    # outer Pipecat turn, so a filler, an entry line, a confirmation and a
    # goodbye were four spans with nothing to tell them apart -- and the
    # question the traces raised was which of them the caller heard before
    # the tool ran.
    origin: Literal['entry', 'tool_filler', 'ask', 'confirmation', 'model', 'goodbye', 'error'] = 'model'
    # Stable within the call: turn, then position in it. What carries through
    # the frame queue so a submitted segment and the audio context that
    # synthesised it can be matched up afterwards.
    id: str = ''
    # Which run of the agency asked for it. A turn can have more than one.
    attempt: int = 0
    # The tool call it was covering, where there was one.
    tool_call_id: str = ''
    # When this segment *started* going out, on the same clock as
    # `Request.started`. Text handed to TTS, which is not audio the caller
    # heard: the runtime's own playout is a separate boundary and nothing
    # here measures it. A benchmark that reports only time to the model's
    # first word still cannot tell a turn that answered quickly from one that
    # said "let me check" quickly and answered slowly, which is why every
    # segment is timed and not just the model's.
    at: float = field(default_factory=time.perf_counter)
    # Cut down to what the caller actually heard, once the runtime says so.
    interrupted: bool = False
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
    two it measured is not a number anybody can compare across two
    architectures.
    """

    started: float
    first_output: float | None = None
    kind: Literal['', 'text', 'tool'] = ''

    @property
    def ttft(self) -> float | None:
        """Seconds from the request going out to its first part, or None if none came."""
        return None if self.first_output is None else round(self.first_output - self.started, 3)


def request_clock() -> Hooks:
    """Stamp every provider request as it goes out, so time to first token is per request.

    The turn's own clock cannot stand in for one. A run that calls a tool makes two
    requests and only the second one speaks, so timing from the start of the run reports
    the tool's entire round trip as time to first token -- which is what the processor's
    `request_ttft` attribute carried before this existed, on a turn with a tool in it.

    `before_model_request` fires exactly once per provider request, including the extra
    one a repair retry buys (probed against 2.46.0), and `ctx.deps` is the call, so one
    list collects every request of every run in the turn. The core closes each entry when
    the first part of the response arrives.
    """
    hooks = Hooks()

    @hooks.on.before_model_request
    async def stamp(ctx, request_context):
        ctx.deps.requests.append(Request(started=time.perf_counter()))
        return request_context

    return hooks


@dataclass(frozen=True)
class Checkpoint:
    """One prerequisite owner; nested prerequisites are forbidden by the graph."""

    owner: str
    messages: list[ModelMessage]
    speech_ids: frozenset[str]
    heard: str


class Handoff(BaseModel):
    """Where the caller goes next. Agencies return this from their `hand_over` action."""

    to: str
    because: str = Field(default='', description='One short sentence: what the caller wants. Never spoken.')
    status: Literal['handoff', 'completed', 'unserved'] = 'handoff'


@runtime_checkable
class Question(Protocol):
    """An agent output that is a question for the caller, worded by the application.

    The core says it and ends the turn. It exists so the one sentence the next
    consequential action will be checked against is the sentence the caller
    actually heard -- not a paraphrase the model wrote and then treated as
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

    Still the scope the prompt templates render against, now one level down: a
    `{{state.placeholder}}` naming no field fails when the prompt is compiled,
    at import, rather than rendering as nothing on a live call.
    """

    model_config = STRICT

    @classmethod
    def flow_fields(cls) -> frozenset[str]:
        """The fields that say where the caller is, rather than what is true of them.

        Read by `CallContext.commit` to decide what a superseded turn may
        still write. A business fact it established really happened; the step
        it was going to send the caller to next is an answer to a question
        they have moved on from.
        """
        return frozenset({'agent', 'pending'})

    agent: str
    # The step waiting to resume: a step whose prerequisite was not met is
    # recorded here and the graph enters the step that satisfies it. One slot,
    # not a list -- `test_graph` refuses a satisfier that has prerequisites of
    # its own, so `route` cannot chain and a second waiting step cannot arise.
    # A list invited in-place mutation that no validator would ever see.
    pending: str | None = None


class CallContext(BaseModel):
    """One call: its validated state, its history, and the line to the caller.

    The agents' `deps`. This object is stable for the whole call and `state` is
    replaced inside it, so a tool holding `ctx.deps` always reads the current
    state -- there is no way to capture a snapshot and write to it later.
    """

    model_config = ConfigDict(validate_assignment=True)

    state: VoiceState
    messages: list[ModelMessage] = Field(default_factory=list)
    # What the caller actually said to open this turn, as the runtime accepted
    # it. A field rather than a private attribute because the prompts are
    # compiled against this schema and may render it -- and because everything
    # an action claims the caller agreed to has to be checkable against
    # something the model did not write. Every consent fact used to enter as a
    # model-authored argument (`confirmed=True`, `additional=True`, the number
    # handed to a lookup), and on the calls of 2026-09-21 the model supplied
    # all three for things the caller had never said.
    heard: str = ''

    # Deliberately not fields: a Callable would land in the JSON schema the
    # prompt templates are compiled against, and Handlebars cannot render one.
    # Methods are not fields either, so `say` and `end_call` stay out of it too.
    _say: SayFn | None = PrivateAttr(default=None)
    _end: EndFn | None = PrivateAttr(default=None)
    _covered: bool = PrivateAttr(default=False)
    # Which accepted caller turn is current. Bumped once per turn, and read
    # back before a move is committed: a turn that was cancelled and then
    # finished its unwinding must not still be able to move the caller, and
    # a late result is not evidence about where the caller is now.
    _generation: int = PrivateAttr(default=0)
    # The one tool call in the current model response that is allowed to change
    # the flow or write to the business. Set by `one_action_per_reply` from the
    # whole response before any tool runs; read by every action that could be
    # the second one. `None` means this response proposed no such action.
    _claim: str | None = PrivateAttr(default=None)
    # Every provider request of the turn now running, timed at the model
    # boundary by `request_clock`. On the call, not on the turn, because one
    # turn's requests are spread over as many agent runs as it hops through.
    _requests: list[Request] = PrivateAttr(default_factory=list)
    # Everything the caller was sent this turn, in order. Fixed lines are here
    # and nowhere else: they never enter the model's history.
    _spoken: list[Spoken] = PrivateAttr(default_factory=list)
    # The model segment currently streaming, held open until something else
    # speaks or the run ends. Model text used to be accumulated for the whole
    # run and appended once at the end, so a run that spoke, called a tool
    # that said a line, and spoke again recorded the fixed line *first* and
    # both model sentences after it. The ledger had every word and the wrong
    # order, and a timestamp cannot put back a boundary that was never kept.
    _open: Spoken | None = PrivateAttr(default=None)
    # Whether the *model* streamed text this turn. Read by `note_spoken`,
    # which may only cut down a response this turn produced.
    _spoke: bool = PrivateAttr(default=False)
    # This call, told apart from every other one sharing the process. Part of
    # the name of every business operation it starts, so one call's write can
    # never be answered out of another call's cache.
    _id: str = PrivateAttr(default_factory=lambda: uuid4().hex[:12])
    # How many caller turns this call has accepted. A question put to the
    # caller is stamped with it, so "they answered the thing we asked" is a
    # comparison and not an assumption: an agreement two turns after the
    # question is not an answer to it.
    _turn: int = PrivateAttr(default=0)
    # Why this call ended, set by whoever established it. Empty while it has
    # not: an absent reason is not "the caller rang off".
    _ended: str = PrivateAttr(default='')
    # Every stretch of speech gets a number, so the ledger entry and the
    # audio context that synthesised it can be matched up afterwards.
    _segments: int = PrivateAttr(default=0)
    # Which run of the agency is going on inside this turn. More than one
    # when a caller speaks again before the agency answers.
    _attempt: int = PrivateAttr(default=0)
    _speech_history: list[Spoken] = PrivateAttr(default_factory=list)
    _inputs: dict[int, str] = PrivateAttr(default_factory=dict)
    _response: str = PrivateAttr(default='')
    _protocol: list[ModelMessage] = PrivateAttr(default_factory=list)
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
        """Canonical segments, including old turns that can receive late playback."""
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

        Every route to the caller comes through here, which is the point: the
        ledger held only the fixed lines routed through `say`, so a turn where
        the model answered read back as a turn that said nothing at all, and
        the core's own stumble and exhausted lines were nowhere in it. A
        transcript missing the model's half cannot show whether consent was
        asked for.
        """
        if text:
            # `at` is when it *started* going out, which for a streamed answer
            # is its first token and not the moment the run finished
            # assembling it. Default only for a line that is written and sent
            # in one go.
            # Whatever the model was in the middle of saying ends here. It
            # went out first, so it is recorded first: the order of this list
            # is the order the caller heard, and that is the only thing it is
            # for.
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
        whichever comes first, which is what keeps fixed and model speech in
        the order they went out.
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
        """Apply a runtime-estimated prefix only to the identified speech segments."""
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
        This base has no business to write to and so nothing to check
        against; an agency that does overrides it. `run_turn` says a
        correction out loud rather than leaving the caller with it, because
        a sentence the caller cannot check is worse than a pause.
        """
        return ''

    def wrote_this_turn(self) -> bool:
        """Whether this turn has already changed the business. Base: it has no business."""
        return False

    def delivered(self, heard: str, excluding: frozenset[int] = frozenset()) -> bool:
        """Whether this utterance reached a model and is in the history it keeps.

        The core retains SDK messages across native cancellation. Input that
        never entered an SDK run still needs carrying into the next turn;
        identity checks distinguish a newly accepted repeated utterance from
        the same words in an older turn.
        """
        return any(
            isinstance(part, UserPromptPart) and part.content == heard and id(part) not in excluding
            for message in self.messages
            for part in message.parts
        )

    def transcript(self) -> list[dict[str, str]]:
        """What was actually said on this call, in order, as a record rather than a protocol.

        Two different things, and merging them is how a trace ends up
        claiming a turn was silent. `messages` is the model and tool protocol
        that executed -- tool calls, their results, retries, the sentences
        the model produced whether or not they were delivered. This is the
        conversation the *caller* had: their own utterances, and every
        segment that went out to them, fixed lines and model speech alike,
        cut down to what they heard where the runtime could tell us.
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

        Asked of each clause **before** it goes out, on a step that can
        write, so the answer can be to not say it. `unbacked_claim` asks the
        same question of a whole turn afterwards, which is all that can be
        done once the words are at TTS -- and on the call of 2026-09-21 12:23
        they were: *"I've cancelled your appointment, and I have you down for
        a redo on Saturday at 2:30 PM."* Nothing had been cancelled.

        This base has no business to write to and so nothing to check
        against; an agency that does overrides it.
        """
        return False

    def note_first_output(self, kind: Literal['text', 'tool']) -> None:
        """The newest provider request has produced its first part.

        Called by the core, which is the only thing that sees the stream. A
        second part in the same response does not overwrite the first: time to
        *first* output is the number, and `kind` says whether that first thing
        was speech the caller can hear or a tool call they cannot.
        """
        if self._requests and self._requests[-1].first_output is None:
            self._requests[-1].first_output = time.perf_counter()
            self._requests[-1].kind = kind

    def begin_turn(self) -> int:
        """Open a new accepted caller turn, and say which one it is.

        The number only has to be different from the last one; nothing reads
        it as a count. What it is for is the comparison: work started under
        an older turn can tell that it is stale without knowing why.
        """
        self._generation += 1
        return self._generation

    def is_current(self, generation: int) -> bool:
        """Whether work from that turn may still change where the caller is."""
        return generation == self._generation

    def commit(self, **updates) -> None:
        """Replace the whole state with one validated candidate, or change nothing.

        Built from values rather than `model_copy(update=...)`, which copies
        updates in without validating them at all, and rather than a sequence of
        assignments, which commits the first field before the second is checked.
        A caller writing a phone number and a verification status together gets
        both or neither: if the status is not a legal one the `ValidationError`
        leaves the previous state object exactly where it was.

        A keyword naming no field is refused rather than dropped. Pydantic
        ignores an extra keyword by default, so `commit(phone_confirmd=True)`
        changed nothing and said so to nobody: a typo in a tool became a state
        write that silently did not happen, which is the one failure a
        validated boundary is supposed to make impossible.
        """
        if unknown := set(updates) - set(type(self.state).model_fields):
            raise ValueError(f'no such field on {type(self.state).__name__}: {", ".join(sorted(unknown))}')
        # A commit from a turn the caller has moved on from. `_hops` already
        # fenced the two transitions it makes itself; this is the same rule
        # for every other write, including one from a tool that kept running
        # after its run was cancelled.
        #
        # Flow only. Where the caller *is* belongs to the current turn and a
        # stale answer about it is wrong by definition -- but a booking that
        # really happened is a fact whichever turn it was made in, and
        # refusing to record it would leave the diary holding an appointment
        # the call has no memory of and would book again. Completed effects
        # land; the routing that was going to follow them does not.
        running = _turn_generation.get()
        if running is not None and running[0] == self._id and running[1] != self._generation:
            if late := set(updates) & self.state.flow_fields():
                logger.info('fenced a late commit of {} from turn {}', ', '.join(sorted(late)), running[1])
                updates = {name: value for name, value in updates.items() if name not in late}
            if not updates:
                return
        self.state = type(self.state)(**(self.state.model_dump() | updates))

    async def say(self, text: str, origin: str = 'entry', tool_call_id: str = '') -> None:
        """Speak one fixed line now, without waiting for the model.

        At most one per gap. A handoff line and a tool's filler line cover the
        same silence, and on a live turn both fired: the caller heard "Sure, let
        me look that up." and then "Let me have a look." back to back. The
        processor opens a new gap at the start of each turn and closes this one
        as soon as the model's own words start.
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
        already knows the answer and the model is never asked a question it
        cannot answer better than the graph.

        Never the step that is finishing. The slot is only cleared by a
        finisher, and a step that is entered and then left without finishing --
        the caller changed the subject -- leaves its record behind. Resuming
        into it would hand the caller to the agent they are already with: a
        whole model request that says nothing, and it happened on the first real
        run of the benchmark.
        """
        step = self.state.pending
        if step is not None:
            self.commit(pending=None)
        return step if step is not None and step != self.state.agent else fallback

    def turn_started(self, heard: str = '') -> None:
        """The caller has said something new. Age out anything that was only news.

        Called by the core before the first agent of a turn runs, so a prompt
        can branch on "has this just happened" as a fact rather than asking the
        model to remember whether it already said it.

        `heard` is what they said. It is kept for the whole turn, because an
        action three hops later still has to be able to ask what the caller
        actually asked for rather than take the model's word for it.
        """
        # Last turn's half of the transcript, before the ledger is emptied
        # to make room for this one's. The ledger is per turn because that is
        # what a turn's own speech is; the record is per call because that is
        # what somebody reading the call back wants.
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
        """Say this whatever else was said. For a goodbye or an error, never a filler."""
        if self._say is not None:
            self.note_said('fixed', text, origin=origin)
            await self._say(text)

    def end_requested(self, why: Literal['caller', 'abandoned', 'transport', 'error']) -> None:
        """Record *why* the call is ending, before anything acts on it.

        Queuing a goodbye and observing a disconnect are different facts, and
        a benchmark that cannot tell "the caller rang off" from "the agency
        hung up on them" scored the live call of 2026-09-21 12:57 as answered.
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


CONFLICT = (
    'Only one of these may run per reply, and another one is already running: {other}. '
    'Nothing was done here. Wait for that result, then decide what to do next.'
)


def claim_or_refuse(ctx, actions: frozenset[str]) -> None:
    """Refuse this action if another one in the same reply already has the claim.

    Called first, before the tool does anything, so a refused action never
    reaches its service. `ctx.deps._claim` was decided from the complete model
    response before any tool ran -- a real preflight, in the order the model
    emitted the calls, never by iterating a set and taking whatever came out
    first.
    """
    claim = ctx.deps._claim
    if ctx.tool_name in actions and claim is not None and ctx.tool_call_id != claim:
        raise ModelRetry(CONFLICT.format(other=claim))


def one_action_per_reply(actions: frozenset[str]) -> Hooks:
    """One flow-changing or business-mutating action per model response.

    Two of them in one reply is the model proposing two futures. Letting both
    run and rejecting one transition afterwards is the thing to avoid: the
    losing action has already written to the salon by then.

    So the winner is chosen from the whole response, before anything executes,
    by the order the model emitted the calls. `after_model_request` is the
    place that can see the complete set -- it runs after the provider replies
    and before the tool-calling node -- which is why controlled iteration is
    not needed here.

    Enforcement is in two halves because the SDK only offers one:
    `before_tool_execute` covers ordinary tools, and does not fire for output
    tools at all (checked against 2.46.0: an output tool ran with the hook
    never called). Output tools call `claim_or_refuse` themselves on their
    first line, which is the same refusal at the same point -- before the
    service.
    """
    hooks = Hooks()

    @hooks.on.after_model_request
    async def choose(ctx, *, request_context, response):
        ctx.deps._claim = None
        for part in response.parts:
            # Emission order, not a set: "whichever the iterator yields first"
            # is not arbitration, and two runs of the same reply have to reach
            # the same answer.
            if isinstance(part, ToolCallPart) and part.tool_name in actions:
                ctx.deps._claim = part.tool_call_id
                break
        return response

    @hooks.on.before_tool_execute
    async def refuse_the_rest(ctx, *, call, tool_def, args):
        if call.tool_name in actions and ctx.deps._claim is not None and call.tool_call_id != ctx.deps._claim:
            raise ModelRetry(CONFLICT.format(other=call.tool_name))
        return args

    return hooks


def speak_by(request: int) -> Hooks:
    """From this request on, a run has no ordinary tools left: it answers with what it has.

    The turn budget is the last resort and its failure mode is an apology: on the call of
    2026-09-21 a run made eight requests, seven of them the same knowledge lookup reworded,
    and the caller heard "I'm going round in circles" five seconds later. Everything needed
    to answer had come back on the first one.

    So this is the bound in front of the budget. Two tool-bearing requests is the longest
    chain any step here legitimately needs -- read the diary, then correct a refusal -- so
    from the third the model is offered speech and nothing else. Its **output** tools stay:
    a run that has read the diary can still hand over or save, because those finish the run
    rather than extending it. Only the ones that buy another request go.

    So this bounds the *loop*, and does not promise the caller a sentence: with its output
    tools still offered, the third request may hand over or finish the step instead of
    speaking. What it guarantees is that the run cannot buy a fourth request to look
    something else up. The turn budget behind it is still what stops the turn.

    `ctx.run_step` counts model requests from 1 within one run, checked against 2.46.0.
    """
    hooks = Hooks()

    @hooks.on.prepare_tools
    async def only_speech(ctx, defs):
        return [] if ctx.run_step >= request else defs

    return hooks


class Exit[S: VoiceState](NamedTuple):
    """One legal move out of a step, and the state it is legal in.

    The exits whose guard holds are lowered into that step's own `hand_over` as
    a narrowed `Literal`, so an illegal transition is not caught at runtime: it
    is unrepresentable in the schema the model is sent.

    `via` is what makes an edge free. A step that names another step's entry
    tools here lends them into its own schema while the guard holds, and calling
    one *is* the move: the caller is in the new step, and the request that moved
    them is the same request that did their work. Without it the move costs a
    model request of its own that says nothing -- measured at 0.85s on a live
    call, three times in twelve turns.

    Only a tool whose call genuinely means the caller has moved belongs here.
    `read_diary` does. `record_complaint` never moves anybody.
    """

    to: str
    guard: Callable[[S], bool] = lambda _: True
    # The functions themselves, not their names: this is what a step lends, and
    # the lender has to be able to hand them to `Agent(tools=...)`.
    via: tuple = ()


@dataclass(frozen=True)
class LocalAction:
    """An application-decided action using the agency's normal validated executor."""

    name: str
    run: Callable[[], Awaitable[object]]


@dataclass(frozen=True)
class Node[S: VoiceState]:
    """One step: the agent that runs it, where it may send the caller, and what
    has to be true before it runs at all.
    """

    # The agent is `Agent[Any, Any]` rather than `Agent[S, ...]`: the output
    # type varies per step -- `str`, a handoff, a finisher's return -- and
    # naming the union here would fix it for every agency. The state type is
    # what the guards and outputs below are parametrised on, and that is what
    # the `invalid-argument-type` suppression used to exist for.
    agent: Agent[Any, Any]
    # Spoken by the processor, not the model, to cover the model round trip the
    # hop costs. Empty means the receiving agent answers straight away.
    enter: tuple[str, ...] = ()
    # The legal model-chosen moves out of this step.
    exits: tuple[Exit[S], ...] = ()
    # State fields that must be truthy before this step may run, and the step
    # that fills them. The graph interposes it and comes back: no model request
    # decides any of that, and no prompt has to state the ordering.
    requires: tuple[str, ...] = ()
    satisfied_by: str = ''
    # This turn's `output_type`, given the state. It is a callable because a
    # guard reads live state, so the narrowed `Literal` cannot be fixed at
    # import. `None` leaves the agent's own, which is the unguarded superset.
    outputs: Callable[[S], list] | None = None
    # Whether this step can change the business. On one that can, the core
    # holds each clause of model speech back until it is complete and drops
    # the run's speech if it claims a change nothing has made -- see
    # `CallContext.claims_completion`. Costs one clause of streaming latency,
    # on these steps only, and only until something is actually written.
    guarded: bool = False
    # Structured-only steps speak through output functions, never provider preambles.
    model_speech: bool = True
    # What conversation this step is sent. `messages` is the delivered
    # conversation so far, which is what an agency whose steps are one
    # continuous call wants. `reset` sends none of it: the step gets its own
    # instructions and the named values projected into them, and nothing the
    # caller said to anybody else. Contract C3's two modes, and the only two
    # this harness implements -- anything finer is a policy nobody has needed.
    history: Literal['messages', 'reset'] = 'messages'
    advance: Callable[[Any], LocalAction | None] | None = None


@asynccontextmanager
async def model_clients(nodes: dict[str, Node[Any]]):
    """Own each distinct provider once per active session; SDK reference counts overlapping sessions."""
    seen = set()
    async with AsyncExitStack() as stack:
        for node in nodes.values():
            model = node.agent.model
            if isinstance(model, Model) and id(model) not in seen:
                seen.add(id(model))
                await stack.enter_async_context(model)
        yield


def route[S: VoiceState](state: S, nodes: dict[str, Node[S]], to: str) -> tuple[str, str | None]:
    """Where the caller actually goes, and what is left waiting.

    Pure now, in the strict sense: it returns the decision and writes nothing.
    Its caller commits the step and the pending slot in one validated candidate,
    because they are one application decision -- a move that half-happened would
    leave the caller in a step with nothing recorded to come back to.

    Free, as before. It interposes a prerequisite by recording the destination
    and entering the step that satisfies it; the finisher there clears it. No
    model request decides any of this, and the step at the other end is the one
    that was going to run anyway, so it costs no extra round trip.
    """
    node = nodes[to]
    # One slot, overwritten rather than stacked: one satisfier is being waited
    # on at a time, so a second interposition means the first was abandoned and
    # its record is stale. ponytail: the cap is one because no satisfier has
    # prerequisites of its own, which `test_graph` refuses to let change. If one
    # ever does, this becomes a real stack and `resume` needs a matching loop.
    if node.satisfied_by and not all(present(getattr(state, field, None)) for field in node.requires):
        return node.satisfied_by, to
    if state.agent != to and any(other.satisfied_by == to for other in nodes.values()):
        # Sent straight to a step whose job is to satisfy something, rather than
        # interposed into it. Same promise either way: the caller came from
        # somewhere and goes back there. Without this a caller mid-complaint who
        # gets identified is handed to the front desk and has to start again.
        return to, state.agent
    return to, state.pending


def lent[S: VoiceState](exits: tuple[Exit[S], ...]) -> dict[str, Exit[S]]:
    """The tools a step borrows from its neighbours, by the name the model sees.

    One map, read twice: `build` uses it to put the borrowed tools in the schema
    and to take them back out when their guard stops holding, and `moved_by`
    uses it to work out which step a call landed the caller in.
    """
    return {tool.__name__: exit for exit in exits for tool in exit.via}


def moved_by[S: VoiceState](state: S, node: Node[S], nodes: dict[str, Node[S]], succeeded: set[str]) -> str:
    """The step a *successful* tool call has put the caller in, or where they were.

    Free, like `route`: the move rides the request that called the tool.

    Three things are re-derived here rather than trusted from when the schema
    was built, because all three can have changed since:

    * the edge is one this step actually owns;
    * its guard still holds -- a tool that ran earlier in the same run can
      undo what made the edge legal;
    * the destination's own prerequisites are met. Entering a step whose
      prerequisites are unmet by the back door skips the interposition `route`
      exists to do.

    `succeeded` is tools that ran *and returned*. A tool that raised, retried
    or was cancelled by barge-in is not a move: the caller is where they were.
    """
    borrowed = lent(node.exits)
    for name in sorted(succeeded):
        exit = borrowed.get(name)
        if exit is None or not exit.guard(state):
            continue
        destination = nodes.get(exit.to)
        if (
            destination is not None
            and destination.requires
            and not all(present(getattr(state, field, None)) for field in destination.requires)
        ):
            continue
        return exit.to
    return state.agent


_LAST: dict[str, str] = {}


def pick(key: str, lines: tuple[str, ...]) -> str:
    """One of these lines, never the one this key used last.

    A line written in code is spoken by us, not generated, so it is the same
    sentence every time it fires unless something varies it. A caller who books
    and then changes their mind hears the same site twice inside a minute.
    """
    # ponytail: process-wide, not per call. Two concurrent callers can only
    # affect each other's variety, which is the whole point of the function.
    choice = random.choice([line for line in lines if line != _LAST.get(key)] or list(lines))
    _LAST[key] = choice
    return choice


# --- a tool whose arguments came from a schema, not from annotations ---------


class UnsupportedSchemaError(Exception):
    """A schema this harness will not build a tool from, said at build time.

    Refused rather than approximated. A converter that silently drops a
    keyword it does not implement -- a nested object, a `oneOf`, a pattern --
    produces a tool that validates less than its schema says it does, and
    nothing downstream can tell.
    """


# What a schema-authored tool may say about one argument. Deliberately small:
# these are the shapes this agency's tools actually use, and anything else is
# an error rather than a guess.
_SCHEMA_TYPES: dict[str, type] = {'string': str, 'boolean': bool, 'integer': int, 'number': float}


def schema_tool(
    name: str,
    description: str,
    schema: dict,
    handler: Callable[..., Awaitable[object]],
    # Keyed by field name; each one is handed the call and returns the value
    # the handler gets. `Any` because the call is the agency's own
    # `CallContext` subclass and this module cannot name it.
    injected: dict[str, Callable[[Any], object]] | None = None,
) -> Tool:
    """A tool whose arguments are described by a JSON schema rather than by annotations.

    `Tool.from_schema` in pydantic-ai 2.46.0 does **no** argument validation
    unless an `args_validator` is supplied: the handler is called with
    whatever the model sent. This builds that validator from the schema, so
    the guarantee an ordinary typed tool gets for free is the same one here.

    Four things happen before the handler is reached, in this order, and the
    handler is not called if any of them fails:

    1. The model's arguments are validated against the schema's own fields.
    2. A field the *application* supplies is refused if the model sent one.
       An injected value is not a default the model may improve on; a phone
       number the model can overwrite is not an injected phone number.
    3. The application's current values are injected.
    4. The whole effective argument set -- the model's and the application's
       together -- is validated again, because an injected value can be
       wrong too and the handler is entitled to one validated call.

    Unsupported constructs raise at build time, so a schema this cannot
    honour never becomes a tool the model is offered.
    """
    injected = injected or {}
    if schema.get('type') != 'object':
        raise UnsupportedSchemaError(f'{name}: only an object schema describes arguments')
    for keyword in ('oneOf', 'anyOf', 'allOf', 'not', '$ref', 'patternProperties', 'additionalProperties'):
        if keyword in schema:
            raise UnsupportedSchemaError(f'{name}: {keyword} is not supported')
    properties = schema.get('properties') or {}
    required = set(schema.get('required') or ())
    fields: dict[str, tuple[Any, Any]] = {}
    for field_name, described in properties.items():
        kind = described.get('type')
        if kind not in _SCHEMA_TYPES:
            raise UnsupportedSchemaError(f'{name}.{field_name}: {kind!r} is not a supported type')
        if set(described) - {'type', 'description', 'enum', 'title'}:
            raise UnsupportedSchemaError(f'{name}.{field_name}: only type, enum and description are read')
        annotation: object = _SCHEMA_TYPES[kind]
        if choices := described.get('enum'):
            annotation = Literal[tuple(choices)]  # ty: ignore[invalid-type-form]
        fields[field_name] = (
            annotation if field_name in required else annotation | None,
            Field(description=described.get('description', '')) if field_name in required else None,
        )
    effective = create_model(f'{name}_arguments', **fields)  # ty: ignore[no-matching-overload]
    # What the *model* is offered: the same fields, minus the ones the
    # application fills in. A field it cannot see is a field it cannot send.
    offered = {key: value for key, value in properties.items() if key not in injected}
    model_schema = dict(schema, properties=offered, required=sorted(required - set(injected)))

    from_model = TypeAdapter(
        create_model(  # ty: ignore[no-matching-overload]
            f'{name}_from_model',
            **{key: value for key, value in fields.items() if key not in injected},
        )
    )

    def validate(ctx, **arguments) -> dict:
        """Step 1 and step 2: what the model sent, and what it may not send."""
        if overridden := set(arguments) & set(injected):
            raise ModelRetry(
                f'{name} was not called: {", ".join(sorted(overridden))} '
                'is supplied by the salon and cannot be set from here.'
            )
        return from_model.validate_python(arguments).model_dump()

    async def call_it(ctx, **arguments) -> object:
        """Step 3 and step 4: inject, validate the whole set, then invoke."""
        arguments |= {key: supply(ctx.deps) for key, supply in injected.items()}
        return await handler(ctx, **effective(**arguments).model_dump())

    return Tool.from_schema(
        call_it,
        name=name,
        description=description,
        json_schema=model_schema,
        takes_ctx=True,
        # Without this, `from_schema` calls the handler with whatever came
        # back. Probed against 2.46.0: the schema alone validates nothing.
        # ty: the SDK annotates `args_validator` as returning `None`, and
        # then uses what it returns as the validated arguments. Probed
        # against 2.46.0: `tool.args_validator_func(ctx, **args)` is assigned.
        args_validator=validate,  # ty: ignore[invalid-argument-type]
    )
