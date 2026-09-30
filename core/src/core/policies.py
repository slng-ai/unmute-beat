"""What one caller turn may spend, and the hooks that keep a model run honest.

Everything here is a plain value or a `Hooks` capability composed onto an
agent: a budget shared by every run in a turn, one consequential action per
model reply, a bound on how many requests a run may spend looking things up,
and a per-request clock.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from pydantic_ai import ModelRetry
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.usage import RunUsage, UsageLimits

from core.state import Request


@dataclass(frozen=True)
class TurnBudget:
    """Everything one accepted caller turn is allowed to spend.

    One object for the whole turn, not one per agent run: a turn that hands
    the caller between four agents has spent four agents' worth of a caller's
    patience, and budgets that reset per run bound nothing. The usage object
    built by `account` is threaded through every run in the turn.

    These numbers are proposals to tune and record, not SDK defaults. They are
    written down here so a change to them is a change to a file rather than a
    different number in three places.

    `repair_retries` counts semantic repair only, a tool refusing and the model
    correcting itself. Provider SDK retries and HTTP attempts are a different
    layer with its own counter, and adding them together would hide a model
    that cannot get a tool call right behind a flaky network. It is supplied to
    each agent run separately, so it bounds repairs *per run*; `step_runs` is
    what bounds how many times it can be spent.

    `tool_calls` is read against `RunUsage.tool_calls`, which the SDK
    increments for tools that **succeeded**. A tool that refused with a
    `ModelRetry` costs a model request and does not cost this.
    """

    step_runs: int = 4
    model_requests: int = 8
    tool_calls: int = 8
    seconds: float = 20.0
    repair_retries: int = 2
    output_retries: int = 1
    """Retries for a run's output alone (Pydantic AI's `retries={'output': ...}`): an empty reply, or an output that fails validation.

    Session 2's matrix (N6 `empty_output`): a doubly empty structured reply
    cost 3 step requests with one budget of 2 for both, native's rerun cost 1.
    One output retry keeps one repair of a malformed output and reaches the
    fixed line a request sooner; tool refusals keep `repair_retries`.
    """
    first_part: float = 1.5
    """Seconds a step request may go without its first part before it is cancelled and its run tried again, once.

    Over Coval run 4bLgLR4t, step requests had their first part at p50 0.74 s
    and p99 1.20 s. One request in the user's call of 24 September sent
    nothing for 8.49 s; the caller heard nothing until they spoke again.
    """
    hold_after: float = 1.5
    """Seconds a tool or an application action may run with nothing said before a holding line goes out.

    The timer belongs to that work and is cancelled when it finishes: a slow
    model request, or a turn's second one, is no reason for the line
    (`core.turn.Turn._hold`). Above a plain answer's p90 (about 1.0 s) so a
    quick turn never gets one. 1.5 s, not 1.2 s: over six calls of
    2026-09-25, 4 of 14 holding lines were followed by the answer within
    0.2 s ("One moment. Shall I book you in...?").
    """

    per_piece: int = 2
    """Requests and tool calls each further piece of a joined turn adds to its limits, up to twice the turn's own.

    A joined turn keeps one tally (`CallContext.continues_turn`), so a loop
    over one input stays bounded. But every piece is new words the model has
    to be sent: live call hzSdfMNn (session 4c) had an interruptive caller
    say their number in eight pieces, and the turn ran out of requests while
    it was still being told the number. Each piece now adds a little; the
    cap keeps a turn that never ends from spending without end.
    """

    def account(self, pieces: int = 1) -> tuple[RunUsage, UsageLimits]:
        """A fresh tally and the limits to read it against, for one turn of this many joined pieces."""
        more = self.per_piece * max(pieces - 1, 0)
        return RunUsage(), UsageLimits(
            request_limit=min(self.model_requests + more, 2 * self.model_requests),
            tool_calls_limit=min(self.tool_calls + more, 2 * self.tool_calls),
        )


def request_clock(into: Hooks | None = None) -> Hooks:
    """Stamp every provider request as it goes out, so time to first token is per request.

    The turn's own clock cannot stand in for one. A run that calls a tool makes
    two requests and only the second one speaks, so timing from the start of
    the run reports the tool's entire round trip as time to first token.

    `before_model_request` fires exactly once per provider request, including
    the extra one a repair retry buys, and `ctx.deps` is the call, so one list
    collects every request of every run in the turn. The turn engine closes
    each entry when the first part of the response arrives.

    `into` registers the hook on an agent's existing `Hooks` rather than a
    new one, as every policy here does: Pydantic AI dispatches each
    capability separately, and measured about 110 us per run for each extra
    `Hooks` on a bare agent (`docs/research/reusable-core/core-optimization.md`).
    """
    hooks = into if into is not None else Hooks()

    @hooks.on.before_model_request
    async def stamp(ctx, request_context):
        ctx.deps.requests.append(Request(started=time.perf_counter()))
        return request_context

    return hooks


CONFLICT = (
    'Only one of these may run per reply, and another one is already running: {other}. '
    'Nothing was done here. Wait for that result, then decide what to do next.'
)


def claim_or_refuse(ctx, actions: frozenset[str]) -> None:
    """Refuse this action if another one in the same reply already has the claim.

    Called first, before the tool does anything, so a refused action never
    reaches its service. `ctx.deps._claim` was decided from the complete model
    response before any tool ran: a real preflight, in the order the model
    emitted the calls.
    """
    claim = ctx.deps._claim
    if ctx.tool_name in actions and claim is not None and ctx.tool_call_id != claim:
        raise ModelRetry(CONFLICT.format(other=claim))
    if ctx.tool_name in actions and ctx.deps._reading:
        raise ModelRetry(READING)


READING = (
    'Nothing was done: this reply also looks something up, and it was decided before the result came back. '
    'Wait for that result, then decide.'
)
"""A structured step's answer or action beside a read in the same response (`one_action_per_reply(reads=...)`)."""

PATCHED = (
    'Nothing was recorded: another call in this reply already carries the update. '
    'One call per reply may change what the caller asked for.'
)


def patch_or_refuse(ctx) -> None:
    """Refuse an `update` on any call but the one `one_action_per_reply` chose to carry it (`_patcher`)."""
    if ctx.deps._patcher is not None and ctx.tool_call_id != ctx.deps._patcher:
        raise ModelRetry(PATCHED)


def one_action_per_reply(
    actions: frozenset[str],
    updates: frozenset[str] = frozenset(),
    into: Hooks | None = None,
    reads: frozenset[str] = frozenset(),
) -> Hooks:
    """One flow-changing or business-mutating action per model response.

    Two of them in one reply is the model proposing two futures. Letting both
    run and rejecting one transition afterwards is the thing to avoid: the
    losing action has already written to the business by then.

    So the winner is chosen from the whole response, before anything executes,
    by the order the model emitted the calls: `after_model_request` runs after
    the provider replies and before the tool-calling node, so it can see the
    complete set. Emission order rather than a set, because two runs of the
    same reply have to reach the same answer.

    Enforcement is in two halves because the SDK only offers one:
    `before_tool_execute` covers ordinary tools and does not fire for output
    tools at all. Output tools call `claim_or_refuse` themselves on their first
    line, which is the same refusal at the same point.

    `updates` names tools that change what the caller asked for (`collect`,
    whose `values` name the fields). Every field one of them tries to set in
    this caller turn is noted (`CallContext._changing`), from the whole
    response and before anything runs, whether or not the change is then
    accepted. An action whose arguments rest on one of those fields asks
    again instead of acting on the old read-back: "yes, nine, actually make it
    ten" is a change whatever order the two calls came in, and a change the
    state refused is still not a yes to the old time. A change to something
    else ("yes, that number is right, and make it Saturday") leaves the
    number's yes standing. A change emitted *after* the action in the same
    response has not run yet when the action does (`_unapplied`), so the
    action refuses with a retry and is called again once it has.

    On a structured step the update travels inside the call it belongs to
    (`update`, `core.structured`), so there is no sibling to order. Two rules
    replace that. A response that calls one of `reads` answers and acts on
    nothing beside it (`_reading`): its answer was written before the result
    came back, in whatever order the calls were emitted. And only the first
    call carrying a non-empty update may apply it (`_patcher`,
    `patch_or_refuse`): two updates in one response are two readings of the
    caller.
    """
    hooks = into if into is not None else Hooks()

    @hooks.on.after_model_request
    async def choose(ctx, *, request_context, response):
        ctx.deps._claim = None
        calls = [part for part in response.parts if isinstance(part, ToolCallPart)]
        for part in calls:
            if part.tool_name in actions:
                ctx.deps._claim = part.tool_call_id
                break
        ctx.deps._unapplied = set()
        ctx.deps._reading = any(part.tool_name in reads for part in calls)
        ctx.deps._patcher = None
        claimed = False
        for part in calls:
            claimed = claimed or part.tool_call_id == ctx.deps._claim
            if part.tool_name in updates:
                try:
                    given = part.args_as_dict()
                except ValueError:
                    given = {}
                values = given.get('values') or given.get('update') or {}
                fields = set(values) if isinstance(values, dict) else set()
                if fields and ctx.deps._patcher is None and 'update' in given:
                    ctx.deps._patcher = part.tool_call_id
                ctx.deps._changing |= fields
                ctx.deps._tries.update(fields)
                if claimed and part.tool_call_id != ctx.deps._claim:
                    ctx.deps._unapplied |= fields
        return response

    @hooks.on.before_tool_execute
    async def refuse_the_rest(ctx, *, call, tool_def, args):
        if call.tool_name in actions and ctx.deps._claim is not None and call.tool_call_id != ctx.deps._claim:
            raise ModelRetry(CONFLICT.format(other=call.tool_name))
        return args

    return hooks


def speak_by(request: int, into: Hooks | None = None) -> Hooks:
    """From this request on, a run has no ordinary tools left: it answers with what it has.

    The turn budget is the last resort and its failure mode is an apology. This
    is the bound in front of it. Two tool-bearing requests is the longest chain
    any step legitimately needs, so from the third the model is offered speech
    and its **output** tools only: a run that has read the diary can still hand
    over or save, because those finish the run rather than extending it. What
    it guarantees is that the run cannot buy another request to look something
    else up; the turn budget behind it is still what stops the turn.

    `ctx.run_step` counts model requests from 1 within one run.
    """
    hooks = into if into is not None else Hooks()

    @hooks.on.prepare_tools
    async def only_speech(ctx, defs):
        return [] if ctx.run_step >= request else defs

    return hooks


_LAST: dict[str, str] = {}


def pick(key: str, lines: tuple[str, ...]) -> str:
    """One of these lines, never the one this key used last.

    A line written in code is spoken by us, not generated, so it is the same
    sentence every time it fires unless something varies it.

    ponytail: process-wide, not per call. Two concurrent callers can only
    affect each other's variety, which is the whole point of the function.
    """
    choice = random.choice([line for line in lines if line != _LAST.get(key)] or list(lines))
    _LAST[key] = choice
    return choice
