"""The graph: steps, the legal moves between them, and the two functions that walk it.

`Node` is one step and `Exit` one legal move out of it; an agency's table of
nodes is the whole graph. `route` decides where a handoff actually puts the
caller, interposing a prerequisite and remembering where to resume, and
`moved_by` decides where a successful borrowed tool has put them. Neither runs
a model.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

from loguru import logger
from pydantic_ai import Agent, ModelRequest
from pydantic_ai.direct import model_request
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits

from core.state import CallContext, Entry, VoiceState, present


class Exit[S: VoiceState](NamedTuple):
    """One legal move out of a step, and the state it is legal in.

    The exits whose guard holds are lowered into that step's own `hand_over` as
    a narrowed `Literal`, so an illegal transition is not caught at runtime: it
    is unrepresentable in the schema the model is sent.

    `via` is what makes an edge free. A step that names another step's entry
    tools here lends them into its own schema while the guard holds, and
    calling one *is* the move: the caller is in the new step, and the request
    that moved them is the same request that did their work. Without it the
    move costs a model request of its own that says nothing.

    Only a tool whose call genuinely means the caller has moved belongs in
    `via`. Reading the diary does; recording a complaint never moves anybody.
    """

    to: str
    guard: Callable[[S], bool] = lambda _: True
    via: tuple = ()
    """The lent tools themselves, not their names: the lender hands them to `Agent(tools=...)`."""


@dataclass(frozen=True)
class LocalAction:
    """An application-decided next action, run through the agency's normal validated executor."""

    name: str
    run: Callable[[], Awaitable[object]]


@dataclass(frozen=True)
class Opening[C: CallContext]:
    """What a step does first on arrival when the application already knows it.

    A step entered by a handoff or a prerequisite's return usually opens with
    one predictable output: ask for the number nobody has given, read back the
    one the carrier supplied, offer times for the request the caller made
    before being verified. Running a model to choose that output costs a full
    round trip, about 0.8s, and decides nothing.

    An opening names the entry it answers, a guard over the call, and the same
    executor the model-facing tool uses, so the fast path and the model path
    share every check. It is tried only after `advance` has declined, so an
    answer to a pending question always wins; a refusal from the executor is
    context for the model run that follows, never a second attempt.
    """

    name: str
    """The action's name in traces and budgets: the tool it stands in for."""
    after: Entry
    """The entry this answers. A step already running at the start of the turn has entry '' and no opening."""
    applies: Callable[[C], bool]
    run: Callable[[C], Awaitable[object]]

    def action(self, call: C) -> LocalAction | None:
        """This opening as the turn's next action, or None when it does not apply to this call now."""
        if not self.after or call.entered != self.after or not self.applies(call):
            return None
        return LocalAction(self.name, lambda: self.run(call))


@dataclass(frozen=True)
class Node[S: VoiceState]:
    """One step: the agent that runs it, where it may send the caller, and what has to be true before it runs."""

    agent: Agent[Any, Any]
    """`Agent[Any, Any]` rather than `Agent[S, ...]`: the output type varies per step and naming the union here would fix it for every agency."""
    enter: tuple[str, ...] = ()
    """Spoken by the turn engine, not the model, to cover the round trip the hop costs. Empty means the agent answers straight away."""
    exits: tuple[Exit[S], ...] = ()
    """The legal model-chosen moves out of this step."""
    requires: tuple[str, ...] = ()
    """State fields that must be present before this step may run."""
    satisfied_by: str = ''
    """The step that fills `requires`. The graph interposes it and comes back; no prompt states the ordering."""
    outputs: Callable[[S], list] | None = None
    """This turn's `output_type`, given the state. A callable because a guard reads live state. `None` leaves the agent's own."""
    guarded: bool = False
    """Whether this step can change the business.

    On one that can, the turn engine holds each clause of model speech back
    until it is complete and drops the run's speech if it claims a change
    nothing has made. Costs one clause of streaming latency, on these steps
    only, and only until something is actually written.
    """
    model_speech: bool = True
    """False for a step that speaks only through output functions, never through provider text."""
    history: Literal['messages', 'reset'] = 'messages'
    """What conversation this step is sent.

    `messages` is the delivered conversation so far. `reset` sends none of it:
    the step gets its own instructions and the named values projected into
    them, and nothing the caller said to anybody else.
    """
    advance: Callable[[Any, RunUsage, UsageLimits], Awaitable[LocalAction | None]] | None = None
    """Resolve a decision within the shared budget, or return None to run the conversational agent."""
    openings: tuple[Opening[Any], ...] = ()
    """What this step does on arrival without a model, tried in order after `advance` declines."""

    def opening(self, call: CallContext) -> LocalAction | None:
        """The first opening that applies to this call on this entry, or None to run the agent."""
        return next((action for o in self.openings if (action := o.action(call)) is not None), None)


@asynccontextmanager
async def model_clients(nodes: dict[str, Node[Any]], warm: bool = False):
    """Own each distinct provider client once for the session; the SDK reference-counts overlapping sessions.

    `warm` sends each client one single-token request in the background as the
    session opens. The HTTP connection opens lazily, so without it the first
    real request of a call pays TCP, TLS and HTTP/2 setup inside its time to
    first token: measured at 1.05s cold against 0.78s warm on 2026-09-23. The
    greeting is playing while it runs, so nobody waits on it. It is not traced
    and not counted: it is not a turn's request.
    """
    seen = set()
    async with AsyncExitStack() as stack:
        warming: list[asyncio.Task[None]] = []
        for node in nodes.values():
            model = node.agent.model
            if isinstance(model, Model) and id(model) not in seen:
                seen.add(id(model))
                await stack.enter_async_context(model)
                if warm:
                    settings = node.agent.model_settings
                    fixed = None if callable(settings) else settings
                    warming.append(asyncio.create_task(_warm(model, fixed)))
        try:
            yield
        finally:
            for task in warming:
                task.cancel()


async def _warm(model: Model, settings: ModelSettings | None) -> None:
    """Open the client's connection with the cheapest request it accepts. A failure only means a cold first turn."""
    try:
        await model_request(
            model,
            [ModelRequest.user_text_prompt('ok')],
            model_settings={**(settings or {}), 'max_tokens': 1},
            instrument=False,
        )
    except Exception as error:
        logger.info('model warm-up failed, the first turn will be cold: {}', type(error).__name__)


def route[S: VoiceState](state: S, nodes: dict[str, Node[S]], to: str) -> tuple[str, str | None]:
    """Where the caller actually goes, and what is left waiting.

    Pure: it returns the decision and writes nothing. Its caller commits the
    step and the pending slot in one validated candidate, because they are one
    application decision.

    It interposes a prerequisite by recording the destination and entering the
    step that satisfies it; the finisher there clears it. No model request
    decides any of this, and the step at the other end is the one that was
    going to run anyway, so it costs no extra round trip.

    One pending slot, overwritten rather than stacked: a second interposition
    means the first was abandoned. A caller sent *straight* to a satisfier,
    rather than interposed into it, still comes back to where they were.

    ponytail: the slot is one deep because no satisfier has prerequisites of
    its own, which the graph tests refuse to let change. If one ever does, this
    becomes a stack and `resume` needs a matching loop.
    """
    node = nodes[to]
    if node.satisfied_by and not all(present(getattr(state, field, None)) for field in node.requires):
        return node.satisfied_by, to
    if state.agent != to and any(other.satisfied_by == to for other in nodes.values()):
        return to, state.agent
    return to, state.pending


def lent[S: VoiceState](exits: tuple[Exit[S], ...]) -> dict[str, Exit[S]]:
    """The tools a step borrows from its neighbours, by the name the model sees.

    One map, read twice: the agency's `build` uses it to put the borrowed tools
    in the schema and to take them back out when their guard stops holding,
    and `moved_by` uses it to work out which step a call landed the caller in.
    """
    return {tool.__name__: exit for exit in exits for tool in exit.via}


def moved_by[S: VoiceState](state: S, node: Node[S], nodes: dict[str, Node[S]], succeeded: set[str]) -> str:
    """The step a *successful* tool call has put the caller in, or where they were.

    Free, like `route`: the move rides the request that called the tool.

    Three things are re-derived here rather than trusted from when the schema
    was built, because all three can have changed since: the edge is one this
    step actually owns; its guard still holds; and the destination's own
    prerequisites are met, because entering a step by the back door would skip
    the interposition `route` exists to do.

    `succeeded` is tools that ran *and returned*. A tool that raised, retried
    or was cancelled by barge-in is not a move.
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
