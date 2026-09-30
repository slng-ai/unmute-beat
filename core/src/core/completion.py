"""When a step's job is done, decided from evidence on the call and never from a flag anybody set.

A `Completion` reads the facts a flow holds and says whether this step's
purpose is met: a backend receipt that says what the step needed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import Any


class Completion(ABC):
    """Whether one step's purpose is met, from the facts alone."""

    name: str = ''

    @abstractmethod
    def is_met(self, facts: Mapping[str, Any]) -> bool:
        """True only on evidence: a missing or failed fact is not completion."""


class ReceiptMatched(Completion):
    """A receipt is on file under `key` and says what the step needed (`accepts`).

    A receipt that exists but says the wrong thing (not eligible, refused) is
    not completion: the step's failure path decides what happens next.
    """

    def __init__(self, key: str, accepts: Callable[[Mapping[str, Any]], bool] = lambda _: True) -> None:
        self.key = key
        self.accepts = accepts
        self.name = f'receipt:{key}'

    def is_met(self, facts: Mapping[str, Any]) -> bool:
        receipt = facts.get(self.key)
        return isinstance(receipt, Mapping) and self.accepts(receipt)
