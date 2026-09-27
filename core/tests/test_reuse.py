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
from functools import partial
from unittest.mock import patch

import httpx2
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from core.graph import Node
from core.locale import Locale
from core.policies import TurnBudget
from core.turn import Turn
from core.turn import take_turn as unread_turn
from tests.clinic import CLINIC, ClinicCall, desk

take_turn = partial(unread_turn, received=True)
"""A turn whose every word reached the caller: these are text conversations, each line read in full."""


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
    call.acknowledge(call.spoken[-1].id)
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
            await unread_turn(call, nodes, 'Yes.')
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


async def a_yes_and_a_goodbye_are_both_done():
    """A yes to the read-back that also says goodbye books, says the receipt without "Anything else?", and ends."""
    call, heard, agent = opened()
    nodes = desk(FunctionModel(stream_function=agent.stream))
    with scripted(chosen_time='0900'):
        await take_turn(call, nodes, 'Nine, please.')
    with scripted(confirmation='accept', conversation='end') as asked:
        await take_turn(call, nodes, 'Yes, that is all. Goodbye.')
    assert call._book == {'0900': 'walk-in'} and call.ended == 'caller', (call._book, call.ended)
    assert heard[-2:] == ['Booked for Tuesday at 9:00 AM.', 'Goodbye.'], heard
    assert sorted(asked) == ['confirmation', 'conversation'] and agent.requests == 0, (asked, agent.requests)


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


async def a_yes_with_a_new_request_is_identified_and_the_rest_answered():
    """Coval run hdbogMqB, AJ4t, in the clinic: the yes identifies, and the step's model answers the rest.

    Identifying says nothing of its own, so the turn used to end there, in
    silence, with the move never answered.
    """
    call = ClinicCall()
    heard: list[str] = []

    async def say(text: str) -> None:
        heard.append(text)

    call._say = say
    asked: list[str] = []

    async def stream(messages, info):
        asked.append(f'{messages!r} {info.instructions}')
        yield 'Thursday, let me look.'

    nodes = desk(FunctionModel(stream_function=stream))
    with scripted():
        await take_turn(call, nodes, 'My patient number is P-1234.')
    assert heard[-1] == 'Is your patient number P-1234?' and not asked, (heard, asked)
    with scripted(confirmation='accept'):
        said, _ = await take_turn(call, nodes, 'Yes, that is me. And can we move my cleaning to Thursday?')
    assert call.state.patient == 'P-1234', 'the yes did not identify'
    assert heard[-1] == 'Thank you.' and said == 'Thursday, let me look.', (heard, said)
    assert len(asked) == 1 and 'move my cleaning to Thursday' in asked[0], asked
    assert 'Application action identify done.' in asked[0], 'the model was not told the yes was acted on'


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


async def a_retried_request_ends_on_the_callers_words():
    """A retry is sent the caller's words last: never the holding line in front of them, never an empty request.

    Replays of 2026-09-25: the holding line said as the retry went out was
    inserted before the caller's words, and in 3 turns of 24 calls a retry
    with no prompt went out on a history that ended on a reply, which the
    SDK refuses ("Processed history must end with a `ModelRequest`"), so the
    caller heard the stumble line. Both the stall and a history that ends on
    a reply are built here.
    """
    sent: list[list] = []

    async def stream(messages, info):
        sent.append(messages)
        if len(sent) == 1:
            await asyncio.sleep(10)
        yield 'It is 40 euros.'

    call, heard, _ = opened()
    nodes = desk(FunctionModel(stream_function=stream))
    with scripted(chosen_time=''):
        said, _ = await take_turn(call, nodes, 'How much is it?', TurnBudget(first_part=0.2))
    last = sent[-1]
    assert said == 'It is 40 euros.' and not set(heard) & set(call.locale.holding), (
        'a provider stall is not a line to fill (session 4e)',
        said,
        heard,
    )
    said_before = [p.content for m in last for p in m.parts if isinstance(p, TextPart)]
    assert isinstance(last[-1], ModelRequest) and not set(said_before) & set(call.locale.holding), last
    assert [p.content for p in last[-1].parts if isinstance(p, UserPromptPart)] == ['How much is it?'], last

    sent.clear()
    call, _, _ = opened()
    call.messages = [
        ModelRequest(parts=[UserPromptPart('Hello.')]),
        ModelResponse(parts=[TextPart('Hi, how can I help?')]),
    ]
    call.begin_turn()
    call.turn_started('How much is it?')
    async for _ in Turn(call, nodes, TurnBudget())._speak(nodes['desk'], None):
        pass
    assert isinstance(sent[-1][-1], ModelRequest), sent[-1]
    assert [p.content for p in sent[-1][-1].parts if isinstance(p, UserPromptPart)] == ['How much is it?']


async def a_slow_turn_says_one_holding_line():
    """A model that is only slow to answer gets no holding line: nothing is being done that the caller waits on.

    Session 3 heard "One moment" in front of a goodbye, from a timer that ran
    for the whole turn. The line now covers running work only (a tool, an
    application action; `core/tests/test_nodes.py` has the slow tool). Nor
    after the answer started: a model that spoke at once and then went quiet
    is not covered, because the caller is already hearing it.
    """
    budget = TurnBudget(hold_after=0.1)
    for delay, expected in ((0.4, 0), (0.0, 0)):
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


async def a_second_request_is_no_reason_to_hold():
    """A fast tool and a fast answer after it get no holding line: a second request is not time waited (session 4e).

    Until session 4e the line went out with every second request, so the
    caller heard "One moment" just before an answer that was about to play.
    Only work that lasts past `hold_after` is covered (`test_nodes` P10), and
    a tool that speaks its own line gets nothing in front of it.
    """
    budget = TurnBudget(hold_after=10.0)

    def looked_up(speaks: bool) -> dict[str, Node]:
        async def stream(messages, info):
            if not any(p.part_kind == 'tool-return' for m in messages for p in m.parts):
                yield {0: DeltaToolCall(name='look_up', json_args='{}')}
                return
            await asyncio.sleep(0.2)
            yield 'It is 40 euros.'

        agent = Agent[ClinicCall, str](FunctionModel(stream_function=stream), deps_type=ClinicCall)

        @agent.tool
        async def look_up(ctx) -> str:
            if speaks:
                await ctx.deps.say('Let me find the price.', origin='tool_filler')
            return '40 euros'

        return {'desk': Node(agent)}

    call, heard, _ = opened()
    said, _ = await take_turn(call, looked_up(speaks=False), 'How much is it?', budget)
    assert [s.origin for s in call.spoken] == ['model'], call.spoken
    assert not set(heard) & set(call.locale.holding) and said == 'It is 40 euros.', (heard, said)

    call, heard, _ = opened()
    await take_turn(call, looked_up(speaks=True), 'How much is it?', budget)
    assert heard[-1] == 'Let me find the price.', heard

    call, heard, agent = opened()
    with scripted(chosen_time=''):
        await take_turn(call, desk(FunctionModel(stream_function=agent.stream)), 'Hm.', budget)
    assert not any(s.origin == 'tool_filler' for s in call.spoken), call.spoken


async def a_read_back_that_played_is_answered_on_the_next_turn():
    """Trace fbdd81b6, in the clinic: the runtime's late report cut a read-back that had played out.

    The yes then joined the turn before, where nothing was waiting on it, and
    nothing was booked.
    """
    call, heard, agent = opened()
    nodes = desk(FunctionModel(stream_function=agent.stream))
    call._playback_known = True
    with scripted(chosen_time='0900'):
        await take_turn(call, nodes, 'Nine, please.')
    assert heard[-1] == 'Shall I book you in for Tuesday at 9:00 AM?', heard
    read_back = call.spoken[-1]
    call.note_reached()
    call._burst -= 60
    call.note_drained()
    call.note_heard('', segment_ids=(read_back.id,), interrupted=True)
    call.note_barge()
    with scripted(confirmation='accept'):
        await take_turn(call, nodes, 'Yes.')
    assert call._book == {'0900': 'walk-in'}, 'a yes to a read-back that played booked nothing'


async def the_step_is_told_the_date_in_its_own_timezone():
    """Trace bbe42ad4: a step that works with days is told today's date and the week ahead, from core."""
    call, _, _ = opened()
    told: list[str] = []

    async def stream(messages, info):
        told.append(str(info.instructions))
        yield 'It is 40 euros.'

    with scripted():
        await take_turn(call, desk(FunctionModel(stream_function=stream)), 'How much is it?')
    assert told and 'Today is ' in told[0] and '(America/New_York)' in told[0], told
    assert told[0].count('\n- ') == 7, 'the clinic shows a week'


async def a_provider_error_never_silences_the_turn():
    """Trace fbdd81b6: one reading hit a DNS failure, the error left the turn, and the caller sat in silence.

    A reading that cannot reach its provider reads as its default, and the
    step answers. An `advance` that raises anyway gives the stumble line.
    """
    call, _, agent = opened()

    async def offline(messages, info):
        raise httpx2.ConnectError('[Errno 8] nodename nor servname provided, or not known')

    with ExitStack() as stack:
        for reading in (*CLINIC.readings, CLINIC.closing.reading):
            stack.enter_context(patch.object(reading.classifier, 'model', FunctionModel(offline)))
        said, done = await take_turn(
            call, desk(FunctionModel(stream_function=agent.stream)), 'How much is it?'
        )
    assert done.status == 'answered' and said == 'It is 40 euros.', (done.status, said)

    call, _, agent = opened()

    async def advance(call, usage, limits):
        raise RuntimeError('a reading nobody guarded')

    nodes = {'desk': Node(desk(FunctionModel(stream_function=agent.stream))['desk'].agent, advance=advance)}
    _, done = await take_turn(call, nodes, 'How much is it?')
    assert done.status == 'failed' and call.spoken[-1].text == call.locale.stumble, (done.status, call.spoken)


async def a_value_that_does_not_parse_is_refused_with_its_reason():
    """Five digits after the P: the clinic says why, by code, and the agent is not asked."""
    call = ClinicCall()
    heard: list[str] = []

    async def say(text: str) -> None:
        heard.append(text)

    call._say = say
    agent = Desk()
    with scripted():
        await take_turn(call, desk(FunctionModel(stream_function=agent.stream)), 'It is P-12345.')
    assert heard[-1] == 'A patient number has four digits after the P. Could you say it again?', heard
    assert agent.requests == 0 and not call.state.patient


async def the_engine_speaks_the_calls_own_language():
    """A use case in another language gives its own locale, and the turn engine's own lines follow it."""

    class Llamada(ClinicCall):
        default_locale = Locale(
            stumble='Perdone, ¿puede repetirlo?',
            exhausted='',
            unbacked='',
            withheld='',
            holding=('Un momento.',),
            unsettled='',
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
    await a_yes_and_a_goodbye_are_both_done()
    await a_pick_with_a_condition_is_left_to_the_model()
    await a_yes_without_its_question_writes_nothing()
    await a_value_said_in_full_is_read_back_by_code()
    await a_yes_with_a_new_request_is_identified_and_the_rest_answered()
    await a_value_that_does_not_parse_is_refused_with_its_reason()
    await a_provider_error_never_silences_the_turn()
    await the_step_is_told_the_date_in_its_own_timezone()
    await a_read_back_that_played_is_answered_on_the_next_turn()
    await a_reading_and_the_step_run_side_by_side()
    await a_reading_that_decides_drops_the_step_run()
    await a_reading_that_changes_the_call_gets_a_fresh_run()
    await a_request_that_sends_nothing_is_tried_again()
    await a_slow_turn_says_one_holding_line()
    await a_second_request_is_no_reason_to_hold()
    await a_retried_request_ends_on_the_callers_words()
    await the_engine_speaks_the_calls_own_language()
    no_salon_code_is_loaded()
    print('reuse ok: a second use case on the same core classes, with no salon code')


if __name__ == '__main__':
    asyncio.run(main())
