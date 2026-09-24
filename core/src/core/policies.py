"""What one caller turn may spend, and the hooks that keep a model run honest.

Everything here is a plain value or a `Hooks` capability composed onto an
agent: a budget shared by every run in a turn, one consequential action per
model reply, a bound on how many requests a run may spend looking things up, a
per-request clock, and a schema-authored tool that validates what its schema
says it does.
"""

from __future__ import annotations

import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field, TypeAdapter, create_model
from pydantic_ai import ModelRetry, Tool
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
    first_part: float = 1.5
    """Seconds a step request may go without its first part before it is cancelled and its run tried again, once.

    Over Coval run 4bLgLR4t, step requests had their first part at p50 0.74 s
    and p99 1.20 s. One request in the user's call of 24 September sent
    nothing for 8.49 s; the caller heard nothing until they spoke again.
    """

    def account(self) -> tuple[RunUsage, UsageLimits]:
        """A fresh tally and the limits to read it against, for one turn."""
        return RunUsage(), UsageLimits(request_limit=self.model_requests, tool_calls_limit=self.tool_calls)


def request_clock() -> Hooks:
    """Stamp every provider request as it goes out, so time to first token is per request.

    The turn's own clock cannot stand in for one. A run that calls a tool makes
    two requests and only the second one speaks, so timing from the start of
    the run reports the tool's entire round trip as time to first token.

    `before_model_request` fires exactly once per provider request, including
    the extra one a repair retry buys, and `ctx.deps` is the call, so one list
    collects every request of every run in the turn. The turn engine closes
    each entry when the first part of the response arrives.
    """
    hooks = Hooks()

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


def one_action_per_reply(actions: frozenset[str]) -> Hooks:
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
    """
    hooks = Hooks()

    @hooks.on.after_model_request
    async def choose(ctx, *, request_context, response):
        ctx.deps._claim = None
        for part in response.parts:
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

    The turn budget is the last resort and its failure mode is an apology. This
    is the bound in front of it. Two tool-bearing requests is the longest chain
    any step legitimately needs, so from the third the model is offered speech
    and its **output** tools only: a run that has read the diary can still hand
    over or save, because those finish the run rather than extending it. What
    it guarantees is that the run cannot buy another request to look something
    else up; the turn budget behind it is still what stops the turn.

    `ctx.run_step` counts model requests from 1 within one run.
    """
    hooks = Hooks()

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


class UnsupportedSchemaError(Exception):
    """A schema this harness will not build a tool from, said at build time.

    Refused rather than approximated. A converter that silently drops a
    keyword it does not implement produces a tool that validates less than its
    schema says it does, and nothing downstream can tell.
    """


_SCHEMA_TYPES: dict[str, type] = {'string': str, 'boolean': bool, 'integer': int, 'number': float}
"""What a schema-authored tool may say about one argument. Anything else is an error rather than a guess."""


def schema_tool(
    name: str,
    description: str,
    schema: dict,
    handler: Callable[..., Awaitable[object]],
    injected: dict[str, Callable[[Any], object]] | None = None,
) -> Tool:
    """A tool whose arguments are described by a JSON schema rather than by annotations.

    `Tool.from_schema` does **no** argument validation unless an
    `args_validator` is supplied: the handler is called with whatever the
    model sent. This builds that validator from the schema, so the guarantee
    an ordinary typed tool gets for free is the same one here.

    Four things happen before the handler is reached, in this order, and the
    handler is not called if any of them fails:

    1. The model's arguments are validated against the schema's own fields.
    2. A field the *application* supplies is refused if the model sent one.
       An injected value is not a default the model may improve on.
    3. The application's current values are injected. `injected` is keyed by
       field name; each entry is handed the call and returns the value.
    4. The whole effective argument set is validated again, because an
       injected value can be wrong too.

    Unsupported constructs raise at build time, so a schema this cannot honour
    never becomes a tool the model is offered.
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
    offered = {key: value for key, value in properties.items() if key not in injected}
    model_schema = dict(schema, properties=offered, required=sorted(required - set(injected)))

    from_model = TypeAdapter(
        create_model(  # ty: ignore[no-matching-overload]
            f'{name}_from_model',
            **{key: value for key, value in fields.items() if key not in injected},
        )
    )

    def validate(ctx, **arguments) -> dict:
        """Steps 1 and 2: what the model sent, and what it may not send."""
        if overridden := set(arguments) & set(injected):
            raise ModelRetry(
                f'{name} was not called: {", ".join(sorted(overridden))} '
                'is supplied by the salon and cannot be set from here.'
            )
        return from_model.validate_python(arguments).model_dump()

    async def call_it(ctx, **arguments) -> object:
        """Steps 3 and 4: inject, validate the whole set, then invoke."""
        arguments |= {key: supply(ctx.deps) for key, supply in injected.items()}
        return await handler(ctx, **effective(**arguments).model_dump())

    return Tool.from_schema(
        call_it,
        name=name,
        description=description,
        json_schema=model_schema,
        takes_ctx=True,
        args_validator=validate,  # ty: ignore[invalid-argument-type]
    )
