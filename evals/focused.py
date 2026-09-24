"""Focused live check of the salon's narrow readings: uv run --env-file .env python -m evals.focused [repeats].

Makes billable model requests: every case below, `repeats` times (default 5).
Each case is the reply a live call sent, taken from its Langfuse trace, put to
the one reading the application runs on it, with the label that must come
back. A case that passes only some of the time is the finding: a reading that
misses a clear yes once in five calls fails the caller once in five calls.

It also prints each reading's input tokens per request, the number the narrow
readings exist to cut. It checks the request shape, context and class
descriptions against the failures seen live. It is not an accuracy claim;
`evals.consent` is the wider set.
"""

import asyncio
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel
from pydantic_ai.usage import RunUsage

from core.classifiers import Exchange
from core.readings import ChoiceInput, DetourInput, Option, ReadBackInput, Reading, TakeBackInput
from salon.classification import (
    APPOINTMENT_CHOICE,
    CHOSEN_OFFER,
    CONFIRMATION,
    CONVERSATION,
    DETOUR,
    LAST_CHANGE,
    MOVE_OR_ADD,
    RETAINED_APPROVAL,
)
from salon.models.consent import OfferInput

READBACK = 'Let me read that back to you: +34 671 273 454. Have I got that right?'
ASK_NUMBER = 'Which phone number should I use? Please say it with the country code.'
TIMES = 'For a haircut tomorrow, I have 9:00 AM, 9:30 AM, 10:00 AM. Which time works for you?'
SLOTS = tuple(
    Option(id=f'2026-09-25T{hhmm}|hair_colour', said=f'tomorrow at {spoken}')
    for hhmm, spoken in (('0900', '9:00 AM'), ('0930', '9:30 AM'), ('1000', '10:00 AM'))
)
NINE = SLOTS[0].id
REQUEST = "Hi, I'd like to book a haircut for this Friday, please."
BOOKED = "Lovely, that's your hair colour booked for tomorrow at 9:00 AM."
COMPLAINT = "I'll note your words: the colour burned my scalp, and you'd like a full refund. Is that right?"
MOVE_OR_ADD_QUESTION = 'Would you like to move your existing appointment, or add another one?'


@dataclass(frozen=True)
class Case:
    id: str
    reading: Reading
    value: BaseModel
    check: Callable[[str], bool]
    wants: str


def phone(answer: str) -> ReadBackInput:
    return ReadBackInput(question=READBACK, answer=answer, proposal=READBACK)


def offer(answer: str, question: str = TIMES) -> OfferInput:
    return OfferInput(question=question, answer=answer, options=SLOTS)


def held(answer: str, question: str, agreed: str = BOOKED) -> TakeBackInput:
    return TakeBackInput(question=question, answer=answer, agreed=agreed)


def choice(answer: str, question: str = MOVE_OR_ADD_QUESTION) -> ChoiceInput:
    return ChoiceInput(question=question, answer=answer, options=MOVE_OR_ADD)


def detour(answer: str) -> DetourInput:
    return DetourInput(question=f'{ASK_NUMBER} {READBACK}', answer=answer, request=REQUEST)


CASES = (
    Case('phone-yes-correct', CONFIRMATION, phone("Yes. That's correct."), lambda r: r == 'accept', 'accept'),
    Case('phone-yes-right', CONFIRMATION, phone("Yes, that's right."), lambda r: r == 'accept', 'accept'),
    Case(
        'phone-yes-got-it',
        CONFIRMATION,
        phone("Yes, you've got it exactly right."),
        lambda r: r == 'accept',
        'accept',
    ),
    Case(
        'phone-no', CONFIRMATION, phone('No, the last digit is five.'), lambda r: r != 'accept', 'not accept'
    ),
    Case('pick-nine', CHOSEN_OFFER, offer("Nine o'clock works for me."), lambda r: r == NINE, 'nine'),
    Case(
        'pick-conditional',
        CHOSEN_OFFER,
        offer("Nine o'clock, but only if it costs less than thirty euros."),
        lambda r: r == '',
        'no pick',
    ),
    Case(
        'pick-after-price',
        CHOSEN_OFFER,
        offer("Oh, OK. Fine, nine o'clock then.", question='Hair colour is 75 euros.'),
        lambda r: r == NINE,
        'nine',
    ),
    Case(
        'pick-then-goodbye',
        CHOSEN_OFFER,
        offer(
            "Oh, okay. Fine. Nine o'clock then. That's all I need. Thank you. Goodbye.",
            question="Hair colour is 75 euros, so it's a bit more than 30 euros. Would you like to go ahead with that?",
        ),
        lambda r: r == NINE,
        'nine',
    ),
    Case(
        'withdraw',
        LAST_CHANGE,
        held(
            "Oh, that's too much for me. Leave it, thanks.",
            "You're all set for tomorrow at 9:00 AM. Anything else I can help with?",
            agreed="You're all set for tomorrow at 9:00 AM.",
        ),
        lambda r: r == 'withdraw',
        'withdraw',
    ),
    Case(
        'keep-after-question',
        LAST_CHANGE,
        held(
            'Oh, okay. Fine. 09:00 then.',
            "A hair colour is 75 euros, so it's over 30 euros. Would you like to keep the appointment, "
            'or shall I cancel it for you?',
        ),
        lambda r: r == 'keep',
        'keep',
    ),
    Case(
        'condition-after-booking',
        LAST_CHANGE,
        held('09:00. But only if it costs less than 30 EUR.', TIMES),
        lambda r: r != 'withdraw',
        'not withdraw',
    ),
    Case(
        'withdraw-later',
        LAST_CHANGE,
        held(
            "Oh, that's too much for me. Leave it, thanks.",
            'A hair colour is 75 euros. Anything else I can help you with?',
            agreed="You're all set for tomorrow at 9:00 AM.",
        ),
        lambda r: r == 'withdraw',
        'withdraw',
    ),
    Case(
        'goodbye-keeps-booking',
        LAST_CHANGE,
        held(
            "No, that's all, thanks. Bye.",
            'Done, tomorrow at 9:00 AM. Anything else?',
            agreed='Done, tomorrow at 9:00 AM.',
        ),
        lambda r: r != 'withdraw',
        'not withdraw',
    ),
    Case(
        'cancel-yes',
        CONFIRMATION,
        ReadBackInput(
            question='So that is your hair colour tomorrow at 9:00 AM cancelled. Shall I go ahead?',
            answer='Yes, please cancel it.',
            proposal='So that is your hair colour tomorrow at 9:00 AM cancelled. Shall I go ahead?',
        ),
        lambda r: r == 'accept',
        'accept',
    ),
    Case(
        'closing',
        CONVERSATION,
        Exchange(question='Done, tomorrow at 9:00 AM. Anything else?', answer="No, that's all, thanks. Bye."),
        lambda r: r == 'end',
        'end',
    ),
    Case(
        'closing-question',
        CONVERSATION,
        Exchange(question='Anything else?', answer='Actually, how long does a colour take?'),
        lambda r: r != 'end',
        'not end',
    ),
    Case('choice-move', APPOINTMENT_CHOICE, choice('Move it, please.'), lambda r: r == 'move', 'move'),
    Case(
        'choice-add',
        APPOINTMENT_CHOICE,
        choice('Keep that one, and add this one as well.'),
        lambda r: r == 'add',
        'add',
    ),
    Case('choice-bare-yes', APPOINTMENT_CHOICE, choice('Yes.'), lambda r: r == '', 'neither'),
    Case(
        'choice-unasked-move',
        APPOINTMENT_CHOICE,
        choice('Can we move it to Saturday, the same time?', question=TIMES),
        lambda r: r == 'move',
        'move',
    ),
    Case(
        'complaint-kept',
        RETAINED_APPROVAL,
        held("Yes, that's right.", READBACK, agreed=COMPLAINT),
        lambda r: r == 'keep',
        'keep',
    ),
    Case(
        'complaint-changed',
        RETAINED_APPROVAL,
        held("Yes. Actually, I'd rather have a redo than a refund.", READBACK, agreed=COMPLAINT),
        lambda r: r == 'withdraw',
        'withdraw',
    ),
    Case(
        'detour-kept',
        DETOUR,
        detour("Sure, it's +34 671 273 454. Yes, that's correct."),
        lambda r: r == 'kept',
        'kept',
    ),
    Case(
        'detour-kept-pieces',
        DETOUR,
        detour("Sure. It's +34 6. 71. 2. 73. 4. 5. 4. Yes. That's correct."),
        lambda r: r == 'kept',
        'kept',
    ),
    Case(
        'detour-changed',
        DETOUR,
        detour(
            "Sure, it's +34 671 273 454. Oh, and sorry, can we make it Saturday instead of Friday? "
            "Yes, that's right."
        ),
        lambda r: r == 'changed',
        'changed',
    ),
    Case(
        'detour-question',
        DETOUR,
        detour("It's +34 671 273 454. How much is a haircut, by the way? Yes, that's right."),
        lambda r: r == 'changed',
        'changed',
    ),
)

TOKENS: dict[str, list[int]] = defaultdict(list)
"""Input tokens of every request, by reading."""


async def one(case: Case) -> tuple[bool, str]:
    usage = RunUsage()
    try:
        result = await case.reading.classifier.classify(case.value, usage=usage)
    except Exception as error:
        return False, type(error).__name__
    TOKENS[case.reading.name].append(usage.input_tokens)
    label = getattr(result, case.reading.field)
    return case.check(label), label


async def main(repeats: int) -> None:
    failed = 0
    for case in CASES:
        results = await asyncio.gather(*(one(case) for _ in range(repeats)))
        passed = sum(ok for ok, _ in results)
        failed += passed < repeats
        print(
            f'{"ok  " if passed == repeats else "FAIL"} {passed}/{repeats}  {case.id:24} '
            f'{case.reading.name:18} wants {case.wants}'
        )
        for ok, seen in results:
            if not ok:
                print('        got', repr(seen))
    print(
        'all cases passed every time'
        if not failed
        else f'{failed} of {len(CASES)} cases failed at least once'
    )
    for name, counts in TOKENS.items():
        print(f'{name:18} input tokens per request: {sum(counts) // len(counts)}')


if __name__ == '__main__':
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 5))
