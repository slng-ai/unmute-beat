"""The graph: steps, the legal moves between them, and the function that walks it.

`Node` is one step and `Exit` one legal move out of it; an agency's table of
nodes is the whole graph. `route` decides where a handoff actually puts the
caller, interposing a prerequisite and remembering where to resume. It runs
no model.
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

from core.state import CallContext, VoiceState, present


class Exit(NamedTuple):
    """One legal move out of a step: where to.

    A conversation step's exits are lowered into its own `hand_over` as the
    destinations its schema allows, so an illegal hand-over is not merely
    caught at runtime: it is unrepresentable in the schema the model is sent.
    The graph (`core.flow`) refuses any other move.
    """

    to: str


@dataclass(frozen=True)
class LocalAction:
    """An application-decided next action, run through the agency's normal validated executor."""

    name: str
    run: Callable[[], Awaitable[object]]
    answers: bool = True
    """False: what it says is not the answer to the caller's words, so the step's model still reads them after it.

    A receipt said again because the caller spoke over it is the first thing
    they must hear, and says nothing about what they said over it (session
    4g, trace 28723e6b: "move it to Saturday" was dropped, and said again).
    """


@dataclass(frozen=True)
class Restated:
    """The caller answered a read-back by saying its value again, unchanged: code carries the operation out.

    "Is your number +34 671 51 01 05?" answered "No, it's six seven one,
    five one zero, one zero five" is the same number, grouped the caller's
    own way. The step's agent only records what it heard (`collect`); code
    finds the recorded value equal to the read-back the caller heard in full
    before they began (`CallContext.pending`), and that is the yes
    (`core.steps.ConversationStep._reads_back`). `run` binds the approval to
    that exact question, says `Locale.restated` and carries it out, as the
    agent's own call would have; it returns None if nothing is pending.
    A different value is a change, read back again as before.

    Benchmark calls amAP5b4A and Tiv9sDHH (session 4c): the agent had read
    such a reply as a yes by itself, silently; the user asked for it to be
    code's decision and said aloud.

    The same shape carries out an operation with no read-back whose fields
    the reply just recorded (`carries_out`): code runs it, and the model is
    never offered it.
    """

    about: str
    run: Callable[[RunUsage | None, UsageLimits | None], Awaitable[Any]]


@dataclass(frozen=True)
class Opening[C: CallContext]:
    """What a step does first on arrival when the application already knows it.

    A step entered by a handoff or a prerequisite's return usually opens with
    one predictable output: ask for the number nobody has given, read back the
    one the carrier supplied, offer times for the request the caller made
    before being verified. Running a model to choose that output costs a full
    round trip, about 0.8s, and decides nothing.

    An opening names a guard over the call and the same executor the
    model-facing tool uses, so the fast path and the model path share every
    check. It answers an arrival by a hand-over only: a step already running
    at the start of the turn, or one a prerequisite returned to, runs its
    agent, which reads what the caller said meanwhile. It is tried only after
    `advance` has declined, so an answer to a pending question always wins; a
    refusal from the executor is context for the model run that follows,
    never a second attempt.
    """

    name: str
    """The action's name in traces and budgets: the tool it stands in for."""
    applies: Callable[[C], bool]
    run: Callable[[C], Awaitable[object]]

    def action(self, call: C) -> LocalAction | None:
        """This opening as the turn's next action, or None when it does not apply to this call now."""
        if call.entered != 'handoff' or not self.applies(call):
            return None
        return LocalAction(self.name, lambda: self.run(call))


@dataclass(frozen=True)
class Node[S: VoiceState]:
    """One step: the agent that runs it, where it may send the caller, and what has to be true before it runs."""

    agent: Agent[Any, Any]
    """`Agent[Any, Any]` rather than `Agent[S, ...]`: the output type varies per step and naming the union here would fix it for every agency."""
    exits: tuple[Exit, ...] = ()
    """The legal moves out of this step."""
    requires: tuple[str, ...] = ()
    """State fields that must be present before this step may run."""
    satisfied_by: str = ''
    """The step that fills `requires`. The graph interposes it and comes back; no prompt states the ordering."""
    model_speech: bool = True
    """False for a step that speaks only through output functions, never through provider text."""
    speaks_for: frozenset[str] = frozenset()
    """Outputs that say their own words (a step's operations): model text in the same reply is not said.

    The model wrote the application's read-back, or a holding line, beside
    the operation that then said it again: 19 times in 10 of 20 calls of the
    session-4 baseline. The turn engine holds a reply's text on such a step
    until the reply shows whether one of these is called.
    """
    reads_back: Callable[[Any], Any] | None = None
    """What code reads back when a reply that only records fields has just recorded an operation's (`reads_back`).

    Returns the read-back (a `Question`) or None. Saves the second request
    that only called the operation for its read-back (43 in the baseline).
    """
    history: Literal['all', 'messages', 'none'] = 'all'
    """What conversation from earlier caller turns this step is sent (`context: {history: ...}`).

    `all` is the whole retained history, tool exchanges included.
    `messages` is what was said and heard: the caller's words and the
    agent's delivered speech, with earlier tool exchanges left out
    (`core.history.conversation`). `none` sends none of it: the step gets
    its own instructions, the facts it may see, and this turn's words. The
    current caller turn always reaches the step whole.
    """
    consults: frozenset[str] = frozenset()
    """The exits that are consultations: the caller comes back here when that step is `done`."""
    capabilities: frozenset[str] = frozenset()
    """The ids of the on-demand capabilities this step registers: their load records survive `history: messages`."""
    advance: Callable[[Any, RunUsage, UsageLimits], Awaitable[LocalAction | None]] | None = None
    """Resolve a decision within the shared budget, or return None to run the conversational agent."""
    openings: tuple[Opening[Any], ...] = ()
    """What this step does on arrival without a model, tried in order after `advance` declines."""
    returns: bool = False
    """Whether a caller handed here goes back to the step that handed them over once it is done (a code node).

    Recorded like a prerequisite's waiting step (`route`), so the return is
    code, never a model's choice, and the graph allows it (`legal_moves`).
    """

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
    and not a turn's request, but it is a paid request: the list this yields
    gets one record per warm-up sent (model, tokens, whether it answered), so
    a launcher can count every one. A worker that owns its clients for its
    whole life (salon-graph's `server.py`) warms once; `bot.py` warms per call.

    On the way out a warm-up still running is cancelled and awaited before
    the clients close, so no request is left using a closed client.
    """
    seen = set()
    warmed: list[dict[str, Any]] = []
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
                    warming.append(asyncio.create_task(_warm(model, fixed, warmed)))
        try:
            yield warmed
        finally:
            for task in warming:
                task.cancel()
            await asyncio.gather(*warming, return_exceptions=True)


async def _warm(model: Model, settings: ModelSettings | None, warmed: list[dict[str, Any]]) -> None:
    """Open the client's connection with the cheapest request it accepts. A failure only means a cold first turn."""
    record: dict[str, Any] = {
        'model': model.model_name,
        'answered': False,
        'input_tokens': None,
        'output_tokens': None,
    }
    warmed.append(record)
    try:
        response = await model_request(
            model,
            [ModelRequest.user_text_prompt('ok')],
            model_settings={**(settings or {}), 'max_tokens': 1},
            instrument=False,
        )
    except Exception as error:
        record['error'] = type(error).__name__
        logger.info('model warm-up failed, the first turn will be cold: {}', type(error).__name__)
    else:
        record.update(
            answered=True,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        logger.info('model warm-up: {}', record)


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
    if node.satisfied_by and not holds(state, node.requires):
        return node.satisfied_by, to
    if state.agent != to and (node.returns or any(other.satisfied_by == to for other in nodes.values())):
        return to, state.agent
    return to, state.pending


def holds(state: VoiceState, requires: tuple[str, ...]) -> bool:
    """Whether every required fact is present, on the state or, for a configured flow, on its application facts."""
    facts = getattr(state, 'facts', None)
    return all(
        present(getattr(state, name))
        if name in type(state).model_fields
        else present(getattr(facts, name, None))
        for name in requires
    )
