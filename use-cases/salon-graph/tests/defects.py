"""The salon's known defects: checks that fail today on purpose, kept out of `python -m tests`.

Run: uv run python -m tests.defects

The same contract as `core/tests/defects.py`: each check says the behaviour
the salon should have, prints `KNOWN DEFECT` while it is still wrong and
`fixed` once it is right, and the exit code is the number still present.
A repaired check moves into the suite, where a regression fails it.

Every check drives the real salon flow (`core.text_turn` with the scripted
provider), or the diary on a real SQLite file, so the same check runs
unchanged before and after a repair: session 4d's before-and-after evidence.
"""

import asyncio
import os
from collections.abc import Callable

import pydantic_ai.models

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
os.environ.setdefault('GOOGLE_API_KEY', 'offline-test-placeholder')
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')

OTHER = '+447700900123'
"""A second, synthetic caller on the same diary file (Ofcom's drama range)."""


def a_restated_booking_value_books() -> bool:
    """Defect 1: "No, do not book Friday at two. I am only checking if it is free.", and the agent records the slot.

    Recording the unchanged slot is extraction, not a yes. The parent booked
    and said "Yes, that's the same as I read back."
    """
    from tests.script import slot, turn
    from tests.test_business import FRIDAY, at_booking, booked, read_back

    async def check() -> bool:
        script, flow, call, diary = await at_booking()
        await read_back(flow, call, script)
        said = await turn(
            flow,
            call,
            script,
            'No, do not book Friday at two. I am only checking if it is free.',
            ('collect', {'slot': slot(FRIDAY, '14:00')}),
            ('say', 'Two is free. Nothing is booked.'),
        )
        return bool(booked(diary)) or call.locale.restated in said

    return asyncio.run(check())


def a_refused_move_is_unknown() -> bool:
    """Defect 3: the move's destination is taken by another caller between its read-back and the yes.

    The diary refuses, correctly, but its receipt carries no booking id, so
    core cannot bind it to the request and the intent is left unknown.
    """
    from salon.diary import Diary
    from tests.script import CLOCK, slot, turn
    from tests.test_business import FRIDAY, at_booking, seed

    async def check() -> bool:
        script, flow, call, diary = await at_booking()
        held = seed(diary)
        await turn(
            flow,
            call,
            script,
            'Can you move my Friday haircut to two?',
            ('collect', {'service': 'haircut', 'booking_day': 'Friday', 'booking_time': '14:00'}),
            ('call', 'options'),
            ('say', 'Two is free. Shall I move it?'),
        )
        await turn(
            flow,
            call,
            script,
            'Yes, to two.',
            ('collect', {'appointment': held, 'slot': slot(FRIDAY, '14:00')}),
            ('call', 'move_appointment'),
        )
        other = Diary(path=diary.path, clock=CLOCK)
        other.find_or_create_customer(OTHER)
        other.find_slots(OTHER, FRIDAY, 'haircut', CLOCK, offered_to='other', prefer='14:00')
        other.save_booking(OTHER, 'book', slot(FRIDAY, '14:00'), '', False, CLOCK, offered_to='other')
        await turn(flow, call, script, 'Yes, move it.', ('call', 'move_appointment'), ('say', 'Sorry.'))
        [move] = [i for i in call.intents if i.operation == 'move_appointment']
        return move.status != 'rejected'

    return asyncio.run(check())


def a_day_change_forgets_the_appointment() -> bool:
    """Defect 4 (trace ce40c124): the caller picks the appointment to move, then names the new day.

    The day is what the destination times rest on; the appointment they hold
    is not. The parent cleared both, so the move was missing its booking.
    """
    from tests.script import slot, turn
    from tests.test_business import TUE, at_booking, seed

    async def check() -> bool:
        script, flow, call, diary = await at_booking()
        held = seed(diary)
        await turn(
            flow,
            call,
            script,
            'I want to move my Friday haircut.',
            ('call', 'options'),
            ('collect', {'appointment': held}),
            ('say', 'Which day would you like instead?'),
        )
        said = await turn(
            flow,
            call,
            script,
            'Tuesday at ten.',
            ('collect', {'service': 'haircut', 'booking_day': 'Tuesday', 'booking_time': '10:00'}),
            ('call', 'options'),
            ('say', 'Ten is free on Tuesday.'),
        )
        said = await turn(
            flow,
            call,
            script,
            'Ten, yes.',
            ('collect', {'slot': slot(TUE, '10:00')}),
            ('call', 'move_appointment'),
        )
        return call.state.facts.appointment != held or not said[-1].startswith('Shall I move')

    return asyncio.run(check())


def day_after_tomorrow_names_no_day() -> bool:
    """Defect 5 (trace 5edecd4c): "the day after tomorrow" is a day; "day after tomorrow" was not."""
    from salon.diary import resolve_day
    from tests.script import CLOCK

    return resolve_day('day after tomorrow', CLOCK.date()) is None


def verify_says_model_digits() -> bool:
    """Defect 6 (trace 5edecd4c, Stage C finding 3): the verify step says a digit read-back in the model's words.

    Code reads the number back in its own grouping; a model sentence with
    other digits contradicts it, and a yes to it can never count.
    """
    from tests.script import opened, turn

    words = 'I have +34 600 111 2222. Which part is wrong?'

    async def check() -> bool:
        script, flow, call, _ = opened(phone='')
        call.commit(agent='verify')
        said = await turn(
            flow,
            call,
            script,
            'It is plus 34 600 111 2222.',
            ('collect', {'customer_phone': '+346001112222'}),
            ('say', words),
        )
        return words in said

    return asyncio.run(check())


DEFECTS: dict[str, tuple[Callable[[], bool], str]] = {
    'a restated booking value books without a yes': (a_restated_booking_value_books, '4d defect 1'),
    'a move refused by the diary is left unknown': (a_refused_move_is_unknown, '4d defect 3'),
    'a day change forgets the appointment being moved': (a_day_change_forgets_the_appointment, '4d defect 4'),
    '"day after tomorrow" names no day': (day_after_tomorrow_names_no_day, '4d defect 5'),
    'verify says a digit read-back in model words': (verify_says_model_digits, '4d defect 6'),
}
"""Each defect, its check (True while the defect is present) and the session that owns the repair."""


def main() -> int:
    present = 0
    for name, (check, session) in DEFECTS.items():
        if check():
            present += 1
            print(f'KNOWN DEFECT (session {session}): {name}')
        else:
            print(f'fixed: {name}')
    return present


if __name__ == '__main__':
    raise SystemExit(main())
