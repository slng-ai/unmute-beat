"""The words core says itself, in one language. A use case in another language gives its own `Locale`.

Everything else a caller hears is the use case's: its prompts, its
read-backs, its receipts. These four are said by the turn engine when
something went wrong, so no use case wrote them. A call picks its locale by
class (`CallContext.locale`).

The instructions every classifier is sent (`core.classifiers.TASK`) and the
class descriptions (`core.readings`) stay in English whatever the caller
speaks: they are read by the model, not said to the caller.

ponytail: only English exists. A second language needs its own four lines
and a live check of the readings on its own dataset.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Locale:
    """The fixed lines of one language."""

    stumble: str
    """Said when a run raised: an error the caller can hear beats silence on an open line."""
    exhausted: str
    """Said when a turn runs out of budget: no further model request, no business work, no second allowance."""
    unbacked: str
    """Said when the model told the caller it changed something the business has no record of changing.

    It cannot be unsaid, the words are already at TTS, so the next thing the
    caller hears is the correction.
    """
    withheld: str
    """Said in place of a clause that claimed a change nothing made, on a step that can write.

    Unlike `unbacked`, these words were caught before they reached TTS, so
    nothing has to be taken back.
    """


ENGLISH = Locale(
    stumble='Sorry, I had a problem there. Could you say that again?',
    exhausted="Sorry, I'm going round in circles there. Could you say that again?",
    unbacked="Sorry, let me correct myself: I haven't actually made that change. Let me check where we are.",
    withheld='Sorry -- nothing has been changed yet. Let me check where we are.',
)
