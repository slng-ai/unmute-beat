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
    goodbye: str = ''
    """The last line of a call the caller ended. Core says it; a model never words a goodbye.

    Before it, core says the recap the model pointed at, rendered by the
    application from a receipt that still stands (`recap:` in the manifest),
    and the `unsettled` line when an intent's outcome is still unknown. So a
    goodbye can claim nothing the business did not do. See
    `core.steps.ConversationStep`.
    """
    claims: tuple[str, ...] = ()
    """Patterns for a model telling the caller the business changed something. Empty here: an application's to give.

    On a step that can write (`guarded`), a clause matching one is never
    said: what was done is said by the receipt, in words code rendered from
    the validated result (`say:`), so it cannot be about a different action,
    different arguments or somebody else. Which verbs claim a change is the
    application's domain ("booked", "resolved"), so core ships none; a
    manifest names its own (`locale: {base: english, claims: [...]}`).

    ponytail: a best-effort guard on model prose, not a guarantee. The
    guarantee is structural: receipt speech, fixed lines and goodbye recaps
    are rendered by code. Ordinary model speech stays a semantic behaviour to
    evaluate.
    """
    gave_up: str = ''
    """Said instead of a read-back the caller never heard in full (`core.steps.READBACKS`, `CUT_READBACKS`): nothing was done."""
    left_undone: str = ''
    """Said in a goodbye when the change last read back was never made, so a caller leaving knows it."""
    still_there: str = ''
    """Said when the caller has been silent a while after the last answer played (`core.turn.remind`).

    Empty turns reminders off. Two Coval calls of session 4 sat in silence
    until the call limit, the bot waiting on a question, the caller on nothing.
    """
    idle_goodbye: str = ''
    """Said before the line is closed, once reminders went unanswered."""
    pardon: str = ''
    """Said on a rendered step (`speech: rendered`) when its agent chose to wait and nothing is missing to ask for."""
    restated: str = ''
    """Said before a read-back is carried out because the caller answered it by saying the same value again.

    The caller heard "Is your number +34 671 51 01 05?" and said "No, it's
    six seven one, five one zero, one zero five": the same digits, grouped
    their own way. Code found the value unchanged (`core.steps.Restated`), so
    that reply is a yes, and this line tells them why the call goes on.
    """


ENGLISH = Locale(
    stumble='Sorry, I had a problem there. Could you say that again?',
    exhausted="Sorry, I'm going round in circles there. Could you say that again?",
    unbacked="Sorry, let me correct myself: I haven't actually made that change. Let me check where we are.",
    withheld='Sorry -- nothing has been changed yet. Let me check where we are.',
    holding=('Let me have a look.', 'One moment.'),
    unsettled="I couldn't confirm whether that went through, so I won't try it again until it's been checked.",
    goodbye='Thanks for calling. Goodbye.',
    gave_up="I'm sorry, I couldn't get that confirmed on this line, so I haven't made that change. "
    'Please call back and we can do it then.',
    left_undone="I haven't made that last change.",
    still_there='Are you still there?',
    idle_goodbye="I'll let you go for now. Call back any time. Goodbye.",
    pardon='Sorry, could you say that again?',
    restated="Yes, that's the same as I read back.",
)

SPANISH = Locale(
    stumble='Perdona, he tenido un problema. ¿Me lo repites?',
    exhausted='Perdona, me estoy liando. ¿Me lo repites?',
    unbacked='Perdona, me corrijo: todavía no he hecho ese cambio. Déjame ver dónde estamos.',
    withheld='Perdona, todavía no se ha cambiado nada. Déjame ver dónde estamos.',
    holding=('Déjame mirar.', 'Un momento.'),
    unsettled='No he podido confirmar si se ha hecho, así que no lo repetiré hasta comprobarlo.',
    goodbye='Gracias por llamar. Adiós.',
    gave_up='Lo siento, no he podido confirmarlo por esta línea, así que no he hecho ese cambio. '
    'Llámanos otra vez y lo hacemos entonces.',
    left_undone='No he hecho ese último cambio.',
    still_there='¿Sigues ahí?',
    idle_goodbye='Te dejo por ahora. Llámanos cuando quieras. Adiós.',
    pardon='Perdona, ¿me lo repites?',
    restated='Sí, es lo mismo que te he leído.',
)
