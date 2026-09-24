"""Focused live check of the salon's classifiers: uv run --env-file .env python -m evals.focused [repeats].

Makes billable model requests: every case below, `repeats` times (default 5).
Each case is the exact input a live call sent, taken from its Langfuse trace,
with the one field that must come back. A case that passes only some of the
time is the finding: a classifier that reads a clear yes as nothing pending
once in five calls fails the caller once in five calls.

This checks the request shape, context and class descriptions against the
failures seen live. It is not an accuracy claim; `evals.consent` is the wider
set.
"""

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from salon.classification import CLASSIFIER, DETOUR_CLASSIFIER
from salon.models.consent import Asked, ConsentInput, DetourInput, OfferedTime

NUMBER = '+34671273454'
READBACK = 'Let me read that back to you: +34 671 273 454. Have I got that right?'
ASK_NUMBER = 'Which phone number should I use? Please say it with the country code.'
TIMES = 'For a haircut tomorrow, I have 9:00 AM, 9:30 AM, 10:00 AM. Which time works for you?'
SLOTS = tuple(
    OfferedTime(slot_id=f'2026-09-25T{hhmm}|hair_colour', spoken=f'tomorrow at {spoken}')
    for hhmm, spoken in (('0900', '9:00 AM'), ('0930', '9:30 AM'), ('1000', '10:00 AM'))
)
NINE = SLOTS[0].slot_id
OFFER = Asked(about='offer', spoken=TIMES, value=','.join(s.slot_id for s in SLOTS), owner=NUMBER, turn=5)
REQUEST = "Hi, I'd like to book a haircut for this Friday, please."


@dataclass(frozen=True)
class Case:
    id: str
    value: BaseModel
    check: Callable[[Any], bool]
    wants: str


def phone(answer: str) -> ConsentInput:
    pending = Asked(about='phone', spoken=READBACK, value=NUMBER, turn=2)
    return ConsentInput(question=READBACK, answer=answer, owner='', pending=pending)


def offer(answer: str, question: str = TIMES, pending: Asked | None = OFFER) -> ConsentInput:
    return ConsentInput(question=question, answer=answer, owner=NUMBER, pending=pending, offered=SLOTS)


def detour(answer: str) -> DetourInput:
    return DetourInput(question=f'{ASK_NUMBER} {READBACK}', answer=answer, request=REQUEST)


CASES = (
    Case('phone-yes-correct', phone("Yes. That's correct."), lambda r: r.confirmation == 'accept', 'accept'),
    Case('phone-yes-right', phone("Yes, that's right."), lambda r: r.confirmation == 'accept', 'accept'),
    Case(
        'phone-yes-got-it',
        phone("Yes, you've got it exactly right."),
        lambda r: r.confirmation == 'accept',
        'accept',
    ),
    Case(
        'phone-no', phone('No, the last digit is five.'), lambda r: r.confirmation != 'accept', 'not accept'
    ),
    Case('pick-nine', offer("Nine o'clock works for me."), lambda r: r.chosen_offer == NINE, 'nine'),
    Case(
        'pick-conditional',
        offer("Nine o'clock, but only if it costs less than thirty euros."),
        lambda r: r.chosen_offer == '' and r.confirmation != 'accept',
        'no pick',
    ),
    Case(
        'pick-after-price',
        offer("Oh, OK. Fine, nine o'clock then.", question='Hair colour is 75 euros.', pending=None),
        lambda r: r.chosen_offer == NINE,
        'nine',
    ),
    Case(
        'pick-then-goodbye',
        offer(
            "Oh, okay. Fine. Nine o'clock then. That's all I need. Thank you. Goodbye.",
            question="Hair colour is 75 euros, so it's a bit more than 30 euros. Would you like to go ahead with that?",
            pending=None,
        ),
        lambda r: r.chosen_offer == NINE,
        'nine',
    ),
    Case(
        'withdraw',
        ConsentInput(
            question="You're all set for tomorrow at 9:00 AM. Anything else I can help with?",
            answer="Oh, that's too much for me. Leave it, thanks.",
            owner=NUMBER,
            just_changed="You're all set for tomorrow at 9:00 AM.",
        ),
        lambda r: r.last_change == 'withdraw',
        'withdraw',
    ),
    Case(
        'keep-after-question',
        ConsentInput(
            question="A hair colour is 75 euros, so it's over 30 euros. Would you like to keep the appointment, "
            'or shall I cancel it for you?',
            answer='Oh, okay. Fine. 09:00 then.',
            owner=NUMBER,
            just_changed="Lovely, that's your hair colour booked for tomorrow at 9:00 AM.",
        ),
        lambda r: r.last_change == 'keep',
        'keep',
    ),
    Case(
        'condition-after-booking',
        ConsentInput(
            question=TIMES,
            answer='09:00. But only if it costs less than 30 EUR.',
            owner=NUMBER,
            just_changed="Lovely, that's your hair colour booked for tomorrow at 9:00 AM.",
        ),
        lambda r: r.last_change != 'withdraw',
        'not withdraw',
    ),
    Case(
        'withdraw-later',
        ConsentInput(
            question='A hair colour is 75 euros. Anything else I can help you with?',
            answer="Oh, that's too much for me. Leave it, thanks.",
            owner=NUMBER,
            just_changed="You're all set for tomorrow at 9:00 AM.",
        ),
        lambda r: r.last_change == 'withdraw',
        'withdraw',
    ),
    Case(
        'goodbye-after-booking',
        ConsentInput(
            question='Done, tomorrow at 9:00 AM. Anything else?',
            answer="No, that's all, thanks. Bye.",
            owner=NUMBER,
            just_changed='Done, tomorrow at 9:00 AM.',
            pending=Asked(about='closing', spoken='Anything else?', owner=NUMBER, turn=6),
        ),
        lambda r: r.last_change != 'withdraw' and r.conversation == 'end',
        'end, not withdraw',
    ),
    Case(
        'closing',
        ConsentInput(
            question='Done, tomorrow at 9:00 AM. Anything else?',
            answer="No, that's all, thanks. Bye.",
            owner=NUMBER,
            pending=Asked(about='closing', spoken='Anything else?', owner=NUMBER, turn=6),
        ),
        lambda r: r.conversation == 'end',
        'end',
    ),
    Case(
        'detour-kept',
        detour("Sure, it's +34 671 273 454. Yes, that's correct."),
        lambda r: r.request_change == 'kept',
        'kept',
    ),
    Case(
        'detour-kept-pieces',
        detour("Sure. It's +34 6. 71. 2. 73. 4. 5. 4. Yes. That's correct."),
        lambda r: r.request_change == 'kept',
        'kept',
    ),
    Case(
        'detour-changed',
        detour(
            "Sure, it's +34 671 273 454. Oh, and sorry, can we make it Saturday instead of Friday? "
            "Yes, that's right."
        ),
        lambda r: r.request_change == 'changed',
        'changed',
    ),
    Case(
        'detour-question',
        detour("It's +34 671 273 454. How much is a haircut, by the way? Yes, that's right."),
        lambda r: r.request_change == 'changed',
        'changed',
    ),
)


async def one(case: Case) -> tuple[bool, str]:
    classifier = DETOUR_CLASSIFIER if isinstance(case.value, DetourInput) else CLASSIFIER
    try:
        result = await classifier.classify(case.value)  # ty: ignore[invalid-argument-type]
    except Exception as error:
        return False, type(error).__name__
    return case.check(result), result.model_dump_json(exclude_defaults=True)


async def main(repeats: int) -> None:
    failed = 0
    for case in CASES:
        results = await asyncio.gather(*(one(case) for _ in range(repeats)))
        passed = sum(ok for ok, _ in results)
        failed += passed < repeats
        print(
            f'{"ok  " if passed == repeats else "FAIL"} {passed}/{repeats}  {case.id:22} wants {case.wants}'
        )
        for ok, seen in results:
            if not ok:
                print('        got', seen)
    print(
        'all cases passed every time'
        if not failed
        else f'{failed} of {len(CASES)} cases failed at least once'
    )


if __name__ == '__main__':
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 5))
