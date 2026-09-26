"""One business action, with the checks every business action needs built in.

An `Operation` is the only way a step changes the business. Its `run` is a
template method marked `@final`, so a subclass fills in the steps and cannot
reorder or skip them:

1. validate the arguments at the boundary, whoever built them;
2. settle anything outstanding that must be resolved first (`_before`);
3. check the caller authorised *these* arguments (`_authorise`);
4. find the intent these arguments are (`CallContext.intent_for`), refuse
   one that may already have landed, and mark it dispatched, synchronously,
   before anything is awaited;
5. execute, once (`_execute`), noted on the call first, so an answer that
   authorised an attempt is never restored and spent a second time;
6. validate the service's answer into the receipt model before a field is read;
7. record the result: commit state, retain the receipt, speak (`_record`).

The default `_authorise` is the common case: the pending proposal must be
this operation's, for this exact value, and the caller must have agreed to
it. A business with richer rules (more than one way to authorise) overrides
it. Claiming the one action allowed per model reply stays with the
model-facing tool, which has the reply in front of it.

What can go wrong after step 4 is kept apart, because each means something
different for the next attempt (the retry rule of
https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/):

* a validated receipt that says it worked: `succeeded`;
* a validated receipt the application declares a terminal no (`rejects`),
  or its own `Refused`: `rejected`, an authoritative no;
* anything else once dispatched (a cancellation, a timeout, a lost
  connection, an answer that does not validate, a plain `ModelRetry`, a
  receipt that says `pending` or anything the application did not declare
  terminal): `unknown`. An exception alone never says the backend did
  nothing. The same intent is never sent again until it is reconciled,
  unless the backend has declared it dedups by the intent id (`idempotent`).

Two runs of one intent on one call cannot both reach the backend: the first
claims it (`CallContext._claimed`) in the same synchronous stretch as the
eligibility check, before anything is awaited. Across processes the backend
arbitrates, by the intent id it is handed.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar, Literal, final

from loguru import logger
from opentelemetry import trace
from pydantic import BaseModel
from pydantic_ai import ModelRetry
from pydantic_ai.usage import RunUsage, UsageLimits

from core.proposals import Proposal
from core.state import CallContext, Intent

_tracer = trace.get_tracer('voice.operations')

NOTE_INTENT_SECONDS = 2.0
"""How long a service's `note_intent` may take before the intent is taken as never dispatched."""


def operation_token(*parts: object) -> str:
    """A stable name derived from its parts. Faro's legacy operations key their own records with it.

    Not an intent: arguments alone cannot tell a retry of one action from a
    second action with the same arguments. Core's operations are named by
    their `Intent`, which it issues.
    """
    return hashlib.sha256('|'.join(str(part) for part in parts).encode()).hexdigest()[:16]


class OutcomeUnknownError(Exception):
    """The service may or may not have done it. Never reported as success or as failure; reconcile first."""


class Refused(ModelRetry):
    """The application's authoritative no from inside `_execute`: nothing was done, and nothing will be.

    Raise it only where the application knows no effect happened, such as a
    check made before its backend is called. The model reads the reason, as
    with any `ModelRetry`. A plain `ModelRetry` after dispatch is not this: it
    leaves the intent unknown.
    """


type Outcome = Literal['succeeded', 'rejected', 'unknown']
"""What a validated receipt that is about these arguments says happened."""


class Operation[C: CallContext, ArgsT: BaseModel, ReceiptT: BaseModel, OutputT](ABC):
    """A business action: authorised by the caller's answer, executed once, answered with a validated receipt.

    Shared config, per-call state: an instance holds no call data, so one
    instance serves every call.
    """

    name: ClassVar[str]
    """The action's name in traces, budgets and receipts: the tool it stands behind."""
    args_type: ClassVar[type[BaseModel]]
    receipt_type: ClassVar[type[BaseModel]]
    needs_approval: ClassVar[bool] = True
    """Whether the caller must agree to a readback of these exact arguments first."""
    changes: ClassVar[bool] = True
    """Whether it changes a business resource. Separate from `needs_approval`: a lookup may need neither."""
    idempotent: ClassVar[bool] = False
    """The backend dedups by the intent id, so an intent whose outcome is unknown may be sent again, as itself."""
    subject: ClassVar[str] = ''
    """The receipt field naming the resource it acts on (a booking id, a ticket id). See `CallContext.effective`."""
    resource: ClassVar[str] = ''
    """The kind of resource `subject` names (`ticket`, `address`): ids are unique only within it. Defaults to `subject`."""
    version: ClassVar[str] = ''
    """The receipt field holding the resource's version after this change: what orders two changes of one resource."""
    recap: ClassVar[str] = ''
    """What a goodbye may say about a receipt of it that still stands, formatted from the receipt. Empty: nothing."""
    proposal_about: ClassVar[str] = ''
    """Which kind of question authorises this, for the default `_authorise`. Defaults to `name`."""

    @final
    async def run(
        self,
        call: C,
        args: ArgsT,
        *,
        usage: RunUsage | None = None,
        usage_limits: UsageLimits | None = None,
    ) -> OutputT:
        """Do it, in the one order that is safe. Refusals are `ModelRetry`, with a reason the model can use.

        Every authorised attempt leaves one `write` span, whichever path ran it
        (a model's tool or `advance` with no model), carrying `write.audit`:
        the action and its intent, its arguments and the arguments the
        question read back when it was asked (`proposal_args`), the question
        the caller was answering (its serial and the segments that carried it
        to them) and what they said, and how it ended (ok, failed, or unknown
        when the service's outcome is). `evals.ledger` and `load_report` read
        it. A refusal before dispatch leaves none: nothing was attempted.

        A validated receipt is filed on the call (`CallContext.evidence`)
        before anything else can wait: a caller cutting in during the
        confirmation cancels the speech, never the record of what the service
        did. Evidence is append-only; a later turn, a new topic or a stale
        generation never erases it.

        A cancellation after dispatch leaves the intent unknown and is
        re-raised: the cleanup is a few synchronous assignments, and nothing
        here shields the service call itself.
        """
        checked: ArgsT = self.args_type.model_validate(args.model_dump())  # ty: ignore[invalid-assignment]
        if (early := await self._before(call, checked)) is not None:
            return early
        asked = call.pending_question()
        answering = call.answering()
        heard = call.heard
        grant = await self._authorise(call, checked, usage=usage, usage_limits=usage_limits)
        intent = call.intent_for(self.name, checked.model_dump_json())
        self._may_dispatch(call, intent)
        call._claimed.add(intent.id)
        try:
            return await self._dispatch(call, checked, intent, asked, answering, heard, grant)
        finally:
            call._claimed.discard(intent.id)

    async def _dispatch(
        self,
        call: C,
        checked: ArgsT,
        intent: Intent,
        asked: Proposal | None,
        answering: tuple[int, tuple[str, ...]],
        heard: str,
        grant: Any,
    ) -> OutputT:
        """Record the intent, send it once, and settle what came back. Runs only while this run holds the claim."""
        with _tracer.start_as_current_span('write') as span:
            audit: dict[str, Any] = {
                'action': self.name,
                'intent': intent.id,
                'operation': intent.id,
                'args': checked.model_dump(mode='json'),
                'proposal_args': _proposal_args(asked),
                'needs_approval': self.needs_approval,
                'changes': self.changes,
                'question': asked.spoken if asked is not None else '',
                'about': asked.about if asked is not None else '',
                'asked_turn': asked.turn if asked is not None else None,
                'proposal': answering[0] if asked is not None else 0,
                'segments': list(answering[1]) if asked is not None else [],
                'call': call.id,
                'turn': call.turn,
                'generation': call._generation,
                'sequence': call.next_audit(),
                'heard': heard,
                'playback': call.playback_for(answering[0]) if asked is not None else None,
                'simulated_delivery': call.simulated_for(answering[0]) if asked is not None else [],
                'status': 'unknown',
            }
            try:
                try:
                    await self._note_intent(call, intent)
                except asyncio.CancelledError:
                    audit.update(status='failed', outcome='prepared', error='cancelled before dispatch')
                    raise
                except Exception as error:
                    audit.update(
                        status='failed', outcome='prepared', error=f'{type(error).__name__}: {error}'
                    )
                    raise ModelRetry(
                        f'Nothing was done: the {self.name} could not be started just now. Tell the caller, '
                        'and do not say it was done.'
                    ) from None
                call.set_intent(intent.id, status='dispatched')
                call.note_business_attempt()
                try:
                    raw = await self._execute(call, checked, grant)
                    receipt: ReceiptT = self.receipt_type.model_validate(raw)  # ty: ignore[invalid-assignment]
                except Refused:
                    call.set_intent(intent.id, status='rejected')
                    audit.update(status='failed', outcome='rejected')
                    raise
                except ModelRetry as error:
                    self._lost(call, intent, checked, audit, f'ModelRetry after dispatch: {error}')
                    raise OutcomeUnknownError(f'{self.name}: a retry signal after dispatch') from error
                except asyncio.CancelledError:
                    self._lost(call, intent, checked, audit, 'cancelled after dispatch')
                    raise
                except OutcomeUnknownError as error:
                    self._lost(call, intent, checked, audit, str(error))
                    raise
                except Exception as error:
                    self._lost(call, intent, checked, audit, f'{type(error).__name__}: {error}')
                    raise OutcomeUnknownError(
                        f'{self.name}: {type(error).__name__} after dispatch'
                    ) from error
                return await self._settled(call, checked, intent, receipt, audit)
            finally:
                span.set_attribute('write.audit', json.dumps(audit, default=str))

    async def _settled(
        self, call: C, args: ArgsT, intent: Intent, receipt: ReceiptT, audit: dict[str, Any]
    ) -> OutputT:
        """A validated receipt for this intent: settle the intent, file the evidence, then record it.

        Shared by a first answer and a reconciliation, so both reach the same
        state. The evidence is filed before `_record` can fail or be
        cancelled: a success whose speech or state projection then fails is
        still a success, kept for repair (`CallContext._unpresented`) and
        never sent again.

        A receipt that is neither a success nor a declared refusal (a
        `pending`, a status nobody declared) settles nothing: the intent stays
        unknown and nothing is filed. A reconciled success whose resource has
        moved on since (`version` below the application's current one) is
        history: filed and audited, never said, assigned or recapped.
        """
        data = receipt.model_dump(mode='json')
        outcome = self.outcome(receipt.model_dump())
        if outcome == 'unknown':
            self._lost(call, intent, args, audit, 'the answer is neither a success nor a declared refusal')
            audit['receipt'] = data
            raise OutcomeUnknownError(f'{self.name}: the answer settles nothing yet ({data})')
        succeeded = outcome == 'succeeded'
        call.set_intent(intent.id, status=outcome)
        call._unknown.pop(intent.id, None)
        resource = str(data.get(self.subject, '')) if self.subject else ''
        version = data.get(self.version) if self.version else None
        kind = self.resource or self.subject
        if succeeded and resource and audit.get('reconciled') and isinstance(version, int):
            call.note_version(kind, resource, await self._current_version(call, kind, resource, version))
        call.note_evidence(
            self.name,
            args.model_dump(mode='json'),
            data,
            proposal=audit.get('proposal', 0),
            segments=tuple(audit.get('segments', ())),
            intent=intent.id,
            succeeded=succeeded,
            changes=self.changes,
            resource=resource,
            identity=intent.subject,
            kind=kind,
            version=version if isinstance(version, int) else None,
        )
        audit.update(status='ok' if succeeded else 'failed', outcome=outcome, receipt=data)
        if intent.subject and call.identity() and intent.subject != call.identity():
            audit['presented'] = False
            logger.info('{} settled for another caller than the one on the line: filed, not said', self.name)
            return None  # ty: ignore[invalid-return-type]
        if succeeded and resource and not call.is_latest(kind, resource, version):
            audit.update(presented=False, historical=True)
            logger.info('{} for {} {} is history: a later change stands', self.name, kind, resource)
            return None  # ty: ignore[invalid-return-type]
        if succeeded:
            call._unpresented[intent.id] = (self, args, receipt.model_dump())
        try:
            output = await self._record(call, args, receipt)
        except ModelRetry:
            call._unpresented.pop(intent.id, None)
            raise
        except BaseException as error:
            audit['presented'] = False
            logger.warning('{} succeeded, and recording it failed: {}', self.name, type(error).__name__)
            raise
        call._unpresented.pop(intent.id, None)
        audit['presented'] = True
        return output

    async def present_again(self, call: C, args: ArgsT, receipt: Mapping[str, Any]) -> OutputT:
        """Record a success whose recording failed, from its filed receipt: state and speech only, nothing sent."""
        owed = next(
            (i for i, (op, held, _) in call._unpresented.items() if op is self and held == args), None
        )
        output = await self._record(call, args, self.receipt_type.model_validate(receipt))  # ty: ignore[invalid-argument-type]
        if owed is not None:
            call._unpresented.pop(owed, None)
        return output

    def _lost(self, call: C, intent: Intent, args: ArgsT, audit: dict[str, Any], why: str) -> None:
        """The intent was dispatched and nothing settled it: unknown, held for reconciliation before any retry."""
        call.set_intent(intent.id, status='unknown')
        call._unknown[intent.id] = (self, args)
        audit.update(status='unknown', outcome='unknown', error=why)

    def _may_dispatch(self, call: C, intent: Intent) -> None:
        """Refuse an intent that may already have landed, and any new one after the caller said goodbye.

        An unknown intent is sent again only as itself, and only to a backend
        that dedups by its id (`idempotent`). While any earlier intent of this
        operation for this caller is unknown, a different one is refused too:
        two bookings in flight for one caller is the duplicate this exists to
        prevent, and which of them landed is for reconciliation to say.
        """
        if intent.status == 'dispatched' or intent.id in call._claimed:
            raise OutcomeUnknownError(f'{self.name}: this request is still waiting for an answer')
        if intent.status == 'unknown' and not self.resendable:
            raise OutcomeUnknownError(
                f'{self.name}: an earlier attempt may have landed, so it is not sent again'
            )
        if self.changes and any(
            other.operation == self.name
            and other.subject == intent.subject
            and other.status in ('dispatched', 'unknown')
            and other.id != intent.id
            for other in call.intents
        ):
            raise OutcomeUnknownError(
                f'{self.name}: an earlier request may have landed; it is checked before anything new is sent'
            )
        if call.departed and intent.status == 'prepared' and self.changes:
            raise ModelRetry(
                f'Nothing was done: the caller has said goodbye, so no new {self.name} starts. Do not say it was done.'
            )

    @property
    def resendable(self) -> bool:
        """Whether an intent whose outcome is unknown may be sent again, as itself: a deduping backend, or a pure read."""
        return self.idempotent or not self.changes

    async def _note_intent(self, call: C, intent: Intent) -> None:
        """Hand the intent to the service's own durable record, if it keeps one (`note_intent`), before dispatch.

        Optional: a service without it keeps intents only as long as this
        process does. Bounded, so a slow store cannot hold the turn; a failure
        means the intent was never dispatched. A read (`changes: false`) has
        no effect to reconcile, so it is not recorded.
        """
        if not self.changes:
            return
        services = getattr(call, 'services', None)
        note = getattr(services, 'note_intent', None)
        if note is None:
            return
        async with asyncio.timeout(NOTE_INTENT_SECONDS):
            result = note(intent)
            if asyncio.iscoroutine(result):
                await result

    def succeeded(self, receipt: Mapping[str, Any]) -> bool:
        """Whether a validated receipt says the action worked. Base: any validated receipt."""
        return True

    def outcome(self, receipt: Mapping[str, Any]) -> Outcome:
        """What a validated receipt about these arguments says. Base: a success, or else an authoritative no."""
        return 'succeeded' if self.succeeded(receipt) else 'rejected'

    async def _current_version(self, call: C, kind: str, resource: str, version: int) -> int:
        """The resource's version now, as the service says (`current_version`), for a success learnt by reconciliation.

        A receipt found by reconciliation says what the intent did then, not
        what stands now: the ticket may have been reopened since. Optional
        on the service, like `note_intent`, and bounded. Without it, or when
        it fails, the receipt's own version is taken as current only if
        nothing in this call knows better; a failure counts as "something may
        be newer", so the old receipt is not said.
        """
        services = getattr(call, 'services', None)
        ask = getattr(services, 'current_version', None)
        if ask is None:
            return version
        try:
            async with asyncio.timeout(NOTE_INTENT_SECONDS):
                if inspect.iscoroutinefunction(ask):
                    found = await ask(kind, resource)
                else:
                    found = await asyncio.to_thread(ask, kind, resource)
        except Exception as error:
            logger.warning('current_version failed ({}): {} taken as history', type(error).__name__, resource)
            return version + 1
        return found if isinstance(found, int) and not isinstance(found, bool) else version + 1

    async def _before(self, call: C, args: ArgsT) -> OutputT | None:
        """Settle anything that must be resolved before a new action. Return an output to stop here."""
        return None

    async def _authorise(
        self, call: C, args: ArgsT, *, usage: RunUsage | None, usage_limits: UsageLimits | None
    ) -> Any:
        """Check the caller agreed to these exact arguments, and consume their answer. Return what `_execute` needs.

        The default: the pending question is this operation's, for the value
        `proposal(args)` would read back, and `_agreed` says the caller agreed.
        """
        if not self.needs_approval:
            return None
        question = call.pending(self.proposal_about or self.name, value=self.proposal(args).value)
        if question is None:
            raise ModelRetry(
                f'Nothing was done: the caller has not been asked to agree to this exact {self.name}. '
                'Read it back and wait for their answer.'
            )
        if not await self._agreed(call, question, usage=usage, usage_limits=usage_limits):
            raise ModelRetry(
                f'Nothing was done: the caller did not agree to the {self.name} read back to them.'
            )
        call.answered()
        return question

    def proposal(self, args: ArgsT) -> Proposal:
        """The question that would authorise these arguments, worded by the application."""
        raise NotImplementedError(f'{type(self).__name__} has no readback; override proposal or _authorise')

    def proposal_for(self, call: C, args: ArgsT) -> Proposal:
        """The same question, worded with whatever the call's services know (a slot's spoken time). What is asked."""
        return self.proposal(args)

    async def _agreed(
        self, call: C, question: Proposal, *, usage: RunUsage | None, usage_limits: UsageLimits | None
    ) -> bool:
        """Whether this turn's reply agrees to `question`. Safe by default: nobody agreed."""
        return False

    @abstractmethod
    async def _execute(self, call: C, args: ArgsT, grant: Any) -> Any:
        """Ask the service to do it, once. Return its raw answer; it is validated into `receipt_type` next."""

    @abstractmethod
    async def _record(self, call: C, args: ArgsT, receipt: ReceiptT) -> OutputT:
        """Commit what the receipt proves, retain it, and say it. Only runs after a validated receipt."""


def _proposal_args(asked: Any) -> Any:
    """The arguments the pending question read back, as they were when it was asked. None when nothing was asked.

    Taken from the question's own `value`, recorded when it was put to the
    caller (`ProposalLedger.ask` copies it), never from the arguments about to
    run: a mismatch between the two is what an evaluator checks for.
    """
    if asked is None:
        return None
    try:
        return json.loads(asked.value)
    except TypeError, ValueError:
        return asked.value
