"""The one question a call is waiting on an answer to, and the rules for when it can still be answered.

A consequential action is authorised by the caller's answer to a question
the application worded and recorded, never by a flag a model set. This module
holds the generic half of that: what a proposal must carry, and the ledger
that says whether the caller's reply this turn can be an answer to it. What
the questions are about, and what agreement to each one allows, stays with
the business.

`Proposal` is structural, a `Protocol`, so a business keeps its own
validated model with its own field order (which is part of what its
interpreter is sent). `BaseProposal` is a ready-made one for a new business.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, Self, runtime_checkable

from pydantic import BaseModel

from core.state import STRICT

if TYPE_CHECKING:
    from core.state import CallContext


@runtime_checkable
class Proposal(Protocol):
    """A question put to the caller: what it is about, the words they heard, and whom it was put to.

    Read-only members, so a business may narrow them (`about` to its own literals).
    """

    @property
    def about(self) -> str: ...

    @property
    def spoken(self) -> str: ...

    @property
    def value(self) -> str:
        """The exact thing agreement would be agreement to: a number read back, a slot offered, a summary's digest."""
        ...

    @property
    def owner(self) -> str:
        """The caller identity it was put under. An answer under a different identity is not an answer to it."""
        ...

    @property
    def turn(self) -> int:
        """The caller turn it went out on. Set by the ledger, never by whoever proposed it."""
        ...

    def model_copy(self, *, update: dict[str, Any] | None = None, deep: bool = False) -> Self: ...

    def model_dump(self) -> dict[str, Any]: ...


class BaseProposal(BaseModel):
    """A ready-made `Proposal` for a business that needs nothing more."""

    model_config = STRICT

    about: str
    spoken: str = ''
    value: str = ''
    owner: str = ''
    turn: int = 0


class ProposalLedger[P: Proposal]:
    """The one outstanding question on a call, and whether this turn's reply can answer it.

    One slot, overwritten: asking a new question abandons the old one, and one
    answer authorises one action (`consume`). A question is answerable only on
    the turn after it went out, under the identity it was put to, and only if
    the caller heard it in full. `last` relaxes the last condition, for
    interpretation only: a question the caller spoke over can be corrected or
    abandoned, but nothing that authorises an action reads it.
    """

    def __init__(self) -> None:
        self.asked: P | None = None
        self._answering: P | None = None
        """The question as it stood when this caller turn began: the one their words are an answer to."""

    def open_turn(self) -> None:
        """A new caller turn begins: remember which question it can answer, in case the turn has to be taken back."""
        self._answering = self.asked

    def rewind(self, call: CallContext) -> None:
        """The last piece of this turn reached nobody, so the caller is still answering the same question.

        Whatever that piece asked was never heard, and whatever it consumed
        was consumed on half a sentence, so the question the turn opened with
        is put back. Unless a business action was attempted on it: that answer
        is spent, and restoring it could authorise the same write twice.
        """
        self.asked = None if call.acted_this_turn else self._answering

    def ask(self, call: CallContext, proposal: P) -> None:
        """Record that this question has gone out to the caller, on this turn."""
        self.asked = proposal.model_copy(update={'turn': call.turn})

    def age(self, call: CallContext) -> None:
        """Forget a question nobody answered on the turn it could have been answered."""
        if self.asked is not None and self.asked.turn < call.turn - 1:
            self.asked = None

    def last(self, call: CallContext) -> tuple[P, bool] | None:
        """Last turn's question under the caller's current identity, and whether they heard it in full."""
        question = self.asked
        if question is None or question.turn != call.turn - 1:
            return None
        if question.owner and question.owner != call.identity():
            return None
        heard = not (
            question.spoken
            and any(
                segment.turn == question.turn and segment.generated == question.spoken and segment.interrupted
                for segment in call.all_spoken
            )
        )
        return question, heard

    def pending(self, call: CallContext) -> P | None:
        """The question this turn's reply can answer: last turn's, same identity, heard in full."""
        last = self.last(call)
        return last[0] if last is not None and last[1] else None

    def pending_about(self, call: CallContext, about: str, **fields: str) -> P | None:
        """The pending question, only if it is this one, with these exact field values."""
        asked = self.pending(call)
        if asked is None or asked.about != about:
            return None
        return asked if all(getattr(asked, name) == value for name, value in fields.items()) else None

    def consume(self) -> None:
        """One answer authorises one action."""
        self.asked = None
