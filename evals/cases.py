"""Reviewed reply cases shared by the live evaluation and the offline replay.

One case is one caller reply to one question this code put to them. Its
labels are what a careful person would say the reply means, in a vocabulary
wider than any single interpreter's, so the same cases can score the current
four-label classifier, a state solver, and a rule-based floor.

`expect` is the full meaning. `legacy` is the closest thing the current
classifier can say, because it has no word for a correction or a change of
task; by default it is the same as `expect`.

ponytail: about sixty hand-written cases, seeded from the three reviewed
Langfuse calls of 2026-09-23 and the old inline list. Enough to catch a design
that skips a step or accepts the wrong thing; not enough for an accuracy
claim. Grow it from reviewed production traces before quoting a number.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

type Meaning = Literal[
    'accept',
    'reject',
    'revise',
    'conditional',
    'unclear',
    'correct_phone',
    'move',
    'add',
    'switch_complaints',
    'switch_booking',
    'end',
    'continue',
]
"""What a reply means. The last group are meanings only a state solver can express."""

type Group = Literal[
    'clear',
    'conditional',
    'question',
    'correction',
    'switch',
    'interrupted',
    'stale',
    'mixed',
    'closing',
    'choice',
]
"""What kind of reply a case is, so results can be reported per kind."""


class Case(BaseModel):
    """One reviewed reply to one pending question."""

    model_config = ConfigDict(extra='forbid', frozen=True)

    id: str
    group: Group
    about: Literal['phone', 'booking', 'move_or_add', 'complaint', 'closing']
    spoken: str
    """The question exactly as the caller heard it (or began to hear it)."""
    utterance: str
    expect: Meaning
    legacy: Meaning | None = None
    """What the current classifier should say, when it cannot say `expect`. None means the same."""
    delivered: bool = True
    """False when the caller spoke over the question before it finished."""
    stale: bool = False
    """True when the question went out two turns ago, so the reply is not an answer to it."""
    digits: str = ''
    """For `correct_phone`: the full corrected number, in E.164."""
    details: str = ''
    """For a switch: any details the caller already gave, which must not be asked for again."""

    @property
    def legacy_meaning(self) -> Meaning:
        """What the current classifier is scored against."""
        return self.expect if self.legacy is None else self.legacy


PHONE = '+34600111222'
READBACK = 'Let me read that back to you: +34 600 111 222. Have I got that right?'
BOOKING = 'Shall I book a haircut on Friday at three?'
COMPLAINT = "I'll record that your haircut was uneven and you'd like a refund. Is that right?"
CHOICE = 'Would you like to move your existing appointment, or add another one?'
CLOSING = 'Is there anything else I can help with?'


CASES: tuple[Case, ...] = (
    Case(id='phone-yes', group='clear', about='phone', spoken=READBACK, utterance='Yes.', expect='accept'),
    Case(
        id='phone-yes-thanks',
        group='clear',
        about='phone',
        spoken=READBACK,
        utterance="Yes, that's right, thank you.",
        expect='accept',
    ),
    Case(
        id='phone-si',
        group='clear',
        about='phone',
        spoken=READBACK,
        utterance='Sí, correcto.',
        expect='accept',
    ),
    Case(id='phone-no', group='clear', about='phone', spoken=READBACK, utterance='No.', expect='reject'),
    Case(
        id='phone-full-correction',
        group='correction',
        about='phone',
        spoken=READBACK,
        utterance="No, it's six hundred, one one one, two three four.",
        expect='correct_phone',
        legacy='revise',
        digits='+34600111234',
    ),
    Case(
        id='phone-partial-correction',
        group='correction',
        about='phone',
        spoken=READBACK,
        utterance='The last three digits are two three four.',
        expect='correct_phone',
        legacy='revise',
        digits='+34600111234',
    ),
    Case(
        id='phone-correction-es',
        group='correction',
        about='phone',
        spoken=READBACK,
        utterance='No, los últimos son dos, tres, cuatro.',
        expect='correct_phone',
        legacy='revise',
        digits='+34600111234',
    ),
    Case(
        id='phone-ambiguous-correction',
        group='correction',
        about='phone',
        spoken=READBACK,
        utterance='Not quite, one of the middle ones is wrong.',
        expect='unclear',
        legacy='revise',
    ),
    Case(
        id='phone-question',
        group='question',
        about='phone',
        spoken=READBACK,
        utterance='Why do you need my number?',
        expect='unclear',
    ),
    Case(
        id='phone-interrupted-correction',
        group='interrupted',
        about='phone',
        spoken=READBACK,
        utterance="No wait, it's two three four at the end.",
        expect='correct_phone',
        legacy='unclear',
        delivered=False,
        digits='+34600111234',
    ),
    Case(
        id='phone-interrupted-yes',
        group='interrupted',
        about='phone',
        spoken=READBACK,
        utterance='Yes.',
        expect='unclear',
        delivered=False,
    ),
    Case(
        id='phone-switch-complaint',
        group='switch',
        about='phone',
        spoken=READBACK,
        utterance="Yes, and I'm calling because my haircut last week was awful.",
        expect='accept',
    ),
    Case(
        id='booking-yes',
        group='clear',
        about='booking',
        spoken=BOOKING,
        utterance='Yes, please.',
        expect='accept',
    ),
    Case(
        id='booking-go',
        group='clear',
        about='booking',
        spoken=BOOKING,
        utterance='Go for it.',
        expect='accept',
    ),
    Case(
        id='booking-si',
        group='clear',
        about='booking',
        spoken=BOOKING,
        utterance='Sí, resérvalo.',
        expect='accept',
    ),
    Case(
        id='booking-mixed-lang',
        group='clear',
        about='booking',
        spoken=BOOKING,
        utterance='Sí, book it please.',
        expect='accept',
    ),
    Case(
        id='booking-no-thanks',
        group='clear',
        about='booking',
        spoken=BOOKING,
        utterance='No, thanks.',
        expect='reject',
    ),
    Case(
        id='booking-conditional',
        group='conditional',
        about='booking',
        spoken=BOOKING,
        utterance='Yes, if it costs less than twenty.',
        expect='conditional',
    ),
    Case(
        id='booking-conditional-es',
        group='conditional',
        about='booking',
        spoken=BOOKING,
        utterance='Sí, pero solo si cuesta menos de veinte.',
        expect='conditional',
    ),
    Case(
        id='booking-conditional-free',
        group='conditional',
        about='booking',
        spoken=BOOKING,
        utterance='Yes, if it is free.',
        expect='conditional',
    ),
    Case(
        id='booking-revise-day',
        group='correction',
        about='booking',
        spoken=BOOKING,
        utterance='Actually, make it Saturday.',
        expect='revise',
    ),
    Case(
        id='booking-revise-es',
        group='correction',
        about='booking',
        spoken=BOOKING,
        utterance='Mejor el sábado.',
        expect='revise',
    ),
    Case(
        id='booking-yes-question',
        group='question',
        about='booking',
        spoken=BOOKING,
        utterance='Yes, how much will it cost?',
        expect='unclear',
    ),
    Case(
        id='booking-question-tentative',
        group='question',
        about='booking',
        spoken=BOOKING,
        utterance='Go for it?',
        expect='unclear',
    ),
    Case(
        id='booking-maybe',
        group='question',
        about='booking',
        spoken=BOOKING,
        utterance='Maybe.',
        expect='unclear',
    ),
    Case(
        id='booking-switch-complaint',
        group='switch',
        about='booking',
        spoken=BOOKING,
        utterance='Actually forget the booking, I want to complain about my last haircut.',
        expect='switch_complaints',
        legacy='reject',
        details='complaint about the last haircut',
    ),
    Case(
        id='booking-switch-complaint-details',
        group='switch',
        about='booking',
        spoken=BOOKING,
        utterance='No. The real reason I called: my colour came out orange and I want my money back.',
        expect='switch_complaints',
        legacy='reject',
        details='colour came out orange; wants a refund',
    ),
    Case(
        id='booking-switch-es',
        group='switch',
        about='booking',
        spoken=BOOKING,
        utterance='Déjalo, quiero poner una queja.',
        expect='switch_complaints',
        legacy='reject',
    ),
    Case(
        id='booking-interrupted-yes',
        group='interrupted',
        about='booking',
        spoken=BOOKING,
        utterance='Yes.',
        expect='unclear',
        delivered=False,
    ),
    Case(
        id='booking-interrupted-switch',
        group='interrupted',
        about='booking',
        spoken=BOOKING,
        utterance='Sorry, before that, I need to make a complaint.',
        expect='switch_complaints',
        legacy='unclear',
        delivered=False,
    ),
    Case(
        id='booking-stale-yes',
        group='stale',
        about='booking',
        spoken=BOOKING,
        utterance='Yes.',
        expect='unclear',
        stale=True,
    ),
    Case(
        id='booking-mixed-yes-complaint',
        group='mixed',
        about='booking',
        spoken=BOOKING,
        utterance='Yes, and I also want to complain about last time.',
        expect='accept',
    ),
    Case(
        id='booking-mixed-yes-but',
        group='mixed',
        about='booking',
        spoken=BOOKING,
        utterance='Yes, but Saturday.',
        expect='revise',
    ),
    Case(
        id='booking-bye',
        group='closing',
        about='booking',
        spoken=BOOKING,
        utterance="No, forget it, I'll call back. Bye.",
        expect='end',
        legacy='reject',
    ),
    Case(
        id='complaint-yes',
        group='clear',
        about='complaint',
        spoken=COMPLAINT,
        utterance="Yes, that's right.",
        expect='accept',
    ),
    Case(
        id='complaint-si',
        group='clear',
        about='complaint',
        spoken=COMPLAINT,
        utterance='Sí.',
        expect='accept',
    ),
    Case(
        id='complaint-revise',
        group='correction',
        about='complaint',
        spoken=COMPLAINT,
        utterance="Not a refund, I'd rather have it fixed for free.",
        expect='revise',
    ),
    Case(
        id='complaint-conditional',
        group='conditional',
        about='complaint',
        spoken=COMPLAINT,
        utterance='Yes, as long as someone calls me back today.',
        expect='conditional',
    ),
    Case(
        id='complaint-switch-booking',
        group='switch',
        about='complaint',
        spoken=COMPLAINT,
        utterance='Never mind the complaint, just book me in to get it fixed.',
        expect='switch_booking',
        legacy='reject',
    ),
    Case(
        id='complaint-interrupted-yes',
        group='interrupted',
        about='complaint',
        spoken=COMPLAINT,
        utterance='Yeah.',
        expect='unclear',
        delivered=False,
    ),
    Case(
        id='choice-add',
        group='choice',
        about='move_or_add',
        spoken=CHOICE,
        utterance='Keep that one and add another.',
        expect='add',
    ),
    Case(
        id='choice-move-es',
        group='choice',
        about='move_or_add',
        spoken=CHOICE,
        utterance='Cambia la que ya tengo.',
        expect='move',
    ),
    Case(
        id='choice-move',
        group='choice',
        about='move_or_add',
        spoken=CHOICE,
        utterance='Move it.',
        expect='move',
    ),
    Case(
        id='choice-bare-yes',
        group='choice',
        about='move_or_add',
        spoken=CHOICE,
        utterance='Yes.',
        expect='unclear',
    ),
    Case(
        id='choice-both',
        group='choice',
        about='move_or_add',
        spoken=CHOICE,
        utterance='Both, I suppose. Or either.',
        expect='unclear',
    ),
    Case(
        id='closing-no-thanks',
        group='closing',
        about='closing',
        spoken=CLOSING,
        utterance='No, thanks.',
        expect='end',
    ),
    Case(
        id='closing-es',
        group='closing',
        about='closing',
        spoken=CLOSING,
        utterance='No, gracias. Eso es todo.',
        expect='end',
    ),
    Case(
        id='closing-bye',
        group='closing',
        about='closing',
        spoken=CLOSING,
        utterance='Take care, bye.',
        expect='end',
    ),
    Case(
        id='closing-bye-but',
        group='closing',
        about='closing',
        spoken=CLOSING,
        utterance='Bye, pero antes, ¿cuánto cuesta?',
        expect='continue',
    ),
    Case(
        id='closing-no-but',
        group='closing',
        about='closing',
        spoken=CLOSING,
        utterance='No, but can you tell me when you open on Sunday?',
        expect='continue',
    ),
    Case(
        id='closing-complaint',
        group='switch',
        about='closing',
        spoken=CLOSING,
        utterance='Actually yes, I want to complain about my last visit.',
        expect='switch_complaints',
        legacy='continue',
    ),
    Case(
        id='closing-booking',
        group='switch',
        about='closing',
        spoken=CLOSING,
        utterance='Yes, can I book another haircut for next week?',
        expect='switch_booking',
        legacy='continue',
    ),
    Case(
        id='closing-thanks',
        group='closing',
        about='closing',
        spoken=CLOSING,
        utterance="That's everything, thank you.",
        expect='end',
    ),
)
"""Every case. Ids are unique; the offline replay checks that and that every group is covered."""


def by_group() -> dict[str, list[Case]]:
    """Cases grouped by kind, for per-kind reporting."""
    groups: dict[str, list[Case]] = {}
    for case in CASES:
        groups.setdefault(case.group, []).append(case)
    return groups
