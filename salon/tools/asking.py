"""A question for the caller, worded here and said by the turn engine."""

from __future__ import annotations

from core.state import STRICT
from salon.models.consent import Asked
from salon.models.state import SalonCall


class Ask(Asked):
    """A question for the caller, worded here and said by the turn engine.

    An output type, so putting the question ends the run: one model request
    decides *that* a question is needed and this code decides what it says. A
    model that words the question also gets to decide afterwards what it was,
    which is how a yes to "which country are you calling from" became agreement
    to a phone number.

    `Asked` is the record the next turn checks against; this adds the sentence
    the caller hears, so the two can never be about different things.
    """

    model_config = STRICT

    spoken: str

    def put(self, call: SalonCall) -> str:
        """Record the question as pending on the call, under the caller's current identity, and hand back what to say."""
        call.ask(self.model_copy(update={'owner': call.state.customer_phone}))
        return self.spoken
