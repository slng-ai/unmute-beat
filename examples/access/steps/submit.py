"""Steps `confirm` and `submit`: the readback the caller agrees to, and the filing it authorises.

Both steps name this module, so they share one operation: the confirm step
reads back `proposal(args)`, and the action runs only for those same
arguments. A submission whose answer never came back is not a failure: it may
well be on file, so it raises `OutcomeUnknownError` and the flow ends pending.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Literal

from pydantic import BaseModel

from core.operations import OutcomeUnknownError
from core.proposals import BaseProposal
from core.steps import ConfirmedOperation, FlowCall
from examples.access.backend import DIRECTORY
from examples.access.steps.fields import Request


class Submitted(BaseModel):
    request_id: str
    status: Literal['submitted']
    """Filed, not approved: the receipt says only what happened."""


class SubmitRequest(ConfirmedOperation[Request, Submitted]):
    """File the request, once the caller agreed to this exact readback of it."""

    name = 'submit'
    args_type = Request
    receipt_type = Submitted
    proposal_about = 'submit'

    def proposal(self, args: Request) -> BaseProposal:
        digest = sha256(args.model_dump_json().encode()).hexdigest()[:12]
        return BaseProposal(
            about='submit',
            value=digest,
            spoken=(
                f'To confirm: {args.level} access to {args.system} for the {args.team} team, '
                f'from {args.start:%d %B} to {args.end:%d %B}, because {args.reason}. Shall I submit it?'
            ),
        )

    async def _execute(self, call: FlowCall, args: Request, grant: object) -> dict:
        try:
            return DIRECTORY.submit(args.model_dump(mode='json'))
        except TimeoutError as lost:
            raise OutcomeUnknownError(str(lost)) from None


OPERATION = SubmitRequest
