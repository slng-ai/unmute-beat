"""Conversation steps: one typed terminal decision per model response.

A step whose agent recorded the caller's words with a tool of their own paid a
second request to say something after it (session 2 of the typed-node
proposal: 17 record, options, speech chains in 20 saved calls). Here the
update travels inside the decision it belongs to, as one validated argument:

- `reply(say, update)` answers the caller and ends the run: one request.
- `record(update)` records and lets code speak what follows (an operation
  carried out, `carries_out`, or a read-back its value said again confirms),
  silently on purpose, which is not the same as an answer that went missing.
- The step's operations, `hand_over` and `done` take the same `update`,
  applied by code before the action, so no sibling tool has to run first.
- A pure read (no approval, no change, no route, no receipt words) is an
  ordinary tool (`Read`), and so is `options` (`Offering`): the update is
  applied, the service is asked once, and the result comes back to the same
  agent, which then answers. Two requests.

Every update is applied whole or not at all (`Decision.apply`): the
application's own types, validators, whole-state rules and validation
context, and the choice rules, through `FlowCall.collect`.
A refusal commits nothing and says nothing. Words are said only after the
decision they come with is committed; partial streamed arguments are never
read. Nothing here decides consent: every operation still reads back and runs
only on a heard yes (`core.steps.ConversationStep._operate`).

The Pydantic AI mechanism is the output function (`ToolOutput`): its
arguments are validated and it runs once, and the run ends on it. A
`NativeOutput` answer beside an ordinary update tool costs a third request
under the default `graceful` end strategy, and `early` skips the update
(`docs/implementation/typed-nodes/session-2.md`).
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Collection, Mapping
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Annotated, Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, create_model
from pydantic.fields import FieldInfo
from pydantic_ai import ModelRetry, RunContext, Tool, ToolOutput
from pydantic_ai.tools import ToolDefinition

from core.operations import Operation, OutcomeUnknownError
from core.policies import claim_or_refuse, patch_or_refuse
from core.state import Handoff
from core.steps import FlowCall, _args, _not_ready, _offer, _resting_on, said_by_caller

if TYPE_CHECKING:
    from core.steps import ConversationStep, StepFlow

Said = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
"""Words for the caller: never blank, so a decision that owes an answer cannot end in silence."""


def _no_default(schema: dict[str, Any]) -> None:
    """Leave a patch field's placeholder default out of its schema: absent is not a value the model is offered."""
    schema.pop('default', None)


def patch_type(state: type[BaseModel], fields: tuple[str, ...], name: str) -> type[BaseModel]:
    """The update a step may send: each of its fields with the state's own type, constraints and description.

    Every field may be left out, and one left out is not in
    `model_fields_set`, so it keeps its value. An explicit null is accepted
    only where the state's type takes None. False, 0 and '' are values.
    The state's defaults and examples are not copied: a default is never an
    update. A nested model is replaced whole, never merged.
    """
    found = state.model_fields

    def typed(info: FieldInfo) -> tuple[Any, Any]:
        kind = Annotated[info.annotation, *info.metadata] if info.metadata else info.annotation  # ty: ignore[invalid-type-form]
        return kind, Field(None, description=info.description, json_schema_extra=_no_default)

    return create_model(  # ty: ignore[no-matching-overload]
        name,
        __config__=ConfigDict(extra='forbid'),
        __doc__=(
            'What the caller has just said for these fields, recorded with this call. Leave a field out to '
            'keep its value; null clears it. Never guess a value.'
        ),
        **{field: typed(found[field]) for field in fields},
    )


def _shaped(
    function: Callable[..., Any], params: Mapping[str, tuple[Any, Any]], returns: Any
) -> Callable[..., Any]:
    """Give a tool function exactly these parameters after `ctx`, with real annotations, for its schema.

    `params` maps a name to (annotation, default), `inspect.Parameter.empty`
    for a required one. Pydantic AI builds the tool's schema from the
    signature and these annotations, so a step shows only what applies to it
    (no `update` on a step with no fields, no `decline` without a read-back).
    """
    kind = inspect.Parameter.POSITIONAL_OR_KEYWORD
    function.__signature__ = inspect.Signature(  # ty: ignore[unresolved-attribute]
        [
            inspect.Parameter('ctx', kind, annotation=RunContext[FlowCall]),
            *(inspect.Parameter(n, kind, annotation=a, default=d) for n, (a, d) in params.items()),
        ],
        return_annotation=returns,
    )
    function.__annotations__ = {
        'ctx': RunContext[FlowCall],
        **{n: a for n, (a, _) in params.items()},
        'return': returns,
    }
    return function


REQUIRED = inspect.Parameter.empty


class Decision:
    """How one structured step's permitted update is typed, checked and applied: whole and once, or not at all."""

    def __init__(self, flow: StepFlow, step: ConversationStep) -> None:
        self.flow = flow
        self.step = step
        self.patch: type[BaseModel] | None = self.typed(step.fields, '')

    def typed(self, fields: Collection[str], label: str) -> type[BaseModel] | None:
        """The update type for these of the step's fields, in the step's order; None when none of them is its."""
        names = tuple(name for name in self.step.fields if name in fields)
        if not names:
            return None
        title = ''.join(part.title() for part in f'{self.step.id}_{label}'.split('_'))
        return patch_type(self.flow.state_type, names, f'{title}Update')

    def param(
        self, fields: Collection[str] | None = None, label: str = '', required: bool = False
    ) -> dict[str, tuple[Any, Any]]:
        """The `update` parameter for a tool of this step, over `fields` (all of them by default), or none.

        An action takes only the fields it rests on (`_resting_on`): what the
        caller said that it needs. The rest goes with the answer after it.
        One schema per tool, so a narrow one costs every request less.
        """
        found = self.patch if fields is None else self.typed(fields, label)
        if found is None:
            return {}
        return {'update': (found, REQUIRED) if required else (found | None, None)}

    async def apply(self, ctx: RunContext[FlowCall[Any]], update: BaseModel | None) -> frozenset[str]:
        """Commit this update as one validated state, or raise `ModelRetry` with nothing committed.

        Only the one call in the response that carries the update may apply
        it (`patch_or_refuse`). A quoted field must be the caller's words. The
        commit is `StepFlow.record_update`: the application's
        validators with the call's `before`, `heard` and clock, whole-state
        rules, the resets of what rested on a changed field, and every pick
        on an offer that stands for the state the update ends in. Any refusal
        records nothing. Returns the fields recorded (`FlowCall._recorded`).
        """
        given = (
            {name: getattr(update, name) for name in update.model_fields_set} if update is not None else {}
        )
        if not given:
            return frozenset()
        patch_or_refuse(ctx)
        call = ctx.deps
        for name in self.step.quoted:
            if given.get(name) and not said_by_caller(call, str(given[name])):
                raise ModelRetry(
                    f"Nothing was recorded: {name} must be the caller's own words, copied from what they said. "
                    'Punctuation and case may differ; nothing else may.'
                )
        before = {name: getattr(call.state.facts, name, None) for name in given}
        try:
            refused = await self.flow.record_update(call, deepcopy(given), self.step.choices)
        except ValidationError as error:
            raise ModelRetry(f'Nothing was recorded: {call.note_refusal(error)}.') from None
        if refused:
            raise ModelRetry(
                'Nothing was recorded: '
                + '; '.join(f'{given[name]!r} is not on offer for {name}' for name in refused)
                + '. Only an id on offer can be recorded.'
            )
        call._kept.update(name for name in given if getattr(call.state.facts, name, None) == before[name])
        call._recorded = frozenset(given)
        return frozenset(given)

    def taking(
        self,
        function: Callable[..., Awaitable[Any]],
        actions: frozenset[str],
        params: Mapping[str, Any] = {},
        update: Mapping[str, tuple[Any, Any]] | None = None,
    ) -> Callable[..., Awaitable[Any]]:
        """An output that also takes an `update` (this step's, or `update`): its claim, then the update, then it runs.

        The claim comes first, so an action refused as the second of its
        response commits no update either. An action refused after its valid
        update (not ready yet) keeps the update, which is what the caller
        said, and says nothing.
        """
        update = self.param() if update is None else update
        if not update:
            return function

        async def taken(ctx: RunContext[FlowCall[Any]], update: BaseModel | None = None, **given: Any) -> Any:
            claim_or_refuse(ctx, actions)
            await self.apply(ctx, update)
            return await function(ctx, **given)

        taken.__name__ = getattr(function, '__name__', 'taken')
        taken.__doc__ = function.__doc__
        return _shaped(taken, {**params, **update}, Any)


class Read:
    """A pure read a structured step's agent calls as an ordinary tool, with the update it rests on.

    Only an operation with no read-back, no change, no route and no receipt
    words: its answer is information for the same agent, not something code
    says. The run is the one operation boundary (`Operation.run`): argument
    binding from state, the audit span, receipt validation, `matches`,
    assignment into state, evidence. Nothing is sent before the update is
    committed, and the arguments are the state's, never the model's.

    The agent is told the result's status and its public part: the fields
    the receipt set that the step may see (`core.history.Scope`), or, for a
    read that sets none, the receipt whole, the application's declared
    return type being its public result. `found`, `refused` (a validated no),
    `absent` (no receipt for this caller) and `failed` (no answer: a read
    changed nothing, so nothing is claimed) are different results. A
    cancelled read assigns nothing. The same read asked again for the same
    turn and state is not sent again.
    """

    def __init__(self, decision: Decision, operation: Operation) -> None:
        self.decision = decision
        self.operation = operation

    @staticmethod
    def eligible(operation: Operation) -> bool:
        """No approval, no business change, no receipt route and no receipt words: a read that continues."""
        return not (
            operation.needs_approval or operation.changes or operation.route or getattr(operation, 'say', '')
        )

    def tool(self) -> Tool:
        operation = self.operation
        own = operation.args_type.__dict__.get('__doc__') or getattr(
            getattr(operation, 'function', None), '__doc__', ''
        )
        about = (inspect.cleandoc(own).split('\n\n')[0] + ' ') if own else ''

        async def read(ctx: RunContext[FlowCall[Any]], update: BaseModel | None = None) -> dict[str, Any]:
            return await self(ctx, update)

        return Tool(
            _shaped(read, self.decision.param(_resting_on(operation), operation.name), dict[str, Any]),
            name=operation.name,
            description=f'{about}Looks it up for the caller from the application state; the result comes back '
            'to you, then answer them.',
            sequential=True,
        )

    async def __call__(
        self, ctx: RunContext[FlowCall[Any]], update: BaseModel | None = None
    ) -> dict[str, Any]:
        call, operation = ctx.deps, self.operation
        await self.decision.apply(ctx, update)
        try:
            args = _args(operation, call)
        except ValidationError as error:
            raise ModelRetry(_not_ready(operation, call, error)) from None
        key = (operation.name, args.model_dump_json())
        if call._reads.get(key) == (call.turn, call._version):
            raise ModelRetry(
                f'Nothing was looked up again: {operation.name} already answered for this request. '
                'Answer from that result.'
            )
        try:
            receipt = await operation.run(call, args, usage=ctx.usage, usage_limits=ctx.usage_limits)
        except OutcomeUnknownError as error:
            logger.info('read {} did not answer: {}', operation.name, error)
            return {
                'status': 'failed',
                'detail': 'The lookup did not answer, so nothing is known from it. Nothing was changed.',
            }
        except ModelRetry as error:
            call._reads[key] = (call.turn, call._version)
            return {'status': 'refused', 'detail': str(error)}
        call._reads[key] = (call.turn, call._version)
        if not isinstance(receipt, BaseModel):
            return {'status': 'absent', 'detail': 'The lookup has nothing for this caller.'}
        return {'status': 'found', 'result': self.public(call, receipt)}

    def public(self, call: FlowCall[Any], receipt: BaseModel) -> Any:
        data = receipt.model_dump(mode='json')
        assign = getattr(self.operation, 'assign', None) or {}
        if not assign:
            return data
        scope = call.scope()
        if scope is not None:
            return scope.public_receipt(self.operation.name, data)
        return {name: data if source == '*' else data.get(source) for name, source in assign.items()}


class Offering:
    """`options` on a structured step: the update applied, then a choice function asked only where no offer stands.

    An offer that still stands is current: the reset rules take it away as
    soon as a field it rests on changes (`FlowCall.collect`). So a repeat
    asks nothing, and an automatic refresh after a change (the booking just
    made, offered again) is never fetched twice. Nothing changed and every
    offer standing is a labelled retry with no read, and while every offer
    stands the tool's schema asks for an update (`current`), so a refetch
    with nothing new is not even offered.
    """

    def __init__(self, decision: Decision) -> None:
        self.decision = decision

    def tool(self) -> Tool:
        async def options(ctx: RunContext[FlowCall[Any]], update: BaseModel | None = None) -> dict[str, Any]:
            """The options you may offer the caller now, by id, for what they asked, with the update it rests on. Only an offered id can be picked. Say them in your own words."""
            return await self(ctx, update)

        return Tool(
            _shaped(options, self.decision.param(self.resting(), 'options'), dict[str, Any]), sequential=True
        )

    def resting(self) -> set[str]:
        """The fields the step's offers rest on: each choice's `after:`, or every field but the choices without one."""
        step = self.decision.step
        return set[str]().union(
            *(step.choice_after.get(name, set(step.fields) - set(step.choices)) for name in step.choices)
        )

    async def __call__(self, ctx: RunContext[FlowCall[Any]], update: BaseModel | None) -> dict[str, Any]:
        call, choices = ctx.deps, self.decision.step.choices
        changed = await self.decision.apply(ctx, update)
        missing = {name: choose for name, choose in choices.items() if name not in call._offers}
        if not missing and not changed:
            raise ModelRetry(
                'Nothing was looked up again: what is on offer now still stands, under "On offer". '
                'Answer from it, or send what the caller changed.'
            )
        found = await _offer(call, missing) if missing else {}
        if 'stale' in found or 'unavailable' in found:
            return found
        standing = {
            name: {'options': dict(call._offers[name])}
            for name in choices
            if name in call._offers and name not in found
        }
        return standing | found

    def current(self, call: FlowCall[Any], defs: list[ToolDefinition]) -> list[ToolDefinition]:
        """While every offer stands, `options` requires an update: nothing new to look up is no reason to call it."""
        if not all(name in call._offers for name in self.decision.step.choices):
            return defs
        kept = []
        for tool_def in defs:
            if tool_def.name == 'options' and 'update' in tool_def.parameters_json_schema.get(
                'properties', {}
            ):
                schema = deepcopy(tool_def.parameters_json_schema)
                given = schema['properties']['update']
                ref = next((item for item in given.get('anyOf', [given]) if '$ref' in item), None)
                if ref is not None:
                    schema['properties']['update'] = {'allOf': [ref], 'minProperties': 1}
                    schema['required'] = ['update']
                    tool_def = replace(tool_def, parameters_json_schema=schema)
            kept.append(tool_def)
        return kept


class Structured:
    """The tools and outputs of one conversation step, built once at compile, shared by every call.

    `ConversationStep.node` hands in what it built (`goodbye`, `say_again`,
    its operations, `hand_over`) and gets back the step's set: no text
    output, and every decision that may carry the caller's words takes them
    as `update`.
    """

    def __init__(
        self,
        flow: StepFlow,
        step: ConversationStep,
        actions: frozenset[str],
        read_back: Callable[[FlowCall[Any]], Any] | None,
        confirms: bool,
    ) -> None:
        self.step = step
        self.decision = Decision(flow, step)
        self.reads = [Read(self.decision, op) for op in step.operations if Read.eligible(op)]
        self.offering = Offering(self.decision) if step.choices else None
        self.read_back = read_back
        self.confirms = confirms
        self.actions = (
            actions - {read.operation.name for read in self.reads}
            | {'reply', 'goodbye', 'record'}
            | ({'done'} if step.consulted else frozenset())
        )

    @property
    def silent(self) -> bool:
        """Whether the step needs `record`: a reply code answers that no operation call could stand for.

        `carries_out` operations are never offered to the model, and a
        read-back confirmed by its value said again (`restated: confirms`) is
        a record, not a call. So is a `reads_back` field the operation's
        update cannot carry, since its arguments do not rest on it: Faro's
        `asked_language`, which changes only the read-back's words. Any other
        `reads_back` operation is offered with its update, which reads it
        back in the same one request. A step that `offers:` has it too: the
        day the caller gives is recorded, and code offers the times for it.
        """
        step = self.step
        read_back = [op for op in step.operations if op.name in step.reads_back]
        return bool(step.offers) or (
            self.read_back is not None
            and (
                bool(step.carries)
                or any(op.confirms_by_restating for op in read_back)
                or any(not set(step.reads_back[op.name]) <= _resting_on(op) for op in read_back)
            )
        )

    @property
    def guarded(self) -> tuple[Operation, ...]:
        """The operations that stay terminal outputs: read back, carried out on a yes, or said by code."""
        read = {r.operation.name for r in self.reads}
        return tuple(op for op in self.step.operations if op.name not in read)

    @property
    def read_names(self) -> frozenset[str]:
        return frozenset(r.operation.name for r in self.reads) | (
            {'options'} if self.offering else frozenset()
        )

    @property
    def updates(self) -> frozenset[str]:
        """Every tool or output that may carry an `update`: what `one_action_per_reply` reads it from."""
        if self.decision.patch is None:
            return frozenset()
        return self.actions | self.read_names

    def tools(self) -> list[Tool]:
        return [*(read.tool() for read in self.reads), *([self.offering.tool()] if self.offering else [])]

    def outputs(
        self,
        goodbye: Callable[..., Awaitable[str]],
        say_again: ToolOutput[Any] | None,
        operate: Callable[[Operation], Callable[..., Awaitable[Any]]],
        hand_over: Callable[..., Awaitable[Handoff]] | None,
    ) -> list[Any]:
        decision, actions = self.decision, self.actions
        found: list[Any] = [
            ToolOutput(self._reply(), name='reply'),
            ToolOutput(self._goodbye(goodbye), name='goodbye'),
            *([say_again] if say_again is not None else []),
        ]
        if self.silent and decision.patch is not None:
            found.append(ToolOutput(self._record(), name='record'))
        found.extend(
            ToolOutput(
                decision.taking(
                    operate(operation),
                    actions,
                    update=decision.param(_resting_on(operation), operation.name),
                ),
                name=operation.name,
                description=f"{operation.name}: the first call reads it back to the caller in the application's "
                'words. Call it again only when their reply to that read-back is a clear yes: that yes '
                'carries it out.',
                sequential=True,
            )
            for operation in self.guarded
        )
        if hand_over is not None:
            found.append(
                ToolOutput(
                    decision.taking(hand_over, actions, {'to': (str, REQUIRED), 'because': (str, REQUIRED)}),
                    name='hand_over',
                    sequential=True,
                )
            )
        if self.step.consulted:
            found.append(ToolOutput(self._done(), name='done', sequential=True))
        return found

    def _reply(self) -> Callable[..., Awaitable[str]]:
        decision, actions, confirms = self.decision, self.actions, self.confirms

        async def reply(
            ctx: RunContext[FlowCall[Any]], say: str, update: BaseModel | None = None, decline: bool = False
        ) -> str:
            """Answer the caller, with what they have just said recorded. Ends your turn.

            Args:
                say: Your words to the caller, as they will hear them.
                update: What the caller has just said for your fields, to record now. Leave it out if nothing.
                decline: True only when their reply turns down the read-back they are answering. Nothing is carried out.
            """
            claim_or_refuse(ctx, actions)
            call = ctx.deps
            await decision.apply(ctx, update)
            if decline and not call.decline():
                logger.info('decline with no read-back heard in full waiting: nothing to turn down')
            await call.speak_now(say, origin='model')
            return say

        return _shaped(
            reply,
            {
                'say': (Said, REQUIRED),
                **decision.param(),
                **({'decline': (bool, False)} if confirms else {}),
            },
            str,
        )

    def _goodbye(self, goodbye: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        actions = self.actions
        if not self.confirms:

            async def leave(ctx: RunContext[FlowCall[Any]], recap: list[str] | None = None) -> str:
                claim_or_refuse(ctx, actions)
                return await goodbye(ctx, recap)

            leave.__doc__ = goodbye.__doc__
            return _shaped(leave, {'recap': (list[str] | None, None)}, str)

        async def leave_declining(
            ctx: RunContext[FlowCall[Any]], recap: list[str] | None = None, decline: bool = False
        ) -> str:
            claim_or_refuse(ctx, actions)
            if decline:
                ctx.deps.decline()
            return await goodbye(ctx, recap)

        leave_declining.__doc__ = (goodbye.__doc__ or '') + (
            '\n    decline: True when they also turn down the read-back they are answering.'
        )
        return _shaped(leave_declining, {'recap': (list[str] | None, None), 'decline': (bool, False)}, str)

    def _record(self) -> Callable[..., Awaitable[Any]]:
        decision, actions, read_back, step = self.decision, self.actions, self.read_back, self.step

        async def record(ctx: RunContext[FlowCall[Any]], update: BaseModel) -> Any:
            """Record what the caller has just said when the application answers it: it reads back or carries out what follows. Say nothing yourself."""
            claim_or_refuse(ctx, actions)
            call = ctx.deps
            if not await decision.apply(ctx, update):
                raise ModelRetry('Nothing was recorded: the update is empty. Answer the caller with reply.')
            asked = read_back(call) if read_back is not None else None
            if asked is None and step.owes_offer(call):
                return await step.offer(call)
            if asked is None:
                raise ModelRetry(
                    f'Recorded: {", ".join(sorted(call._recorded))}. Nothing follows from it for the application '
                    'to say: answer the caller with reply.'
                )
            return asked

        return _shaped(record, decision.param(required=True), Any)

    def _done(self) -> Callable[..., Awaitable[Handoff]]:
        actions = self.actions

        async def done(ctx: RunContext[FlowCall[Any]], answer: str, handback: str = '') -> Handoff:
            """You have answered what you were asked. Your answer is said to the caller, then they go back to the step that asked you.

            Args:
                answer: Your answer to what you were asked, as the caller will hear it. Said once.
                handback: One short sentence for the step that asked you: what else the caller said for it (a change to their request). Never spoken. Empty when there is nothing.
            """
            claim_or_refuse(ctx, actions)
            call = ctx.deps
            back = call.state.consulting
            if back is None:
                raise ModelRetry('Nothing was done: nobody sent the caller here to ask you something.')
            await call.speak_now(answer, origin='model')
            handback = handback.strip()
            return Handoff(to=back, status='returned', ends_turn=not handback, because=handback)

        return _shaped(done, {'answer': (Said, REQUIRED), 'handback': (str, '')}, Handoff)

    def hooks(self, call_hooks: Any) -> None:
        """Register the `options` schema rule on the step's one `Hooks` (`Offering.current`)."""
        if self.offering is None:
            return
        offering = self.offering

        @call_hooks.on.prepare_tools
        async def current(ctx: RunContext[FlowCall[Any]], defs: list[ToolDefinition]) -> list[ToolDefinition]:
            return offering.current(ctx.deps, defs)
