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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, create_model
from pydantic.fields import FieldInfo
from pydantic_ai import Agent, ModelRetry, RunContext, Tool, ToolOutput
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage, UsageLimits

from core.classifiers import Classifier, output_model
from core.completion import FieldsPresent, ReceiptMatched
from core.decisions import Accept, Clarify, Conditional, NoDecision, Reject, Revise
from core.desk import Desk
from core.graph import Exit, LocalAction, Node, Opening, lent
from core.interpreters import Interpreter
from core.operations import Operation, OutcomeUnknownError
from core.policies import one_action_per_reply, request_clock, speak_by
from core.proposals import BaseProposal, Proposal
from core.readings import CONFIRM, ReadBackInput
from core.state import CallContext, Handoff, VoiceState, present


class NoFacts(BaseModel):
    """The state of a flow that keeps none of its own: a conversation, or a flow of hand-built steps."""


class FlowState[S: BaseModel](VoiceState):
    """Where a configured flow's call stands, around the application's own state.

    `facts` is the application's model, exactly the type its flow file names
    (`state:`), validated whole on every change. Where the caller is and how
    the flow ended are core's and sit beside it, never inside it, so the
    application's model stays plain data it could use anywhere.
    """

    agent: str
    facts: S
    outcome: Literal['', 'completed', 'cancelled', 'failed', 'pending'] = ''


class FlowCall[S: BaseModel](CallContext):
    """One call through a configured flow: the engine's session around the application's state.

    No application subclasses it. What an application would otherwise
    override is declared (`identity` in the flow file) or passed in
    (`StepFlow.open`: services and a clock). Services are private, so no
    prompt template can name them.
    """

    state: FlowState[S]
    _identity_field: str = PrivateAttr(default='')
    _collectible: frozenset[str] = PrivateAttr(default=frozenset())
    _services: Any = PrivateAttr(default=None)

    @property
    def services(self) -> Any:
        """The backend clients and other services this session was opened with.

        ponytail: typed `Any`, since a second type parameter only pays once an
        application reads its services through a type checker; add it then.
        """
        return self._services

    def identity(self) -> str:
        """The value of the state field the flow file names as the caller's identity. '' while unknown."""
        value = getattr(self.state.facts, self._identity_field, None) if self._identity_field else None
        return '' if value is None else str(value)

    def update(self, **fields: Any) -> None:
        """Replace the application state with one validated candidate carrying these values, or change nothing.

        The trusted path: an operation's result, or the application's own
        code. The model's path is `collect`, which may write fewer fields.
        """
        facts = self.state.facts
        if unknown := set(fields) - set(type(facts).model_fields):
            raise ValueError(f'no such field on {type(facts).__name__}: {", ".join(sorted(unknown))}')
        candidate = type(facts).model_validate(facts.model_dump() | deepcopy(fields), extra='forbid')
        self.commit(facts=candidate)

    def collect(self, **fields: Any) -> None:
        """What the caller said, as a model heard it: only fields a collect step asks for.

        Anything else (verification, approval, a receipt) is refused before it
        is looked at, whatever shape it has: a well-formed "verified" value
        from a model is still the model's word.
        """
        if refused := set(fields) - self._collectible:
            raise ValueError(f'not collected from the caller: {", ".join(sorted(refused))}')
        self.update(**fields)


@dataclass(frozen=True)
class Prompted:
    """A question this code worded: recorded as the pending question, then said by the turn engine."""

    about: str
    spoken: str
    value: str = ''

    def put(self, call: CallContext) -> str:
        call.ask(BaseProposal(about=self.about, spoken=self.spoken, value=self.value, owner=call.identity()))
        return self.spoken


class ConfirmReading(BaseModel):
    """The reply to a readback, as one decision."""

    model_config = ConfigDict(extra='forbid', frozen=True)
    decision: Accept | Reject | Revise | Conditional | Clarify | NoDecision = Field(discriminator='kind')


CONFIRMATION = output_model('ConfirmationReading', {'confirmation': CONFIRM})
"""What the model is asked for: core's `CONFIRM` classes, the same request a business's `confirmation` reading sends.

One shape and one set of descriptions for every read-back in every flow, so
the `harness-reading/confirm` dataset measures this step as well as the
salon's (`evals.readings`), and tuning one tunes both.
"""

DECISIONS: dict[str, type[Accept | Reject | Revise | Conditional | NoDecision]] = {
    'accept': Accept,
    'reject': Reject,
    'revise': Revise,
    'conditional': Conditional,
    'unclear': NoDecision,
}
"""Each class as the decision the step acts on. `unclear` lets the step's model answer, as a question needs."""


class ConfirmInterpreter(Interpreter[FlowCall[Any], ReadBackInput, ConfirmReading]):
    """Reads a reply to the last readback with the `CONFIRM` classes. A readback spoken over is never accepted."""

    def __init__(self, name: str, classifier: Classifier[ReadBackInput, Any]) -> None:
        super().__init__(name, ConfirmReading(decision=NoDecision()))
        self.classifier = classifier

    def _input(self, call: FlowCall[Any]) -> ReadBackInput:
        last = call.last_proposal()
        return ReadBackInput(
            question=call.said_last_turn(), answer=call.heard, proposal=last[0].spoken if last else ''
        )

    def _still_valid(self, call: FlowCall[Any], value: ReadBackInput) -> bool:
        last = call.last_proposal()
        return value.answer == call.heard and value.proposal == (last[0].spoken if last else '')

    async def _decide(
        self, value: ReadBackInput, *, usage: RunUsage | None, usage_limits: UsageLimits | None
    ) -> ConfirmReading:
        result = await self.classifier.classify(value, usage=usage, usage_limits=usage_limits)
        return ConfirmReading(decision=DECISIONS[result.confirmation]())

    def _settle(self, call: FlowCall[Any], value: ReadBackInput, result: ConfirmReading) -> ConfirmReading:
        last = call.last_proposal()
        if not (last and last[1]) and isinstance(result.decision, Accept):
            return ConfirmReading(decision=NoDecision())
        return result


def confirm_interpreter(
    model: Model | str,
    settings: ModelSettings | None = None,
    context: str = 'The person was read back a proposal and is waiting to confirm it.',
) -> ConfirmInterpreter:
    """The default reader of confirmations: one structured request, no tools, no history."""
    classifier = Classifier(model, ReadBackInput, CONFIRMATION, context=context, model_settings=settings)
    return ConfirmInterpreter('confirm', classifier)


class ConfirmedOperation[ArgsT: BaseModel, ReceiptT: BaseModel](
    Operation[FlowCall[Any], ArgsT, ReceiptT, ReceiptT]
):
    """An operation authorised by the flow's confirmation: the default readback check, and the interpreter's yes.

    `_record` returns the receipt, which the `ActionStep` files.
    """

    def __init__(self, interpreter: Interpreter[FlowCall[Any], Any, ConfirmReading]) -> None:
        self.interpreter = interpreter

    async def _agreed(
        self,
        call: FlowCall[Any],
        question: Proposal,
        *,
        usage: RunUsage | None,
        usage_limits: UsageLimits | None,
    ) -> bool:
        reading = await self.interpreter.interpret(call, usage=usage, usage_limits=usage_limits)
        return isinstance(reading.decision, Accept)

    async def _record(self, call: FlowCall[Any], args: ArgsT, receipt: ReceiptT) -> ReceiptT:
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

    def done(self, call: FlowCall[Any]) -> bool:
        """Whether the call's state and evidence show this step's purpose is met. Never true without evidence."""
        return False

    @abstractmethod
    def node(self, flow: StepFlow) -> Node[Any]:
        """This step as a node the turn engine runs."""


def _agent(flow: StepFlow, step: Step, instructions: Any, outputs: list) -> Agent[Any, object]:
    return Agent[Any, object](
        flow.model,
        name=step.id,
        deps_type=flow.call_type,
        output_type=outputs,  # type: ignore[arg-type]
        instructions=instructions,
        model_settings=flow.settings,
    )


def _node(flow: StepFlow, step: Step, agent: Agent[Any, object], **node: Any) -> Node[Any]:
    return Node(agent, exits=tuple(Exit(to) for to in flow.destinations(step)), **node)


def _entered(call: FlowCall[Any]) -> bool:
    return True


def _optional(info: FieldInfo) -> Any:
    """A state field's type, constraints kept, or None: what a collect tool's schema offers for it."""
    kind = Annotated[info.annotation, *info.metadata] if info.metadata else info.annotation  # ty: ignore[invalid-type-form]
    return kind | None  # ty: ignore[unsupported-operator]


def _args(operation: Operation, call: FlowCall[Any]) -> BaseModel:
    """An operation's arguments, taken field by field from the application state, validated whole."""
    facts = call.state.facts
    return operation.args_type.model_validate(
        {name: getattr(facts, name) for name in operation.args_type.model_fields}
    )


def receipt_for(operation: Operation, call: FlowCall[Any]) -> Mapping[str, Any] | None:
    """The latest receipt this operation filed for the arguments the state gives now, under the current caller.

    Read from the call's evidence, which nothing erases: a changed argument or
    identity makes an older receipt stop counting here, while it stays on
    record for what it did.
    """
    try:
        args = _args(operation, call).model_dump(mode='json')
    except ValidationError:
        return None
    for filed in reversed(call.evidence):
        if filed.operation == operation.name and filed.args == args and filed.identity == call.identity():
            return filed.receipt
    return None


@dataclass(frozen=True, kw_only=True)
class CollectStep(Step):
    """Ask for fields of the application state; done once every one of them has a validated value.

    `fields` names fields of the flow's state model: their descriptions and
    constraints are what the model is told to listen for and sent as the
    tool's schema, and the state model (with every other field it holds)
    decides what is acceptable. Only fields some collect step names can be
    written by the model at all (`FlowCall.collect`). `after` names fields its
    values rest on, so changing one of those resets this step's answer.
    """

    fields: tuple[str, ...]
    question: str
    after: frozenset[str] = frozenset()
    lookahead: bool = True
    """Whether a caller may answer later collect steps early, in the same breath."""
    prompt: Any = None
    """This step's own instructions (text or a compiled template), from its prompt file. None writes them from the fields."""

    @property
    def produces(self) -> frozenset[str]:
        return frozenset(self.fields)

    @property
    def depends_on(self) -> frozenset[str]:
        return self.after

    def done(self, call: FlowCall[Any]) -> bool:
        return FieldsPresent(*self.fields).is_met(dict(call.state.facts))

    def ask(self) -> Prompted:
        return Prompted(about=f'collect:{self.id}', spoken=self.question)

    def node(self, flow: StepFlow) -> Node[Any]:
        listening = [self, *(flow.collects_after(self) if self.lookahead else [])]
        state = flow.state_type.model_fields
        values = create_model(  # ty: ignore[no-matching-overload]
            f'{self.id.title()}Values',
            __config__=ConfigDict(extra='forbid'),
            **{
                name: (
                    _optional(state[name]),
                    Field(None, description=state[name].description),
                )
                for step in listening
                for name in step.fields
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
        wanted = '; '.join(f'{name}: {state[name].description or name}' for name in self.fields)
        instructions = self.prompt or (
            f'You are on the phone, collecting: {wanted}. When the caller gives any value, call `collect` '
            'with only what they said. If they ask something else, answer in one short sentence and ask again.'
        )
        agent = _agent(flow, self, instructions, [str, ToolOutput(collect, name='collect')])
        return _node(flow, self, agent, openings=(Opening('ask', 'handoff', _entered, self.arrive(flow)),))

    def arrive(self, flow: StepFlow):
        async def arrive(call: FlowCall[Any]) -> Handoff | Prompted:
            """Ask the question on arrival, or move straight on if it was answered early."""
            if self.done(call):
                return Handoff(to=flow.next_after(self, call))
            return self.ask()

        return arrive

    async def record(
        self, flow: StepFlow, call: FlowCall[Any], proposed: dict[str, Any], listening: Sequence[CollectStep]
    ) -> Handoff | Prompted:
        """Keep the caller's values only as one valid state; this step's own must be valid, or nothing is kept.

        Values for later steps that do not fit are left out rather than
        failing the answer to this one, which is how a caller who answered
        ahead of time with one unusable value still has the rest kept.
        """
        heard = {
            name: value for name, value in proposed.items() if any(name in step.fields for step in listening)
        }
        try:
            flow.record(call, heard)
        except ValidationError as error:
            bad = {str(e['loc'][0]) for e in error.errors() if e['loc']}
            kept = {name: value for name, value in heard.items() if name not in bad}
            if bad & set(self.fields) or not bad or not kept:
                problems = '; '.join(e['msg'] for e in error.errors())
                raise ModelRetry(f'Nothing was recorded: {problems}. Ask the caller again.') from None
            try:
                flow.record(call, kept)
            except ValidationError as again:
                problems = '; '.join(e['msg'] for e in again.errors())
                raise ModelRetry(f'Nothing was recorded: {problems}. Ask the caller again.') from None
        if self.done(call):
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

    def done(self, call: FlowCall[Any]) -> bool:
        """Done once the operation it confirms filed a receipt for these arguments, for this caller."""
        return bool(self.then) and receipt_for(self.operation, call) is not None

    def readback(self, call: FlowCall[Any]) -> Prompted:
        proposal = self.operation.proposal(_args(self.operation, call))
        return Prompted(about=proposal.about, spoken=proposal.spoken, value=proposal.value)

    def node(self, flow: StepFlow) -> Node[Any]:
        agent = _agent(
            flow,
            self,
            'The caller was just asked to confirm a request. Answer their question in one short sentence. '
            'Never say the request is confirmed or submitted.',
            [str],
        )

        async def read_back(call: FlowCall[Any]) -> Prompted:
            """Put the exact proposal to the caller."""
            return self.readback(call)

        async def advance(call: FlowCall[Any], usage: RunUsage, limits: UsageLimits) -> LocalAction | None:
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


async def _consumed(call: FlowCall[Any], to: str) -> Handoff:
    call.answered()
    return Handoff(to=to)


async def _revised(flow: StepFlow, call: FlowCall[Any], to: str) -> Handoff:
    """Go back to the step being revised, forgetting its answer so it is asked again."""
    call.answered()
    flow.forget(call, flow.by_id[to].produces)
    return Handoff(to=to)


@dataclass(frozen=True, kw_only=True)
class ActionStep(Step):
    """Run one operation with arguments taken from the application state; done only on its receipt.

    `accepts` says whether the receipt is what this step needed (eligible,
    submitted); one that is not goes to `on_error`, as does a refusal. An
    outcome nobody knows goes to `on_unknown`, never to either. Whether it is
    done is read from the call's evidence (`receipt_for`), so a receipt filed
    before a cancellation still counts, and the operation never runs twice for
    the same arguments and caller.
    """

    operation: Operation
    after: frozenset[str] = frozenset()
    accepts: Callable[[Mapping[str, Any]], bool] = field(default=lambda _: True)
    on_error: str = ''
    on_unknown: str = ''

    @property
    def depends_on(self) -> frozenset[str]:
        return self.after | frozenset(self.operation.args_type.model_fields)

    @property
    def elsewhere(self) -> tuple[str, ...]:
        return tuple(to for to in (self.on_error, self.on_unknown) if to)

    def done(self, call: FlowCall[Any]) -> bool:
        receipt = receipt_for(self.operation, call)
        return ReceiptMatched(self.id, self.accepts).is_met({self.id: receipt} if receipt is not None else {})

    def node(self, flow: StepFlow) -> Node[Any]:
        async def act(call: FlowCall[Any]) -> Handoff:
            """Run the operation now; where the caller goes depends only on what came back."""
            if self.done(call):
                return Handoff(to=flow.next_after(self, call))
            try:
                await self.operation.run(call, _args(self.operation, call))
            except OutcomeUnknownError:
                return Handoff(to=self.on_unknown or self.on_error)
            except ModelRetry:
                return Handoff(to=self.on_error)
            if not self.done(call):
                return Handoff(to=self.on_error)
            return Handoff(to=flow.next_after(self, call))

        agent = _agent(flow, self, 'Say one short sentence: you are working on it.', [str])
        return _node(flow, self, agent, openings=(Opening(self.operation.name, 'handoff', _entered, act),))


@dataclass(frozen=True, kw_only=True)
class ConversationStep(Step):
    """Answer from instructions and knowledge; stays active every turn until the caller says goodbye."""

    instructions: Any
    """Text, or a compiled template from a prompt file (which already carries any knowledge)."""
    knowledge: str = ''

    def node(self, flow: StepFlow) -> Node[Any]:
        async def goodbye(ctx: RunContext[FlowCall[Any]], words: str) -> str:
            """End the call, only once the caller's own words say they are finished. Say one short goodbye.

            Args:
                words: The goodbye. It is the last thing they hear.
            """
            ctx.deps.end_requested('caller')
            await ctx.deps.speak_now(words, origin='goodbye')
            await ctx.deps.end_call()
            return words

        text = self.instructions
        if self.knowledge and isinstance(text, str):
            text += f'\n\nKnowledge:\n{self.knowledge}'
        agent = _agent(flow, self, text, [str, ToolOutput(goodbye, name='goodbye')])
        return _node(flow, self, agent)


@dataclass(frozen=True, kw_only=True)
class End(Step):
    """Say one closing line and record how the flow ended. `say` may use the facts: `{field}`."""

    outcome: Literal['completed', 'cancelled', 'failed', 'pending']
    say: str

    def node(self, flow: StepFlow) -> Node[Any]:
        async def close(call: FlowCall[Any]) -> str:
            """Record the outcome and say it."""
            call.commit(outcome=self.outcome)
            words = self.say.format_map(_Facts(call.state.facts.model_dump()))
            await call.speak_now(words, origin='confirmation')
            return words

        agent = _agent(flow, self, 'The request is finished. Answer briefly; do not start a new one.', [str])
        return _node(flow, self, agent, openings=(Opening('close', 'handoff', _entered, close),))


@dataclass(frozen=True, kw_only=True)
class AgentStep(Step):
    """A hand-built step: its own prompt, and a module that builds its agent, tools and openings.

    What a flow file says about the step (its prompt, its exits, what it
    needs first, whether it can write) arrives here; the module's `node`
    function turns it into a `Node`, with whatever tools the business wrote.
    This is how the salon's four steps sit in a flow file.
    """

    instructions: Any
    """The step's compiled prompt."""
    build: Callable[[AgentStep], Node[Any]]
    """The step module's `node` function."""
    exits: tuple[Exit[Any], ...] = ()
    requires: tuple[str, ...] = ()
    satisfied_by: str = ''
    guarded: bool = False

    @property
    def elsewhere(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                [*(exit.to for exit in self.exits), *([self.satisfied_by] if self.satisfied_by else [])]
            )
        )

    def node(self, flow: StepFlow) -> Node[Any]:
        return self.build(self)


def _moved_this_run(messages: Sequence[ModelMessage], borrowed: Mapping[str, Any]) -> bool:
    """Whether a borrowed tool has returned since the caller's words: the move it stands for is made."""
    for message in reversed(messages):
        if not isinstance(message, ModelRequest):
            continue
        if any(isinstance(p, ToolReturnPart) and p.tool_name in borrowed for p in message.parts):
            return True
        if any(isinstance(p, UserPromptPart) for p in message.parts):
            return False
    return False


class AgentBuilder[C: CallContext]:
    """Builds a hand-built step's agent the same way for every use case.

    A use case gives it its call type, model and settings, the names of its
    actions (at most one runs per model reply), its `Desk`, its `advance` (what
    decides a turn before any model runs) and the request by which a run must
    answer with what it holds. Each step then passes only what its own code
    can say (`build`).
    """

    def __init__(
        self,
        *,
        call_type: type[C],
        model: Model | str,
        settings: ModelSettings | None,
        actions: frozenset[str],
        desk: Desk[C],
        advance: Callable[[Any, RunUsage, UsageLimits], Awaitable[LocalAction | None]] | None,
        speak_by: int,
    ) -> None:
        self.call_type = call_type
        self.model = model
        self.settings = settings
        self.actions = actions
        self.desk = desk
        self.advance = advance
        self.speak_by = speak_by

    def build(
        self,
        step: AgentStep,
        tools: Sequence[Any] = (),
        finishers: Sequence[Any] = (),
        asks: Sequence[Any] = (),
        openings: tuple[Opening[Any], ...] = (),
        structured_speech: bool = False,
        offered_when: Mapping[str, Callable[[Any], bool]] | None = None,
        stand_ins: Mapping[Callable[..., Any], Callable[..., Any]] | None = None,
        model: Model | str | None = None,
        shares_detour: bool = False,
    ) -> Node[Any]:
        """One step: its agent, its legal exits, and what has to hold before it runs.

        The step's name, prompt, exits and requirements come from its entry in
        the flow file; the arguments here are what only its code can say.
        Everything that differs between steps is an argument, so this names no
        step and no tool: `structured_speech` for a step that speaks only
        through its outputs, `offered_when` for an output offered only while a
        fact holds, and `stand_ins` for an output whose name the model knows but
        whose implementation is this step's own.

        Every output tool is named explicitly. Pydantic AI calls a lone output
        function `final_result` and renames a bare `hand_over` to
        `final_result_hand_over` as soon as there is more than one output, which
        the per-request narrowing then fails to find by name. Everything that
        writes is `sequential`, so two of them can never overlap; the conflict
        policy refusing the second is the brace to that belt.

        A question ends the run the same way finishing the step does, and for the
        same reason: the words are already decided, so a further model request
        would only be a chance to word them differently. Questions are not
        actions, because asking changes nothing and writes nothing.

        `str` in the output list is what keeps speech streaming to TTS. A step
        that can write or verify speaks only through its output functions, so it
        does not get it and provider text never reaches TTS from it.

        The whole output set is always declared; what varies per request is which
        of it is *offered*, decided by the two prepare hooks below.
        """
        exits, actions = step.exits, self.actions
        borrowed = lent(exits)
        named = [
            ToolOutput((stand_ins or {}).get(fn, fn), name=fn.__name__, sequential=fn.__name__ in actions)
            for fn in finishers
        ]
        questions = [ToolOutput(fn, name=fn.__name__) for fn in asks]
        handoff = (
            [
                ToolOutput(
                    self.desk.hand_over_for(exits, tuple(fn.__name__ for fn in asks)),
                    name='hand_over',
                    sequential=True,
                )
            ]
            if exits
            else []
        )
        offered = offered_when or {}
        declared = [*([str] if not structured_speech else []), *handoff, *named, *questions]

        def outputs(state: VoiceState) -> list:
            """What this step may return: the superset `narrow` picks from at each request."""
            return declared

        async def only_reachable(ctx: RunContext[Any], defs: list[ToolDefinition]) -> list[ToolDefinition]:
            """Take a borrowed tool back out while the edge it stands for is shut.

            A caller the step cannot yet serve is not offered the tool the edge
            lends, so the model cannot call it and be refused, which costs the
            round trip the lending was there to save. Re-run before every model
            request, so a tool that ran a moment ago and changed the state is
            reflected in the very next schema.
            """
            return [d for d in defs if d.name not in borrowed or borrowed[d.name].guard(ctx.deps.state)]

        async def narrow(ctx: RunContext[Any], defs: list[ToolDefinition]) -> list[ToolDefinition]:
            """Offer only the destinations that are open at this request.

            The output-tool half of `only_reachable`, and a separate hook because
            Pydantic AI prepares ordinary and output tools through different ones.
            An exit whose guard has stopped holding leaves the enum the model is
            sent, so it cannot be chosen rather than being refused after it was;
            with none left, `hand_over` goes entirely. An output with an
            `offered_when` fact is offered only while it holds.

            Once a borrowed tool has returned in this run, `hand_over` goes too:
            the tool's success already moves the caller (`moved_by`), so a
            hand-over after it is a second request for a move already made.
            The user's call of 24 September read the diary from the front desk,
            then handed over to booking as well.
            """
            legal = list(dict.fromkeys(e.to for e in exits if e.guard(ctx.deps.state)))
            if _moved_this_run(ctx.messages, borrowed):
                legal = []
            kept = []
            for tool_def in defs:
                if (when := offered.get(tool_def.name)) is not None and not when(ctx.deps):
                    continue
                if tool_def.name != 'hand_over':
                    kept.append(tool_def)
                    continue
                if not legal:
                    continue
                schema = deepcopy(tool_def.parameters_json_schema)
                if (to := schema.get('properties', {}).get('to')) is not None:
                    to.pop('const', None)
                    to['enum'] = legal
                kept.append(replace(tool_def, parameters_json_schema=schema))
            return kept

        return Node(
            Agent[Any, object](
                model if model is not None else self.model,
                name=step.id,
                deps_type=self.call_type,
                output_type=declared,
                instructions=step.instructions,
                tools=[
                    *(
                        fn if isinstance(fn, Tool) else Tool(fn, sequential=fn.__name__ in actions)
                        for fn in tools
                    ),
                    *(Tool(fn, sequential=fn.__name__ in actions) for exit in exits for fn in exit.via),
                ],
                capabilities=[
                    one_action_per_reply(actions),
                    Hooks(prepare_tools=only_reachable, prepare_output_tools=narrow),
                    *([speak_by(self.speak_by)] if not structured_speech else []),
                    request_clock(),
                ],
                model_settings=self.settings,
            ),
            exits=exits,
            requires=step.requires,
            satisfied_by=step.satisfied_by,
            outputs=outputs,
            guarded=step.guarded,
            shares_detour=shares_detour,
            model_speech=not structured_speech,
            advance=self.advance,
            openings=openings,
        )


class _Facts(dict):
    """Facts for `str.format_map`, with nested receipts reachable as `{step[field]}` and a blank for anything missing."""

    def __missing__(self, key: str) -> str:
        return ''


class StepFlow:
    """An ordered list of steps, checked and compiled into the turn engine's nodes table.

    Shared config, per-call state: the flow holds steps, a model and an
    interpreter; each call holds its own application state (`open`).
    Construction refuses a flow whose destinations do not exist, whose ids
    repeat, or that has business steps and no `End` with outcome `completed`,
    so a broken flow fails where it is defined rather than on a call.
    """

    def __init__(
        self,
        steps: Sequence[Step],
        *,
        model: Model | str,
        interpreter: Interpreter[FlowCall[Any], Any, ConfirmReading] | None = None,
        settings: ModelSettings | None = None,
        state_type: type[BaseModel] = NoFacts,
        identity: str = '',
        call_type: type[CallContext] | None = None,
    ) -> None:
        self.state_type = state_type
        """The application's state model: every call's `state.facts`."""
        self.identity = identity
        """The state field that says who the caller is. Empty: nobody in particular."""
        self.call_type = call_type or FlowCall[state_type]  # ty: ignore[invalid-type-form]
        """The call every step's agent is given, and what prompt templates are checked against."""
        self.steps = tuple(steps)
        self.by_id = {step.id: step for step in self.steps}
        if len(self.by_id) != len(self.steps):
            raise ValueError('step ids must be unique')
        finished = [s for s in self.steps if isinstance(s, End) and s.outcome == 'completed']
        if any(isinstance(s, CollectStep | ConfirmStep | ActionStep) for s in self.steps) and not finished:
            raise ValueError('a flow with business steps needs an End with outcome completed')
        self.finish = finished[0].id if finished else ''
        for step in self.steps:
            for to in (step.then, *step.elsewhere):
                if to and to not in self.by_id:
                    raise ValueError(f'{step.id} sends the caller to {to}, which is not a step')
        self.collectible = frozenset(
            name for s in self.steps if isinstance(s, CollectStep) for name in s.fields
        )
        """The only state fields a model may write: the ones some collect step asks the caller for."""
        self.model = model
        self.settings = settings
        self.interpreter = interpreter if interpreter is not None else confirm_interpreter(model, settings)
        self.nodes: dict[str, Node[Any]] = {step.id: step.node(self) for step in self.steps}

    @property
    def start(self) -> str:
        return self.steps[0].id

    def open(
        self, state: BaseModel, *, services: Any = None, clock: Callable[[], datetime] | None = None
    ) -> FlowCall[Any]:
        """A new session on this flow, standing on its first step: the application's state, its services, its clock.

        The state is copied in and validated as the flow's model, so the
        object the caller passed can change afterwards without reaching the
        call. Services are kept as given: whether a client is shared between
        calls or made per call is the application's decision.
        """
        if not (isinstance(self.call_type, type) and issubclass(self.call_type, FlowCall)):
            raise TypeError(f'{self.call_type.__name__} is its own call class; open its calls with it')
        facts = self.state_type.model_validate(deepcopy(state.model_dump()), extra='forbid')
        call = self.call_type(state=FlowState[self.state_type](agent=self.start, facts=facts))  # ty: ignore[invalid-type-form]
        call._identity_field, call._collectible = self.identity, self.collectible
        call._services, call._clock = services, clock
        return call

    def sequence(self) -> list[Step]:
        """The steps walked in order, without the endings and without hand-built steps, which move by their exits."""
        return [s for s in self.steps if not isinstance(s, End | AgentStep)]

    def collects_after(self, step: Step) -> list[CollectStep]:
        order = self.sequence()
        return [s for s in order[order.index(step) + 1 :] if isinstance(s, CollectStep)]

    def destinations(self, step: Step) -> tuple[str, ...]:
        """Every step this one can hand the caller to: onward (skipping done steps), or elsewhere."""
        if isinstance(step, AgentStep):
            return step.elsewhere
        if isinstance(step, End | ConversationStep):
            return ()
        order = self.sequence()
        onward = [s.id for s in order[order.index(step) + 1 :]] if not step.then else [step.then]
        return tuple(dict.fromkeys([*onward, *step.elsewhere, *([self.finish] if self.finish else [])]))

    def next_after(self, step: Step, call: FlowCall[Any]) -> str:
        """The first step after this one that is not done yet, or the finishing `End`."""
        if step.then:
            target = self.by_id[step.then]
            return (
                target.id
                if isinstance(target, End) or not target.done(call)
                else self.next_after(target, call)
            )
        order = self.sequence()
        for later in order[order.index(step) + 1 :]:
            if not later.done(call):
                return later.id
        return self.finish

    def _default(self, name: str) -> Any:
        return self.state_type.model_fields[name].get_default(call_default_factory=True)

    def _resting_on(self, changed: list[str], keep: set[str]) -> dict[str, Any]:
        """Every collected field built on these, reset to its default: a changed fact takes its dependents with it."""
        resets: dict[str, Any] = {}
        while changed:
            name = changed.pop()
            for step in self.steps:
                if name in step.depends_on:
                    for produced in step.produces - keep - set(resets):
                        resets[produced] = self._default(produced)
                        changed.append(produced)
        return resets

    def forget(self, call: FlowCall[Any], names: frozenset[str]) -> None:
        """Reset these fields and everything that rested on them, as one state candidate."""
        resets = {name: self._default(name) for name in names}
        resets |= self._resting_on(list(names), set(names))
        call.collect(**resets)

    def record(self, call: FlowCall[Any], updates: Mapping[str, Any]) -> None:
        """Commit the caller's values as one state candidate, resetting whatever rested on a value that changed.

        Raises `ValidationError` and changes nothing when the candidate is not
        a valid state.
        """
        facts = call.state.facts
        candidate = type(facts).model_validate(facts.model_dump() | deepcopy(dict(updates)), extra='forbid')
        changed = [
            name
            for name in updates
            if present(getattr(facts, name)) and getattr(facts, name) != getattr(candidate, name)
        ]
        call.collect(**dict(updates), **self._resting_on(changed, set(updates)))
