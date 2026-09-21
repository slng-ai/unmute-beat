"""What every agency on this harness shares: state, handoffs, and the step table.

An agency -- a set of agents that take one call between them -- supplies its own
subclasses of `VoiceState` and `Handoff` and its own `AGENTS` table. The
processor knows only what is here.
"""

from __future__ import annotations

import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, NamedTuple
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.messages import ModelMessage, ToolCallPart
from pydantic_ai.usage import RunUsage, UsageLimits

SayFn = Callable[[str], Awaitable[None]]
EndFn = Callable[[], Awaitable[None]]


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


class Handoff(BaseModel):
    """Where the caller goes next. Agencies return this from their `hand_over` action."""

    to: str
    because: str = Field(default='', description='One short sentence: what the caller wants. Never spoken.')


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
    # Whether the *model* streamed text this turn. Read by `note_spoken`,
    # which may only cut down a response this turn produced.
    _spoke: bool = PrivateAttr(default=False)
    # This call, told apart from every other one sharing the process. Part of
    # the name of every business operation it starts, so one call's write can
    # never be answered out of another call's cache.
    _id: str = PrivateAttr(default_factory=lambda: uuid4().hex[:12])

    @property
    def id(self) -> str:
        """This call's own identity, stable for its whole life."""
        return self._id

    @property
    def requests(self) -> list[Request]:
        """This turn's provider requests, oldest first."""
        return self._requests

    @property
    def spoken(self) -> list[Spoken]:
        """This turn's delivered speech segments, in the order they went out."""
        return self._spoken

    def note_said(self, kind: Literal['fixed', 'model'], text: str, at: float | None = None) -> None:
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
            self._spoken.append(Spoken(kind, text, at=time.perf_counter() if at is None else at))

    def note_heard(self, delivered: str) -> None:
        """Cut this turn's last model segment down to what was actually heard.

        Generated text is not heard text. The runtime is the only thing that
        knows where speech stopped, so it says, and the ledger stops claiming
        the rest of the sentence.
        """
        for segment in reversed(self._spoken):
            if segment.kind == 'model':
                segment.text, segment.interrupted = delivered, True
                return

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
        self.state = type(self.state)(**(self.state.model_dump() | updates))

    async def say(self, text: str) -> None:
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
        self.note_said('fixed', text)
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

    def turn_started(self) -> None:
        """The caller has said something new. Age out anything that was only news.

        Called by the processor before the first agent of a turn runs, so a
        prompt can branch on "has this just happened" as a fact rather than
        asking the model to remember whether it already said it.
        """
        self._requests.clear()
        self._spoken.clear()
        self._spoke = False

    async def speak_now(self, text: str) -> None:
        """Say this whatever else was said. For a goodbye or an error, never a filler."""
        if self._say is not None:
            self.note_said('fixed', text)
            await self._say(text)

    async def end_call(self) -> None:
        """Hang up, once the caller has said the call is over."""
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


def filler_hook(lines_by_tool: dict[str, tuple[str, ...]]) -> Hooks:
    """Speak into the gap a tool leaves, before it runs.

    The model is never asked to narrate: a generated "let me check" costs the
    round trip it is meant to cover, and arrives after it. These go to TTS the
    moment the call is validated, and `VoiceState.say` allows one per silence,
    so a handoff line already spoken suppresses them.
    """
    hooks = Hooks()

    @hooks.on.before_tool_execute
    async def cover_the_wait(ctx, *, call, tool_def, args):
        if lines := lines_by_tool.get(call.tool_name):
            await ctx.deps.say(pick(f'tool:{call.tool_name}', lines))
        return args

    return hooks


class Exit(NamedTuple):
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
    guard: Callable[[VoiceState], bool] = lambda _: True
    # The functions themselves, not their names: this is what a step lends, and
    # the lender has to be able to hand them to `Agent(tools=...)`.
    via: tuple = ()


@dataclass(frozen=True)
class Node:
    """One step: the agent that runs it, where it may send the caller, and what
    has to be true before it runs at all.
    """

    # ponytail: `Node` is not generic, so `agent` is a bare `Agent` (ty resolves
    # it to `Agent[object, str]`) and the callables below are fixed to
    # `VoiceState`, while every salon agent is `Agent[SalonCall, ...]` with
    # `SalonState` callables. That is most of the `invalid-argument-type`
    # diagnostics switched off in pyproject.toml -- 31 of them, measured, not
    # the ~15 this said before. Five are inside the SDK and would survive the
    # fix; see the note there. Upgrade path: make `Node` generic over the
    # state type, re-measure, narrow what is left.
    agent: Agent
    # Spoken by the processor, not the model, to cover the model round trip the
    # hop costs. Empty means the receiving agent answers straight away.
    enter: tuple[str, ...] = ()
    # The legal model-chosen moves out of this step.
    exits: tuple[Exit, ...] = ()
    # State fields that must be truthy before this step may run, and the step
    # that fills them. The graph interposes it and comes back: no model request
    # decides any of that, and no prompt has to state the ordering.
    requires: tuple[str, ...] = ()
    satisfied_by: str = ''
    # This turn's `output_type`, given the state. It is a callable because a
    # guard reads live state, so the narrowed `Literal` cannot be fixed at
    # import. `None` leaves the agent's own, which is the unguarded superset.
    outputs: Callable[[VoiceState], list] | None = None


def route(state: VoiceState, nodes: dict[str, Node], to: str) -> tuple[str, str | None]:
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


def lent(exits: tuple[Exit, ...]) -> dict[str, Exit]:
    """The tools a step borrows from its neighbours, by the name the model sees.

    One map, read twice: `build` uses it to put the borrowed tools in the schema
    and to take them back out when their guard stops holding, and `moved_by`
    uses it to work out which step a call landed the caller in.
    """
    return {tool.__name__: exit for exit in exits for tool in exit.via}


def moved_by(state: VoiceState, node: Node, nodes: dict[str, Node], succeeded: set[str]) -> str:
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
