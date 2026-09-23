"""Typed steps that compile into the turn engine's nodes: collect, confirm, action, conversation, end.

A business describes a flow as an ordered list of steps and `StepFlow`
compiles it into the same `Node` table the salon hand-writes, so the same
turn engine, budget, barge-in handling and Pydantic graph run it. What each
kind does:

* `CollectStep` asks for typed fields and records them only once they
  validate. A caller who answers a later step's question early has that
  answer kept, and the later step is skipped when its turn comes.
* `ConfirmStep` reads back the exact proposal an operation would carry out,
  in words this code chose, and advances only on acceptance of *that*
  proposal. Anything conditional or unclear gets the proposal read back again.
* `ActionStep` runs one `Operation` and files its receipt. Only a receipt
  that says what the step needed completes it; a refusal goes to the step's
  failure path, and an unknown outcome to its pending path, never to success.
* `ConversationStep` answers from its instructions and knowledge and stays
  active across turns until the caller says goodbye.
* `End` says one closing line and records how the flow ended.

Whether a step is done is always derived from the facts on the call (see
`core.completion`), never stored as a flag. When the caller changes a fact,
everything built on it is forgotten, so a changed system means the level and
the eligibility check are asked for and run again.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from pydantic_ai import Agent, ModelRetry, RunContext, ToolOutput
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits

from core.classifiers import Classifier
from core.completion import FieldsPresent, ReceiptMatched
from core.decisions import Accept, Clarify, Conditional, NoDecision, Reject, Revise
from core.graph import Exit, LocalAction, Node, Opening
from core.interpreters import ClassifierInterpreter, Interpreter
from core.operations import Operation, OutcomeUnknownError
from core.proposals import BaseProposal, Proposal
from core.state import CallContext, Handoff, VoiceState, present


class FlowState(VoiceState):
    """What a configured flow knows: the facts it has collected, and how it ended."""

    agent: str
    facts: dict[str, Any] = Field(default_factory=dict)
    """Validated field values, and each action step's receipt under the step's id."""
    outcome: Literal['', 'completed', 'cancelled', 'failed', 'pending'] = ''


class FlowCall(CallContext):
    """One call through a configured flow."""

    state: FlowState


@dataclass(frozen=True)
class Prompted:
    """A question this code worded: recorded as the pending question, then said by the turn engine."""

    about: str
    spoken: str
    value: str = ''

    def put(self, call: CallContext) -> str:
        call.ask(BaseProposal(about=self.about, spoken=self.spoken, value=self.value, owner=call.identity()))
        return self.spoken


class ConfirmInput(BaseModel):
    """What the confirmation interpreter reads: the reply, and the exact proposal it may be answering."""

    model_config = ConfigDict(extra='forbid', frozen=True)
    utterance: str
    proposal: str
    delivered: bool


class ConfirmReading(BaseModel):
    """The reply to a readback, as one decision."""

    model_config = ConfigDict(extra='forbid', frozen=True)
    decision: Accept | Reject | Revise | Conditional | Clarify | NoDecision = Field(discriminator='kind')


class ConfirmInterpreter(ClassifierInterpreter[FlowCall, ConfirmInput, ConfirmReading]):
    """Reads a reply to the last readback. A readback the caller spoke over can never be accepted."""

    def _input(self, call: FlowCall) -> ConfirmInput:
        last = call.last_proposal()
        return ConfirmInput(
            utterance=call.heard, proposal=last[0].spoken if last else '', delivered=bool(last and last[1])
        )

    def _still_valid(self, call: FlowCall, value: ConfirmInput) -> bool:
        last = call.last_proposal()
        return value.utterance == call.heard and value.proposal == (last[0].spoken if last else '')

    def _settle(self, call: FlowCall, value: ConfirmInput, result: ConfirmReading) -> ConfirmReading:
        if not value.delivered and isinstance(result.decision, Accept):
            return ConfirmReading(decision=NoDecision())
        return result


def confirm_interpreter(model: Model | str, settings: ModelSettings | None = None) -> ConfirmInterpreter:
    """The default reader of confirmations: one structured request, no tools, no history."""
    classifier = Classifier(
        model,
        ConfirmInput,
        ConfirmReading,
        instructions='Read the utterance as exactly one decision about the proposal, using the schema descriptions.',
        model_settings=settings,
    )
    return ConfirmInterpreter('confirm', classifier, ConfirmReading(decision=NoDecision()))


class ConfirmedOperation[ArgsT: BaseModel, ReceiptT: BaseModel](
    Operation[FlowCall, ArgsT, ReceiptT, ReceiptT]
):
    """An operation authorised by the flow's confirmation: the default readback check, and the interpreter's yes.

    `_record` returns the receipt, which the `ActionStep` files.
    """

    def __init__(self, interpreter: Interpreter[FlowCall, Any, ConfirmReading]) -> None:
        self.interpreter = interpreter

    async def _agreed(
        self, call: FlowCall, question: Proposal, *, usage: RunUsage | None, usage_limits: UsageLimits | None
    ) -> bool:
        reading = await self.interpreter.interpret(call, usage=usage, usage_limits=usage_limits)
        return isinstance(reading.decision, Accept)

    async def _record(self, call: FlowCall, args: ArgsT, receipt: ReceiptT) -> ReceiptT:
        return receipt


@dataclass(frozen=True, kw_only=True)
class Step(ABC):
    """One step of a flow: what it needs, what it produces, and whether it is done."""

    id: str
    then: str = ''
    """The step after this one. Empty means the next listed step that is not already done."""

    @property
    def produces(self) -> frozenset[str]:
        """The facts this step writes, forgotten when a fact it depends on changes."""
        return frozenset()

    @property
    def depends_on(self) -> frozenset[str]:
        """The facts this step's result rests on."""
        return frozenset()

    @property
    def elsewhere(self) -> tuple[str, ...]:
        """Where this step can send the caller other than onward: failure, rejection, revision."""
        return ()

    def done(self, facts: Mapping[str, Any]) -> bool:
        """Whether the facts show this step's purpose is met. Never true without evidence."""
        return False

    @abstractmethod
    def node(self, flow: StepFlow) -> Node[FlowState]:
        """This step as a node the turn engine runs."""


def _agent(flow: StepFlow, step: Step, instructions: str, outputs: list) -> Agent[FlowCall, object]:
    return Agent[FlowCall, object](
        flow.model,
        name=step.id,
        deps_type=FlowCall,
        output_type=outputs,  # type: ignore[arg-type]
        instructions=instructions,
        model_settings=flow.settings,
    )


def _node(flow: StepFlow, step: Step, agent: Agent[FlowCall, object], **node: Any) -> Node[FlowState]:
    return Node(agent, exits=tuple(Exit(to) for to in flow.destinations(step)), **node)


def _entered(call: FlowCall) -> bool:
    return True


@dataclass(frozen=True, kw_only=True)
class CollectStep(Step):
    """Ask for typed fields; done once every required one has a validated value.

    `fields` is a Pydantic model: its field descriptions are what the model
    is told to listen for, and its validators (with the flow's facts as
    context) decide what is acceptable. `after` names facts its values are
    checked against, so changing one of those forgets this step's answer.
    """

    fields: type[BaseModel]
    question: str
    after: frozenset[str] = frozenset()
    lookahead: bool = True
    """Whether a caller may answer later collect steps early, in the same breath."""

    @property
    def produces(self) -> frozenset[str]:
        return frozenset(self.fields.model_fields)

    @property
    def depends_on(self) -> frozenset[str]:
        return self.after

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(name for name, info in self.fields.model_fields.items() if info.is_required())

    def done(self, facts: Mapping[str, Any]) -> bool:
        return FieldsPresent(*self.required).is_met(facts)

    def ask(self) -> Prompted:
        return Prompted(about=f'collect:{self.id}', spoken=self.question)

    def node(self, flow: StepFlow) -> Node[FlowState]:
        listening = [self, *(flow.collects_after(self) if self.lookahead else [])]
        values = create_model(  # ty: ignore[no-matching-overload]
            f'{self.id.title()}Values',
            __config__=ConfigDict(extra='forbid'),
            **{
                name: (info.annotation | None, Field(None, description=info.description))
                for step in listening
                for name, info in step.fields.model_fields.items()
            },
        )

        async def collect(ctx: RunContext[FlowCall], values: BaseModel) -> Handoff | Prompted:
            """Record exactly what the caller has just said for any of these fields. Never guess a value."""
            return await self.record(flow, ctx.deps, values.model_dump(exclude_none=True), listening)

        collect.__annotations__ = {
            'ctx': RunContext[FlowCall],
            'values': values,
            'return': Handoff | Prompted,
        }
        wanted = '; '.join(
            f'{name}: {info.description or name}' for name, info in self.fields.model_fields.items()
        )
        instructions = (
            f'You are on the phone, collecting: {wanted}. When the caller gives any value, call `collect` '
            'with only what they said. If they ask something else, answer in one short sentence and ask again.'
        )
        agent = _agent(flow, self, instructions, [str, ToolOutput(collect, name='collect')])
        return _node(flow, self, agent, openings=(Opening('ask', 'handoff', _entered, self.arrive(flow)),))

    def arrive(self, flow: StepFlow):
        async def arrive(call: FlowCall) -> Handoff | Prompted:
            """Ask the question on arrival, or move straight on if it was answered early."""
            if self.done(call.state.facts):
                return Handoff(to=flow.next_after(self, call))
            return self.ask()

        return arrive

    async def record(
        self, flow: StepFlow, call: FlowCall, proposed: dict[str, Any], listening: Sequence[CollectStep]
    ) -> Handoff | Prompted:
        """Keep each step's values only if they validate as a whole; this step's own must, or nothing is kept."""
        facts = {**call.state.facts, **proposed}
        accepted: dict[str, Any] = {}
        for step in listening:
            names = step.fields.model_fields
            if not any(name in proposed for name in names):
                continue
            data = {name: facts[name] for name in names if present(facts.get(name))}
            try:
                model = step.fields.model_validate(data, context={'facts': facts})
            except ValidationError as error:
                if step is self:
                    problems = '; '.join(e['msg'] for e in error.errors())
                    raise ModelRetry(f'Nothing was recorded: {problems}. Ask the caller again.') from None
                continue
            accepted |= {name: value for name, value in model.model_dump(mode='json').items() if name in data}
        flow.record(call, accepted)
        if self.done(call.state.facts):
            return Handoff(to=flow.next_after(self, call))
        return self.ask()


@dataclass(frozen=True, kw_only=True)
class ConfirmStep(Step):
    """Read back an operation's exact proposal and wait for agreement to *that*.

    Acceptance moves to `then`, the action, in the same turn, and the action
    consumes the approval for those same arguments. Rejection and revision go
    where they are told. A conditional or unclear answer, or a yes after the
    readback has expired, gets the proposal read back again; a question is
    answered by the step's agent, and the next yes still needs a fresh readback.
    """

    operation: Operation
    on_reject: str
    on_revise: str

    @property
    def depends_on(self) -> frozenset[str]:
        return frozenset(self.operation.args_type.model_fields)

    @property
    def elsewhere(self) -> tuple[str, ...]:
        return (self.on_reject, self.on_revise)

    def done(self, facts: Mapping[str, Any]) -> bool:
        return bool(self.then) and isinstance(facts.get(self.then), Mapping)

    def readback(self, call: FlowCall) -> Prompted:
        args = self.operation.args_type.model_validate(call.state.facts)
        proposal = self.operation.proposal(args)
        return Prompted(about=proposal.about, spoken=proposal.spoken, value=proposal.value)

    def node(self, flow: StepFlow) -> Node[FlowState]:
        agent = _agent(
            flow,
            self,
            'The caller was just asked to confirm a request. Answer their question in one short sentence. '
            'Never say the request is confirmed or submitted.',
            [str],
        )

        async def read_back(call: FlowCall) -> Prompted:
            """Put the exact proposal to the caller."""
            return self.readback(call)

        async def advance(call: FlowCall, usage: RunUsage, limits: UsageLimits) -> LocalAction | None:
            proposal = self.readback(call)
            if call.pending(proposal.about, value=proposal.value) is None:
                return LocalAction('readback', lambda: read_back(call)) if call.heard.strip() else None
            reading = await flow.interpreter.interpret(call, usage=usage, usage_limits=limits)
            match reading.decision:
                case Accept():
                    return LocalAction('confirm', lambda: _moved(self.then))
                case Reject():
                    return LocalAction('reject', lambda: _consumed(call, self.on_reject))
                case Revise():
                    return LocalAction('revise', lambda: _revised(flow, call, self.on_revise))
                case Conditional() | Clarify():
                    return LocalAction('readback', lambda: read_back(call))
            return None

        return _node(
            flow,
            self,
            agent,
            advance=advance,
            openings=(Opening('readback', 'handoff', _entered, read_back),),
            model_speech=True,
        )


async def _moved(to: str) -> Handoff:
    return Handoff(to=to)


async def _consumed(call: FlowCall, to: str) -> Handoff:
    call.answered()
    return Handoff(to=to)


async def _revised(flow: StepFlow, call: FlowCall, to: str) -> Handoff:
    """Go back to the step being revised, forgetting its answer so it is asked again."""
    call.answered()
    flow.forget(call, flow.by_id[to].produces)
    return Handoff(to=to)


@dataclass(frozen=True, kw_only=True)
class ActionStep(Step):
    """Run one operation with the collected facts and file its receipt under this step's id.

    `accepts` says whether the receipt is what this step needed (eligible,
    submitted); one that is not goes to `on_error`, as does a refusal. An
    outcome nobody knows goes to `on_unknown`, never to either.
    """

    operation: Operation
    after: frozenset[str] = frozenset()
    accepts: Callable[[Mapping[str, Any]], bool] = field(default=lambda _: True)
    on_error: str = ''
    on_unknown: str = ''

    @property
    def produces(self) -> frozenset[str]:
        return frozenset({self.id})

    @property
    def depends_on(self) -> frozenset[str]:
        return self.after | frozenset(self.operation.args_type.model_fields)

    @property
    def elsewhere(self) -> tuple[str, ...]:
        return tuple(to for to in (self.on_error, self.on_unknown) if to)

    def done(self, facts: Mapping[str, Any]) -> bool:
        return ReceiptMatched(self.id, self.accepts).is_met(facts)

    def node(self, flow: StepFlow) -> Node[FlowState]:
        async def act(call: FlowCall) -> Handoff:
            """Run the operation now; where the caller goes depends only on what came back."""
            args = self.operation.args_type.model_validate(call.state.facts)
            try:
                receipt = await self.operation.run(call, args)
            except OutcomeUnknownError:
                return Handoff(to=self.on_unknown or self.on_error)
            except ModelRetry:
                return Handoff(to=self.on_error)
            flow.record(call, {self.id: receipt.model_dump(mode='json')})
            if not self.done(call.state.facts):
                return Handoff(to=self.on_error)
            return Handoff(to=flow.next_after(self, call))

        agent = _agent(flow, self, 'Say one short sentence: you are working on it.', [str])
        return _node(flow, self, agent, openings=(Opening(self.operation.name, 'handoff', _entered, act),))


@dataclass(frozen=True, kw_only=True)
class ConversationStep(Step):
    """Answer from instructions and knowledge; stays active every turn until the caller says goodbye."""

    instructions: str
    knowledge: str = ''

    def node(self, flow: StepFlow) -> Node[FlowState]:
        async def goodbye(ctx: RunContext[FlowCall], words: str) -> str:
            """End the call, only once the caller's own words say they are finished. Say one short goodbye.

            Args:
                words: The goodbye. It is the last thing they hear.
            """
            ctx.deps.end_requested('caller')
            await ctx.deps.speak_now(words, origin='goodbye')
            await ctx.deps.end_call()
            return words

        text = self.instructions + (f'\n\nKnowledge:\n{self.knowledge}' if self.knowledge else '')
        agent = _agent(flow, self, text, [str, ToolOutput(goodbye, name='goodbye')])
        return _node(flow, self, agent)


@dataclass(frozen=True, kw_only=True)
class End(Step):
    """Say one closing line and record how the flow ended. `say` may use the facts: `{field}`."""

    outcome: Literal['completed', 'cancelled', 'failed', 'pending']
    say: str

    def node(self, flow: StepFlow) -> Node[FlowState]:
        async def close(call: FlowCall) -> str:
            """Record the outcome and say it."""
            call.commit(outcome=self.outcome)
            words = self.say.format_map(_Facts(call.state.facts))
            await call.speak_now(words, origin='confirmation')
            return words

        agent = _agent(flow, self, 'The request is finished. Answer briefly; do not start a new one.', [str])
        return _node(flow, self, agent, openings=(Opening('close', 'handoff', _entered, close),))


class _Facts(dict):
    """Facts for `str.format_map`, with nested receipts reachable as `{step[field]}` and a blank for anything missing."""

    def __missing__(self, key: str) -> str:
        return ''


class StepFlow:
    """An ordered list of steps, checked and compiled into the turn engine's nodes table.

    Shared config, per-call state: the flow holds steps, a model and an
    interpreter; facts live on each call. Construction refuses a flow whose
    destinations do not exist, whose ids repeat, or that has no `End` with
    outcome `completed`, so a broken flow fails where it is defined rather
    than on a call.
    """

    def __init__(
        self,
        steps: Sequence[Step],
        *,
        model: Model | str,
        interpreter: Interpreter[FlowCall, Any, ConfirmReading] | None = None,
        settings: ModelSettings | None = None,
    ) -> None:
        self.steps = tuple(steps)
        self.by_id = {step.id: step for step in self.steps}
        if len(self.by_id) != len(self.steps):
            raise ValueError('step ids must be unique')
        finished = [s for s in self.steps if isinstance(s, End) and s.outcome == 'completed']
        if any(not isinstance(s, End | ConversationStep) for s in self.steps) and not finished:
            raise ValueError('a flow with business steps needs an End with outcome completed')
        self.finish = finished[0].id if finished else ''
        for step in self.steps:
            for to in (step.then, *step.elsewhere):
                if to and to not in self.by_id:
                    raise ValueError(f'{step.id} sends the caller to {to}, which is not a step')
        self.model = model
        self.settings = settings
        self.interpreter = interpreter if interpreter is not None else confirm_interpreter(model, settings)
        self.nodes: dict[str, Node[FlowState]] = {step.id: step.node(self) for step in self.steps}

    @property
    def start(self) -> str:
        return self.steps[0].id

    def open_call(self, identity: Callable[[FlowCall], str] | None = None) -> FlowCall:
        """A fresh call, standing on the first step."""
        return FlowCall(state=FlowState(agent=self.start))

    def sequence(self) -> list[Step]:
        """The business steps in order, without the endings."""
        return [s for s in self.steps if not isinstance(s, End)]

    def collects_after(self, step: Step) -> list[CollectStep]:
        order = self.sequence()
        return [s for s in order[order.index(step) + 1 :] if isinstance(s, CollectStep)]

    def destinations(self, step: Step) -> tuple[str, ...]:
        """Every step this one can hand the caller to: onward (skipping done steps), or elsewhere."""
        if isinstance(step, End | ConversationStep):
            return ()
        order = self.sequence()
        onward = [s.id for s in order[order.index(step) + 1 :]] if not step.then else [step.then]
        return tuple(dict.fromkeys([*onward, *step.elsewhere, *([self.finish] if self.finish else [])]))

    def next_after(self, step: Step, call: FlowCall) -> str:
        """The first step after this one that is not done yet, or the finishing `End`."""
        if step.then:
            target = self.by_id[step.then]
            return (
                target.id
                if isinstance(target, End) or not target.done(call.state.facts)
                else self.next_after(target, call)
            )
        order = self.sequence()
        for later in order[order.index(step) + 1 :]:
            if not later.done(call.state.facts):
                return later.id
        return self.finish

    def forget(self, call: FlowCall, names: frozenset[str]) -> None:
        """Drop these facts and everything that rested on them."""
        facts = {name: value for name, value in call.state.facts.items() if name not in names}
        stale = [name for name in names if name in call.state.facts]
        while stale:
            name = stale.pop()
            for step in self.steps:
                if name in step.depends_on:
                    for produced in step.produces:
                        if facts.pop(produced, None) is not None:
                            stale.append(produced)
        call.commit(facts=facts)

    def record(self, call: FlowCall, updates: Mapping[str, Any]) -> None:
        """Commit new facts in one candidate, forgetting whatever rested on a fact whose value changed."""
        facts = dict(call.state.facts)
        changed = [name for name, value in updates.items() if name in facts and facts[name] != value]
        facts.update(updates)
        while changed:
            name = changed.pop()
            for step in self.steps:
                if name in step.depends_on:
                    for produced in step.produces - set(updates):
                        if facts.pop(produced, None) is not None:
                            changed.append(produced)
        call.commit(facts=facts)
