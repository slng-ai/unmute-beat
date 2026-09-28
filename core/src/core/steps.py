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

import asyncio
import inspect
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, create_model
from pydantic.fields import FieldInfo
from pydantic_ai import Agent, ModelRetry, RunContext, Tool, ToolOutput
from pydantic_ai.agent.spec import AgentSpec
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage, UsageLimits

from core.completion import FieldsPresent, ReceiptMatched
from core.graph import Exit, LocalAction, Node, Opening, Restated, holds
from core.locale import Locale
from core.operations import Operation, OutcomeUnknownError, _tracer
from core.policies import claim_or_refuse, one_action_per_reply, request_clock, speak_by
from core.proposals import BaseProposal, Proposal
from core.state import CallContext, Handoff, Intent, VoiceState, present


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
    _offered: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    """For each choice field, the ids offered to this caller and how each was said. Only these can be picked."""
    _locale: Locale | None = PrivateAttr(default=None)
    _version: int = PrivateAttr(default=0)
    """Bumped by every accepted change to the application state: work begun on an older version is stale."""
    _derived: dict[str, frozenset[str]] = PrivateAttr(default_factory=dict)
    """State fields a receipt sets (`assign`), each with the state fields its operation's arguments came from."""
    _choices: frozenset[str] = PrivateAttr(default=frozenset())
    """State fields picked from offered ids (`choices`): valid only while the request they were offered for stands."""
    _recorded: frozenset[str] = PrivateAttr(default=frozenset())
    """The fields the latest `collect` recorded: what a step's `reads_back` rule is keyed on."""
    _goodbye_checked: int = PrivateAttr(default=0)
    """The turn on which a goodbye beside a waiting read-back was sent back once, to carry out a yes first."""
    _reconcile_with: dict[str, tuple[Any, type[BaseModel]]] = PrivateAttr(default_factory=dict)
    """Each declared operation and its argument model, by name: what a restored intent is reconciled with."""
    _asked_owed: set[str] = PrivateAttr(default_factory=set)
    """The identities whose owed intents the services were already asked for (`take_owed`)."""

    @property
    def locale(self) -> Locale:
        """The flow file's `locale:`, else the class default."""
        return self._locale or self.default_locale

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
        if self.state.facts is not facts:
            self._version += 1
            self.take_owed()

    def take_owed(self) -> None:
        """Once the caller is known, take back what earlier calls still owe them, to settle before anything new.

        The services may say (`owed(identity) -> intents`): intents of calls
        that are over, made for this caller, with no answer that settles
        them. Restored here, they are unknown on this call, so the next
        turn reconciles them by their own operation before any model runs,
        and nothing of the same operation for this caller is sent until then
        (`Operation._may_dispatch`). Asked once per identity. A caller known
        at open is asked at open; one who says who they are later is asked
        then, which on a phone line is the usual case.
        """
        identity = self.identity()
        owed = getattr(self._services, 'owed', None)
        if not identity or identity in self._asked_owed or owed is None:
            return
        self._asked_owed.add(identity)
        intents = [i for i in owed(identity) if i.id not in self._intents]
        if intents:
            logger.info('{} intent(s) owed to this caller by earlier calls: settled first', len(intents))
            self.restore(intents, self._reconcile_with)

    def wrote_this_turn(self) -> bool:
        """Whether a business action filed a validated receipt in this caller turn. Evidence, never a flag."""
        return any(filed.turn == self.turn for filed in self.evidence)

    def claims_completion(self, clause: str) -> bool:
        """Whether this clause tells the caller a business action is done (`Locale.claims`).

        Asked of model speech on a step that can write, before it is said.
        Always yes for such a clause, receipt or no receipt: what was done is
        said by the receipt, in words code rendered from it.
        """
        return any(re.search(pattern, clause, re.IGNORECASE) for pattern in self.locale.claims)

    def unbacked_claim(self) -> str:
        """Model speech this turn claiming a completed action when this turn completed none. '' when clean."""
        if self.wrote_this_turn():
            return ''
        for segment in self.spoken:
            if segment.kind == 'model':
                for pattern in self.locale.claims:
                    if hit := re.search(pattern, segment.text, re.IGNORECASE):
                        return hit.group(0)
        return ''

    def settlement(self) -> Callable[[], Awaitable[object]] | None:
        """What settles earlier intents before any model runs this turn, or None when nothing is outstanding.

        Each intent whose outcome is unknown is asked about by its own
        operation's `reconcile`, by its intent, with the same arguments and
        caller. Found and valid: the receipt is filed and said, exactly as if
        the first answer had arrived. Not found, or an answer that does not
        validate or is about something else: it stays unknown, and nothing is
        sent again (`Operation._may_dispatch`). An authoritative no (a
        backend that fenced the intent as void) settles it, silently: nothing
        was done, and no model is waiting to be told. A success whose recording
        failed is recorded again from its receipt, with nothing sent. No
        model is asked what happened.

        ponytail: an intent that stays unknown is asked about once per caller
        turn, for the rest of the call. A backoff belongs here if a backend
        ever minds.
        """
        pending = [
            (operation, args, self._intents[intent_id])
            for intent_id, (operation, args) in self._unknown.items()
            if operation.reconcile
        ]
        repairs = list(self._unpresented.values())
        if not pending and not repairs:
            return None

        async def settle() -> object:
            found = None
            for operation, args, intent in pending:
                try:
                    found = await operation.reconcile_intent(self, args, intent) or found
                except OutcomeUnknownError as error:
                    logger.info('still unknown after reconciliation: {}', error)
                except ModelRetry:
                    logger.info('settled by reconciliation as not done: {}', intent.id)
            for operation, args, receipt in repairs:
                found = await operation.present_again(self, args, receipt) or found
            return found

        return settle

    def collect(self, **fields: Any) -> None:
        """What the caller said, as a model heard it: only fields a collect step asks for.

        Anything else (verification, approval, a receipt) is refused before it
        is looked at, whatever shape it has: a well-formed "verified" value
        from a model is still the model's word.

        A change takes what rested on the old value with it, in the same
        candidate: a fact a receipt set from these arguments (a corrected phone
        number is no longer the verified one), and every offer together with
        every pick made from one, unless the change is only a pick among the
        offered ids or the caller's identity. "Actually, make it Saturday"
        leaves no Friday slot behind for an old yes to book.

        ponytail: offers and picks are taken to rest on every other field,
        which is right for a request (day, time, service) and too eager for a
        field no offer reads. A per-choice `after:` list is the upgrade if a
        flow needs it.
        """
        if refused := set(fields) - self._collectible:
            raise ValueError(f'not collected from the caller: {", ".join(sorted(refused))}')
        facts = self.state.facts
        default = lambda name: type(facts).model_fields[name].get_default(call_default_factory=True)  # noqa: E731
        changed = {name for name, value in fields.items() if getattr(facts, name) != value}
        request_changed = bool(changed - self._choices - {self._identity_field})
        picks = {name: default(name) for name in self._choices - set(fields)} if request_changed else {}
        changed |= {name for name, value in picks.items() if getattr(facts, name) != value}
        resets = {
            name: default(name)
            for name, sources in self._derived.items()
            if sources & changed and name not in fields
        }
        self.update(**fields, **picks, **resets)
        if request_changed:
            self._offered = {}


@dataclass(frozen=True)
class Prompted:
    """A question this code worded: recorded as the pending question, then said by the turn engine."""

    about: str
    spoken: str
    value: str = ''

    def put(self, call: CallContext) -> str:
        """Record the question and return its words; or, for an intent read back `READBACKS` times unheard, the fallback.

        An operation's read-back names its intent, so every copy of it counts
        against that intent (`CallContext.note_unheard_readbacks`), across the
        caller's joined pieces and reissued questions alike. Past the limit it
        is not asked again on this call: the caller hears `Locale.gave_up`,
        which is true, because nothing was dispatched. Copies lost on a line
        that proves nothing count against `READBACKS`; copies the caller
        talked over on a line that works count against `CUT_READBACKS`.
        """
        intent = call.open_intent(self.about, self.value)
        if (
            intent is not None
            and intent.status == 'prepared'
            and (intent.readbacks >= READBACKS or intent.cut_readbacks >= CUT_READBACKS)
        ):
            logger.info(
                '{} read back unheard {} times, cut {} times: fallback',
                self.about,
                intent.readbacks,
                intent.cut_readbacks,
            )
            return call.locale.gave_up
        call.ask(
            BaseProposal(
                about=self.about,
                spoken=self.spoken,
                value=self.value,
                owner=call.identity(),
                intent=intent.id if intent is not None else '',
            )
        )
        return self.spoken


READBACKS = 2
"""Read-backs of one intent lost on a line that has proven no delivery, after which it is not asked again."""
CUT_READBACKS = 5
"""Read-backs of one intent the caller talked over on a line that works, after which it is not asked again.

ponytail: a fixed count, picked so an interruptive caller gets several
tries while a caller who never lets it finish still ends; per-step limits
if one application needs another number.
"""


class ConfirmedOperation[ArgsT: BaseModel, ReceiptT: BaseModel](
    Operation[FlowCall[Any], ArgsT, ReceiptT, ReceiptT]
):
    """An operation authorised by a heard read-back and the active step's yes to it.

    The yes is the step's own agent's: it reads the caller's reply in the
    whole conversation and, for a yes, calls the step's accept output, which
    binds it to the pending question (`CallContext.approve`). Nothing here
    asks a second model what the reply meant. `_record` returns the receipt,
    which the `ActionStep` files.
    """

    async def _agreed(
        self,
        call: FlowCall[Any],
        question: Proposal,
        *,
        usage: RunUsage | None,
        usage_limits: UsageLimits | None,
    ) -> bool:
        return call.approved()

    async def _record(self, call: FlowCall[Any], args: ArgsT, receipt: ReceiptT) -> ReceiptT:
        return receipt


async def _result(value: Any) -> Any:
    """A domain function's answer, whether it was written sync or async."""
    return await value if inspect.isawaitable(value) else value


class FunctionOperation(ConfirmedOperation[Any, Any]):
    """An ordinary typed domain function, declared in the manifest, run through the one `Operation.run` boundary.

    The application writes `def book(clinic: Backend, appointment: Appointment) -> Booked`
    and declares it under `operations:`. Its second parameter is the argument
    model and its return annotation the receipt model: the function's own
    signature is the schema, and YAML only connects it (`FlowSpec` builds one
    subclass per declaration, so no application writes one).

    `binds` takes each argument from a named state field, never from a model's
    words. `assign` copies receipt fields into state fields once the receipt
    validates and `succeeds` holds; `say` is the receipt's speech. A receipt
    that validates but does not say it succeeded is filed as evidence, assigns
    nothing, says nothing and completes nothing. An outcome the function
    reports unknown (`OutcomeUnknownError`) is settled by `reconcile`, with the
    same arguments and caller, before anything runs again; without a
    `reconcile` it is never retried in this call. An offer an action used is
    spent once it succeeds: after the booking, the times it was picked from
    are no longer on offer.
    """

    function: ClassVar[Callable[..., Any]]
    binds: ClassVar[Mapping[str, str]] = {}
    """Argument name to the state field it is taken from."""
    assign: ClassVar[Mapping[str, str]] = {}
    """State field to the receipt field it is set from."""
    succeeds: ClassVar[Mapping[str, Any]] = {}
    """Receipt fields and the values that mean it worked, like `{status: booked}`. Empty: a valid receipt is success."""
    matches: ClassVar[Mapping[str, str]] = {}
    """Receipt fields that must equal an argument, like `{customer_phone: phone}`: a receipt about someone else is refused."""
    readback: ClassVar[Callable[[Any], str] | None] = None
    reconcile: ClassVar[Callable[..., Any] | None] = None
    """`(services, args) -> receipt | None`: what the service did with an attempt whose outcome was unknown."""
    say: ClassVar[str] = ''

    readback_uses_services: ClassVar[bool] = False
    """The readback is `(services, args) -> str`: the words need a lookup, like a slot's spoken time."""

    def proposal(self, args: BaseModel) -> Proposal:
        """What authorises these arguments: its value is theirs whatever the words, so a check needs no services."""
        if self.readback is None:
            raise NotImplementedError(f'{self.name} is declared without a readback')
        spoken = '' if self.readback_uses_services else self.readback(args)
        return BaseProposal(about=self.name, spoken=spoken, value=args.model_dump_json())

    def proposal_for(self, call: FlowCall[Any], args: BaseModel) -> Proposal:
        """The read-back as it is asked: its intent is made now, before anything could be dispatched."""
        call.intent_for(self.name, args.model_dump_json())
        if self.readback is None or not self.readback_uses_services:
            return self.proposal(args)
        readback: Callable[[Any, Any], str] = self.readback  # ty: ignore[invalid-assignment]
        return BaseProposal(
            about=self.name, spoken=readback(call.services, args), value=args.model_dump_json()
        )

    def succeeded(self, receipt: Mapping[str, Any]) -> bool:
        return all(receipt.get(name) == value for name, value in self.succeeds.items())

    rejects: ClassVar[Mapping[str, Any]] = {}
    """Receipt fields and the values that mean a terminal no, like `{status: [full, stale]}` (a list: any of them)."""

    def outcome(self, receipt: Mapping[str, Any]) -> Literal['succeeded', 'rejected', 'unknown']:
        """`succeeds` is a success; a declared `rejects` is an authoritative no; any other answer to a change settles nothing.

        A read changes nothing, so any validated answer to it is final.
        """
        if self.succeeded(receipt):
            return 'succeeded'
        refused = self.rejects and all(
            receipt.get(name) in (value if isinstance(value, list) else [value])
            for name, value in self.rejects.items()
        )
        return 'rejected' if refused or not self.changes else 'unknown'

    function_takes_intent: ClassVar[bool] = False
    """The function declares a third parameter, the `Intent`: its idempotency key for the backend."""
    reconcile_takes_intent: ClassVar[bool] = False

    async def _call(
        self,
        function: Callable[..., Any],
        takes_intent: bool,
        call: FlowCall[Any],
        args: BaseModel,
        intent: Intent,
    ) -> Any:
        """Call a domain function: a coroutine is awaited; a plain function runs in a worker thread.

        A thread keeps a slow database or HTTP client off the event loop that
        carries the call's audio. It also means the function can still be
        running, and can still commit, after the task awaiting it was
        cancelled: that is exactly why a cancellation after dispatch leaves the
        intent unknown rather than failed.
        """
        given = (call.services, args, intent) if takes_intent else (call.services, args)
        if inspect.iscoroutinefunction(function):
            return await function(*given)
        return await _result(await asyncio.to_thread(function, *given))

    def _bound(self, data: Any, args: BaseModel) -> list[str]:
        """The `matches` fields on which a receipt is about something other than these arguments."""
        given = args.model_dump()
        return [
            f for f, arg in self.matches.items() if not isinstance(data, Mapping) or data.get(f) != given[arg]
        ]

    async def _before(self, call: FlowCall[Any], args: BaseModel) -> Any:
        """Before sending anything: re-record a success that failed to record, or settle this intent if it is unknown.

        A reconciliation answer clears the unknown state only once it
        validates as the receipt, is about these arguments (`matches`), and
        says an outcome. None (the backend does not know of it, yet) is not a
        refusal: the intent stays unknown, unless the backend dedups by the
        intent id (`idempotent`), when sending it again, as itself, is safe.
        """
        key = args.model_dump_json()
        for intent_id, (operation, _, receipt) in list(call._unpresented.items()):
            held = call._intents[intent_id]
            if operation is self and held.args == key and held.subject == call.identity():
                return await self.present_again(call, args, receipt)
        intent = next(
            (
                i
                for i in call.intents
                if i.operation == self.name
                and i.args == key
                and i.subject == call.identity()
                and i.status == 'unknown'
            ),
            None,
        )
        if intent is None:
            return None
        return await self.reconcile_intent(call, args, intent)

    async def reconcile_intent(self, call: FlowCall[Any], args: BaseModel, intent: Intent) -> Any:
        """Ask the backend what this intent did, and settle it only on an answer that proves it. See `_before`.

        By intent, not by who is on the line: settlement at the start of a
        turn, or in a fresh process before the caller is known again, asks
        about the intent the arguments were recorded for.
        """
        if self.reconcile is None:
            if self.resendable:
                return None
            raise OutcomeUnknownError(
                f'{self.name}: an earlier attempt may have landed, so it is not retried'
            )
        with _tracer.start_as_current_span('write') as span:
            audit: dict[str, Any] = {
                'action': self.name,
                'intent': intent.id,
                'operation': intent.id,
                'reconciled': True,
                'args': args.model_dump(mode='json'),
                'needs_approval': self.needs_approval,
                'changes': self.changes,
                'call': call.id,
                'turn': call.turn,
                'generation': call._generation,
                'sequence': call.next_audit(),
                'status': 'unknown',
            }
            try:
                try:
                    found = await self._call(self.reconcile, self.reconcile_takes_intent, call, args, intent)
                except OutcomeUnknownError:
                    raise
                except Exception as error:
                    raise OutcomeUnknownError(
                        f'{self.name}: reconciliation failed ({type(error).__name__})'
                    ) from error
                if found is None:
                    audit['found'] = False
                    if self.resendable:
                        return None
                    raise OutcomeUnknownError(
                        f'{self.name}: the backend has no record of it yet, so it is not retried'
                    )
                data = found.model_dump() if isinstance(found, BaseModel) else found
                try:
                    receipt = self.receipt_type.model_validate(data)
                except ValidationError as error:
                    raise OutcomeUnknownError(
                        f'{self.name}: reconciliation answered in a shape that is not the receipt'
                    ) from error
                if wrong := self._bound(receipt.model_dump(), args):
                    raise OutcomeUnknownError(
                        f'{self.name}: reconciliation answered about a different {", ".join(wrong)}'
                    )
                return await self._settled(call, args, intent, receipt, audit)
            finally:
                span.set_attribute('write.audit', json.dumps(audit, default=str))

    async def _execute(self, call: FlowCall[Any], args: BaseModel, grant: Any) -> Any:
        intent = call.intent_for(self.name, args.model_dump_json())
        raw = await self._call(self.function, self.function_takes_intent, call, args, intent)
        data = raw.model_dump() if isinstance(raw, BaseModel) else raw
        if wrong := self._bound(data, args):
            raise OutcomeUnknownError(
                f'{self.name}: the service answered about a different {", ".join(wrong)}, so whether it was '
                'done for this request is unknown'
            )
        return data

    async def _record(self, call: FlowCall[Any], args: BaseModel, receipt: BaseModel) -> Any:
        data = receipt.model_dump()
        if not self.succeeded(data):
            raise ModelRetry(
                f'Nothing was done: the {self.name} service answered {data}. Do not say it was done.'
            )
        if self.assign:
            call.update(**{field: data[source] for field, source in self.assign.items()})
        for source in self.binds.values():
            call._offered.pop(source, None)
        if self.say:
            words = self.say.format_map(_Facts(data))
            given = args.model_dump(mode='json')
            filed = next(
                (e for e in reversed(call.evidence) if e.operation == self.name and e.args == given), None
            )
            call.note_receipt(words, filed.intent if filed is not None else '')
            await call.speak_now(words, origin='confirmation')
        return receipt


@dataclass(frozen=True, kw_only=True)
class Step(ABC):
    """One step of a flow: what it needs, what it produces, and whether it is done."""

    id: str
    then: str = ''
    """The step after this one. Empty means the next listed step that is not already done."""
    spec: AgentSpec | None = None
    """The persona's native `AgentSpec` (model, settings, retries) this step's agent is built from, if it names one."""

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


def _agent(flow: StepFlow, step: Step, instructions: Any, outputs: list, **agent: Any) -> Agent[Any, object]:
    """A step's agent, from its persona's native `AgentSpec`: its model unless the flow's was given explicitly.

    `FlowSpec.compile(model=...)` strips each persona's model, so an explicit
    model is used everywhere; otherwise a persona's own model wins over the
    flow's default. Settings and instructions merge, as `Agent.from_spec` does.

    Every step's agent times each provider request (`request_clock`), which is
    where a turn's time to first token comes from; the hand-built steps had it
    from `AgentBuilder`, and session 4 found the public steps without it.
    """
    spec = step.spec or AgentSpec()
    capabilities = [*(agent.pop('capabilities', None) or []), request_clock()]
    built = Agent.from_spec(
        spec,
        deps_type=flow.call_type,
        model=None if spec.model else flow.model,
        name=step.id,
        output_type=outputs,
        instructions=instructions,
        model_settings=flow.settings,
        capabilities=capabilities,
        **agent,
    )
    if issubclass(flow.call_type, FlowCall):

        @built.instructions
        def application_state(ctx: RunContext[Any]) -> str:
            return context(flow, step, ctx.deps)

    return built


def context(flow: StepFlow, step: Step, call: FlowCall[Any]) -> str:
    """What this step's agent is told the application knows, rendered fresh before every request.

    The facts, what is on offer, the question the caller is answering and
    whether they heard it, what was done on this call and what the caller
    missed, and where this step may hand them. Sent as instructions, apart
    from anything the caller said or a service returned, so a reply cannot
    pose as the application. Written from the call's own records, and from
    nothing a caller is not entitled to: never the services, the session's
    private objects or a secret.

    Seeing a fact is not permission to act on it: every action is still
    checked by its own guard when it runs.
    """
    lines = [
        '## Application state',
        'Written by the application, not by the caller. It is true now; the conversation may be older.',
        f'You are the `{step.id}` step.',
        f'Facts: {call.state.facts.model_dump_json()}',
    ]
    offered = {name: ids for name, ids in call._offered.items() if ids}
    lines.append(
        f'On offer to the caller now, by id: {json.dumps(offered)}. Only these ids can be picked.'
        if offered
        else 'Nothing is on offer to the caller now.'
    )
    last = call.last_proposal()
    if last is not None:
        question, heard = last
        lines.append(
            f'The caller is answering: "{question.spoken}" ({question.about}). '
            + (
                'They heard it in full.'
                if heard
                else 'They did not hear all of it, so their reply cannot agree to it: put it to them again.'
            )
        )
    done = [e for e in call.evidence if e.identity == call.identity()]
    for filed in done[-3:]:
        lines.append(
            f'Done on this call: {filed.operation} {json.dumps(filed.args)} -> {json.dumps(filed.receipt)}'
        )
    for intent in call.unresolved():
        lines.append(
            f'Outcome unknown: {intent.operation} {intent.args}. Do not say it was done, or that it was not: '
            'the application is checking, and will not send it again until it knows.'
        )
    if standing := recaps(flow, call):
        lines.append(
            'Can recap on goodbye, by reference: '
            + '; '.join(f'{ref}: "{words}"' for ref, words in standing.items())
            + '.'
        )
    if call.spoken_over:
        lines.append(call.spoken_over)
    exits = list(step.exits) if isinstance(step, ConversationStep) else []
    purposes = step.purposes if isinstance(step, ConversationStep) else {}
    if exits:
        lines.append(
            'You may hand the caller to: '
            + '; '.join(f'{to} ({purposes[to]})' if purposes.get(to) else to for to in exits)
            + '.'
        )
    return '\n'.join(lines)


def _node(flow: StepFlow, step: Step, agent: Agent[Any, object], **node: Any) -> Node[Any]:
    return Node(agent, exits=tuple(Exit(to) for to in flow.destinations(step)), **node)


def _entered(call: FlowCall[Any]) -> bool:
    return True


def _optional(info: FieldInfo) -> Any:
    """A state field's type, constraints kept, or None: what a collect tool's schema offers for it."""
    kind = Annotated[info.annotation, *info.metadata] if info.metadata else info.annotation  # ty: ignore[invalid-type-form]
    return kind | None  # ty: ignore[unsupported-operator]


RECAP_REF = 8
"""How many characters of an intent id a recap reference is: unique within a call, short enough to copy."""


def recaps(flow: StepFlow, call: FlowCall[Any]) -> dict[str, str]:
    """What a goodbye may recap, by reference: each receipt that still stands, in its operation's `recap` words.

    Only `CallContext.effective` receipts: a lookup, a refusal, a booking
    since moved or cancelled, and anything done for another caller are not
    here, so no reference to them exists for a model to give.
    """
    known = {op.name: op for op in flow.operations()}
    found = {}
    for filed in call.effective():
        template = getattr(known.get(filed.operation), 'recap', '')
        if filed.intent and template:
            found[filed.intent[:RECAP_REF]] = template.format_map(_Facts(filed.receipt))
    return found


def goodbye_words(flow: StepFlow, call: FlowCall[Any], recap: Sequence[str]) -> str:
    """The goodbye core says: the recaps pointed at, then `unsettled` if anything is still unknown, then the line.

    A reference that is not a standing receipt of this call is refused back
    to the model; it cannot name one into existence, and it writes none of
    the words. An entry that is exactly a standing recap's words (the live
    text checkpoint's model sent those instead of the reference) counts as
    that reference: the words were rendered by the application from the
    receipt, so matching them names nothing new, and saves a retry.
    """
    standing = recaps(flow, call)
    by_words = {' '.join(words.split()): ref for ref, words in standing.items()}
    recap = [ref if ref in standing else by_words.get(' '.join(ref.split()), ref) for ref in recap]
    if wrong := [ref for ref in recap if ref not in standing]:
        raise ModelRetry(
            f'Nothing was said: {", ".join(map(repr, wrong))} is not something done on this call that still '
            'stands. Recap only references listed under "Can recap", or none.'
        )
    words = [standing[ref] for ref in dict.fromkeys(recap)]
    if call.unresolved():
        words.append(call.locale.unsettled)
    elif call.last_asked_left_undone():
        words.append(call.locale.left_undone)
    words.append(call.locale.goodbye)
    return ' '.join(w for w in words if w)


def _args(operation: Operation, call: FlowCall[Any]) -> BaseModel:
    """An operation's arguments, taken field by field from the application state, validated whole.

    A declared operation names the state field each argument comes from
    (`FunctionOperation.binds`); otherwise the argument's own name is the field.
    """
    facts = call.state.facts
    binds = getattr(operation, 'binds', None) or {}
    return operation.args_type.model_validate(
        {name: getattr(facts, binds.get(name, name)) for name in operation.args_type.model_fields}
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
    choices: Mapping[str, Callable[[Any, Any], Mapping[str, str]]] = field(default_factory=dict)
    """For a field picked from options: `(services, state) -> {id: how it is said}`, asked on arrival.

    The ids go on the call as offered (`FlowCall._offered`) and the question's
    `{options}` becomes their words, so a value can only be one the caller was
    actually offered. A receipt listing options offers nothing by itself.
    """

    @property
    def produces(self) -> frozenset[str]:
        return frozenset(self.fields)

    @property
    def depends_on(self) -> frozenset[str]:
        return self.after

    def done(self, call: FlowCall[Any]) -> bool:
        return FieldsPresent(*self.fields).is_met(dict(call.state.facts))

    def ask(self, call: FlowCall[Any]) -> Prompted:
        """The question, with this caller's options offered in it when the step has choices."""
        said = []
        for name, options in self.choices.items():
            offered = dict(options(call.services, call.state.facts))
            call._offered[name] = offered
            said.extend(offered.values())
        spoken = (
            self.question.format_map(_Facts(options=' or '.join(said))) if self.choices else self.question
        )
        return Prompted(about=f'collect:{self.id}', spoken=spoken)

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
            return self.ask(call)

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
        for name, value in heard.items():
            if any(name in step.choices for step in listening) and str(value) not in call._offered.get(
                name, {}
            ):
                offered = ', '.join(call._offered.get(name, {})) or 'none yet'
                raise ModelRetry(
                    f'Nothing was recorded: {value!r} was not offered for {name} (offered: {offered}).'
                )
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
        return self.ask(call)


@dataclass(frozen=True, kw_only=True)
class ConfirmStep(Step):
    """Read back an operation's exact proposal and wait for agreement to *that*.

    The step's own agent reads the reply, in the whole conversation, and
    answers through one of four outputs: `accept`, `decline`, `change`, or
    `answer` for anything else (a question, a condition, an unclear reply).
    Acceptance is bound by code to the pending question (`CallContext.approve`)
    and moves to `then`, the action, in the same turn; the action consumes the
    approval for those same arguments. Rejection and revision go where they
    are told. An answer is said, and the proposal read back again after it, so
    the next yes answers a question the caller has just heard. A yes after
    the read-back has expired gets it read back again, by code, with no model.
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
        proposal = self.operation.proposal_for(call, _args(self.operation, call))
        return Prompted(about=proposal.about, spoken=proposal.spoken, value=proposal.value)

    def node(self, flow: StepFlow) -> Node[Any]:
        async def accept(ctx: RunContext[FlowCall[Any]]) -> Handoff | Prompted:
            """The caller clearly agreed to exactly what was read back to them, with nothing changed or conditional."""
            proposal = self.readback(ctx.deps)
            if not ctx.deps.approve(proposal.about, value=proposal.value):
                return proposal
            return Handoff(to=self.then)

        async def decline(ctx: RunContext[FlowCall[Any]]) -> Handoff:
            """The caller said no to what was read back to them."""
            return await _revised(flow, ctx.deps, self.on_reject)

        async def change(ctx: RunContext[FlowCall[Any]]) -> Handoff:
            """The caller wants something in the read-back changed."""
            return await _revised(flow, ctx.deps, self.on_revise)

        async def answer(ctx: RunContext[FlowCall[Any]], words: str) -> Prompted:
            """Anything else: a question, a condition, or a reply you cannot read as yes or no. Answer it; the read-back is asked again after.

            Args:
                words: One short sentence to the caller. Never say the request is done.
            """
            if ctx.deps.claims_completion(' '.join(words.split())):
                raise ModelRetry(
                    'Nothing was said: those words claim the request is done, and nothing was done yet. '
                    'Answer without saying it was done; the read-back is asked again after.'
                )
            await ctx.deps.speak_now(words, origin='model')
            return self.readback(ctx.deps)

        agent = _agent(
            flow,
            self,
            'The caller was just read back a request and asked to confirm it; the application state shows '
            'the exact words and whether they heard them. Read their reply and answer with exactly one output. '
            'Never say the request is confirmed or submitted.',
            [
                ToolOutput(accept, name='accept'),
                ToolOutput(decline, name='decline'),
                ToolOutput(change, name='change'),
                ToolOutput(answer, name='answer'),
            ],
        )

        async def read_back(call: FlowCall[Any]) -> Prompted:
            """Put the exact proposal to the caller."""
            return self.readback(call)

        async def advance(call: FlowCall[Any], usage: RunUsage, limits: UsageLimits) -> LocalAction | None:
            """A reply with no read-back it could answer (expired, unheard, changed) gets it read back, by code."""
            proposal = self.readback(call)
            if call.pending(proposal.about, value=proposal.value) is None and call.heard.strip():
                return LocalAction('readback', lambda: read_back(call))
            return None

        return _node(
            flow,
            self,
            agent,
            advance=advance,
            openings=(Opening('readback', 'handoff', _entered, read_back),),
            model_speech=False,
        )


async def _moved(to: str) -> Handoff:
    return Handoff(to=to)


async def _revised(flow: StepFlow, call: FlowCall[Any], to: str) -> Handoff:
    """Go back to the step a no or a change points at, forgetting its answer so it is asked again.

    A no to "Shall I book Tuesday at 10?" is a no to that time: kept, the
    step would be skipped as done and the same read-back asked again.
    """
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
    returns: bool = False
    """A code node: on success, go back to the step that handed the caller here, not onward.

    The step handing over is recorded on the way in (`core.graph.route`), the
    way a prerequisite records who is waiting, so a reusable piece of known
    work can serve several agent nodes. No model decides the return.
    """

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
                onward = flow.next_after(self, call)
                return Handoff(to=call.resume(onward) if self.returns else onward)
            try:
                await self.operation.run(call, _args(self.operation, call))
            except OutcomeUnknownError:
                return Handoff(to=self.on_unknown or self.on_error)
            except ModelRetry:
                return Handoff(to=self.on_error)
            if not self.done(call):
                return Handoff(to=self.on_error)
            if self.returns:
                return Handoff(to=call.resume(flow.next_after(self, call)))
            return Handoff(to=flow.next_after(self, call))

        agent = _agent(flow, self, 'Say one short sentence: you are working on it.', [str])
        return _node(
            flow,
            self,
            agent,
            openings=(Opening(self.operation.name, 'handoff', _entered, act),),
            returns=self.returns,
        )


def _values(flow: StepFlow, name: str, fields: Sequence[str]) -> type[BaseModel]:
    """The collect tool's argument: each field's own type from the state, constraints kept, every one optional."""
    state = flow.state_type.model_fields
    return create_model(  # ty: ignore[no-matching-overload]
        name,
        __config__=ConfigDict(extra='forbid'),
        **{
            field: (_optional(state[field]), Field(None, description=state[field].description))
            for field in fields
        },
    )


def said_by_caller(call: CallContext, excerpt: str) -> bool:
    """Whether these words are the caller's: found, as whole words, in what they said, never in model history.

    An extractive read-back trades paraphrase for evidence anyone can check.
    Case and punctuation may differ, because speech-to-text adds its own.
    """

    def words(text: str) -> str:
        return ' '.join(re.findall(r'\w+', text.casefold()))

    sources = [row['text'] for row in call.transcript() if row['said'] == 'caller']
    sources += [
        part.content
        for message in call.messages
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    ]
    wanted = words(excerpt)
    return bool(wanted) and any(f' {wanted} ' in f' {words(source)} ' for source in sources)


def _refuse_unoffered(call: FlowCall[Any], choices: Mapping[str, Any], values: Mapping[str, Any]) -> None:
    """Refuse a value for a choice field that was never offered to this caller."""
    for name, value in values.items():
        if name in choices and str(value) not in call._offered.get(name, {}):
            offered = ', '.join(call._offered.get(name, {})) or 'none yet'
            raise ModelRetry(
                f'Nothing was recorded: {value!r} was not offered for {name} (offered: {offered}).'
            )


SPEAK_BY = 3
"""The request by which an agent node's run has no ordinary tools left and answers with what it has (`speak_by`)."""


class Options(BaseModel):
    """What a choice function returns, as core keeps it: the options by id, and a note for the agent.

    A choice function returns a plain `{id: words}`, or any object with
    `options` and `note` (the application's own model; it need not import
    this). The note is how the domain says something the ids cannot, such as
    a day that could mean two dates: the agent reads it and asks the caller,
    and only the ids are ever offered.
    """

    model_config = ConfigDict(extra='forbid')
    options: dict[str, str] = {}
    note: str = ''


async def _offer(call: FlowCall[Any], choices: Mapping[str, Callable[[Any, Any], Any]]) -> dict[str, Any]:
    """Ask each choice function for this caller's options, and record them as offered, unless the call moved on.

    A choice function may be sync or async, and may take long: the caller can
    change what they asked for, or be handed elsewhere, before it answers. A
    plain one runs in a worker thread, like a domain function (`_call`), so a
    database or HTTP read never holds the event loop that carries audio. An
    answer computed for a state that is no longer the call's, or in a turn the
    caller has left, is returned as stale and offers nothing, so it can never
    replace the current offer or make an old slot pickable.
    """
    version, generation, agent = call._version, call._generation, call.state.agent
    found: dict[str, Options] = {}
    for name, choose in choices.items():
        try:
            if inspect.iscoroutinefunction(choose):
                answer = await choose(call.services, call.state.facts)
            else:
                answer = await _result(await asyncio.to_thread(choose, call.services, call.state.facts))
        except Exception as error:
            logger.exception('choice function for {} failed', name)
            return {
                'unavailable': f'The options could not be looked up just now ({type(error).__name__}). Nothing is offered.'
            }
        found[name] = (
            Options(options=dict(answer))
            if isinstance(answer, Mapping)
            else Options(options=dict(answer.options), note=getattr(answer, 'note', ''))
        )
    if call._version != version or call._generation != generation or call.state.agent != agent:
        return {'stale': 'The request changed while these were looked up. Nothing is offered; look again.'}
    result: dict[str, Any] = {}
    for name, options in found.items():
        call._offered[name] = dict(options.options)
        result[name] = {'options': options.options, **({'note': options.note} if options.note else {})}
    return result


def _resting_on(operation: Operation) -> set[str]:
    """The state fields an operation's arguments are built from: a change to one of them makes its read-back stale."""
    return set(getattr(operation, 'binds', {}).values()) or set(operation.args_type.model_fields)


async def _carry_out(
    call: FlowCall[Any],
    operation: Operation,
    usage: RunUsage | None,
    limits: UsageLimits | None,
    *,
    rendered: bool = False,
) -> str | None:
    """Run an approved operation and return what the caller heard; an unknown outcome is said as unknown.

    On a rendered step an authoritative no is said by code too
    (`Locale.withheld`, or `unsettled` while anything is unknown), instead
    of being handed back to a model to put in its own words.
    """
    before = {i.id for i in call.intents if i.status == 'rejected'}
    try:
        receipt = await operation.run(call, _args(operation, call), usage=usage, usage_limits=limits)
    except OutcomeUnknownError:
        await call.speak_now(call.locale.unsettled, origin='confirmation')
        return call.locale.unsettled
    except ModelRetry:
        refused = {i.id for i in call.intents if i.status == 'rejected' and i.operation == operation.name}
        if not rendered or not refused - before:
            raise
        words = call.correction(call.locale.withheld)
        await call.speak_now(words, origin='error')
        return words
    say = getattr(operation, 'say', '')
    return say.format_map(_Facts(receipt.model_dump())) if say and isinstance(receipt, BaseModel) else None


@dataclass(frozen=True, kw_only=True)
class ConversationStep(Step):
    """An agent node: one agent, its instructions, what it may record, offer, carry out and hand over to.

    The step stays active across turns until it hands the caller on or they
    say goodbye. Its agent reads every reply itself, in the shared
    conversation and the application state (`context`), and answers through
    its outputs: text, `goodbye`, one of its declared `operations`, or
    `hand_over` to one of its `exits`. `fields` it may record from the caller
    (`collect`) and `choices` it may offer (`options`) are ordinary tools, and
    their results come back to this same agent in the same run.

    An operation is read back in the application's words and runs only when
    the agent calls it again on the turn after that read-back reached the
    caller in full: the agent's yes, bound by code to that exact question
    (`CallContext.approve`), through the same `Operation.run` a controlled
    flow uses. No second model reads the reply. A reply that also changes the
    request (`collect` beside the operation) gets a fresh read-back instead.

    `requires` names state facts this step needs before it runs, and
    `satisfied_by` the step that establishes them; the graph interposes it
    and comes back (`core.graph.route`). A step that satisfies another returns
    to the one waiting, by code, as soon as one of its operations has made the
    waiting step's requirements true.
    """

    instructions: Any
    """Text, or a compiled template from a prompt file (which already carries any knowledge)."""
    knowledge: str = ''
    fields: tuple[str, ...] = ()
    choices: Mapping[str, Callable[[Any, Any], Any]] = field(default_factory=dict)
    operations: tuple[Operation, ...] = ()
    exits: tuple[str, ...] = ()
    purposes: Mapping[str, str] = field(default_factory=dict)
    """What each exit is for, in the words the agent is shown with it."""
    requires: tuple[str, ...] = ()
    satisfied_by: str = ''
    guarded: bool = False
    rendered: bool = False
    """`speech: rendered`: the agent chooses and code words everything. No text output, so no model prose to TTS.

    Its outputs are the step's operations (read back, then carried out),
    `hand_over`, `goodbye`, and `wait`, after which code asks for the first
    missing `asks:` field, or says `Locale.pardon`. An authoritative no is
    said in core's fixed words (`_carry_out`).
    """
    quoted: tuple[str, ...] = ()
    """Fields whose value must be the caller's own words, found in what they said: never a paraphrase or a record."""
    opens_with: Operation | None = None
    """An operation read back on arrival, by code, when its arguments are already known (a carrier's number)."""
    asks: Mapping[str, str] = field(default_factory=dict)
    """Field -> words: on arrival by a hand-over, code asks for the first of these fields still empty, no model.

    What a step says first when it lacks a field is known; a model asked to
    open with it said "I'll connect you with someone" instead (Coval gate 1
    rerun, cBrx4NhR). Tried after `opens_with`.
    """
    reads_back: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """Operation -> fields: a reply that only records one of these fields gets that operation read back by code.

    The same read-back the model would have asked for with a second request,
    and the same approval after it: only a later yes, with the model calling
    the operation, carries it out.
    """

    @property
    def produces(self) -> frozenset[str]:
        return frozenset(self.fields)

    @property
    def elsewhere(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([*self.exits, *([self.satisfied_by] if self.satisfied_by else [])]))

    def opening(self) -> tuple[Opening[Any], ...]:
        """On arrival by a hand-over: read back `opens_with` if the state gives its arguments, else ask (`asks`). No model."""
        return (*self._read_back_first(), *self._ask_first())

    def _ask_first(self) -> tuple[Opening[Any], ...]:
        if not self.asks:
            return ()

        def missing(call: FlowCall[Any]) -> str:
            return next((name for name in self.asks if not getattr(call.state.facts, name)), '')

        async def ask(call: FlowCall[Any]) -> str:
            words = self.asks[missing(call)]
            await call.speak_now(words, origin='ask')
            return words

        return (Opening('ask', 'handoff', lambda call: bool(missing(call)), ask),)

    def _read_back_first(self) -> tuple[Opening[Any], ...]:
        operation = self.opens_with
        if operation is None:
            return ()

        def ready(call: FlowCall[Any]) -> bool:
            try:
                args = _args(operation, call)
            except ValidationError:
                return False
            return call.pending(operation.name, value=operation.proposal(args).value) is None

        async def read_back(call: FlowCall[Any]) -> Prompted:
            proposal = operation.proposal_for(call, _args(operation, call))
            return Prompted(about=proposal.about, spoken=proposal.spoken, value=proposal.value)

        return (Opening(operation.name, 'handoff', ready, read_back),)

    def _reads_back(self, flow: StepFlow) -> Callable[[FlowCall[Any]], Prompted | Restated | None] | None:
        """The `reads_back` rule: the operation whose field was just recorded, when it is the only one that could run.

        An operation could run when its arguments are complete, or when the
        ones missing are choices on offer (a held booking the caller could
        name). So where a new booking and a move of a held one are both
        possible, which one the caller means is the model's to read, as
        before. When that exact question is already waiting, heard in full,
        the caller has said its value again: `Restated`, carried out by code.
        None when something it rests on is still changing.
        """
        if not self.reads_back:
            return None

        def possible(call: FlowCall[Any], operation: Operation) -> bool:
            try:
                _args(operation, call)
            except ValidationError as error:
                binds = getattr(operation, 'binds', None) or {}
                missing = {binds.get(str(e['loc'][0]), str(e['loc'][0])) for e in error.errors() if e['loc']}
                return all(call._offered.get(name) for name in missing)
            return True

        def read(call: FlowCall[Any]) -> Prompted | Restated | None:
            if [op for op in self.operations if possible(call, op)] != [
                op for op in self.operations if op.name in self.reads_back and possible(call, op)
            ]:
                return None
            for operation in self.operations:
                if not call._recorded & set(self.reads_back.get(operation.name, ())):
                    continue
                try:
                    proposal = operation.proposal_for(call, _args(operation, call))
                except ValidationError:
                    return None
                if call.pending(operation.name, value=proposal.value) is not None:
                    if not operation.needs_approval or any(
                        call._tries[name] != call._kept[name]
                        for name in call._changing & _resting_on(operation)
                    ):
                        return None
                    return Restated(
                        about=proposal.about,
                        run=lambda usage, limits, operation=operation: self._confirmed(
                            flow, operation, call, usage, limits
                        ),
                    )
                return Prompted(about=proposal.about, spoken=proposal.spoken, value=proposal.value)
            return None

        return read

    async def _confirmed(
        self,
        flow: StepFlow,
        operation: Operation,
        call: FlowCall[Any],
        usage: RunUsage | None,
        limits: UsageLimits | None,
    ) -> Any:
        """Carry out a read-back the caller confirmed by saying its value again (`Restated`). None if nothing is pending."""
        proposal = operation.proposal_for(call, _args(operation, call))
        if not call.approve(proposal.about, value=proposal.value):
            return None
        await call.speak_now(call.locale.restated, origin='confirmation')
        return await self._approved(flow, operation, call, usage, limits)

    async def _approved(
        self,
        flow: StepFlow,
        operation: Operation,
        call: FlowCall[Any],
        usage: RunUsage | None,
        limits: UsageLimits | None,
    ) -> Prompted | Handoff | str | None:
        """Run an operation whose approval is bound, then hand back to a waiting step, if this was its prerequisite."""
        said = await _carry_out(call, operation, usage, limits, rendered=self.rendered)
        if (back := flow.return_after(self, call)) is not None:
            return back
        return said

    def node(self, flow: StepFlow) -> Node[Any]:
        async def goodbye(ctx: RunContext[FlowCall[Any]], recap: list[str] | None = None) -> str:
            """End the call, only once the caller's own words say they are finished. The application says the goodbye.

            Args:
                recap: The short references (like 3fa85f64) listed under "Can recap" in the application state, for what was done on this call that the caller should hear once more as they leave. Usually none.
            """
            call = ctx.deps
            if call.pending_question() is not None and call._goodbye_checked != call.turn:
                call._goodbye_checked = call.turn
                raise ModelRetry(
                    "Nothing was said: a read-back is waiting for the caller's answer. If their words agree to "
                    'it, carry it out now. If they are leaving, call goodbye again.'
                )
            words = goodbye_words(flow, call, recap or [])
            call.note_departure()
            call.end_requested('caller')
            await call.speak_now(words, origin='goodbye')
            await call.end_call()
            return words

        async def wait(ctx: RunContext[FlowCall[Any]]) -> str:
            """Nothing to read back, carry out or hand over yet. The application asks the caller for what it still needs."""
            call = ctx.deps
            missing = next((name for name in self.asks if not getattr(call.state.facts, name)), '')
            words = self.asks[missing] if missing else call.locale.pardon
            await call.speak_now(words, origin='ask')
            return words

        actions = frozenset(op.name for op in self.operations) | (
            {'hand_over'} if self.exits else frozenset()
        )
        outputs: list[Any] = [
            *([] if self.rendered else [str]),
            ToolOutput(goodbye, name='goodbye'),
            *([ToolOutput(wait, name='wait')] if self.rendered else []),
        ]
        tools: list[Any] = []
        if self.fields:
            values_type = _values(flow, f'{self.id.title()}Values', self.fields)

            async def collect(ctx: RunContext[FlowCall[Any]], values: BaseModel) -> str:
                """Record exactly what the caller has just said for any of these fields. Never guess a value."""
                proposed = values.model_dump(exclude_none=True)
                call = ctx.deps
                before = {name: getattr(call.state.facts, name, None) for name in proposed}
                _refuse_unoffered(call, self.choices, proposed)
                for name in self.quoted:
                    if proposed.get(name) and not said_by_caller(call, str(proposed[name])):
                        raise ModelRetry(
                            f"Nothing was recorded: {name} must be the caller's own words, copied from what "
                            'they said. Punctuation and case may differ; nothing else may.'
                        )
                try:
                    flow.record(call, proposed)
                except ValidationError as error:
                    problems = '; '.join(e['msg'] for e in error.errors())
                    raise ModelRetry(f'Nothing was recorded: {problems}. Ask the caller again.') from None
                call._kept.update(
                    name for name in proposed if getattr(call.state.facts, name, None) == before[name]
                )
                call._recorded = frozenset(proposed)
                return f'Recorded: {", ".join(sorted(proposed)) or "nothing"}.'

            collect.__annotations__ = {'ctx': RunContext[FlowCall], 'values': values_type, 'return': str}
            tools.append(Tool(collect))
        if self.choices:

            async def options(ctx: RunContext[FlowCall[Any]]) -> dict[str, Any]:
                """The options you may offer the caller now, by id, for what they asked. Only an offered id can be recorded. Say them in your own words."""
                return await _offer(ctx.deps, self.choices)

            tools.append(Tool(options))
        for operation in self.operations:
            outputs.append(
                ToolOutput(
                    self._operate(flow, operation, actions),
                    name=operation.name,
                    description=f"{operation.name}: the first call reads it back to the caller in the application's "
                    'words. Call it again only when their reply to that read-back is a clear yes: that yes '
                    'carries it out.',
                    sequential=True,
                )
            )
        exits = self.exits
        if exits:
            outputs.append(ToolOutput(self._hand_over(actions), name='hand_over', sequential=True))

        async def narrow(ctx: RunContext[FlowCall[Any]], defs: list[ToolDefinition]) -> list[ToolDefinition]:
            """Send `hand_over` with only this step's exits as the destinations the model can name."""
            kept = []
            for tool_def in defs:
                if tool_def.name == 'hand_over':
                    schema = deepcopy(tool_def.parameters_json_schema)
                    if (to := schema.get('properties', {}).get('to')) is not None:
                        to.pop('const', None)
                        to['enum'] = list(exits)
                    tool_def = replace(tool_def, parameters_json_schema=schema)
                kept.append(tool_def)
            return kept

        text = self.instructions
        if self.knowledge and isinstance(text, str):
            text += f'\n\nKnowledge:\n{self.knowledge}'
        agent = _agent(
            flow,
            self,
            text,
            outputs,
            tools=tools,
            capabilities=[
                *([one_action_per_reply(actions, frozenset({'collect'}))] if actions else []),
                Hooks(prepare_output_tools=narrow),
                speak_by(SPEAK_BY),
            ],
        )
        return _node(
            flow,
            self,
            agent,
            requires=self.requires,
            satisfied_by=self.satisfied_by,
            guarded=self.guarded,
            openings=self.opening(),
            speaks_for=frozenset(op.name for op in self.operations),
            reads_back=self._reads_back(flow),
            model_speech=not self.rendered,
        )

    def _operate(self, flow: StepFlow, operation: Operation, actions: frozenset[str]):
        async def operate(ctx: RunContext[FlowCall[Any]]) -> Prompted | Handoff | str | None:
            claim_or_refuse(ctx, actions)
            call = ctx.deps
            try:
                args = _args(operation, call)
            except ValidationError as error:
                missing = ', '.join(str(e['loc'][0]) for e in error.errors() if e['loc'])
                raise ModelRetry(f'Not yet: {missing} missing. Ask the caller and record it first.') from None
            if operation.needs_approval:
                proposal = operation.proposal_for(call, args)
                read_back = Prompted(about=proposal.about, spoken=proposal.spoken, value=proposal.value)
                resting_on = _resting_on(operation)
                if call._unapplied & resting_on:
                    raise ModelRetry(
                        f'Nothing was done: this reply also changes {", ".join(sorted(call._unapplied & resting_on))}. '
                        f'Record the change first, then call {operation.name} again to read it back.'
                    )
                if call._changing & resting_on or not call.approve(proposal.about, value=proposal.value):
                    return read_back
            return await self._approved(flow, operation, call, ctx.usage, ctx.usage_limits)

        return operate

    def _hand_over(self, actions: frozenset[str]):
        exits = self.exits

        async def hand_over(ctx: RunContext[FlowCall[Any]], to: str, because: str) -> Handoff:
            """Hand the caller to the step that serves what they asked for.

            Args:
                to: Where they go.
                because: One short sentence: what the caller wants. Never spoken.
            """
            claim_or_refuse(ctx, actions)
            if to not in exits:
                raise ModelRetry(f'{to!r} is not a step this one hands over to: {", ".join(exits)}.')
            return Handoff(to=to, because=because)

        return hand_over


@dataclass(frozen=True, kw_only=True)
class End(Step):
    """Say one closing line and record how the flow ended. `say` may use the facts: `{field}`.

    `take_back` is where a caller who takes the result back is sent, normally
    a confirm step for the undo operation. The ending's own agent reads each
    later reply in the conversation and, only for a clear withdrawal, calls
    `take_back`; the undo still needs its own read-back and yes. Anything
    else it answers in words.
    """

    outcome: Literal['completed', 'cancelled', 'failed', 'pending']
    say: str
    take_back: str = ''

    @property
    def elsewhere(self) -> tuple[str, ...]:
        return (self.take_back,) if self.take_back else ()

    def said(self, call: FlowCall[Any]) -> str:
        return self.say.format_map(_Facts(call.state.facts.model_dump()))

    def node(self, flow: StepFlow) -> Node[Any]:
        async def close(call: FlowCall[Any]) -> str:
            """Record the outcome and say it."""
            call.commit(outcome=self.outcome)
            words = self.said(call)
            await call.speak_now(words, origin='confirmation')
            return words

        async def take_back(ctx: RunContext[FlowCall[Any]]) -> Handoff:
            """The caller clearly wants what was just done undone. Not for a question about it, or for keeping it."""
            return Handoff(to=self.take_back)

        outputs: list[Any] = [str]
        if self.take_back:
            outputs.append(ToolOutput(take_back, name='take_back'))
        agent = _agent(
            flow, self, 'The request is finished. Answer briefly; do not start a new one.', outputs
        )
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


class _Facts(dict):
    """Facts for `str.format_map`, with nested receipts reachable as `{step[field]}` and a blank for anything missing."""

    def __missing__(self, key: str) -> str:
        return ''


class StepFlow:
    """An ordered list of steps, checked and compiled into the turn engine's nodes table.

    Shared config, per-call state: the flow holds steps and a model; each
    call holds its own application state (`open`). No step of it asks a
    second model what a reply meant: each agent node reads its own replies.
    Construction refuses a flow whose destinations do not exist, whose ids
    repeat, or that has business steps and no `End` with outcome `completed`,
    so a broken flow fails where it is defined rather than on a call.
    """

    def __init__(
        self,
        steps: Sequence[Step],
        *,
        model: Model | str,
        settings: ModelSettings | None = None,
        state_type: type[BaseModel] = NoFacts,
        identity: str = '',
        call_type: type[CallContext] | None = None,
        entry: str = '',
        services: Callable[[], Any] | None = None,
        locale: Locale | None = None,
        version: str = '',
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
            name for s in self.steps if isinstance(s, CollectStep | ConversationStep) for name in s.fields
        )
        """The only state fields a model may write: the ones some collect or conversation step asks the caller for."""
        self.derived: dict[str, frozenset[str]] = {}
        """Each state field a receipt sets, with the state fields its operation's arguments were bound to."""
        for step in self.steps:
            for operation in (getattr(step, 'operation', None), *getattr(step, 'operations', ())):
                for assigned in getattr(operation, 'assign', {}):
                    sources = frozenset(getattr(operation, 'binds', {}).values())
                    self.derived[assigned] = self.derived.get(assigned, frozenset()) | sources
        self.entry = entry or self.steps[0].id
        if self.entry not in self.by_id:
            raise ValueError(f'the entry step {self.entry} is not a step')
        self.services = services
        """Makes one call's services when `open` is given none: the manifest's `services:` factory."""
        self.locale = locale
        self.version = version
        """The application's own version, from the manifest (`app_version`): what a trace or a report names."""
        self.model = model
        self.settings = settings
        self.nodes: dict[str, Node[Any]] = {step.id: step.node(self) for step in self.steps}

    @property
    def start(self) -> str:
        return self.entry

    def open(
        self,
        state: BaseModel,
        *,
        services: Any = None,
        clock: Callable[[], datetime] | None = None,
        intents: Sequence[Intent] = (),
    ) -> FlowCall[Any]:
        """A new session on this flow, standing on its first step: the application's state, its services, its clock.

        The state is copied in and validated as the flow's model, so the
        object the caller passed can change afterwards without reaching the
        call. Services are kept as given: whether a client is shared between
        calls or made per call is the application's decision.

        `intents` are unresolved intents an earlier process persisted (through
        its services' `note_intent`, or `FlowCall.unresolved`): they come back
        unknown, and are reconciled before any of them, or any other intent of
        the same operation for the same caller, can be sent.
        """
        if not (isinstance(self.call_type, type) and issubclass(self.call_type, FlowCall)):
            raise TypeError(f'{self.call_type.__name__} is its own call class; open its calls with it')
        facts = self.state_type.model_validate(deepcopy(state.model_dump()), extra='forbid')
        call = self.call_type(state=FlowState[self.state_type](agent=self.start, facts=facts))  # ty: ignore[invalid-type-form]
        call._identity_field, call._collectible, call._derived = self.identity, self.collectible, self.derived
        call._choices = frozenset(name for step in self.steps for name in getattr(step, 'choices', {}))
        if services is None and self.services is not None:
            services = self.services()
        call._services, call._clock, call._locale = services, clock, self.locale
        call._reconcile_with = {op.name: (op, op.args_type) for op in self.operations()}
        if intents:
            call.restore(list(intents), call._reconcile_with)
        call.take_owed()
        return call

    def operations(self) -> list[Operation]:
        """Every operation a step of this flow can run, once each."""
        found: dict[str, Operation] = {}
        for step in self.steps:
            for operation in (
                getattr(step, 'operation', None),
                getattr(step, 'opens_with', None),
                *getattr(step, 'operations', ()),
            ):
                if operation is not None:
                    found.setdefault(operation.name, operation)
        return list(found.values())

    def sequence(self) -> list[Step]:
        """The steps walked in order, without the endings and without hand-built steps, which move by their exits."""
        return [s for s in self.steps if not isinstance(s, End | AgentStep)]

    def collects_after(self, step: Step) -> list[CollectStep]:
        order = self.sequence()
        return [s for s in order[order.index(step) + 1 :] if isinstance(s, CollectStep)]

    def return_after(self, step: Step, call: FlowCall[Any]) -> Handoff | None:
        """Where a prerequisite hands back once its work made the waiting step's requirements true, or None.

        Code decides it, from the pending slot `route` filled on the way in and
        the facts the step's operation just established: no model is asked
        where to go, and a step that is nobody's prerequisite never returns.
        """
        waiting = call.state.pending
        if waiting is None or not any(getattr(s, 'satisfied_by', '') == step.id for s in self.steps):
            return None
        if not holds(call.state, getattr(self.by_id[waiting], 'requires', ())):
            return None
        return Handoff(to=call.resume(waiting), status='completed')

    def destinations(self, step: Step) -> tuple[str, ...]:
        """Every step this one can hand the caller to: onward (skipping done steps), or elsewhere."""
        if isinstance(step, AgentStep | End | ConversationStep):
            return step.elsewhere
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
