"""A fake collections ledger standing in for Wasla's backend: accounts, and what each call records about them.

    ledger = Ledger()                        # seeded with three invented accounts
    ledger.account('WA-1001')                # what a call is opened with
    ledger.verify('WA-1001', date(1990, 3, 14))

What it records uses the outcome codes collections teams use for a call:

| Code | Meaning |
|---|---|
| `RPC` | right-party contact: the account holder was verified |
| `PTP` | promise to pay, with a day |
| `CB` | a callback day and time, from the holder or a third party |
| `WN` | wrong number: the number is taken off the account |
| `HARDSHIP`, `LEGAL`, `DISPUTE`, `RTP` | a concern noted, with the caller's own words (`RTP`: refuses to pay) |

Every write is keyed by core's intent id: sent twice, it is written once, and
`outcome_of` finds an answer that was lost. Three wrong dates of birth lock
the account for the rest of the day. Thread-safe: core runs each operation in
a worker thread.

ponytail: in memory, so a restart forgets every call. A real backend is a
database; keep the same method names and answers.
"""

from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal
from threading import Lock
from typing import Any

TRIES = 3
"""Wrong dates of birth before the account is locked for verification."""


@dataclass
class Account:
    """One account a call can be about. The date of birth never leaves the ledger."""

    account_id: str
    first_name: str
    last_name: str
    birth_date: date
    amount: Decimal
    merchant: str
    purchase_date: date
    due_date: date


SEED = (
    Account('WA-1001', 'Layla', 'Haddad', date(1990, 3, 14), Decimal('434.50'), 'Falcon Electronics',
            date(2026, 8, 21), date(2026, 9, 20)),
    Account('WA-1002', 'Omar', 'Khalil', date(1985, 11, 2), Decimal('1249.00'), 'Oasis Furniture',
            date(2026, 8, 2), date(2026, 9, 2)),
    Account('WA-1003', 'Sara', 'Nasser', date(1998, 7, 30), Decimal('89.75'), 'Saffron Fashion',
            date(2026, 9, 12), date(2026, 10, 12)),
)  # fmt: skip
"""Invented people, amounts and shops. Any resemblance to a real one is a coincidence."""


@dataclass
class Ledger:
    """The accounts, and every record a call made, by intent id."""

    accounts: dict[str, Account] = field(default_factory=lambda: {a.account_id: a for a in SEED})
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Every write's answer, by intent id, in the order written."""
    wrong_tries: dict[str, int] = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock)

    def account(self, account_id: str) -> Account:
        return self.accounts[account_id]

    def verify(self, account_id: str, birth_date: date) -> dict[str, Any]:
        """`verified`, `mismatch` with the tries left, or `locked`."""
        with self.lock:
            tries = self.wrong_tries.get(account_id, 0)
            if tries >= TRIES:
                return {'status': 'locked', 'account_id': account_id}
            if self.accounts[account_id].birth_date == birth_date:
                self.wrong_tries.pop(account_id, None)
                return {'status': 'verified', 'account_id': account_id}
            self.wrong_tries[account_id] = tries + 1
            if tries + 1 >= TRIES:
                return {'status': 'locked', 'account_id': account_id}
            return {'status': 'mismatch', 'account_id': account_id, 'tries_left': TRIES - tries - 1}

    def write(self, token: str, code: str, account_id: str, **fields: Any) -> dict[str, Any]:
        """One record, once per token: the same token again answers what was written the first time."""
        with self.lock:
            if token not in self.records:
                reference = f'{code}-{len(self.records) + 1:04d}'
                self.records[token] = {
                    'code': code,
                    'account_id': account_id,
                    'reference': reference,
                    **fields,
                }
            return dict(self.records[token])

    def promise(self, token: str, account_id: str, day: date, amount: Decimal) -> dict[str, Any]:
        return self.write(token, 'PTP', account_id, day=day, amount=amount)

    def callback(self, token: str, account_id: str, day: date, at: time) -> dict[str, Any]:
        return self.write(token, 'CB', account_id, day=day, at=at)

    def wrong_number(self, token: str, account_id: str) -> dict[str, Any]:
        return self.write(token, 'WN', account_id)

    def concern(self, token: str, account_id: str, code: str, note: str) -> dict[str, Any]:
        return self.write(token, code, account_id, note=note)

    def outcome_of(self, token: str) -> dict[str, Any] | None:
        """What a write with this token did, or None when the ledger never saw it."""
        with self.lock:
            found = self.records.get(token)
            return dict(found) if found else None

    def codes(self, account_id: str) -> list[str]:
        """The codes recorded for an account, in order: what a test or a report reads."""
        return [r['code'] for r in self.records.values() if r['account_id'] == account_id]
