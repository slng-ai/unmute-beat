"""Wasla's services and its six actions, one class each: arguments, what it does, how it is read back.

Two are lookups. `VerifyHolder` checks a date of birth, read back first; its
receipt is the only way a caller becomes verified, and it moves them to the
account. `OpenAccount` is carried out by code when the holder says whether
now is a good time: its words are the recording notice and the amount, or it
moves them to a callback. The four writes record a promise, a callback, a
wrong number and a concern; core runs each only on a heard yes to its
read-back. Each write's ledger token is core's intent id, so it lands once.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import AliasPath, Field

import core
from wasla import words
from wasla.ledger import Ledger
from wasla.receipts import CODES, CalledBack, Concern, Noted, Opened, Promised, Unlisted, Verified

TZ = ZoneInfo('Asia/Dubai')
"""The call's clock zone, as in `agent.yaml`."""
SUPPORT = (
    'Wasla support is on eight hundred, nine two seven, five two, from nine AM to six PM, '
    'or by email at help at wasla dot a e.'
)


@dataclass
class Wasla:
    """One call's services: the ledger, and the clock. The model never sees them."""

    ledger: Ledger
    clock: Callable[[], datetime] | None = None

    def now(self) -> datetime:
        """The call's clock: core reads the call's today from it once, when the call opens."""
        return self.clock() if self.clock else datetime.now(TZ)


AccountId = Annotated[str, Field(min_length=1)]


def _found(ledger: Ledger, intent: core.Intent) -> dict[str, Any] | None:
    """What a write with a lost answer did, by its token. None: the ledger never saw it, so it stays unknown."""
    return ledger.outcome_of(intent.id)


class VerifyHolder(core.Lookup[Wasla]):
    """Check the date of birth the caller gave against the account. Run only on their yes to it read back."""

    account_id: AccountId
    birth_date: date
    matches = ('account_id',)
    restated_confirms = True
    """The same date said again is a yes to its read-back."""
    route = 'next'
    say = 'Thank you, that matches.'

    def run(self, wasla: Wasla, intent: core.Intent) -> Verified:
        """One receipt type, and a wrong date as `core.Refused`: the check changes nothing but its try count.

        ponytail: `core.Refused` carries what the model must do next (ask
        again, or give the support line and leave), in words a typed no would
        not. A union with a `core.Rejection` (`Verified | NotVerified`) now
        routes as well; switch to it only after a live check of the refusals.
        """
        answer = wasla.ledger.verify(self.account_id, self.birth_date)
        if answer['status'] == 'mismatch':
            raise core.Refused(
                f'That date of birth does not match the account; {answer["tries_left"]} tries left. '
                'Reveal nothing about the account. Say kindly that it does not match and ask them to say it again.'
            )
        if answer['status'] == 'locked':
            raise core.Refused(
                'Too many wrong dates of birth: the account is locked for today. Reveal nothing. Say you cannot '
                f'go on with this call, give this support line, and call goodbye: {SUPPORT}'
            )
        return Verified.model_validate(answer)

    def readback(self, wasla: Wasla) -> str:
        return f'Is your date of birth {words.birthday(self.birth_date)}?'


class OpenAccount(core.Lookup[Wasla]):
    """Open the account with the verified holder: the recording notice and the amount, or a callback if now is bad."""

    account_id: AccountId
    good_time: bool
    amount_words: str = Field(validation_alias=AliasPath('account', 'amount_words'))
    merchant: str = Field(validation_alias=AliasPath('account', 'merchant'))
    matches = ('account_id',)
    route = 'next'
    say = '{said}'

    def run(self, wasla: Wasla, intent: core.Intent) -> Opened:
        if not self.good_time:
            return Opened(
                status='opened', account_id=self.account_id, said='No problem at all.', next='callback'
            )
        return Opened(
            status='opened',
            account_id=self.account_id,
            said='Thank you. This call is recorded for training and quality. '
            f"I'm calling about {self.amount_words} for your purchase from {self.merchant}, which is now overdue. "
            'Could you make the payment today?',
        )


class RecordPromise(core.Write[Wasla]):
    """Record the holder's promise to pay the full amount on a day. Run only on their yes to the read-back."""

    account_id: AccountId
    promise_day: date
    amount: Decimal = Field(validation_alias=AliasPath('account', 'amount'))
    amount_words: str = Field(validation_alias=AliasPath('account', 'amount_words'))
    matches = ('account_id',)
    idempotent = True
    say = '{said}'
    recap = 'Your promise to pay is recorded, reference {reference}.'

    def _promised(self, raw: dict[str, Any], today: date) -> Promised:
        when = 'today' if raw['day'] == today else f'on {words.day(raw["day"])}'
        said = (
            f'Thank you. Your promise to pay {when} is recorded. '
            'You can pay in the Wasla app or on our website. Is there anything else I can help with?'
        )
        return Promised(
            status='PTP', account_id=raw['account_id'], reference=raw['reference'], day=raw['day'], said=said
        )

    def run(self, wasla: Wasla, intent: core.Intent) -> Promised:
        try:
            raw = wasla.ledger.promise(intent.id, self.account_id, self.promise_day, self.amount)
        except Exception as error:
            raise core.OutcomeUnknownError(f'the ledger did not answer: {error}') from error
        return self._promised(raw, wasla.now().date())

    def reconcile(self, wasla: Wasla, intent: core.Intent) -> Promised | None:
        raw = _found(wasla.ledger, intent)
        return self._promised(raw, wasla.now().date()) if raw else None

    def readback(self, wasla: Wasla) -> str:
        return f'Shall I record your promise to pay {self.amount_words} on {words.day(self.promise_day)}?'


class ScheduleCallback(core.Write[Wasla]):
    """Note a day and time to call back, for the holder or someone who answered for them."""

    account_id: AccountId
    callback_day: date
    callback_time: time
    matches = ('account_id',)
    idempotent = True
    say = '{said}'
    recap = 'A callback is noted, reference {reference}.'

    def _noted(self, raw: dict[str, Any]) -> CalledBack:
        said = f"That's noted: we'll call back on {words.day(raw['day'])} at {words.clock(raw['at'])}. Thank you for your time."
        return CalledBack(
            status='CB',
            account_id=raw['account_id'],
            reference=raw['reference'],
            day=raw['day'],
            at=raw['at'],
            said=said,
        )

    def run(self, wasla: Wasla, intent: core.Intent) -> CalledBack:
        try:
            raw = wasla.ledger.callback(intent.id, self.account_id, self.callback_day, self.callback_time)
        except Exception as error:
            raise core.OutcomeUnknownError(f'the ledger did not answer: {error}') from error
        return self._noted(raw)

    def reconcile(self, wasla: Wasla, intent: core.Intent) -> CalledBack | None:
        raw = _found(wasla.ledger, intent)
        return self._noted(raw) if raw else None

    def readback(self, wasla: Wasla) -> str:
        return f'Shall I note that we call back on {words.day(self.callback_day)} at {words.clock(self.callback_time)}?'


class MarkWrongNumber(core.Write[Wasla]):
    """Take this number off the account, so it is never called about it again."""

    account_id: AccountId
    matches = ('account_id',)
    idempotent = True
    say = "Done. You won't get any more calls from us on this number. Sorry for the trouble."
    recap = 'This number is off our list.'

    def run(self, wasla: Wasla, intent: core.Intent) -> Unlisted:
        try:
            raw = wasla.ledger.wrong_number(intent.id, self.account_id)
        except Exception as error:
            raise core.OutcomeUnknownError(f'the ledger did not answer: {error}') from error
        return Unlisted(status='WN', account_id=raw['account_id'], reference=raw['reference'])

    def reconcile(self, wasla: Wasla, intent: core.Intent) -> Unlisted | None:
        raw = _found(wasla.ledger, intent)
        return (
            Unlisted(status='WN', account_id=raw['account_id'], reference=raw['reference']) if raw else None
        )

    def readback(self, wasla: Wasla) -> str:
        return "I'm sorry to have bothered you. Shall I take this number off our list, so we don't call it again?"


NEXT_STEP: dict[Concern, str] = {
    'hardship': f"I've passed that on. Please contact support with any documents, and they'll look at it with you. {SUPPORT}",
    'legal': "I've passed that to our internal team. They'll look into it and contact you within twenty-four hours.",
    'dispute': "I've recorded your dispute. Our team will look into the purchase and contact you; "
    'nothing more is asked of you until then.',
    'refused': "I've noted your answer and your feedback. Thank you for your time today.",
}
"""What Wasla does next for each concern, said once it is noted."""


class RecordConcern(core.Write[Wasla]):
    """Note hardship, a legal matter, a dispute or a refusal to pay, in the caller's own words."""

    account_id: AccountId
    concern_kind: Concern
    concern_note: str = Field(min_length=1)
    matches = ('account_id',)
    idempotent = True
    say = '{said}'
    recap = 'Your concern is noted, reference {reference}.'

    def _noted(self, raw: dict[str, Any]) -> Noted:
        kind = next(kind for kind, code in CODES.items() if code == raw['code'])
        return Noted(
            status=raw['code'], account_id=raw['account_id'], reference=raw['reference'], said=NEXT_STEP[kind]
        )

    def run(self, wasla: Wasla, intent: core.Intent) -> Noted:
        try:
            raw = wasla.ledger.concern(
                intent.id, self.account_id, CODES[self.concern_kind], self.concern_note
            )
        except Exception as error:
            raise core.OutcomeUnknownError(f'the ledger did not answer: {error}') from error
        return self._noted(raw)

    def reconcile(self, wasla: Wasla, intent: core.Intent) -> Noted | None:
        raw = _found(wasla.ledger, intent)
        return self._noted(raw) if raw else None

    def readback(self, wasla: Wasla) -> str:
        note = self.concern_note.strip().rstrip('.?!')
        return {
            'hardship': f'Shall I pass this to our support team: {note}?',
            'legal': f'Shall I pass this to our internal team: {note}?',
            'dispute': f'Shall I record that you dispute this purchase: {note}?',
            'refused': f"Shall I note that you can't pay in this period: {note}?",
        }[self.concern_kind]
