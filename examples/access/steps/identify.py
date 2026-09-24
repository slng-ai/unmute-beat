"""Step `identify`: look the requester up in the directory. Reading needs no approval."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from core.steps import ConfirmedOperation, FlowCall
from examples.access.backend import DIRECTORY


class Lookup(BaseModel):
    employee_id: str


class Identified(BaseModel):
    employee_id: str
    name: str
    match: bool


class LookupEmployee(ConfirmedOperation[Lookup, Identified]):
    """Identify the requester."""

    name = 'identify'
    args_type = Lookup
    receipt_type = Identified
    needs_approval = False

    async def _execute(self, call: FlowCall, args: Lookup, grant: None) -> dict:
        return DIRECTORY.lookup(args.employee_id)


OPERATION = LookupEmployee


def accepts(receipt: Mapping[str, Any]) -> bool:
    """Only a match identifies anybody; any other answer goes to the step's `on_error`."""
    return bool(receipt['match'])
