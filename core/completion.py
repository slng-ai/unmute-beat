"""When a step's job is done, decided from evidence on the call and never from a flag anybody set.

A `Completion` reads the facts a flow has collected and says whether this
step's purpose is met: validated fields present, or a backend receipt that
says what the step needed. `depends_on` names the facts it rests on, so when
the caller changes one of them the flow forgets what was built on it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import Any

from core.state import present


class Completion(ABC):
    """Whether one step's purpose is met, from the facts alone."""

    name: str = ''
    depends_on: frozenset[str] = frozenset()

    @abstractmethod
    def is_met(self, facts: Mapping[str, Any]) -> bool:
        """True only on evidence: a missing or failed fact is not completion."""


class FieldsPresent(Completion):
    """Every named field has a validated value."""

    def __init__(self, *fields: str) -> None:
        self.fields = fields
        self.name = 'fields:' + ','.join(fields)
        self.depends_on = frozenset(fields)

    def is_met(self, facts: Mapping[str, Any]) -> bool:
        return all(present(facts.get(field)) for field in self.fields)


class ReceiptMatched(Completion):
    """A receipt is on file under `key` and says what the step needed (`accepts`).

    A receipt that exists but says the wrong thing (not eligible, refused) is
    not completion: the step's failure path decides what happens next.
    """

    def __init__(
        self,
        key: str,
        accepts: Callable[[Mapping[str, Any]], bool] = lambda _: True,
        depends_on: frozenset[str] = frozenset(),
    ) -> None:
        self.key = key
        self.accepts = accepts
        self.name = f'receipt:{key}'
        self.depends_on = depends_on

    def is_met(self, facts: Mapping[str, Any]) -> bool:
        receipt = facts.get(self.key)
        return isinstance(receipt, Mapping) and self.accepts(receipt)
