"""Wasla's startup: its manifest, compiled once, and one session per call, opened on one ledger account.

flow = wasla.app.FLOW
call = wasla.app.open_call(flow, 'WA-1001', ledger)   # the holder and the account come from the ledger
core.greet(call, wasla.app.greeting(flow, call))      # "... Am I speaking with Layla Haddad?"
"""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import core
from wasla import words
from wasla.ledger import Ledger
from wasla.operations import Wasla
from wasla.receipts import Account, Holder
from wasla.state import WaslaState

SPEC = core.load(Path(__file__).with_name('agent.yaml'))
"""The manifest, checked whole: an unknown key, a missing file or a prompt naming a hidden field fails here."""
FLOW = SPEC.compile()
"""The compiled flow every call of this process shares. Calls share nothing else."""


def open_call(
    flow: core.StepFlow,
    account_id: str,
    ledger: Ledger,
    *,
    clock: Callable[[], datetime] | None = None,
) -> core.FlowCall:
    """A new session about one account: its holder and amount are put in the state before the first word."""
    found = ledger.account(account_id)
    state = WaslaState(
        account_id=account_id,
        holder=Holder(first_name=found.first_name, last_name=found.last_name),
        account=Account(
            account_id=account_id,
            amount=found.amount,
            amount_words=words.amount(found.amount),
            merchant=found.merchant,
            purchase_spoken=words.day(found.purchase_date),
            due_spoken=words.day(found.due_date),
        ),
    )
    return flow.open(state, services=Wasla(ledger, clock), clock=clock)


def greeting(flow: core.StepFlow, call: core.FlowCall) -> Any:
    """The opening line, by code: who is calling, and whether this is the account holder."""
    holder = call.state.facts.holder
    return flow.greeting(call, f'{SPEC.greeting} Am I speaking with {holder.first_name} {holder.last_name}?')
