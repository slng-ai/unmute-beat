"""Step `eligibility`: ask the access policy whether this exact request is allowed. A check, not a change."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from core.steps import ConfirmedOperation, FlowCall
from examples.access.backend import DIRECTORY
from examples.access.steps.fields import Request


class Eligibility(BaseModel):
    eligible: bool
    reason: str


class CheckEligibility(ConfirmedOperation[Request, Eligibility]):
    """Ask the policy about the request as collected."""

    name = 'eligibility'
    args_type = Request
    receipt_type = Eligibility
    needs_approval = False

    async def _execute(self, call: FlowCall, args: Request, grant: None) -> dict:
        return DIRECTORY.check(args.team, args.system, args.level, args.start, args.end)


OPERATION = CheckEligibility


def accepts(receipt: Mapping[str, Any]) -> bool:
    """A refusal is a receipt too, but it does not complete the step."""
    return bool(receipt['eligible'])
