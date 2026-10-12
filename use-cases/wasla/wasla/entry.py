"""Wasla, as every launcher needs it (`core.Entry`): its flow, its ledger, the account it rings about.

Each call is about one account of the fake ledger, `ACCOUNT_ID` (default `WA-1001`, Layla Haddad):
Wasla rings the holder, so the bot speaks first.

`LEDGER`: The fake ledger, shared by every call of this process: what one call records, the next one
sees.

`ACCOUNT_ID`: The account each call is about. ponytail: one per process; a dialer would pass it per
call.
"""

import os
from pathlib import Path

import core
from wasla import app
from wasla.ledger import Ledger

LEDGER = Ledger()
ACCOUNT_ID = os.getenv('ACCOUNT_ID', 'WA-1001')

ENTRY = core.Entry(
    id=app.SPEC.id or 'wasla',
    name='wasla call',
    flow=lambda: app.FLOW,
    open=lambda flow, opening: app.open_call(flow, ACCOUNT_ID, LEDGER),
    greeting=app.greeting,
    manifest=Path(app.__file__).with_name('agent.yaml'),
)
