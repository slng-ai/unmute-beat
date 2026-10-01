"""What Wasla's ledger may answer, as `core.Receipt` models, and what a call is opened with.

`Holder` and `Account` are set when the call opens, from the ledger
(`app.open_call`), never by a model. They are two fields on purpose: a step
that may not reveal the account sees `holder` and not `account`
(`context.facts` in `agent.yaml`), so its prompt cannot even name the amount.
"""

from datetime import date, time
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict

import core

Concern = Literal['hardship', 'legal', 'dispute', 'refused']
CODES: dict[Concern, str] = {'hardship': 'HARDSHIP', 'legal': 'LEGAL', 'dispute': 'DISPUTE', 'refused': 'RTP'}
"""The ledger's outcome code for each concern (`ledger.py`)."""


class Holder(BaseModel):
    """Who the call is for. Safe to say before they are verified: the caller is asked if they are this person."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    first_name: str
    last_name: str


class Account(BaseModel):
    """The account the call is about, with its words for speech. Only verified steps see it."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    account_id: str
    amount: Decimal
    amount_words: str
    merchant: str
    purchase_spoken: str
    due_spoken: str


class Verified(core.Receipt):
    """The ledger matched the date of birth: the caller is the account holder for the rest of the call."""

    status: Literal['verified']
    account_id: str
    next: Literal['account'] = 'account'


class Opened(core.Receipt):
    """The account opened for the holder: the recording notice and the amount said, or a time to call back."""

    status: Literal['opened']
    account_id: str
    said: str
    next: Literal['', 'callback'] = ''


class Promised(core.Receipt):
    """A promise to pay recorded for a day."""

    status: Literal['PTP']
    account_id: str
    reference: Annotated[str, core.Subject('promise')]
    day: date
    said: str
    revision: Annotated[int, core.Version()] = 1


class CalledBack(core.Receipt):
    """A callback noted for a day and time."""

    status: Literal['CB']
    account_id: str
    reference: Annotated[str, core.Subject('callback')]
    day: date
    at: time
    said: str
    revision: Annotated[int, core.Version()] = 1


class Unlisted(core.Receipt):
    """The number taken off the account: no more calls to it."""

    status: Literal['WN']
    account_id: str
    reference: Annotated[str, core.Subject('number')]
    revision: Annotated[int, core.Version()] = 1


class Noted(core.Receipt):
    """A concern noted with the caller's own words, and what happens next."""

    status: Literal['HARDSHIP', 'LEGAL', 'DISPUTE', 'RTP']
    account_id: str
    reference: Annotated[str, core.Subject('concern')]
    said: str
    revision: Annotated[int, core.Version()] = 1
