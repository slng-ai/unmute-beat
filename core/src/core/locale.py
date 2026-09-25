"""The words core says itself, in one language. A use case in another language gives its own `Locale`.

Everything else a caller hears is the use case's: its prompts, its
read-backs, its receipts. These are said by the turn engine: while a turn
takes a while, or when something went wrong, so no use case wrote them. A call picks its locale by
class (`CallContext.default_locale`), or per call by overriding `locale`, as a
bilingual use case does (`use-cases/faro-language-learning`).

The instructions every classifier is sent (`core.classifiers.TASK`) and the
class descriptions (`core.readings`) stay in English whatever the caller
speaks: they are read by the model, not said to the caller.

ponytail: English and Spanish. The Spanish readings are checked only by
Faro's live runs (`use-cases/faro-tools/bench.py`), not on a dataset of their own.
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
    holding: tuple[str, ...]
    """Said once in a turn that is taking a while, before anything else: a tool is running, or nothing was said yet.

    Generic on purpose: it promises no work, so it is true before a lookup, a
    hand-over or a cancellation alike. More than one, so it does not repeat.
    """
    unsettled: str
    """Said when a business action's outcome is unknown: nothing is claimed, and nothing is tried again until it is settled."""


ENGLISH = Locale(
    stumble='Sorry, I had a problem there. Could you say that again?',
    exhausted="Sorry, I'm going round in circles there. Could you say that again?",
    unbacked="Sorry, let me correct myself: I haven't actually made that change. Let me check where we are.",
    withheld='Sorry -- nothing has been changed yet. Let me check where we are.',
    holding=('Let me have a look.', 'One moment.'),
    unsettled="I couldn't confirm whether that went through, so I won't try it again until it's been checked.",
)

SPANISH = Locale(
    stumble='Perdona, he tenido un problema. ¿Me lo repites?',
    exhausted='Perdona, me estoy liando. ¿Me lo repites?',
    unbacked='Perdona, me corrijo: todavía no he hecho ese cambio. Déjame ver dónde estamos.',
    withheld='Perdona, todavía no se ha cambiado nada. Déjame ver dónde estamos.',
    holding=('Déjame mirar.', 'Un momento.'),
    unsettled='No he podido confirmar si se ha hecho, así que no lo repetiré hasta comprobarlo.',
)
