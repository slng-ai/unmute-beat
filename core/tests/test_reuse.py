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
import time
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from tests.clinic import CLINIC, ClinicCall, desk

from core.graph import Node
from core.locale import Locale
from core.policies import TurnBudget
from core.turn import take_turn


@contextmanager
def scripted(delay: float = 0.0, **labels: str):
    """Every clinic reading answers its label here, or its default, after `delay` seconds. Yields the readings asked."""
    asked: list[str] = []
    with ExitStack() as stack:
        for reading in (*CLINIC.readings, CLINIC.closing.reading):
            value = labels.get(reading.name, getattr(reading.fallback, reading.field))

            async def answer(messages, info, name=reading.name, field=reading.field, value=value):
                asked.append(name)
                await asyncio.sleep(delay)
                return ModelResponse(parts=[TextPart(json.dumps({field: value}))])

            stack.enter_context(patch.object(reading.classifier, 'model', FunctionModel(answer)))
        yield asked


class Desk:
    """The clinic's agent, scripted: it counts the requests it gets and says one line, after `delay` seconds."""

    def __init__(self, delay: float = 0.0) -> None:
        self.requests = 0
        self.delay = delay

    async def stream(self, messages, info):
        self.requests += 1
        await asyncio.sleep(self.delay)
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
    assert agent.requests == 0 and sorted(asked) == ['chosen_time', 'conversation'], (agent.requests, asked)
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


async def a_change_is_not_taken_as_an_undo():
    """ "Can we do Thursday instead?" after a booking changes it, it does not take it back: no cancel is offered."""
    call, heard, agent = opened()
    nodes = desk(FunctionModel(stream_function=agent.stream))
    with scripted(chosen_time='0900'):
        await take_turn(call, nodes, 'Nine, please.')
    with scripted(confirmation='accept'):
        await take_turn(call, nodes, 'Yes.')
    assert call._book and call.held('undo') is not None
    with scripted(undo_booking='change') as asked:
        said, _ = await take_turn(call, nodes, 'Can we do Thursday instead?')
    assert 'undo_booking' in asked and not any('cancel' in line for line in heard[-1:]), heard
    assert agent.requests == 1 and said == 'It is 40 euros.' and call._book, (agent.requests, said)


async def a_goodbye_to_a_cut_off_question_does_not_hang_up():
    """The caller cuts in 0.2 s into "Booked ... Anything else?": their words go on the same turn, not answer it.

    Whichever order the cut-in, the runtime's report of the whole line, and
    the audio stopping arrive in (Coval run 4bLgLR4t, case 05, hung up here).
    """
    from core import turn as engine

    for order in (('barge', 'report', 'stop'), ('stop', 'barge', 'report')):
        call, heard, agent = opened()
        nodes = desk(FunctionModel(stream_function=agent.stream))
        with scripted(chosen_time='0900'):
            await take_turn(call, nodes, 'Nine, please.')
        with scripted(confirmation='accept'):
            await take_turn(call, nodes, 'Yes.')
        told, turn = heard[-1], call.turn
        call.note_reached()
        for signal in order:
            if signal == 'barge':
                call.note_barge()
            elif signal == 'report':
                engine.note_spoken(call, told, interrupted=True)
            else:
                call.note_drained()
        with scripted(conversation='end'):
            await take_turn(call, nodes, 'That is perfect.')
        assert call.ended == '' and call.turn == turn, (order, call.ended, call.turn)
        assert call.heard == 'Yes. That is perfect.' and len(call._book) == 1, (order, call.heard, call._book)


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


def _said(call: ClinicCall) -> str:
    """Every model response the call kept, as text."""
    return ' '.join(
        part.content
        for message in call.messages
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, TextPart)
    )


async def a_reading_and_the_step_run_side_by_side():
    """A reading that decides nothing costs no wait of its own: the step's request went out beside it."""
    call, _, agent = opened()
    agent.delay = 0.4
    nodes = desk(FunctionModel(stream_function=agent.stream))
    with scripted(delay=0.4, chosen_time=''):
        started = time.perf_counter()
        said, _ = await take_turn(call, nodes, 'How much is it?')
        took = time.perf_counter() - started
    assert said == 'It is 40 euros.' and agent.requests == 1, (said, agent.requests)
    assert took < 0.7, f'{took:.2f}s: the reading and the step request ran one after the other'


async def a_reading_that_decides_drops_the_step_run():
    """The early run is cancelled: nothing it said reaches the caller or the history, and its tool never runs."""
    call, heard, _ = opened()
    ran: list[str] = []

    async def stream(messages, info):
        await asyncio.sleep(0.1)
        yield {0: DeltaToolCall(name='look_up', json_args='{}')}

    async def advance(call, usage, limits):
        await CLINIC.read(call, usage=usage, usage_limits=limits, before_model=True)
        return CLINIC.decide(call)

    agent = Agent[ClinicCall, str](
        FunctionModel(stream_function=stream), deps_type=ClinicCall, output_type=str
    )

    @agent.tool_plain
    def look_up() -> str:
        ran.append('look_up')
        return 'nothing'

    with scripted(delay=0.3, chosen_time='0900'):
        said, _ = await take_turn(call, {'desk': Node(agent, advance=advance)}, 'Nine, please.')
    assert heard[-1] == 'Shall I book you in for Tuesday at 9:00 AM?' and not said, (heard, said)
    assert not ran, 'a tool of the dropped run ran'
    assert not any(isinstance(m, ModelResponse) for m in call.messages), call.messages


async def a_reading_that_changes_the_call_gets_a_fresh_run():
    """The early run's prompt was built from the call as it was; a reading that changed it means a new run."""
    call, _, agent = opened()
    nodes = desk(FunctionModel(stream_function=agent.stream))

    async def advance(call, usage, limits):
        await asyncio.sleep(0.2)
        call.commit(patient='P-1234')
        return None

    nodes = {'desk': Node(nodes['desk'].agent, advance=advance)}
    said, _ = await take_turn(call, nodes, 'It is P-1234.')
    assert said == 'It is 40 euros.' and agent.requests == 2, (said, agent.requests)
    assert _said(call).count('It is 40 euros.') == 1, _said(call)


async def a_request_that_sends_nothing_is_tried_again():
    """A step request with no first part in `first_part` seconds is cancelled and its run tried once more.

    The user's call of 24 September: one request sent nothing for 8.49 s,
    and the caller heard nothing until they asked "You still with me?".
    """
    for stalls, expected in ((1, 'It is 40 euros.'), (2, None)):
        call, _, _ = opened()
        calls: list[int] = []

        async def stream(messages, info, stalls=stalls, calls=calls):
            calls.append(1)
            if len(calls) <= stalls:
                await asyncio.sleep(10)
            yield 'It is 40 euros.'

        nodes = desk(FunctionModel(stream_function=stream))
        with scripted(chosen_time=''):
            started = time.perf_counter()
            said, done = await take_turn(call, nodes, 'How much is it?', TurnBudget(first_part=0.2))
            took = time.perf_counter() - started
        assert len(calls) == 2 and took < 1, (len(calls), took)
        prompts = [p for m in call.messages for p in m.parts if isinstance(p, UserPromptPart)]
        assert [p.content for p in prompts] == ['How much is it?'], prompts
        if expected:
            assert said == expected and done.status == 'answered', (said, done)
        else:
            assert said == call.locale.exhausted and done.status == 'exhausted', (said, done)


async def a_slow_turn_says_one_holding_line():
    """A turn silent for `hold_after` seconds says one holding line, before the answer; a quick one says none.

    Nor after the answer started: a model that spoke at once and then went
    quiet is not covered, because the caller is already hearing it.
    """
    budget = TurnBudget(hold_after=0.1)
    for delay, expected in ((0.4, 1), (0.0, 0)):
        call, _, agent = opened()
        agent.delay = delay
        with scripted(chosen_time=''):
            said, _ = await take_turn(call, desk(FunctionModel(stream_function=agent.stream)), 'Hm.', budget)
        fillers = [s for s in call.spoken if s.origin == 'tool_filler']
        assert len(fillers) == expected and said == 'It is 40 euros.', (delay, call.spoken)
        if expected:
            assert call.spoken[0] is fillers[0] and fillers[0].text in call.locale.holding, call.spoken

    async def speaks_then_pauses(messages, info):
        yield 'Well, '
        await asyncio.sleep(0.4)
        yield 'it is 40 euros.'

    call, _, _ = opened()
    with scripted(chosen_time=''):
        await take_turn(call, desk(FunctionModel(stream_function=speaks_then_pauses)), 'Hm.', budget)
    assert not any(s.origin == 'tool_filler' for s in call.spoken), call.spoken


async def the_engine_speaks_the_calls_own_language():
    """A use case in another language gives its own locale, and the turn engine's own lines follow it."""

    class Llamada(ClinicCall):
        default_locale = Locale(
            stumble='Perdone, ¿puede repetirlo?',
            exhausted='',
            unbacked='',
            withheld='',
            holding=('Un momento.',),
        )

    async def broken(messages, info):
        raise RuntimeError('the model is down')
        yield ''

    call = Llamada()
    with scripted():
        said, finished = await take_turn(call, desk(FunctionModel(stream_function=broken)), '¿Hola?')
    assert finished.status == 'failed' and said == 'Perdone, ¿puede repetirlo?', (finished.status, said)


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
    await a_change_is_not_taken_as_an_undo()
    await a_goodbye_to_a_cut_off_question_does_not_hang_up()
    await a_pick_with_a_condition_is_left_to_the_model()
    await a_yes_without_its_question_writes_nothing()
    await a_value_said_in_full_is_read_back_by_code()
    await a_reading_and_the_step_run_side_by_side()
    await a_reading_that_decides_drops_the_step_run()
    await a_reading_that_changes_the_call_gets_a_fresh_run()
    await a_request_that_sends_nothing_is_tried_again()
    await a_slow_turn_says_one_holding_line()
    await the_engine_speaks_the_calls_own_language()
    no_salon_code_is_loaded()
    print('reuse ok: a second use case on the same core classes, with no salon code')


if __name__ == '__main__':
    asyncio.run(main())
