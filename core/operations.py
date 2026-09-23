"""One business action, with the checks every business action needs built in.

An `Operation` is the only way a step changes the business. Its `run` is a
template method marked `@final`, so a subclass fills in the steps and cannot
reorder or skip them:

1. validate the arguments at the boundary, whoever built them;
2. settle anything outstanding that must be resolved first (`_before`);
3. check the caller authorised *these* arguments (`_authorise`);
4. execute, once (`_execute`), noted on the call first, so an answer that
   authorised an attempt is never restored and spent a second time;
5. validate the service's answer into the receipt model before a field is read;
6. record the result: commit state, retain the receipt, speak (`_record`).

The default `_authorise` is the common case: the pending proposal must be
this operation's, for this exact value, and the caller must have agreed to
it. A business with richer rules (more than one way to authorise) overrides
it. Claiming the one action allowed per model reply stays with the
model-facing tool, which has the reply in front of it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, final

from pydantic import BaseModel
from pydantic_ai import ModelRetry
from pydantic_ai.usage import RunUsage, UsageLimits

from core.proposals import Proposal
from core.state import CallContext


class OutcomeUnknownError(Exception):
    """The service may or may not have done it. Never reported as success or as failure; reconcile first."""


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
        """Do it, in the one order that is safe. Refusals are `ModelRetry`, with a reason the model can use."""
        checked: ArgsT = self.args_type.model_validate(args.model_dump())  # ty: ignore[invalid-assignment]
        if (early := await self._before(call, checked)) is not None:
            return early
        grant = await self._authorise(call, checked, usage=usage, usage_limits=usage_limits)
        call.note_business_attempt()
        raw = await self._execute(call, checked, grant)
        receipt: ReceiptT = self.receipt_type.model_validate(raw)  # ty: ignore[invalid-assignment]
        return await self._record(call, checked, receipt)

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
