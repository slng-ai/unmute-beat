"""Typed steps that compile into the turn engine's nodes: conversation, action, end.

A business describes a flow as steps and `StepFlow` compiles it into a
`Node` table, so one turn engine, budget, barge-in handling and Pydantic
graph run every application. What each kind does:

* `ConversationStep` is one agent: it records what the caller says, offers,
  reads back and carries out its operations, and hands over, each as one
  typed decision (`core.structured`). It stays active across turns until it
  hands the caller on or they say goodbye.
* `ActionStep` runs one `Operation` and files its receipt. Only a receipt
  that says what the step needed completes it; a refusal goes to the step's
  failure path, and an unknown outcome to its pending path, never to success.
* `End` says one closing line and records how the flow ended.

Whether a step is done is always derived from the facts on the call (see
`core.completion`), never stored as a flag. When the caller changes a fact,
what rested on it goes with it (`FlowCall.collect`).
"""

from __future__ import annotations

import asyncio
import inspect
import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, ClassVar, Literal, get_args

from loguru import logger
from pydantic import BaseModel, ConfigDict, PrivateAttr, ValidationError
from pydantic_ai import Agent, ModelRetry, RunContext, ToolOutput
from pydantic_ai.agent.spec import AgentSpec
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage, UsageLimits

from core.clock import CallClock
from core.completion import ReceiptMatched
from core.flow import Nodes
from core.graph import Exit, Node, Opening, Restated, holds
from core.history import History, Scope
from core.locale import Locale
from core.operations import Operation, OutcomeUnknownError, _tracer
from core.policies import claim_or_refuse, one_action_per_reply, request_clock, speak_by
from core.proposals import BaseProposal, Proposal
from core.state import CallContext, Handoff, Intent, VoiceState, present
from core.values import Phone, Rejection, words


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

    One record, so nothing can take the ids and leave the words: what code
    says about an offer is exactly what can still be picked from it (session 4f: billing resolutions said after the category became
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
        self._by_choice.update(
            {name: replace(offer, announced=True) for name, offer in self._made(step, choices, turn)}
        )
        return said

    def _made(self, step: str, choices: Sequence[str], turn: int) -> list[tuple[str, StandingOffer]]:
        return [
            (name, o)
            for name in choices
            if (o := self._by_choice.get(name)) is not None
            and o.step == step
            and o.turn == turn
            and not o.announced
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
    """The fields this caller turn's latest update recorded: what a step's `reads_back` rule is keyed on.

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
    _scopes: Mapping[str, Scope] = PrivateAttr(default_factory=dict)
    """Each conversation step's view (`core.history.Scope`), by step id, from the flow."""
    _reads: dict[tuple[str, str], tuple[int, int]] = PrivateAttr(default_factory=dict)
    """Each read a structured step ran, by operation and arguments: the caller turn and state version it answered for.

    The same read asked again for the same turn and state is a repeat, never
    sent to the service again (`core.structured.Read`).
    """

    def scope(self) -> Scope | None:
        """The view of the conversation step the caller is with, or None at an action or end step."""
        return self._scopes.get(self.state.agent)

    def reissue_waiting(self) -> str:
        """The read-back waiting when the consultation began, put again while its intent is only prepared.

        Never answered and never withdrawn: a question the caller already
        said yes or no to, or one past its limit, is not asked again. Nor one
        whose arguments the state no longer gives: a value changed during the
        consultation makes it a different request, which its step reads back
        itself.
        """
        question, self._detour_question = self._detour_question, None
        if question is None:
            return ''
        about, value = str(getattr(question, 'about', '')), str(getattr(question, 'value', ''))
        if (found := self._reconcile_with.get(about)) is not None:
            try:
                if _args(found[0], self).model_dump_json() != value:
                    return ''
            except ValidationError:
                return ''
        if not any(
            i.operation == about
            and i.args == value
            and i.status == 'prepared'
            and i.subject == self.identity()
            for i in self.intents
        ):
            return ''
        return Prompted(about=about, spoken=str(question.spoken), value=value).put(self)

    def note_refusal(self, error: ValidationError) -> str:
        """Why an update was refused, from the validator's own error, said for the model.

        The error's type picks the locale's words for the caller
        (`Locale.refusals`), formatted with the error's context: the same
        error, never the value validated a second time. The model reads the
        validator's message, with those words beside it as what the caller
        can be told.
        """
        refused = [(item, self.locale.refusal(item)) for item in error.errors()]
        return '; '.join(
            item['msg'] + (f' (tell the caller: "{said}")' if said else '') for item, said in refused
        )

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
        code. The model's path is `collect`, a step's update, which may write only that step's fields.
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
        alone. `said` is everything the caller said on this call, turn by
        turn: what `core.values.Quoted` looks a value up in.
        """
        return super().validation_context() | {
            'before': self.state.facts,
            'heard': self.heard,
            'said': caller_words(self),
        }

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
        """What the caller said, as a model heard it: only fields a conversation step may record.

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
        already holds is not a new pick. A refused pick refuses the whole
        update: nothing is recorded and the refused names are returned.
        Nothing changes either when the candidate is not a valid state
        (`ValidationError`).
        """
        unoffered, updates, stale = self._planned(offered, fields)
        if unoffered:
            return unoffered
        self.update(**updates)
        self._offers.take(*stale)
        return unoffered

    def _planned(
        self, offered: Collection[str], fields: Mapping[str, Any]
    ) -> tuple[list[str], dict[str, Any], set[str]]:
        """What `collect` would do, with nothing committed: the refused picks, the whole update, the offers it takes."""
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
        return unoffered, kept | picks | resets, stale


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
    paths: ClassVar[Mapping[str, tuple[str, ...]]] = {}
    """An argument taken from inside a state field (`customer.phone`), by name: the path, its first step in `binds`.

    A class operation names it with `validation_alias=AliasPath(...)`. A
    missing step (no customer yet) is a missing argument, never a fallback
    to a field of the same name.
    """
    assign: ClassVar[Mapping[str, str]] = {}
    """State field to the receipt field it is set from; `'*'` sets the whole receipt, a nested model."""
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
        """Every `succeeds` field holds its value, or one of its values when a list is given."""
        return all(
            receipt.get(name) in (value if isinstance(value, list) else [value])
            for name, value in self.succeeds.items()
        )

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
        given = args.model_dump(include=set(self.matches.values()))
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
                    receipt = self._receipt(data)
                except ValidationError as error:
                    raise OutcomeUnknownError(
                        f'{self.name}: reconciliation answered in a shape that is not the receipt'
                    ) from error
                if wrong := self._bound(receipt.model_dump(include=set(self.matches)), args):
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
                'offer (`options`), and record what the caller picks.'
                if spent
                else ''
            )
            scope = call.scope()
            answered = data if scope is None else scope.public_receipt(self.name, data)
            if scope is not None and isinstance(receipt, Rejection) and receipt.reason:
                answered |= {'reason': receipt.reason}
            raise ModelRetry(
                f'Nothing was done: the {self.name} service answered {answered}. Do not say it was done.{again}'
            )
        if self.assign:
            call.update(
                **{field: data if source == '*' else data[source] for field, source in self.assign.items()}
            )
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
    """One step of a flow: where it goes, and whether it is done."""

    id: str
    then: str = ''
    """The step after this one. Empty means the next listed step that is not already done."""
    spec: AgentSpec | None = None
    """The persona's native `AgentSpec` (model, settings, retries) this step's agent is built from, if it names one."""

    @property
    def elsewhere(self) -> tuple[str, ...]:
        """Where this step can send the caller other than onward: failure, a hand-over, a route."""
        return ()

    def done(self, call: FlowCall[Any]) -> bool:
        """Whether the call's state and evidence show this step's purpose is met. Never true without evidence."""
        return False

    @abstractmethod
    def node(self, flow: StepFlow) -> Node[Any]:
        """This step as a node the turn engine runs."""


def _agent(
    flow: StepFlow,
    step: Step,
    instructions: Any,
    outputs: list,
    hooks: Hooks | None = None,
    extra: Sequence[Any] = (),
    **agent: Any,
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
    capabilities = [request_clock(into=hooks), *extra]
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

    A conversation step (`ConversationStep.scope`) is told only what rests on what
    it sees: the offers of its own visible choices, its own question, and its
    own operations' intents and receipts, their arguments and results only
    through the fields it sees (`Scope.public_args`, `Scope.public_receipt`).
    Another step's pending question or unknown outcome is one line with no
    words or arguments, so it is neither answered nor claimed. The call keeps
    everything; nothing is removed from its records. What the caller missed
    (`spoken_over`) comes from the turn engine's own note alone.

    Seeing a fact is not permission to act on it: every action is still
    checked by its own guard when it runs.
    """
    scope = getattr(step, 'scope', None)
    lines = [
        '## Application state',
        'Written by the application, not by the caller. It is true now; the conversation may be older.',
        f'You are the `{step.id}` step.',
    ]
    shown = getattr(step, 'facts', None)
    if shown is None:
        lines.append(f'Facts: {call.state.facts.model_dump_json()}')
    elif shown:
        lines.append(f'Facts: {call.state.facts.model_dump_json(include=set(shown))}')
    offered = {name: dict(ids) for name, ids in call._offers.items() if ids}
    if scope is not None:
        offered = scope.offers(offered)
    lines.append(
        f'On offer to the caller now, by id: {json.dumps(offered)}. Only these ids can be picked.'
        if offered
        else 'Nothing is on offer to the caller now.'
    )
    last = call.last_proposal()
    if last is not None:
        question, _ = last
        if scope is None or scope.owns(question.about):
            lines.append(
                f'The caller is answering: "{question.spoken}" ({question.about}). '
                + _HEARING[call.last_hearing() or 'unconfirmed']
            )
        else:
            lines.append(
                "Another step's question is waiting for the caller's answer. It is that step's to act on, not yours."
            )

    def shown(intent: Intent) -> str:
        return intent.args if scope is None else json.dumps(scope.public_args(intent.operation, intent.args))

    lines.extend(
        (
            f'Withdrawn: {intent.operation} {shown(intent)}. Its read-back could not be confirmed on this line, '
            'and the caller was told. It is not read back or carried out again on this call: do not offer it.'
        )
        for intent in call.intents
        if intent.status == 'withdrawn'
        and intent.subject == call.identity()
        and (scope is None or scope.owns(intent.operation))
    )
    done = [
        e
        for e in call.evidence
        if e.identity == call.identity() and (scope is None or scope.owns(e.operation))
    ]
    for filed in done[-3:]:
        if scope is None:
            lines.append(
                f'Done on this call: {filed.operation} {json.dumps(filed.args)} -> {json.dumps(filed.receipt)}'
            )
            continue
        result = 'succeeded' if filed.succeeded else 'refused by the service'
        if found := scope.public_receipt(filed.operation, filed.receipt):
            result += f', set {json.dumps(found)}'
        lines.append(
            f'Done on this call: {filed.operation} {json.dumps(scope.public_args(filed.operation, filed.args))} '
            f'-> {result}'
        )
    lines.extend(
        (
            f'Outcome unknown: {intent.operation} {shown(intent)}. Do not say it was done, or that it was not: '
            'the application is checking, and will not send it again until it knows.'
        )
        if scope is None or scope.owns(intent.operation)
        else (
            "Outcome unknown: another step's action. Do not say it was done, or that it was not: "
            'the application is checking.'
        )
        for intent in call.unresolved()
    )
    if standing := flow.recaps(call, only=scope.operations if scope is not None else None):
        lines.append(
            'Can recap on goodbye, by reference: '
            + '; '.join(f'{ref}: "{words}"' for ref, words in standing.items())
            + '.'
        )
    if call.spoken_over and scope is None:
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


def _refusal_words(hooks: Hooks, tools: list[str]) -> None:
    """A value these tools' schemas refused: the same retry, with the locale's words for the caller beside it.

    Every tool or output carrying a step's `update`: its fields keep the
    state's own types, so the application's validators refuse in the
    arguments, before the update runs. A function tool's arguments and an
    output tool's are validated apart, so both hooks are registered. Only a
    refusal the locale has words for is reworded: any other error (an
    argument of the wrong shape) is the model's to fix, and the SDK's own
    retry names the field.
    """

    def worded(ctx: RunContext[FlowCall[Any]], error: Any) -> Any:
        if isinstance(error, ValidationError) and any(
            ctx.deps.locale.refusal(item) for item in error.errors()
        ):
            raise ModelRetry(
                f'Nothing was recorded: {ctx.deps.note_refusal(error)}. Ask the caller again.'
            ) from None
        raise error

    @hooks.on.tool_validate_error(tools=tools)
    async def refused(
        ctx: RunContext[FlowCall[Any]], *, call: Any, tool_def: Any, args: Any, error: Any
    ) -> Any:
        worded(ctx, error)

    @hooks.on.output_validate_error
    async def refused_output(
        ctx: RunContext[FlowCall[Any]], *, output_context: Any, output: Any, error: Any
    ) -> Any:
        if output_context.tool_call is not None and output_context.tool_call.tool_name in tools:
            worded(ctx, error)
        raise error


def _entered(call: FlowCall[Any]) -> bool:
    return True


RECAP_REF = 8
"""How many characters of an intent id a recap reference is: unique within a call, short enough to copy."""


def _args(operation: Operation, call: FlowCall[Any]) -> BaseModel:
    """An operation's arguments, taken field by field from the application state, validated whole.

    A declared operation names the state field each argument comes from
    (`FunctionOperation.binds`); otherwise the argument's own name is the field.
    """
    facts = call.state.facts
    binds = getattr(operation, 'binds', None) or {}
    paths = getattr(operation, 'paths', None) or {}

    def value(name: str) -> Any:
        found: Any = facts
        for step in paths.get(name) or (binds.get(name, name),):
            found = getattr(found, step, None)
        return found

    return operation.args_type.model_validate(
        {name: value(name) for name in operation.args_type.model_fields}
    )


def _args_or_none(operation: Operation, call: FlowCall[Any]) -> BaseModel | None:
    """`_args`, or None while the state cannot give them yet."""
    try:
        return _args(operation, call)
    except ValidationError:
        return None


def receipt_for(operation: Operation, call: FlowCall[Any]) -> Mapping[str, Any] | None:
    """The latest receipt this operation filed for the arguments the state gives now, under the current caller.

    Read from the call's evidence, which nothing erases: a changed argument or
    identity makes an older receipt stop counting here, while it stays on
    record for what it did.
    """
    if (given := _args_or_none(operation, call)) is None:
        return None
    args = given.model_dump(mode='json')
    return next(
        (
            filed.receipt
            for filed in reversed(call.evidence)
            if filed.operation == operation.name and filed.args == args and filed.identity == call.identity()
        ),
        None,
    )


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


def caller_words(call: CallContext) -> tuple[str, ...]:
    """Everything the caller said on this call, never model history: the transcript, the prompts, this turn."""
    sources = [row['text'] for row in call.transcript() if row['said'] == 'caller']
    sources += [
        part.content
        for message in call.messages
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    ]
    return (*sources, call.heard) if call.heard else tuple(sources)


def said_by_caller(call: CallContext, excerpt: str) -> bool:
    """Whether these words are the caller's: found, as whole words, in what they said, never in model history.

    An extractive read-back trades paraphrase for evidence anyone can check.
    Case and punctuation may differ, because speech-to-text adds its own.
    """
    wanted = words(excerpt)
    return bool(wanted) and any(f' {wanted} ' in f' {words(source)} ' for source in caller_words(call))


def _not_ready(operation: Operation, call: FlowCall[Any], error: ValidationError) -> str:
    """What an operation still lacks, told as what to do next: a pick comes from an offer, anything else from the caller.

    Two kinds, kept apart because they are fixed differently (session 4d,
    trace 5edecd4c: "slot missing. Ask the caller" twice, then an exhausted
    turn, when the times for the new day had never been offered).
    """
    binds = getattr(operation, 'binds', None) or {}
    missing = [binds.get(str(e['loc'][0]), str(e['loc'][0])) for e in error.errors() if e['loc']]
    picks = [name for name in missing if name in call._choices]
    asks = [name for name in missing if name not in call._choices]
    said = []
    if picks:
        said.append(
            f'no {" or ".join(picks)} picked yet: call options for what the caller asked, then record the id they pick'
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

    It may also carry `say`: what the caller hears about this offer when code
    says it (`offers:`), in the application's words, including when nothing
    is free or the day could mean two dates. Core never words an offer. ''
    leaves the offer to the agent.
    """

    model_config = ConfigDict(extra='forbid')
    options: dict[str, str] = {}
    note: str = ''
    say: str = ''
    pick: str = ''
    """One of `options` the application picks itself, for a step that offers it (`offers:`): the caller has only one.

    The pick still has to be on offer, and whatever an operation does with it
    is read back first: a caller who holds one booking hears "Shall I cancel
    your haircut Friday?" without being asked which one.
    """


async def _options(choose: Callable[[Any, Any], Any], services: Any, facts: BaseModel) -> Options:
    """One choice function's answer for these facts, as `Options`: async as it is, plain in a worker thread."""
    if inspect.iscoroutinefunction(choose):
        answer = await choose(services, facts)
    else:
        answer = await _result(await asyncio.to_thread(choose, services, facts))
    if isinstance(answer, Mapping):
        return Options(options=dict(answer))
    return Options(
        options=dict(answer.options),
        note=getattr(answer, 'note', ''),
        say=getattr(answer, 'say', ''),
        pick=getattr(answer, 'pick', ''),
    )


async def _offer(
    call: FlowCall[Any], choices: Mapping[str, Callable[[Any, Any], Any]], announce: bool = False
) -> dict[str, Any]:
    """Ask each choice function for this caller's options, and record them as offered, unless the call moved on.

    A choice function may be sync or async, and may take long: the caller can
    change what they asked for, or be handed elsewhere, before it answers. A
    plain one runs in a worker thread, like a domain function (`_call`), so a
    database or HTTP read never holds the event loop that carries audio. An
    answer computed for a state that is no longer the call's, or in a turn the
    caller has left, is returned as stale and offers nothing, so it can never
    replace the current offer or make an old slot pickable.

    The words are kept with the ids (`Offers`), so whatever takes the offer
    takes its words too. `announce` keeps them for this step and turn: an
    offer code says on arrival (`ConversationStep.offers`), which falls back
    to the agent when a choice function has none. The agent is still shown
    the ids and notes, to read the caller's pick.
    """
    version, generation, agent = call._version, call._generation, call.state.agent
    found: dict[str, Options] = {}
    for name, choose in choices.items():
        try:
            found[name] = await _options(choose, call.services, call.state.facts)
        except Exception as error:
            logger.exception('choice function for {} failed', name)
            return {
                'unavailable': f'The options could not be looked up just now ({type(error).__name__}). Nothing is offered.'
            }
    if call._version != version or call._generation != generation or call.state.agent != agent:
        return {'stale': 'The request changed while these were looked up. Nothing is offered; look again.'}
    result: dict[str, Any] = {}
    for name, options in found.items():
        call._offers.put(name, options.options, options.say, call.turn if announce else 0, agent)
        result[name] = {'options': options.options, **({'note': options.note} if options.note else {})}
        if options.pick in options.options:
            result[name]['pick'] = options.pick
    return result


def _resting_on(operation: Operation) -> set[str]:
    """The state fields an operation's arguments are built from: a change to one of them makes its read-back stale."""
    return set(getattr(operation, 'binds', {}).values()) or set(operation.args_type.model_fields)


async def _carry_out(
    call: FlowCall[Any], operation: Operation, usage: RunUsage | None, limits: UsageLimits | None
) -> tuple[str | None, BaseModel | None]:
    """Run an approved operation; return what the caller heard and its receipt. An unknown outcome is said as unknown.

    An authoritative no goes back to the step's agent with the refusal
    (`ModelRetry`), to choose what comes next (look again, ask).
    """
    try:
        receipt = await operation.run(call, _args(operation, call), usage=usage, usage_limits=limits)
    except OutcomeUnknownError:
        await call.speak_now(call.locale.unsettled, origin='confirmation')
        return call.locale.unsettled, None
    if not isinstance(receipt, BaseModel):
        return None, None
    say = getattr(operation, 'say', '')
    return (say.format_map(_Facts(receipt.model_dump())) if say else None), receipt


@dataclass(frozen=True, kw_only=True)
class ConversationStep(Step):
    """An agent node: one agent, its instructions, what it may record, offer, carry out and hand over to.

    The step stays active across turns until it hands the caller on or they
    say goodbye. Its agent reads every reply itself, in the conversation it
    sees and the application state (`context`), and answers with one typed
    decision carrying the caller's update (`core.structured`): `reply`,
    `record`, `goodbye`, one of its declared `operations`, or `hand_over` to
    one of its `exits`. `choices` it may offer come from `options`.

    An operation is read back in the application's words and runs only when
    the agent calls it again on the turn after that read-back reached the
    caller in full: the agent's yes, bound by code to that exact question
    (`CallContext.approve`), through `Operation.run`. No second model reads
    the reply. A reply that also changes the request gets a fresh read-back
    instead. A reply the agent reads as a no sets `decline`
    (`CallContext.decline`): the question is spent, and a goodbye does not
    tell the caller the change was left undone. A question in reply is not
    a no, and leaves it waiting (session 4f).

    A step whose operations say their receipts also has `say_again`: when the
    caller asks to hear what was done, code says the newest receipt that
    still stands, in the application's words (`StepFlow.receipt_words`). It
    only speaks: nothing is sent, no intent is made, no delivery is assumed,
    and the automatic repeat of an unheard receipt keeps its own limit
    (`RECEIPT_REPEATS`).

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
    consults: frozenset[str] = frozenset()
    """The exits that are consultations (`consults:`): the specialist answers, and `done` brings the caller back."""
    consulted: bool = False
    """Another step consults this one, so its agent has `done`, which hands the caller back to that step."""
    requires: tuple[str, ...] = ()
    satisfied_by: str = ''
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
    offers: tuple[str, ...] = ()
    """Choices whose function says the step's next line (`say`), with no model request: `ConversationStep.offer`.

    On arrival by a hand-over or a completed prerequisite, and after a
    `record` that left one with no offer of this step's own (the caller gave
    the day). The function decides: an offer (ids and words), a question for
    what it still needs (words alone), ids for the agent to word, a `pick`
    when the caller has only one, or a note for the agent. A reply that only
    gave the day for the times cost two requests (`options`, then `reply`),
    and a hand-over with the new day three (session 4, text paths). In Coval runs
    82gmB8t5 and DezcEAK4, booking reached from verify spent three requests
    (`options`, then `reply`) on the offer the request already decided, and the
    caller talked over the silence after "Thanks, I have you." A choice with
    nothing to offer, or no words, leaves the arrival to the step's agent.
    """
    routes: tuple[str, ...] = ()
    """Steps a receipt of one of its operations can move the caller to (`route:`): code's moves, never `hand_over`'s."""
    carries: tuple[tuple[Operation, tuple[str, ...]], ...] = ()
    """Operations code carries out after a reply that only records one of their fields (`carries_out`), never the model."""
    history: Literal['none'] | History = field(default_factory=History)
    """What of earlier caller turns this step's agent is sent (`context: {history: ...}`; `Node.history`)."""
    facts: tuple[str, ...] | None = None
    """The state fields this step's agent is shown (`context: {facts: [...]}`). None: all of them; (): none."""
    scope: Scope | None = None
    """The step's view (`core.history.Scope`), always set by the manifest; None only for a step built by hand."""
    capabilities: tuple[Any, ...] = ()
    """Native Pydantic AI capabilities this step's agent carries, on-demand ones (`defer_loading`) among them."""
    reads_back: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """Operation -> fields: a reply that only records one of these fields gets that operation read back by code.

    The same read-back the model would have asked for with a second request,
    and the same approval after it: only a later yes, with the model calling
    the operation, carries it out.
    """

    @property
    def elsewhere(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys([*self.exits, *self.routes, *([self.satisfied_by] if self.satisfied_by else [])])
        )

    def opening(self) -> tuple[Opening[Any], ...]:
        """On arrival: read back `opens_with` if the state gives its arguments, else ask (`asks`), else offer (`offers`). No model."""
        return (*self._read_back_first(), *self._ask_first(), *self._offer_first())

    def first_line(self, call: FlowCall[Any]) -> Prompted | str:
        """What this step would open with on arrival, as words to put after a greeting: the same two rules as `opening`.

        The call's first arrival is its entry step, with no hand-over, so
        this is how a caller ID read back in the greeting is the question the
        first reply answers (`StepFlow.greeting`). '' when neither applies.
        """
        if (operation := self.opens_with) is not None and (
            args := _args_or_none(operation, call)
        ) is not None:
            proposal = operation.proposal_for(call, args)
            return Prompted(about=proposal.about, spoken=proposal.spoken, value=proposal.value)
        missing = next((name for name in self.asks if not present(getattr(call.state.facts, name))), '')
        return self.asks[missing] if missing else ''

    def _offer_first(self) -> tuple[Opening[Any], ...]:
        if not self.offers:
            return ()
        return (
            Opening('options', self.owes_offer, self.offer, arrivals=frozenset({'handoff', 'completed'})),
        )

    def owes_offer(self, call: FlowCall[Any]) -> bool:
        """Some choice in `offers:` has no offer this step made standing: none, or one another step made."""
        return any(self._unoffered(call, name) for name in self.offers)

    def _unoffered(self, call: FlowCall[Any], name: str) -> bool:
        standing = call._offers._by_choice.get(name)
        return standing is None or standing.step != self.id

    async def offer(self, call: FlowCall[Any]) -> Prompted | str:
        """Code's next line for `offers:`, on arrival or after a `record`: a read-back, an offer, or a question.

        Each choice in turn that has no offer of this step's own asks its
        function. A `pick` (the caller has only one) is recorded, and when an
        operation of this step then has every argument, it is read back. Else
        the function's words are said: an offer, or a question for what it
        still needs. A choice with no words goes on to the next. When none has
        anything to say, the step's agent answers with the functions' notes.
        """
        notes = []
        for name in self.offers:
            if not self._unoffered(call, name):
                continue
            found = await _offer(call, {name: self.choices[name]}, announce=True)
            problem = found.get('stale') or found.get('unavailable') or ''
            if problem:
                call._offers.take(name)
                raise ModelRetry(problem)
            pickable = bool(call._offers.get(name))
            if pick := found[name].get('pick'):
                call.collect(tuple(self.choices), **{name: pick})
                if (asked := self._ready(call)) is not None:
                    return asked
            words = call._offers.announce(call.state.agent, [name], call.turn)
            if not pickable:
                call._offers.take(name)
            if words:
                await call.speak_now(words, origin='ask')
                return words
            note = found[name].get('note', '')
            notes.append(
                f'{name} is on offer now, under "On offer": carry on from it. {note}'.strip()
                if pickable
                else f'nothing is on offer for {name}. {note}'.strip()
            )
        raise ModelRetry(' '.join(notes) or 'Everything is on offer already: carry on from it.')

    def _ready(self, call: FlowCall[Any]) -> Prompted | None:
        """The read-back of this step's one operation that now has every argument and is not already asked."""
        ready = [
            (operation, args)
            for operation in self.operations
            if (args := _args_or_none(operation, call)) is not None and operation.needs_approval
        ]
        if len(ready) != 1:
            return None
        operation, args = ready[0]
        proposal = operation.proposal_for(call, args)
        if call.pending(operation.name, value=proposal.value) is not None:
            return None
        return Prompted(about=proposal.about, spoken=proposal.spoken, value=proposal.value)

    def _ask_first(self) -> tuple[Opening[Any], ...]:
        if not self.asks:
            return ()

        def missing(call: FlowCall[Any]) -> str:
            return next((name for name in self.asks if not present(getattr(call.state.facts, name))), '')

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
            args = _args_or_none(operation, call)
            return (
                args is not None
                and call.pending(operation.name, value=operation.proposal(args).value) is None
            )

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
            if not all(op.name in self.reads_back for op in self.operations if possible(call, op)):
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
        said, receipt = await _carry_out(call, operation, usage, limits)
        to = (
            str(getattr(receipt, operation.route, ''))
            if operation.route and isinstance(receipt, BaseModel)
            else ''
        )
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
                    + ('If their words turned it down, set decline. ' if confirms else '')
                    + 'If they are leaving, call goodbye again.'
                )
            words = flow.goodbye_words(call, recap or [])
            call.note_departure()
            call.end_requested('caller')
            await call.speak_now(words, origin='goodbye')
            await call.end_call()
            return words

        async def say_again(ctx: RunContext[FlowCall[Any]]) -> str | Prompted:
            """The caller asked to hear again what was done for them. The application says it again."""
            call = ctx.deps
            words = flow.receipt_words(call)
            if words and call.receipt_said_again():
                return ''
            if not words:
                raise ModelRetry(
                    'Nothing was said: nothing done on this call still stands. Answer the caller.'
                )
            await call.speak_now(words, origin='confirmation')
            return words

        read_back = self._reads_back(flow)
        confirms = any(op.needs_approval for op in self.operations)
        actions = (
            frozenset(op.name for op in self.operations)
            | ({'hand_over'} if self.exits else frozenset())
            | ({'done'} if self.consulted else frozenset())
        )
        repeats = any(op.changes and getattr(op, 'say', '') for op in self.operations)
        exits = self.exits

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
        from core.structured import Structured

        built = Structured(flow, self, actions, read_back, confirms)
        outputs = built.outputs(
            goodbye,
            ToolOutput(say_again, name='say_again') if repeats else None,
            lambda operation: self._operate(flow, operation, built.actions),
            self._hand_over(built.actions) if self.exits else None,
        )
        hooks = Hooks(prepare_output_tools=narrow)
        one_action_per_reply(built.actions, built.updates, into=hooks, reads=built.read_names)
        speak_by(SPEAK_BY, into=hooks)
        built.hooks(hooks)
        if built.updates:
            _refusal_words(hooks, sorted(built.updates))
        agent = _agent(flow, self, text, outputs, hooks=hooks, tools=built.tools(), extra=self.capabilities)
        return _node(
            flow,
            self,
            agent,
            history=self.history,
            scope=self.scope,
            consults=self.consults,
            capabilities=frozenset(c.id for c in self.capabilities if getattr(c, 'id', None)),
            requires=self.requires,
            satisfied_by=self.satisfied_by,
            openings=self.opening(),
            model_speech=False,
        )

    def _operate(self, flow: StepFlow, operation: Operation, actions: frozenset[str]):
        async def operate(ctx: RunContext[FlowCall[Any]]) -> Prompted | Handoff | str | None:
            claim_or_refuse(ctx, actions)
            call = ctx.deps
            try:
                args = _args(operation, call)
            except ValidationError as error:
                raise ModelRetry(_not_ready(operation, call, error)) from None
            if operation.needs_approval:
                proposal = operation.proposal_for(call, args)
                read_back = Prompted(about=proposal.about, spoken=proposal.spoken, value=proposal.value)
                changed = {
                    name
                    for name in call._changing & _resting_on(operation)
                    if call._tries[name] != call._kept[name]
                }
                if changed or not call.approve(proposal.about, value=proposal.value):
                    _blocked(call, proposal, 'changing' if changed else '')
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

    `take_back` is where a caller who takes the result back is sent: the
    step whose operation undoes it. The ending's own agent reads each
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
    repeat, or whose action steps run in order (no `then`) with no `End` with
    outcome `completed` to finish on, so a broken flow fails where it is
    defined rather than on a call.
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
        clock: CallClock | None = None,
    ) -> None:
        self.clock = clock
        """The manifest's `current_datetime:`: every call's timezone and calendar. None: the call's own defaults."""
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
        onward = [s for s in self.steps if isinstance(s, ActionStep) and not s.then]
        if onward and not finished:
            raise ValueError('a flow whose steps run in order needs an End with outcome completed')
        self.finish = finished[0].id if finished else ''
        for step in self.steps:
            for to in (step.then, *step.elsewhere):
                if to and to not in self.by_id:
                    raise ValueError(f'{step.id} sends the caller to {to}, which is not a step')
        self.collectible = frozenset(
            name for s in self.steps if isinstance(s, ConversationStep) for name in s.fields
        )
        """The only state fields a model may write: the ones some conversation step may record."""
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
        caller_id: str = '',
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

        `caller_id` is the number the carrier sent. When the flow's
        `identity:` field is a `core.Phone`, it is filled with that number,
        or left empty when the number is withheld or not a real one
        (`Phone.from_carrier`). A caller ID for any other identity is an
        error: core cannot know what it would mean.

        The call's clock is chosen here: `clock`, else the services' own
        `now` (a diary with a settable clock), else the wall clock in the
        manifest's timezone. It is read once, now, so the whole call agrees
        on today.
        """
        if caller_id:
            kind = self.state_type.model_fields[self.identity].annotation if self.identity else None
            if kind is not Phone and Phone not in get_args(kind):
                raise ValueError('caller_id fills the identity field, which is not a core.Phone in this flow')
            state = state.model_copy(update={self.identity: Phone.from_carrier(caller_id)})
        facts = self.state_type.model_validate(deepcopy(state.model_dump()), extra='forbid')
        call = self.call_type(state=FlowState[self.state_type](agent=self.start, facts=facts))  # ty: ignore[invalid-type-form]
        call._identity_field, call._collectible, call._derived = self.identity, self.collectible, self.derived
        call._choices = frozenset(name for step in self.steps for name in getattr(step, 'choices', {}))
        call._scopes = {
            step.id: step.scope
            for step in self.steps
            if isinstance(step, ConversationStep) and step.scope is not None
        }
        call._choice_after = {
            name: after for step in self.steps for name, after in getattr(step, 'choice_after', {}).items()
        }
        call._resource_choices = self.resource_choices
        if services is None and self.services is not None:
            services = self.services()
        if clock is None and callable(getattr(services, 'now', None)):
            clock = services.now
        call._services, call._clock, call._locale = services, clock, self.locale
        if self.clock is not None:
            call._zone, call._calendar_days = self.clock.zone, self.clock.calendar_days
            call._today_shown = self.clock.shown
            call.today  # noqa: B018
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

    def recaps(self, call: FlowCall[Any], only: Collection[str] | None = None) -> dict[str, str]:
        """What a goodbye may recap, by reference: each receipt that still stands, in its operation's `recap` words.

        Only `CallContext.effective` receipts: a lookup, a refusal, a booking
        since moved or cancelled, and anything done for another caller are not
        here, so no reference to them exists for a model to give. `only`
        limits them to those operations: what a step is shown.
        """
        known = {op.name: op for op in self.operations()}
        return {
            filed.intent[:RECAP_REF]: template.format_map(_Facts(filed.receipt))
            for filed in call.effective()
            if (only is None or filed.operation in only)
            and filed.intent
            and (template := getattr(known.get(filed.operation), 'recap', ''))
        }

    def receipt_words(self, call: FlowCall[Any]) -> str:
        """The newest receipt that still stands for the caller on the line, in its operation's own `say` words.

        What `say_again` says when the caller asks to hear what was done.
        Read from `CallContext.effective`, so a change since superseded, a
        refusal, a lookup and anything done for someone else are never said
        as the current outcome. '' when nothing stands.
        """
        known = {op.name: op for op in self.operations()}
        standing = call.effective()
        return next(
            (
                template.format_map(_Facts(filed.receipt))
                for filed in reversed(call.evidence)
                if (template := getattr(known.get(filed.operation), 'say', ''))
                and any(filed is s for s in standing)
            ),
            '',
        )

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
        return next(
            (later.id for later in order[order.index(step) + 1 :] if not later.done(call)), self.finish
        )

    async def record_update(
        self,
        call: FlowCall[Any],
        updates: Mapping[str, Any],
        choices: Mapping[str, Callable[[Any, Any], Any]],
    ) -> list[str]:
        """A structured step's update, whole or not at all: each pick counts on the offer for the state it ends in.

        What the caller asked for and what they picked often come in one
        reply: "access, and unlock my account". The pick rests on what was
        asked for, so the update takes the old offer away and the pick came
        from an offer that no longer stands (session 4g, trace 28723e6b: a
        time and its slot together, the slot refused, three requests). So a
        pick refused against the offers standing now is checked again: each
        of those choices is asked of its own function for the state the rest
        of the update ends in, in the step's order, so a pick another pick
        rests on counts first (a ticket, then a category, then a resolution).
        Nothing is committed until every pick is on its new offer; then the
        update, its resets and the new offers go in together. A stale id, one
        offered to another call, one nobody offered, and one wrong for the new
        request are still refused, and so is the whole update. Returns the
        refused picks. Raises `ValidationError` for a candidate that is not a
        valid state, with nothing changed.
        """
        refused = call.collect(choices, **updates)
        if not refused:
            return []
        rest = {name: value for name, value in updates.items() if name not in refused}
        unoffered, planned, stale = call._planned(choices, rest)
        if unoffered:
            return refused
        version, generation = call._version, call._generation
        facts = call.validated(call.state.facts.model_dump() | deepcopy(planned))
        made: dict[str, Options] = {}
        for name, choose in choices.items():
            if name not in refused:
                continue
            try:
                made[name] = await _options(choose, call.services, facts)
            except Exception:
                logger.exception('choice function for {} failed', name)
                return refused
            if str(updates[name]) not in made[name].options:
                return refused
            planned[name] = updates[name]
            facts = call.validated(facts.model_dump() | {name: deepcopy(updates[name])})
        if call._version != version or call._generation != generation:
            return refused
        call.update(**planned)
        call._offers.take(*stale)
        for name, options in made.items():
            call._offers.put(name, options.options, options.say, 0, call.state.agent)
        return []
