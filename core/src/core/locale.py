"""The words core says itself, in one language. A use case in another language gives its own `Locale`.

Everything else a caller hears is the use case's: its prompts, its
read-backs, its receipts. These are said by the turn engine: while a turn
takes a while, or when something went wrong, so no use case wrote them. A
flow picks its locale in its manifest (`locale:`), or per call from a state
field (`language:` and `locales:`), as the bilingual Faro does.

ponytail: English and Spanish. Add a language as another `Locale` here when
an application needs one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Locale:
    """The fixed lines of one language."""

    stumble: str
    """Said when a run raised: an error the caller can hear beats silence on an open line."""
    exhausted: str
    """Said when a turn runs out of budget: no further model request, no business work, no second allowance."""
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
    gave_up: str = ''
    """Said once, instead of a read-back past its limit (`core.steps.READBACKS`, `CUT_READBACKS`), as core withdraws it.

    Nothing was done, and it is not read back again on this call
    (`CallContext.withdraw`); asked for again, the caller hears `withdrawn`.
    """
    unconfirmable: str = ''
    """Said instead of the greeting when the line cannot report playback (`core.processor.reports_playback`) and the
    flow needs confirmations: the caller is told before giving anything, and the call ends."""
    withdrawn: str = ''
    """Said when a read-back already given up on (`gave_up`) is asked for again: it stays undone on this call."""
    left_undone: str = ''
    """Said in a goodbye when the change last read back was never made, so a caller leaving knows it."""
    still_there: str = ''
    """Said when the caller has been silent a while after the last answer played (`core.turn.remind`).

    Empty turns reminders off. Two Coval calls of session 4 sat in silence
    until the call limit, the bot waiting on a question, the caller on nothing.
    """
    idle_goodbye: str = ''
    """Said before the line is closed, once reminders went unanswered."""
    refusals: Mapping[str, str] = field(default_factory=dict, hash=False)
    """The caller's words for a refused value, by the error type its validator raised (`core.values`).

    Formatted with the error's own context (`{grouped}`, `{day}`). The
    model reads the validator's message; these are what the caller hears
    about the same problem, suggested to the model beside it. An application
    adds its own types, or words them its own way, under
    `locale: {refusals: {...}}`.
    """
    restated: str = ''
    """Said before a read-back is carried out because the caller answered it by saying the same value again.

    The caller heard "Is your number +34 671 51 01 05?" and said "No, it's
    six seven one, five one zero, one zero five": the same digits, grouped
    their own way. Code found the value unchanged (`core.steps.Restated`), so
    that reply is a yes, and this line tells them why the call goes on.
    """

    def refusal(self, error: Mapping[str, Any]) -> str:
        """The caller's words for one Pydantic error (`ValidationError.errors()` item), or '' when this locale has none."""
        words = self.refusals.get(str(error.get('type', '')), '')
        try:
            return words.format(**(error.get('ctx') or {}))
        except KeyError, IndexError, ValueError:
            return words


ENGLISH = Locale(
    stumble='Sorry, I had a problem there. Could you say that again?',
    exhausted="Sorry, I'm going round in circles there. Could you say that again?",
    holding=('Let me have a look.', 'One moment.'),
    unsettled="I couldn't confirm whether that went through, so I won't try it again until it's been checked.",
    goodbye='Thanks for calling. Goodbye.',
    gave_up="I'm sorry, I couldn't get that confirmed on this line, so I haven't made that change. "
    'Please call back and we can do it then.',
    unconfirmable="Sorry, this connection can't confirm that you've heard me, so I can't make any changes on it. "
    'Please call back on a supported line. Goodbye.',
    withdrawn="I still can't confirm that on this line, so it stays as it was.",
    left_undone="I haven't made that last change.",
    still_there='Are you still there?',
    idle_goodbye="I'll let you go for now. Call back any time. Goodbye.",
    restated="Yes, that's the same as I read back.",
    refusals={
        'phone_no_country_code': 'Which country is that phone number from?',
        'phone_too_long': 'I have {grouped}. Which part is wrong?',
        'phone_too_short': 'Please give me the rest of the digits.',
        'phone_unknown_country': 'Which phone number should I use? Please say it with the country code.',
        'phone_invalid': "That number doesn't look right. Could you say the whole number again, with the country code?",
        'date_past': 'That day has already gone. Which other day would suit you?',
        'not_quoted': 'Could you tell me that again in your own words?',
    },
)

SPANISH = Locale(
    stumble='Perdona, he tenido un problema. ¿Me lo repites?',
    exhausted='Perdona, me estoy liando. ¿Me lo repites?',
    holding=('Déjame mirar.', 'Un momento.'),
    unsettled='No he podido confirmar si se ha hecho, así que no lo repetiré hasta comprobarlo.',
    goodbye='Gracias por llamar. Adiós.',
    gave_up='Lo siento, no he podido confirmarlo por esta línea, así que no he hecho ese cambio. '
    'Llámanos otra vez y lo hacemos entonces.',
    unconfirmable='Lo siento, esta conexión no puede confirmar que me has oído, así que no puedo hacer cambios por ella. '
    'Llámanos por una línea compatible. Adiós.',
    withdrawn='Sigo sin poder confirmarlo por esta línea, así que queda como estaba.',
    left_undone='No he hecho ese último cambio.',
    still_there='¿Sigues ahí?',
    idle_goodbye='Te dejo por ahora. Llámanos cuando quieras. Adiós.',
    restated='Sí, es lo mismo que te he leído.',
    refusals={
        'phone_no_country_code': '¿De qué país es ese número?',
        'phone_too_long': 'Tengo {grouped}. ¿Qué parte está mal?',
        'phone_too_short': 'Dime el resto de los dígitos, por favor.',
        'phone_unknown_country': '¿Qué número uso? Dímelo con el prefijo del país, por favor.',
        'phone_invalid': 'Ese número no parece correcto. ¿Me lo dices entero, con el prefijo del país?',
        'date_past': 'Ese día ya ha pasado. ¿Qué otro día te viene bien?',
        'not_quoted': '¿Me lo cuentas otra vez con tus palabras?',
    },
)
