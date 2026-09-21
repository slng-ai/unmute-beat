"""What every agency on this harness shares: state, handoffs, and the step table.

An agency -- a set of agents that take one call between them -- supplies its own
subclasses of `VoiceState` and `Handoff` and its own `AGENTS` table. The
processor knows only what is here.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import NamedTuple

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
    a model that cannot get a tool call right behind a flaky network.
    """

    step_runs: int = 4
    model_requests: int = 8
    tool_calls: int = 8
    seconds: float = 20.0
    repair_retries: int = 2

    def account(self) -> tuple[RunUsage, UsageLimits]:
        """A fresh tally and the limits to read it against, for one turn."""
        return RunUsage(), UsageLimits(request_limit=self.model_requests, tool_calls_limit=self.tool_calls)


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
    # The one tool call in the current model response that is allowed to change
    # the flow or write to the business. Set by `one_action_per_reply` from the
    # whole response before any tool runs; read by every action that could be
    # the second one. `None` means this response proposed no such action.
    _claim: str | None = PrivateAttr(default=None)

    def commit(self, **updates) -> None:
        """Replace the whole state with one validated candidate, or change nothing.

        Built from values rather than `model_copy(update=...)`, which copies
        updates in without validating them at all, and rather than a sequence of
        assignments, which commits the first field before the second is checked.
        A caller writing a phone number and a verification status together gets
        both or neither: if the status is not a legal one the `ValidationError`
        leaves the previous state object exactly where it was.
        """
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

    async def speak_now(self, text: str) -> None:
        """Say this whatever else was said. For a goodbye or an error, never a filler."""
        if self._say is not None:
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
    `read_diary` does. `look_up_salon_info` never moves anybody.
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
    # `SalonState` callables. That one mismatch is the whole of the
    # `invalid-argument-type` diagnostics switched off in pyproject.toml, at
    # ~15 sites. Upgrade path: make `Node` generic over the state type, then
    # delete that line from `[tool.ty.rules]`.
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
