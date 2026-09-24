"""A second use case runs on the same core classes as the salon, with no salon code at all.

The clinic (`tests/clinic.py`) gives core's acts its own information and
nothing else. These checks run it through the real turn engine with
scripted classifier answers: the plumbing, not language understanding,
which the Langfuse reading datasets measure.
"""

import asyncio
import json
import os
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from core.turn import take_turn
from tests.clinic import CLINIC, ClinicCall, desk


@contextmanager
def scripted(**labels: str):
    """Every clinic reading answers its label here, or its default. Yields the names of the readings asked."""
    asked: list[str] = []
    with ExitStack() as stack:
        for reading in (*CLINIC.readings, CLINIC.closing.reading):
            value = labels.get(reading.name, getattr(reading.fallback, reading.field))

            def answer(messages, info, name=reading.name, field=reading.field, value=value):
                asked.append(name)
                return ModelResponse(parts=[TextPart(json.dumps({field: value}))])

            stack.enter_context(patch.object(reading.classifier, 'model', FunctionModel(answer)))
        yield asked


class Desk:
    """The clinic's agent, scripted: it counts the requests it gets and says one line."""

    def __init__(self) -> None:
        self.requests = 0

    async def stream(self, messages, info):
        self.requests += 1
        yield 'It is 40 euros.'


def opened() -> tuple[ClinicCall, list[str], Desk]:
    call = ClinicCall()
    heard: list[str] = []

    async def say(text: str) -> None:
        heard.append(text)

    call._say = say
    call.note_said('fixed', call.offer(), origin='ask')
    return call, heard, Desk()


async def a_pick_is_read_back_and_only_the_yes_books():
    call, heard, agent = opened()
    nodes = desk(FunctionModel(stream_function=agent.stream))
    with scripted(chosen_time='0900') as asked:
        await take_turn(call, nodes, 'Nine, please.')
    assert heard[-1] == 'Shall I book you in for Tuesday at 9:00 AM?', heard
    assert agent.requests == 0 and asked == ['chosen_time', 'conversation'], (agent.requests, asked)
    assert not call._book, 'a pick is not a yes: nothing is booked yet'

    with scripted(confirmation='accept') as asked:
        await take_turn(call, nodes, 'Yes.')
    assert call._book == {'0900': 'walk-in'} and call.state.booked == '0900', call._book
    assert heard[-1] == 'Booked for Tuesday at 9:00 AM. Anything else?' and agent.requests == 0

    with scripted(undo_booking='withdraw') as asked:
        await take_turn(call, nodes, 'Actually, leave it.')
    assert heard[-1] == 'Shall I cancel Tuesday at 9:00 AM?' and call._book, (heard, call._book)
    assert 'undo_booking' in asked

    with scripted(confirmation='accept'):
        await take_turn(call, nodes, 'Yes, cancel it.')
    assert not call._book and call.held('undo') is None and heard[-1] == 'That is cancelled. Anything else?'

    with scripted(conversation='end'):
        await take_turn(call, nodes, 'No, that is all. Bye.')
    assert call.ended == 'caller' and heard[-1] == 'Goodbye.' and agent.requests == 0


async def a_pick_with_a_condition_is_left_to_the_model():
    """The pick reading finds no plain pick in a condition, so nothing is read back and the agent answers."""
    call, heard, agent = opened()
    nodes = desk(FunctionModel(stream_function=agent.stream))
    with scripted(chosen_time=''):
        said, _ = await take_turn(call, nodes, 'Nine, but only if it is under 50 euros.')
    assert agent.requests == 1 and 'Shall I book' not in ' '.join(heard) and not call._book, (said, heard)


async def a_yes_without_its_question_writes_nothing():
    """Consent is core's: a yes with no booking read-back waiting runs no write, whatever the reading says."""
    call, heard, agent = opened()
    nodes = desk(FunctionModel(stream_function=agent.stream))
    with scripted(confirmation='accept', chosen_time=''):
        await take_turn(call, nodes, 'Yes.')
    assert not call._book and agent.requests == 1 and not any('Booked' in line for line in heard), heard


async def a_value_said_in_full_is_read_back_by_code():
    call = ClinicCall()
    heard: list[str] = []

    async def say(text: str) -> None:
        heard.append(text)

    call._say = say
    agent = Desk()
    nodes = desk(FunctionModel(stream_function=agent.stream))
    with scripted() as asked:
        await take_turn(call, nodes, 'Hi, my patient number is P-1234.')
    assert heard[-1] == 'Is your patient number P-1234?' and agent.requests == 0 and not asked, (heard, asked)
    with scripted(confirmation='accept'):
        await take_turn(call, nodes, 'Yes.')
    assert call.state.patient == 'P-1234'


def no_salon_code_is_loaded():
    """In a fresh interpreter, because `python -m tests` runs every module in one process, salon included."""
    probe = (
        "import sys, tests.clinic; sys.exit(any(n == 'salon' or n.startswith('salon.') for n in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, '-c', probe], env={**os.environ, 'GOOGLE_API_KEY': 'x'}, check=False
    )
    assert result.returncode == 0, 'the clinic imported salon code'


async def main():
    await a_pick_is_read_back_and_only_the_yes_books()
    await a_pick_with_a_condition_is_left_to_the_model()
    await a_yes_without_its_question_writes_nothing()
    await a_value_said_in_full_is_read_back_by_code()
    no_salon_code_is_loaded()
    print('reuse ok: a second use case on the same core classes, with no salon code')


if __name__ == '__main__':
    asyncio.run(main())
