"""Typed steps that compile into the turn engine's nodes: collect, confirm, action, conversation, end.

A business describes a flow as a list of steps and `StepFlow` compiles it
into a `Node` table, so one turn engine, budget, barge-in handling and
Pydantic graph run every application. What each kind does:

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
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
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
from pydantic_ai.messages import ModelMessage, RetryPromptPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage, UsageLimits

from core.completion import FieldsPresent, ReceiptMatched
from core.flow import Nodes
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


@dataclass(frozen=True)
class StandingOffer:
    """One choice's offer to this caller: the ids that can be picked and the application's words for them.

    One record, so nothing can take the ids and leave the words: what a
    rendered step says about an offer is exactly what can still be picked
    from it (session 4f: billing resolutions said after the category became
    access). `turn` and `step` say when and by whom it was made; a refreshed
    offer nobody asked to hear has `turn` 0 and is never announced.
    """

    ids: Mapping[str, str]
    say: str = ''
    turn: int = 0
    step: str = ''
    announced: bool = False


class Offers(Mapping[str, Mapping[str, str]]):
    """Every choice's standing offer on one call, read as `{choice: {id: words}}`: only these ids can be picked."""

    def __init__(self) -> None:
        self._by_choice: dict[str, StandingOffer] = {}

    def __getitem__(self, name: str) -> Mapping[str, str]:
        return self._by_choice[name].ids

    def __iter__(self):
        return iter(self._by_choice)

    def __len__(self) -> int:
        return len(self._by_choice)

    def put(self, name: str, ids: Mapping[str, str], say: str = '', turn: int = 0, step: str = '') -> None:
        """Offer these ids for this choice, replacing whatever was on offer for it."""
        self._by_choice[name] = StandingOffer(dict(ids), say, turn, step)

    def take(self, *names: str) -> None:
        """Take these choices' offers away, ids and words together."""
        for name in names:
            self._by_choice.pop(name, None)

    def words(self, step: str, choices: Sequence[str], turn: int) -> str:
        """What `step` offered on `turn` and has not said yet, in the order it lists its choices."""
        return ' '.join(o.say for _, o in self._made(step, choices, turn) if o.say)

    def announce(self, step: str, choices: Sequence[str], turn: int) -> str:
        """Those words, now said: the same offer is not said twice in one turn."""
        said = self.words(step, choices, turn)
        for name, offer in self._made(step, choices, turn):
            self._by_choice[name] = replace(offer, announced=True)
        return said

    def _made(self, step: str, choices: Sequence[str], turn: int) -> list[tuple[str, StandingOffer]]:
        found = [(name, self._by_choice.get(name)) for name in choices]
        return [
            (name, o)
            for name, o in found
            if o is not None and o.step == step and o.turn == turn and not o.announced
        ]


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
    _offers: Offers = PrivateAttr(default_factory=Offers)
    """For each choice field, the ids offered to this caller, how each was said, and the offer's own words."""
    _refused_on: int = PrivateAttr(default=0)
    """The turn on which a rendered step's operation met an authoritative no: `wait` says so if nothing else."""
    _locale: Locale | None = PrivateAttr(default=None)
    _locales: Mapping[str, Locale] = PrivateAttr(default_factory=dict)
    """The manifest's `locales`, by the value of the state attribute it names (`_language`)."""
    _language: str = PrivateAttr(default='')
    _version: int = PrivateAttr(default=0)
    """Bumped by every accepted change to the application state: work begun on an older version is stale."""
    _derived: dict[str, frozenset[str]] = PrivateAttr(default_factory=dict)
    """State fields a receipt sets (`assign`), each with the state fields any operation setting it binds."""
    _assigned_by: dict[str, frozenset[str]] = PrivateAttr(default_factory=dict)
    """Each state field a receipt of this call set, with the state fields that receipt's arguments came from.

    What the value rests on is the request that produced it, not every
    operation that could have: a ticket's revision from a look-up rests on
    the account and ticket, not on the resolution a later resolve would
    send (session 4f: a new resolution wiped the looked-up revision, and
    the look-up, already done, would not run again).
    """
    _choices: frozenset[str] = PrivateAttr(default=frozenset())
    """State fields picked from offered ids (`choices`): valid only while the request they were offered for stands."""
    _choice_after: dict[str, frozenset[str]] = PrivateAttr(default_factory=dict)
    """The fields a choice's offer rests on, when its manifest says (`after:`); the others rest on every field."""
    _resource_choices: dict[str, dict[str, Callable[[Any, Any], Any]]] = PrivateAttr(default_factory=dict)
    """Choice fields that name a resource, by its kind, with the choice function that offers them (`StepFlow`)."""
    _recorded: frozenset[str] = PrivateAttr(default=frozenset())
    """The fields this caller turn's latest `collect` recorded: what a step's `reads_back` rule is keyed on.

    Cleared when each piece of a caller turn starts (`turn_started`): a slot
    recorded last turn is not this reply's, so after a no it does not read
    the same booking back again (session 4f, scripted case K7).
    """
    _goodbye_checked: int = PrivateAttr(default=0)
    """The turn on which a goodbye beside a waiting read-back was sent back once, to carry out a yes first."""
    _reconcile_with: dict[str, tuple[Any, type[BaseModel]]] = PrivateAttr(default_factory=dict)
    """Each declared operation and its argument model, by name: what a restored intent is reconciled with."""
    _asked_owed: set[str] = PrivateAttr(default_factory=set)
    """The identities whose owed intents the services were already asked for (`take_owed`)."""

    def turn_started(self, heard: str = '') -> None:
        """A piece of a caller turn starts: nothing is recorded by this reply yet."""
        super().turn_started(heard)
        self._recorded = frozenset()

    @property
    def locale(self) -> Locale:
        """The locale for the language the call is in now (`language:`), else the flow file's `locale:`, else the class default.

        Read on every line, so a caller who switches language hears the
        engine's next fixed line in the new one.
        """
        if self._language and (
            found := self._locales.get(str(getattr(self.state.facts, self._language, '')))
        ):
            return found
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
        self.commit(facts=self.validated(facts.model_dump() | deepcopy(fields)))
        if self.state.facts is not facts:
            self._version += 1
            self.take_owed()

    def validation_context(self) -> dict[str, Any]:
        """The call's clock, the application state as it stands before this change (`before`), and `heard`.

        `before` is the committed state, which is replaced whole and never
        changed in place: a validator can read what the caller was talking
        about (a day under discussion) without any service.

        `heard` is what the caller said this turn, every piece of it: a
        validator can refuse a new value the caller never said (a country
        code a model added). The whole state is validated on every change,
        so such a check compares with `before` and leaves an unchanged value
        alone.
        """
        return super().validation_context() | {'before': self.state.facts, 'heard': self.heard}

    def validated(self, values: Mapping[str, Any]) -> Any:
        """The application's model built from these values, validated with this call's context, or `ValidationError`."""
        return type(self.state.facts).model_validate(
            values, extra='forbid', context=self.validation_context()
        )

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
        """Whether a change this caller turn made still stands (`effective`). Evidence, never a flag.

        A lookup changed nothing, a refusal did nothing, and a change undone
        since stands no more: none of them backs a claim that something was
        done (session 4d, defect 6; before, any receipt this turn did).
        """
        return any(filed.turn == self.turn for filed in self.effective())

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

        A repair is said only to the caller it was done for, as `_before`
        does: after a number correction, another caller's receipt must not be
        assigned into this caller's state.

        ponytail: an intent that stays unknown is asked about once per caller
        turn, for the rest of the call. A backoff belongs here if a backend
        ever minds.
        """
        pending = [
            (operation, args, self._intents[intent_id])
            for intent_id, (operation, args) in self._unknown.items()
            if operation.reconcile
        ]
        repairs = [
            repair
            for intent_id, repair in self._unpresented.items()
            if self._intents[intent_id].subject == self.identity()
        ]
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

    def collect(self, offered: Collection[str] = (), /, **fields: Any) -> list[str]:
        """What the caller said, as a model heard it: only fields a collect step asks for.

        Anything else (verification, approval, a receipt) is refused before it
        is looked at, whatever shape it has: a well-formed "verified" value
        from a model is still the model's word.

        A change takes what rested on the old value with it, in the same
        candidate: a fact a receipt set from these arguments (a corrected phone
        number is no longer the verified one), and each offer that rested on a
        changed field, with the pick made from it. "Actually, make it
        Saturday" leaves no Friday slot behind for an old yes to book.

        A change is a change of meaning: the values are validated first, with
        the call's context, and compared as the application's model holds
        them. "Thursday" recorded again as the date it already stands for is
        no change, and takes nothing away (session 4d, live call 5).

        What an offer rests on is its manifest's `after:` list, which may name
        another choice; without one, every field but the choices and the
        identity. An offer taken away counts as a change, so what rested on
        it goes too: a new ticket takes the category offered for it, and that
        takes the resolution offered for the category. A new day takes the
        times away and keeps the appointment being moved, which rests on no
        field (session 4d, defect 4). A known identity changed to another
        takes every offer and pick away: they were made to someone else.
        Naming the caller for the first time is not a change of caller.

        A pick counts only against an offer made for the state this update
        ends in. For the choice fields named in `offered`, a new pick must be
        on offer now, and a pick whose offer this same update takes away came
        from an offer that no longer stands (session 4e: "access, with a
        refund", said about the billing resolutions). A value the state
        already holds is not a new pick. Refused picks are left out and their
        names returned; the rest is recorded. Nothing changes when the
        candidate is not a valid state (`ValidationError`).
        """
        if refused := set(fields) - self._collectible:
            raise ValueError(f'not collected from the caller: {", ".join(sorted(refused))}')
        facts = self.state.facts
        default = lambda name: type(facts).model_fields[name].get_default(call_default_factory=True)  # noqa: E731
        unoffered = _not_on_offer(self, offered, fields)
        kept = {name: value for name, value in fields.items() if name not in unoffered}
        candidate = self.validated(facts.model_dump() | deepcopy(kept))
        changed = {name for name in kept if getattr(facts, name) != getattr(candidate, name)}
        someone_else = bool(self.identity()) and self._identity_field in changed
        plain = changed - self._choices - {self._identity_field}
        stale: set[str] = set()
        while True:
            moved = changed | stale
            found = {
                name
                for name in self._choices
                if someone_else
                or ((moved - {name}) & self._choice_after[name] if name in self._choice_after else plain)
            }
            if found <= stale:
                break
            stale |= found
        late = [name for name in offered if name in stale and kept.get(name, default(name)) != default(name)]
        unoffered += late
        kept = {name: value for name, value in kept.items() if name not in late}
        picks = {name: default(name) for name in stale if name not in kept}
        changed |= {name for name, value in picks.items() if getattr(facts, name) != value}
        resets = {
            name: default(name)
            for name, sources in (self._derived | self._assigned_by).items()
            if sources & changed and name not in kept
        }
        self.update(**kept, **picks, **resets)
        self._offers.take(*stale)
        return unoffered


@dataclass(frozen=True)
class Prompted:
    """A question this code worded: recorded as the pending question, then said by the turn engine."""

    about: str
    spoken: str
    value: str = ''

    def put(self, call: CallContext) -> str:
        """Record the question and return its words; or, for an intent past its read-back limit, take it back.

        An operation's read-back names its intent, so every copy of it counts
        against that intent (`CallContext.note_unheard_readbacks`), across the
        caller's joined pieces and reissued questions alike. Copies with no
        evidence on a line that proves nothing count against `READBACKS`;
        copies the caller talked over on a line that works count against
        `CUT_READBACKS`. Past the limit the intent is withdrawn
        (`CallContext.withdraw`): terminal, never dispatched, nothing left
        pending, so the caller hears `Locale.gave_up` once, which is true.
        Asked for again (the caller confirms again, an arrival reads it back),
        the same arguments find the same withdrawn intent, and the caller
        hears `Locale.withdrawn`: no fresh read-back, no fresh allowance, no
        dispatch. A corrected value is different arguments: a new intent, read
        back with its own allowance.
        """
        intent = call.open_intent(self.about, self.value)
        if intent is not None and intent.status == 'withdrawn':
            call.note_confirmation('blocked', self.about, 'withdrawn')
            return call.locale.withdrawn
        if (
            intent is not None
            and intent.status == 'prepared'
            and (intent.readbacks >= READBACKS or intent.cut_readbacks >= CUT_READBACKS)
        ):
            logger.info(
                '{} read back unheard {} times before any delivery was proven, {} times after: withdrawn',
                self.about,
                intent.readbacks,
                intent.cut_readbacks,
            )
            call.withdraw(intent.id)
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


def _blocked(call: CallContext, proposal: Proposal, reason: str) -> None:
    """Audit a reply that tried to agree to this read-back and could not (`CallContext.note_confirmation`).

    Only when the caller was answering this same question: the first
    read-back of a value is not a blocked confirmation. `reason` defaults to
    the hearing of its last copy.
    """
    last = call.last_proposal()
    if last is None or last[0].about != proposal.about or last[0].value != proposal.value:
        return
    call.note_confirmation('blocked', proposal.about, reason or call.last_hearing() or 'unconfirmed')


_HEARING: dict[str, str] = {
    'heard': 'They heard it in full.',
    'early': 'They began answering before it finished playing, so their reply cannot agree to it: put it to them again.',
    'cut': 'It was cut off before they heard all of it, so their reply cannot agree to it: put it to them again.',
    'failed': 'Its audio failed to play, so their reply cannot agree to it: put it to them again.',
    'unconfirmed': (
        'This line has not confirmed that it finished playing. That is missing evidence, not a sign they '
        'missed it, but their reply cannot agree to it yet: put it to them again.'
    ),
}
"""What the model is told about the question the caller is answering, by `CallContext.hearing`. Prompt text."""

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
        """Commit a success; for a refusal, tell the model and take back the picks it was built from.

        A refused change leaves the caller's choices as they were shown, and
        the service has just said they do not work together (a time taken
        since, a booking changed since it was shown). Offering them again,
        fresh, is the way on; reading the same pick back again would earn
        the same no (session 4d, defect 3).

        A success takes back the picks it used, and then asks the
        application's own choice function again for any choice that names a
        resource of the kind this change touched (`StepFlow.resource_choices`):
        the booking just made or moved is on offer again, quietly, so moving
        or cancelling it next needs no second lookup by the model (session
        4f, trace e67c9034: `collect(appointment="BK0002")` refused right
        after BK0002 was booked). The domain decides what stands: a
        cancelled booking is not offered back. Other choices, the
        alternatives, need a fresh search.

        ponytail: every choice the operation binds is taken back. Per-choice
        refusal reasons belong here if a flow needs to keep the others.
        """
        data = receipt.model_dump()
        if not self.succeeded(data):
            spent = sorted(set(self.binds.values()) & call._choices)
            if spent:
                facts = type(call.state.facts).model_fields
                call.update(**{name: facts[name].get_default(call_default_factory=True) for name in spent})
                call._offers.take(*spent)
            again = (
                f' {", ".join(spent)} {"is" if len(spent) == 1 else "are"} no longer on offer: get a fresh '
                'offer (`options`, or `wait` on a step whose application says it), and record what the caller picks.'
                if spent
                else ''
            )
            raise ModelRetry(
                f'Nothing was done: the {self.name} service answered {data}. Do not say it was done.{again}'
            )
        if self.assign:
            call.update(**{field: data[source] for field, source in self.assign.items()})
            call._assigned_by.update(dict.fromkeys(self.assign, frozenset(self.binds.values())))
        call._offers.take(*self.binds.values())
        if self.changes and (named := call._resource_choices.get(self.resource or self.subject)):
            await _offer(call, named)
        if words := self.say.format_map(_Facts(data)).strip():
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


def _agent(
    flow: StepFlow, step: Step, instructions: Any, outputs: list, hooks: Hooks | None = None, **agent: Any
) -> Agent[Any, object]:
    """A step's agent, from its persona's native `AgentSpec`: its model unless the flow's was given explicitly.

    `FlowSpec.compile(model=...)` strips each persona's model, so an explicit
    model is used everywhere; otherwise a persona's own model wins over the
    flow's default. Settings and instructions merge, as `Agent.from_spec` does.

    Every step's agent times each provider request (`request_clock`), which is
    where a turn's time to first token comes from; the hand-built steps had it
    from `AgentBuilder`, and session 4 found the public steps without it.

    All of core's hooks for the step go on one `Hooks` (`hooks`, with the
    clock added), each on its own event, so the handlers and their order are
    those of separate capabilities: one capability is dispatched once, where
    four cost the SDK four dispatches per hook point on every request.
    """
    spec = step.spec or AgentSpec()
    capabilities = [request_clock(into=hooks)]
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
    offered = {name: dict(ids) for name, ids in call._offers.items() if ids}
    lines.append(
        f'On offer to the caller now, by id: {json.dumps(offered)}. Only these ids can be picked.'
        if offered
        else 'Nothing is on offer to the caller now.'
    )
    last = call.last_proposal()
    if last is not None:
        question, _ = last
        lines.append(
            f'The caller is answering: "{question.spoken}" ({question.about}). '
            + _HEARING[call.last_hearing() or 'unconfirmed']
        )
    for intent in call.intents:
        if intent.status == 'withdrawn' and intent.subject == call.identity():
            lines.append(
                f'Withdrawn: {intent.operation} {intent.args}. Its read-back could not be confirmed on this line, '
                'and the caller was told. It is not read back or carried out again on this call: do not offer it.'
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
    if standing := flow.recaps(call):
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

    The ids go on the call as offered (`FlowCall._offers`) and the question's
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
            call._offers.put(name, offered)
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
        return _node(flow, self, agent, openings=(Opening('ask', _entered, self.arrive(flow)),))

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
            if any(name in step.choices for step in listening) and str(value) not in call._offers.get(
                name, {}
            ):
                offered = ', '.join(call._offers.get(name, {})) or 'none yet'
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
            openings=(Opening('readback', _entered, read_back),),
            model_speech=False,
        )


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
            openings=(Opening(self.operation.name, _entered, act),),
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


def _refused(messages: list[ModelMessage], fields: Mapping[str, Any]) -> tuple[str, str] | None:
    """The field and value of this caller turn's latest `collect`, if it was refused, among `fields`.

    Read from the run's own messages: the retry the SDK sent back for that
    call, and the call's arguments. A later `collect` that was recorded
    supersedes it, and the caller's words mark where this turn began.

    The arguments come in either shape the SDK accepts for a tool whose one
    parameter is a model: flat, as a live model sends them, or under the
    parameter's name. Session 4g: only the second was read, so live calls
    never heard a refusal (trace 28723e6b, 07:24:16).
    """
    if not fields:
        return None
    asked = {
        part.tool_call_id: part.args_as_dict()
        for message in messages
        for part in message.parts
        if isinstance(part, ToolCallPart) and part.tool_name == 'collect'
    }
    for message in reversed(messages):
        for part in reversed(message.parts):
            if isinstance(part, UserPromptPart) or (
                isinstance(part, ToolReturnPart) and part.tool_name == 'collect'
            ):
                return None
            if isinstance(part, RetryPromptPart) and part.tool_name == 'collect':
                sent = asked.get(part.tool_call_id, {})
                values = sent.get('values', sent) or {}
                for name in fields:
                    if isinstance(values, dict) and values.get(name):
                        return name, str(values[name])
    return None


def _not_ready(
    operation: Operation, call: FlowCall[Any], error: ValidationError, rendered: bool = False
) -> str:
    """What an operation still lacks, told as what to do next: a pick comes from an offer, anything else from the caller.

    Two kinds, kept apart because they are fixed differently (session 4d,
    trace 5edecd4c: "slot missing. Ask the caller" twice, then an exhausted
    turn, when the times for the new day had never been offered). A rendered
    step has no `options`: the offer is in the application state, and `wait`
    tells the caller what it is.
    """
    binds = getattr(operation, 'binds', None) or {}
    missing = [binds.get(str(e['loc'][0]), str(e['loc'][0])) for e in error.errors() if e['loc']]
    picks = [name for name in missing if name in call._choices]
    asks = [name for name in missing if name not in call._choices]
    said = []
    if picks:
        said.append(
            f'no {" or ".join(picks)} picked yet: record the id they pick from what is on offer, or call wait '
            'to tell them what is on offer'
            if rendered
            else f'no {" or ".join(picks)} picked yet: call options for what the caller asked, then record the id they pick'
        )
    if asks:
        said.append(f'{", ".join(asks)} missing: ask the caller and record it')
    return f'Not yet: {"; ".join(said)}.'


def _not_on_offer(call: FlowCall[Any], choices: Collection[str], values: Mapping[str, Any]) -> list[str]:
    """The choice fields among `values` whose value is not on offer to this caller now.

    A value the state already holds is not a new pick: it was recorded from an
    offer, and still stands, so recording it again changes nothing (a move
    spends the offer of the booking it moved, and the cancel that follows
    names the same booking). The service checks ownership and revision when
    it acts on it, whatever was offered (session 4d, trace ce40c124). A
    field's default is no pick either: it takes a pick back.
    """
    fields = type(call.state.facts).model_fields
    return [
        name
        for name, value in values.items()
        if name in choices
        and value != fields[name].get_default(call_default_factory=True)
        and getattr(call.state.facts, name, None) != value
        and str(value) not in call._offers.get(name, {})
    ]


SPEAK_BY = 3
"""The request by which an agent node's run has no ordinary tools left and answers with what it has (`speak_by`)."""


class Options(BaseModel):
    """What a choice function returns, as core keeps it: the options by id, and a note for the agent.

    A choice function returns a plain `{id: words}`, or any object with
    `options` and `note` (the application's own model; it need not import
    this). The note is how the domain says something the ids cannot, such as
    a day that could mean two dates: the agent reads it and asks the caller,
    and only the ids are ever offered.

    On a rendered step the object also carries `say`: what the caller hears
    about this offer, in the application's words, including when nothing is
    free or the day could mean two dates. Core never words an offer. ''
    says nothing for this choice.
    """

    model_config = ConfigDict(extra='forbid')
    options: dict[str, str] = {}
    note: str = ''
    say: str = ''


async def _offer(
    call: FlowCall[Any], choices: Mapping[str, Callable[[Any, Any], Any]], rendered: bool = False
) -> dict[str, Any]:
    """Ask each choice function for this caller's options, and record them as offered, unless the call moved on.

    A choice function may be sync or async, and may take long: the caller can
    change what they asked for, or be handed elsewhere, before it answers. A
    plain one runs in a worker thread, like a domain function (`_call`), so a
    database or HTTP read never holds the event loop that carries audio. An
    answer computed for a state that is no longer the call's, or in a turn the
    caller has left, is returned as stale and offers nothing, so it can never
    replace the current offer or make an old slot pickable.

    On a `rendered` step every answer must carry `say`, the application's
    words for it: one without is a fault in the application, and offers
    nothing. The words are kept with the ids (`Offers`), so whatever takes
    the offer takes its words too, and `wait` says them, in the order the
    step lists its choices, only on the step and turn that made them. The agent is still shown the ids and notes, to read
    the caller's pick.
    """
    version, generation, agent = call._version, call._generation, call.state.agent
    found: dict[str, Options] = {}
    for name, choose in choices.items():
        try:
            if inspect.iscoroutinefunction(choose):
                answer = await choose(call.services, call.state.facts)
            else:
                answer = await _result(await asyncio.to_thread(choose, call.services, call.state.facts))
            if rendered and not hasattr(answer, 'say'):
                raise TypeError(
                    f'the choice function for {name} returned no words to say, on a rendered step'
                )
        except Exception as error:
            logger.exception('choice function for {} failed', name)
            return {
                'unavailable': f'The options could not be looked up just now ({type(error).__name__}). Nothing is offered.'
            }
        found[name] = (
            Options(options=dict(answer))
            if isinstance(answer, Mapping)
            else Options(
                options=dict(answer.options), note=getattr(answer, 'note', ''), say=getattr(answer, 'say', '')
            )
        )
    if call._version != version or call._generation != generation or call.state.agent != agent:
        return {'stale': 'The request changed while these were looked up. Nothing is offered; look again.'}
    result: dict[str, Any] = {}
    for name, options in found.items():
        call._offers.put(
            name, options.options, options.say, call.turn if rendered else 0, agent if rendered else ''
        )
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
) -> tuple[str | None, BaseModel | None]:
    """Run an approved operation; return what the caller heard and its receipt. An unknown outcome is said as unknown.

    An authoritative no goes back to the step's agent with the refusal, to
    choose what comes next (look again, ask). On a rendered step the agent
    has no words of its own: its `wait` says the application's fresh offer,
    or `Locale.withheld` when there is nothing else to say (`_refused_on`).
    """
    before = {i.id for i in call.intents if i.status == 'rejected'}
    try:
        receipt = await operation.run(call, _args(operation, call), usage=usage, usage_limits=limits)
    except OutcomeUnknownError:
        await call.speak_now(call.locale.unsettled, origin='confirmation')
        return call.locale.unsettled, None
    except ModelRetry:
        refused = {i.id for i in call.intents if i.status == 'rejected' and i.operation == operation.name}
        if rendered and refused - before:
            call._refused_on = call.turn
        raise
    if not isinstance(receipt, BaseModel):
        return None, None
    say = getattr(operation, 'say', '')
    return (say.format_map(_Facts(receipt.model_dump())) if say else None), receipt


@dataclass(frozen=True, kw_only=True)
class ConversationStep(Step):
    """An agent node: one agent, its instructions, what it may record, offer, carry out and hand over to.

    The step stays active across turns until it hands the caller on or they
    say goodbye. Its agent reads every reply itself, in the shared
    conversation and the application state (`context`), and answers through
    its outputs: text, `goodbye`, one of its declared `operations`, or
    `hand_over` to one of its `exits`. `fields` it may record from the caller
    (`collect`) and `choices` it may offer (`options`) are ordinary tools, and
    their results come back to this same agent in the same run. `collect`
    makes each offer that no longer stands again, for the state it leaves,
    and returns it (`_record`), so a rendered step, whose offers code says,
    has no `options` at all: in the session 4g traces it was only ever a
    request between `collect` and `wait` or an operation.

    An operation is read back in the application's words and runs only when
    the agent calls it again on the turn after that read-back reached the
    caller in full: the agent's yes, bound by code to that exact question
    (`CallContext.approve`), through the same `Operation.run` a controlled
    flow uses. No second model reads the reply. A reply that also changes the
    request (`collect` beside the operation) gets a fresh read-back instead.
    A reply the agent reads as a no calls `decline` (`CallContext.decline`):
    the question is spent, and a goodbye does not tell the caller the change
    was left undone. A question in reply is not a no, and leaves it waiting
    (session 4f). `decline` is a function tool, so under Pydantic AI's
    `graceful` end strategy it runs beside `wait` or `goodbye` in one response.

    A step whose operations say their receipts also has `say_again`: when the
    caller asks to hear what was done, code says the newest receipt that
    still stands, in the application's words (`StepFlow.receipt_words`). It
    only speaks: nothing is sent, no intent is made, no delivery is assumed,
    and the automatic repeat of an unheard receipt keeps its own limit
    (`RECEIPT_REPEATS`). Session 4e found a rendered step answering "say that
    again?" with `pardon`.

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
    choice_after: Mapping[str, frozenset[str]] = field(default_factory=dict)
    """The fields each choice's offer rests on, where the manifest says (`after:`). See `FlowCall.collect`."""
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
    `hand_over`, `goodbye`, and `wait`, after which code says, in this
    order: a read-back the reply calls for; the application's words for a
    refused value, then for the offer made this turn (a choice function's
    `say`); `Locale.withheld` after an authoritative no this turn
    (`_carry_out`); the first missing `asks:` field; with none of these and
    choices on the step, the offer as it stands, looked up by code; or
    `Locale.pardon`. Session 4e, live call s4e-latency-1: booking's agent
    waited on arrival from verify without looking at the times, and the
    caller heard "Sorry, could you say that again?" after "Thanks, I have
    you." What there is to offer is code's to look up; no model is asked.
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
    refusals: Mapping[str, Callable[[str], str]] = field(default_factory=dict)
    """Field -> the application's words for a value `collect` refused, said by `wait` on a rendered step."""
    routes: tuple[str, ...] = ()
    """Steps a receipt of one of its operations can move the caller to (`route:`): code's moves, never `hand_over`'s."""
    carries: tuple[tuple[Operation, tuple[str, ...]], ...] = ()
    """Operations code carries out after a reply that only records one of their fields (`carries_out`), never the model."""
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
        return tuple(
            dict.fromkeys([*self.exits, *self.routes, *([self.satisfied_by] if self.satisfied_by else [])])
        )

    def opening(self) -> tuple[Opening[Any], ...]:
        """On arrival by a hand-over: read back `opens_with` if the state gives its arguments, else ask (`asks`). No model."""
        return (*self._read_back_first(), *self._ask_first())

    def first_line(self, call: FlowCall[Any]) -> Prompted | str:
        """What this step would open with on arrival, as words to put after a greeting: the same two rules as `opening`.

        The call's first arrival is its entry step, with no hand-over, so
        this is how a caller ID read back in the greeting is the question the
        first reply answers (`StepFlow.greeting`). '' when neither applies.
        """
        if (operation := self.opens_with) is not None:
            try:
                args = _args(operation, call)
            except ValidationError:
                args = None
            if args is not None:
                proposal = operation.proposal_for(call, args)
                return Prompted(about=proposal.about, spoken=proposal.spoken, value=proposal.value)
        missing = next((name for name in self.asks if not getattr(call.state.facts, name)), '')
        return self.asks[missing] if missing else ''

    def _ask_first(self) -> tuple[Opening[Any], ...]:
        if not self.asks:
            return ()

        def missing(call: FlowCall[Any]) -> str:
            return next((name for name in self.asks if not getattr(call.state.facts, name)), '')

        async def ask(call: FlowCall[Any]) -> str:
            words = self.asks[missing(call)]
            await call.speak_now(words, origin='ask')
            return words

        return (Opening('ask', lambda call: bool(missing(call)), ask),)

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

        return (Opening(operation.name, ready, read_back),)

    def _reads_back(self, flow: StepFlow) -> Callable[[FlowCall[Any]], Prompted | Restated | None] | None:
        """The `reads_back` rule: the operation whose field was just recorded, when it is the only one that could run.

        An operation could run when its arguments are complete, or when the
        ones missing are choices on offer (a held booking the caller could
        name). So where a new booking and a move of a held one are both
        possible, which one the caller means is the model's to read, as
        before. When that exact question is already waiting, heard in full,
        the caller has said its value again: `Restated`, carried out by code,
        only for an operation whose manifest says so (`restated: confirms`,
        a lookup). For any other, recording the same value is only a record:
        None, and the step's own agent reads the reply (session 4d, defect 1).
        None too when something it rests on is still changing.

        `carries_out` comes first: an operation with no read-back whose
        fields this reply recorded, with every argument there, is carried
        out by code (`Restated`, run as the agent's own call would be). The
        model never calls it, so it runs only on what this reply recorded.

        A waiting question whose words the application now renders
        differently for the same value (the caller asked for another
        language, and the read-back follows it) is put again in the new
        words: the same question, a new copy, so the next reply answers
        words they heard. A model paraphrase in its place would be a
        different sentence from the one a yes is bound to.
        """
        if not self.reads_back and not self.carries:
            return None

        def possible(call: FlowCall[Any], operation: Operation) -> bool:
            try:
                _args(operation, call)
            except ValidationError as error:
                binds = getattr(operation, 'binds', None) or {}
                missing = {binds.get(str(e['loc'][0]), str(e['loc'][0])) for e in error.errors() if e['loc']}
                return all(call._offers.get(name) for name in missing)
            return True

        def read(call: FlowCall[Any]) -> Prompted | Restated | None:
            for operation, fields in self.carries:
                if not call._recorded & set(fields):
                    continue
                try:
                    _args(operation, call)
                except ValidationError:
                    continue
                return Restated(
                    about=operation.name,
                    run=lambda usage, limits, operation=operation: self._approved(
                        flow, operation, call, usage, limits
                    ),
                )
            if not self.reads_back:
                return None
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
                if (pending := call.pending(operation.name, value=proposal.value)) is not None:
                    if not str(pending.spoken).endswith(proposal.spoken):
                        return Prompted(about=proposal.about, spoken=proposal.spoken, value=proposal.value)
                    if not operation.confirms_by_restating or any(
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

    async def _record(
        self, flow: StepFlow, call: FlowCall[Any], asked: Mapping[str, Any]
    ) -> tuple[list[str], dict[str, Any]]:
        """Record the caller's values; each pick counts on the offer for the state the update ends in, made by code.

        What the caller asked for (a service, a day, a category) and what they
        picked from an offer (a slot, a resolution) often come in one reply:
        "let's do 9:00", "access: unlock my account". The pick rests on what
        was asked for, so an update that changes one takes the old offer away
        (`FlowCall.collect`), and the pick came from an offer that no longer
        stands. Session 4g, trace 28723e6b: `booking_time` and `slot` sent
        together recorded the time and refused the slot, the caller's own
        choice; it cost three requests and the caller said it again.

        So the update is taken in two moves, each one whole, validated
        candidate. First everything but the refused picks. Then each of this
        step's choices with no standing offer, or with a pick refused, is
        offered again by its own function, for the state as it now stands, as
        `options` would have: the application decides what is eligible, no
        model does. A refused pick on that offer is recorded; one that is not
        stays refused, and on a rendered step `wait` says the refusal, then
        this fresh offer. In dependency order, so a pick that a pick rests on
        is taken first (a ticket, then a category, then a resolution).

        Nothing old is accepted for looking familiar: the pick must be on an
        offer the choice function has just made for this state and this
        caller. A stale id, one offered to another call, one nobody offered,
        and one wrong for the new request are all refused. A pick is still
        not a yes: every operation reads back and waits for a heard one.

        Returns the picks still refused, and the offers made here, which the
        agent is shown with its result: what it needs to record a pick, with
        no request for `options`.
        """
        refused = flow.record(call, asked, self.choices)
        made: dict[str, Any] = {}
        for name, choose in self.choices.items():
            if name not in call._offers or name in refused:
                found = await _offer(call, {name: choose}, rendered=self.rendered)
                if 'stale' in found or 'unavailable' in found:
                    refused += [
                        field for field in _not_on_offer(call, self.choices, asked) if field not in refused
                    ]
                    return refused, made | found
                made |= {
                    name: shown for name, shown in found.items() if shown.get('options') or shown.get('note')
                }
            if name in asked:
                if flow.record(call, {name: asked[name]}, self.choices):
                    if name not in refused:
                        refused.append(name)
                elif name in refused:
                    refused.remove(name)
        return refused, made

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
        """Run an operation whose approval is bound, then move the caller if its receipt or a waiting step says so.

        A receipt that names another step (`route:`) moves the caller there,
        by code: its words were just said, so the turn ends there unless
        that step opens with something of its own. The receipt is the
        application's record of where the caller stands, so it wins over a
        prerequisite's return. Otherwise a prerequisite hands back to the
        step waiting on it (`return_after`).
        """
        said, receipt = await _carry_out(call, operation, usage, limits, rendered=self.rendered)
        to = str(getattr(receipt, operation.route, '')) if receipt is not None and operation.route else ''
        if to and to != self.id:
            if to not in self.routes:
                raise ValueError(
                    f'{operation.name} routed the caller to {to!r}, which {self.id} does not route to'
                )
            return Handoff(to=to, because=f'{operation.name} says so', ends_turn=True)
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
                    'it, carry it out now. '
                    + ('If their words turned it down, call decline as well. ' if confirms else '')
                    + 'If they are leaving, call goodbye again.'
                )
            words = flow.goodbye_words(call, recap or [])
            call.note_departure()
            call.end_requested('caller')
            await call.speak_now(words, origin='goodbye')
            await call.end_call()
            return words

        async def wait(ctx: RunContext[FlowCall[Any]]) -> str | Prompted:
            """Nothing to read back, carry out or hand over yet. The application tells the caller what is on offer, or asks for what it still needs."""
            call = ctx.deps
            if read_back is not None and isinstance(asked := read_back(call), Prompted):
                return asked
            missing = next((name for name in self.asks if not getattr(call.state.facts, name)), '')
            refused = _refused(ctx.messages, self.refusals)
            offer = call._offers.announce(self.id, tuple(self.choices), call.turn)
            words = (
                ' '.join(
                    filter(
                        None,
                        [
                            self.refusals[refused[0]](refused[1]) if refused else '',
                            offer,
                        ],
                    )
                )
                or (call.correction(call.locale.withheld) if call._refused_on == call.turn else '')
                or (self.asks[missing] if missing else '')
            )
            answered = call.receipt_said_again()
            if not words and self.choices and not answered:
                await _offer(call, self.choices, rendered=True)
                words = call._offers.announce(self.id, tuple(self.choices), call.turn)
            if call.declined_this_turn():
                words = ' '.join(filter(None, [call.locale.declined, words]))
            words = words or ('' if answered else call.locale.pardon)
            if words:
                await call.speak_now(words, origin='ask')
            return words

        async def say_again(ctx: RunContext[FlowCall[Any]]) -> str | Prompted:
            """The caller asked to hear again what was done for them. The application says it again."""
            call = ctx.deps
            words = flow.receipt_words(call)
            if words and call.receipt_said_again():
                return ''
            if not words:
                if self.rendered:
                    return await wait(ctx)
                raise ModelRetry(
                    'Nothing was said: nothing done on this call still stands. Answer the caller.'
                )
            await call.speak_now(words, origin='confirmation')
            return words

        read_back = self._reads_back(flow)
        confirms = any(op.needs_approval for op in self.operations)
        actions = frozenset(op.name for op in self.operations) | (
            {'hand_over'} if self.exits else frozenset()
        )
        outputs: list[Any] = [
            *([] if self.rendered else [str]),
            ToolOutput(goodbye, name='goodbye'),
            *([ToolOutput(wait, name='wait')] if self.rendered else []),
            *(
                [ToolOutput(say_again, name='say_again')]
                if any(op.changes and getattr(op, 'say', '') for op in self.operations)
                else []
            ),
        ]
        tools: list[Any] = []
        if self.fields:
            values_type = _values(flow, f'{self.id.title()}Values', self.fields)

            async def collect(ctx: RunContext[FlowCall[Any]], values: BaseModel) -> str:
                """Record exactly what the caller has just said for any of these fields. Never guess a value."""
                asked = values.model_dump(exclude_none=True)
                call = ctx.deps
                before = {name: getattr(call.state.facts, name, None) for name in asked}
                for name in self.quoted:
                    if asked.get(name) and not said_by_caller(call, str(asked[name])):
                        raise ModelRetry(
                            f"Nothing was recorded: {name} must be the caller's own words, copied from what "
                            'they said. Punctuation and case may differ; nothing else may.'
                        )
                try:
                    unoffered, made = await self._record(flow, call, asked)
                except ValidationError as error:
                    problems = '; '.join(e['msg'] for e in error.errors())
                    raise ModelRetry(f'Nothing was recorded: {problems}. Ask the caller again.') from None
                proposed = {name: value for name, value in asked.items() if name not in unoffered}
                call._kept.update(
                    name for name in proposed if getattr(call.state.facts, name, None) == before[name]
                )
                call._recorded = frozenset(proposed)
                recorded = f'Recorded: {", ".join(sorted(proposed)) or "nothing"}.' + (
                    f' On offer now, by id: {json.dumps(made)}.' if made else ''
                )
                if unoffered:
                    raise ModelRetry(
                        f'{recorded} Not recorded: '
                        + '; '.join(f'{asked[name]!r} is not on offer for {name}' for name in unoffered)
                        + '. Only an id on offer can be recorded.'
                    )
                return recorded

            collect.__annotations__ = {'ctx': RunContext[FlowCall], 'values': values_type, 'return': str}
            tools.append(Tool(collect))
        if self.choices and not self.rendered:

            async def options(ctx: RunContext[FlowCall[Any]]) -> dict[str, Any]:
                """The options you may offer the caller now, by id, for what they asked. Only an offered id can be recorded. Say them in your own words."""
                return await _offer(ctx.deps, self.choices)

            tools.append(Tool(options))
        if confirms:

            async def decline(ctx: RunContext[FlowCall[Any]]) -> str:
                """The caller said no to the read-back they are answering. Nothing is carried out."""
                if ctx.deps.decline():
                    return 'Recorded: they turned it down. Nothing was done.'
                return 'Nothing recorded: no read-back they heard in full is waiting for an answer.'

            tools.append(Tool(decline))
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
        hooks = Hooks(prepare_output_tools=narrow)
        if actions:
            one_action_per_reply(actions, frozenset({'collect'}), into=hooks)
        speak_by(SPEAK_BY, into=hooks)
        agent = _agent(flow, self, text, outputs, hooks=hooks, tools=tools)
        return _node(
            flow,
            self,
            agent,
            requires=self.requires,
            satisfied_by=self.satisfied_by,
            guarded=self.guarded,
            openings=self.opening(),
            speaks_for=frozenset(op.name for op in self.operations),
            reads_back=read_back,
            model_speech=not self.rendered,
        )

    def _operate(self, flow: StepFlow, operation: Operation, actions: frozenset[str]):
        async def operate(ctx: RunContext[FlowCall[Any]]) -> Prompted | Handoff | str | None:
            claim_or_refuse(ctx, actions)
            call = ctx.deps
            try:
                args = _args(operation, call)
            except ValidationError as error:
                raise ModelRetry(_not_ready(operation, call, error, self.rendered)) from None
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
                    _blocked(call, proposal, 'changing' if call._changing & resting_on else '')
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
        return _node(flow, self, agent, openings=(Opening('close', _entered, close),))


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
    repeat, or whose steps run in order (a collect, confirm or action step
    with no `then`) with no `End` with outcome `completed` to finish on, so
    a broken flow fails where it is defined rather than on a call.
    """

    def __init__(
        self,
        steps: Sequence[Step],
        *,
        model: Model | str,
        settings: ModelSettings | None = None,
        state_type: type[BaseModel] = NoFacts,
        identity: str = '',
        entry: str = '',
        services: Callable[[], Any] | None = None,
        locale: Locale | None = None,
        locales: Mapping[str, Locale] | None = None,
        language: str = '',
        version: str = '',
    ) -> None:
        self.state_type = state_type
        """The application's state model: every call's `state.facts`."""
        self.identity = identity
        """The state field that says who the caller is. Empty: nobody in particular."""
        self.call_type = FlowCall[state_type]  # ty: ignore[invalid-type-form]
        """The call every step's agent is given, and what prompt templates are checked against."""
        self.steps = tuple(steps)
        self.by_id = {step.id: step for step in self.steps}
        if len(self.by_id) != len(self.steps):
            raise ValueError('step ids must be unique')
        finished = [s for s in self.steps if isinstance(s, End) and s.outcome == 'completed']
        onward = [
            s for s in self.steps if isinstance(s, CollectStep | ConfirmStep | ActionStep) and not s.then
        ]
        if onward and not finished:
            raise ValueError('a flow whose steps run in order needs an End with outcome completed')
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
            for operation in (
                getattr(step, 'operation', None),
                *getattr(step, 'operations', ()),
                *(carried for carried, _ in getattr(step, 'carries', ())),
            ):
                for assigned in getattr(operation, 'assign', {}):
                    sources = frozenset(getattr(operation, 'binds', {}).values())
                    self.derived[assigned] = self.derived.get(assigned, frozenset()) | sources
        offered_by = {name: fn for step in self.steps for name, fn in getattr(step, 'choices', {}).items()}
        self.resource_choices: dict[str, dict[str, Callable[[Any, Any], Any]]] = {}
        """Choice fields that name a resource, by its kind: an operation binds the field to its `subject` argument.

        Read from what the manifest already declares: move and cancel take
        `booking_id` from `appointment`, and say their receipts are about a
        `booking`, so `appointment` names a booking. After any successful
        change of a booking, that choice is offered again by its own function.
        """
        for operation in self.operations():
            named = getattr(operation, 'binds', {}).get(operation.subject, '')
            if operation.subject and named in offered_by:
                kind = operation.resource or operation.subject
                self.resource_choices.setdefault(kind, {})[named] = offered_by[named]
        self.entry = entry or self.steps[0].id
        if self.entry not in self.by_id:
            raise ValueError(f'the entry step {self.entry} is not a step')
        self.services = services
        """Makes one call's services when `open` is given none: the manifest's `services:` factory."""
        self.locale = locale
        self.locales = dict(locales or {})
        """The fixed lines for each value of the state attribute `language` names: a call's language, per call."""
        self.language = language
        self.version = version
        """The application's own version, from the manifest (`app_version`): what a trace or a report names."""
        self.model = model
        self.settings = settings
        self.nodes: dict[str, Node[Any]] = Nodes({step.id: step.node(self) for step in self.steps})
        """The steps table, owning its graph (`core.flow.Nodes`): built here, once per compile."""

    @property
    def start(self) -> str:
        return self.entry

    def greeting(self, call: FlowCall[Any], words: str = '') -> str | Prompted:
        """The call's opening line: these words, then what the entry step opens with (`ConversationStep.first_line`).

        With a read-back it is a question (`core.greet` puts it on the call),
        so the caller's first reply answers exactly what they heard. The
        words are the application's, and so is the read-back; nothing here
        is a model's.
        """
        entry = self.by_id[self.entry]
        line = entry.first_line(call) if isinstance(entry, ConversationStep) else ''
        if isinstance(line, Prompted):
            return replace(line, spoken=f'{words} {line.spoken}'.strip())
        return f'{words} {line}'.strip()

    @property
    def confirms(self) -> bool:
        """Whether any operation waits for a heard read-back and a yes: the flow needs playback evidence to act."""
        return any(operation.needs_approval for operation in self.operations())

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
        call; once the call has its clock, it is validated again with the
        call's context (`validation_context`). Services are kept as given: whether a client is shared between
        calls or made per call is the application's decision.

        `intents` are unresolved intents an earlier process persisted (through
        its services' `note_intent`, or `FlowCall.unresolved`): they come back
        unknown, and are reconciled before any of them, or any other intent of
        the same operation for the same caller, can be sent.
        """
        facts = self.state_type.model_validate(deepcopy(state.model_dump()), extra='forbid')
        call = self.call_type(state=FlowState[self.state_type](agent=self.start, facts=facts))  # ty: ignore[invalid-type-form]
        call._identity_field, call._collectible, call._derived = self.identity, self.collectible, self.derived
        call._choices = frozenset(name for step in self.steps for name in getattr(step, 'choices', {}))
        call._choice_after = {
            name: after for step in self.steps for name, after in getattr(step, 'choice_after', {}).items()
        }
        call._resource_choices = self.resource_choices
        if services is None and self.services is not None:
            services = self.services()
        call._services, call._clock, call._locale = services, clock, self.locale
        call._locales, call._language = self.locales, self.language
        call.state = call.state.model_copy(update={'facts': call.validated(facts.model_dump())})
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
                *(carried for carried, _ in getattr(step, 'carries', ())),
            ):
                if operation is not None:
                    found.setdefault(operation.name, operation)
        return list(found.values())

    def recaps(self, call: FlowCall[Any]) -> dict[str, str]:
        """What a goodbye may recap, by reference: each receipt that still stands, in its operation's `recap` words.

        Only `CallContext.effective` receipts: a lookup, a refusal, a booking
        since moved or cancelled, and anything done for another caller are not
        here, so no reference to them exists for a model to give.
        """
        known = {op.name: op for op in self.operations()}
        found = {}
        for filed in call.effective():
            template = getattr(known.get(filed.operation), 'recap', '')
            if filed.intent and template:
                found[filed.intent[:RECAP_REF]] = template.format_map(_Facts(filed.receipt))
        return found

    def receipt_words(self, call: FlowCall[Any]) -> str:
        """The newest receipt that still stands for the caller on the line, in its operation's own `say` words.

        What `say_again` says when the caller asks to hear what was done.
        Read from `CallContext.effective`, so a change since superseded, a
        refusal, a lookup and anything done for someone else are never said
        as the current outcome. '' when nothing stands.
        """
        known = {op.name: op for op in self.operations()}
        standing = call.effective()
        for filed in reversed(call.evidence):
            template = getattr(known.get(filed.operation), 'say', '')
            if template and any(filed is s for s in standing):
                return template.format_map(_Facts(filed.receipt))
        return ''

    def goodbye_words(self, call: FlowCall[Any], recap: Sequence[str]) -> str:
        """The goodbye core says: the recaps pointed at, then `unsettled` if anything is still unknown, then the line.

        A reference that is not a standing receipt of this call is refused back
        to the model; it cannot name one into existence, and it writes none of
        the words. An entry that is exactly a standing recap's words (the live
        text checkpoint's model sent those instead of the reference) counts as
        that reference: the words were rendered by the application from the
        receipt, so matching them names nothing new, and saves a retry.
        """
        standing = self.recaps(call)
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

    def sequence(self) -> list[Step]:
        """The steps walked in order, without the endings."""
        return [s for s in self.steps if not isinstance(s, End)]

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
        if isinstance(step, End | ConversationStep):
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

    def record(
        self, call: FlowCall[Any], updates: Mapping[str, Any], offered: Collection[str] = ()
    ) -> list[str]:
        """Commit the caller's values as one state candidate, resetting whatever rested on a value that changed.

        Returns the picks among `offered` that were not on offer for the state
        this update ends in, left out (`FlowCall.collect`). Raises
        `ValidationError` and changes nothing when the candidate is not a
        valid state.
        """
        facts = call.state.facts
        unoffered = _not_on_offer(call, offered, updates)
        updates = {name: value for name, value in updates.items() if name not in unoffered}
        candidate = call.validated(facts.model_dump() | deepcopy(updates))
        changed = [
            name
            for name in updates
            if present(getattr(facts, name)) and getattr(facts, name) != getattr(candidate, name)
        ]
        return unoffered + call.collect(offered, **updates, **self._resting_on(changed, set(updates)))
