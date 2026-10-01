"""What a Wasla call knows: one plain Pydantic model. Everything else is core's.

`holder` and `account` are set when the call opens. `verified` is the date of
birth check's receipt, set whole and only by it: no model can mark a caller
verified. A promise day is a `date` from today up to the deadline, today plus
`WINDOW_DAYS`; a day past it is refused with the locale's words
(`after_deadline` in `agent.yaml`).
"""

from datetime import date, time, timedelta
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationInfo
from pydantic_core import PydanticCustomError

import core
from wasla import words
from wasla.receipts import Account, Concern, Holder, Verified

WINDOW_DAYS = 10
"""How far ahead a promise to pay or a callback may be: today plus ten days."""


def _by_deadline(value: date, info: ValidationInfo) -> date:
    """A day no later than the call's today plus `WINDOW_DAYS`. Without a call there is no today: nothing refused."""
    today = (info.context or {}).get('today')
    if isinstance(today, date) and value > (last := today + timedelta(days=WINDOW_DAYS)):
        raise PydanticCustomError(
            'after_deadline',
            '{day} is after the deadline, {deadline}. Ask for a day from today up to the deadline.',
            {'day': value.isoformat(), 'deadline': last.isoformat(), 'deadline_spoken': words.day(last)},
        )
    return value


InWindow = Annotated[date, core.NotPast, AfterValidator(_by_deadline)]


class WaslaState(BaseModel):
    """The facts of one Wasla call. Every field has a default; the ledger fills the first three at open."""

    model_config = ConfigDict(extra='forbid')

    account_id: str | None = None
    holder: Holder | None = None
    account: Account | None = None

    birth_date: date | None = Field(
        None, description="The caller's date of birth as they said it, as YYYY-MM-DD. Never guess a part."
    )
    verified: Verified | None = None
    """Set only by the date of birth check. The account step requires it."""

    good_time: bool | None = Field(
        None, description='Whether now is a good time for the holder to talk about their account.'
    )
    promise_day: InWindow | None = Field(
        None,
        description='The day the holder promises to pay, as YYYY-MM-DD, worked out from the calendar you are '
        'shown: today when they pay today.',
    )
    callback_day: InWindow | None = Field(
        None, description='The day to call back, as YYYY-MM-DD, worked out from the calendar you are shown.'
    )
    callback_time: time | None = Field(
        None, description='The time to call back, HH:MM on a 24-hour clock (17:00 for 5 PM).'
    )
    concern_kind: Concern | None = Field(
        None,
        description='What the caller raised: hardship (illness, disability, money trouble), legal (police, a '
        'lawyer, a regulator, threats), dispute (they never made or got this purchase, or suspect fraud), or '
        'refused (they will not pay in this period).',
    )
    concern_note: core.Quoted | None = Field(
        None, description='What they said about it, in their own words: the shortest complete excerpt.'
    )
