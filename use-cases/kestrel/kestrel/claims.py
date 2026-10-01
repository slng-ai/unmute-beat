"""A fake claims desk standing in for Kestrel Insurance's backend: policies, and the claims filed against them.

    claims = Claims()                                   # seeded with two invented policies
    claims.policy('KP482913', date(1979, 6, 5))         # the policy, or None when the two do not match
    claims.file(token, 'KP482913', {...})               # a claim, once per token

Every claim is keyed by core's intent id: filed twice, it is filed once, and
`outcome_of` finds an answer that was lost. `lose_next` makes the next
filing land but lose its answer, as a timeout would. Thread-safe: core runs
each operation in a worker thread.

ponytail: in memory, so a restart forgets every claim. A real desk is a
database; keep the same method names and answers.
"""

from dataclasses import dataclass, field
from datetime import date
from threading import Lock
from typing import Any


@dataclass(frozen=True)
class Car:
    plate: str
    words: str
    """How the car is said: "the blue Toyota Corolla"."""


@dataclass(frozen=True)
class Policy:
    policy_number: str
    first_name: str
    birth_date: date
    cars: tuple[Car, ...]


SEED = (
    Policy('KP482913', 'Daniel', date(1979, 6, 5), (Car('AB12CDE', 'the blue Toyota Corolla'),)),
    Policy(
        'KP731508',
        'Priya',
        date(1992, 1, 23),
        (Car('LM70XYZ', 'the white Kia Niro'), Car('GH19RST', 'the black Ford Fiesta')),
    ),
)
"""Invented people and cars. Any resemblance to a real one is a coincidence."""


class LostAnswerError(Exception):
    """The claim landed, but its answer never came back."""


@dataclass
class Claims:
    """The policies, and every claim filed, by intent id."""

    policies: dict[str, Policy] = field(default_factory=lambda: {p.policy_number: p for p in SEED})
    filed: dict[str, dict[str, Any]] = field(default_factory=dict)
    lose_next: bool = False
    lock: Lock = field(default_factory=Lock)

    def policy(self, policy_number: str, birth_date: date) -> Policy | None:
        """The policy when the number and the holder's date of birth match it; None otherwise."""
        found = self.policies.get(policy_number)
        return found if found is not None and found.birth_date == birth_date else None

    def file(self, token: str, policy_number: str, facts: dict[str, Any]) -> dict[str, Any]:
        """One claim, once per token: the same token again answers what was filed the first time."""
        with self.lock:
            if token not in self.filed:
                number = f'KC{100231 + len(self.filed)}'
                self.filed[token] = {'claim_number': number, 'policy_number': policy_number, **facts}
            if self.lose_next:
                self.lose_next = False
                raise LostAnswerError(token)
            return dict(self.filed[token])

    def outcome_of(self, token: str) -> dict[str, Any] | None:
        with self.lock:
            found = self.filed.get(token)
            return dict(found) if found else None
