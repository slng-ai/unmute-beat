"""The turn engine's own promises, on the public clinic (`clinic_flow/agent.yaml`), with no reader or classifier.

Ported from the legacy clinic (`tests/test_reuse.py`, retired with the
classifier path in session 5); what they check is the engine's, not a
reading's:

* E1: a step request that sends nothing in time is cancelled and tried once more; a second stall is
  the exhausted line, never silence;
* E2: the retry is sent the caller's words last, never a holding line in front of them, and a run on
  a history that ends on a reply is sent the caller's words again;
* E3: a model that is only slow gets no holding line, and one that spoke and paused gets none;
* E4: a fast tool and a fast answer get no holding line;
* E5: a runtime's late report cannot cut a read-back whose audio already ran out;
* E6: a step is told today's date and the week ahead, in the call's own time zone;
* E7: a provider that fails gives the stumble line in the call's own language, never silence.

Each stream here is written by hand, for its timing: a structured step's
answer is its `reply` output (`tests.script.Reply`), streamed like any other.
Scripted models prove runtime properties, not language understanding.
"""

import asyncio
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

import core
from core.flowspec import load
from core.locale import SPANISH
from core.policies import TurnBudget
from core.turn import Turn, take_turn
from tests.clinic_flow.clinic import Backend, ClinicState
from tests.script import Call, Calls, Reply

HERE = Path(__file__).parent / 'clinic_flow'


def opened(stream, clock=None):
    """The one-agent clinic on this model, and a call on it that hears every fixed line."""
    flow = core.load(HERE / 'agent.yaml').compile(model=FunctionModel(stream_function=stream))
    call = flow.open(ClinicState(), services=Backend(), clock=clock)
    heard: list[str] = []

    async def say(text: str) -> None:
        heard.append(text)

    call._say = say
    return flow, call, heard


async def a_request_that_sends_nothing_is_tried_again():
    """E1."""
    for stalls, expected in ((1, 'It is 40 euros.'), (2, None)):
        calls: list[int] = []

        async def stream(messages, info, stalls=stalls, calls=calls):
            calls.append(1)
            if len(calls) <= stalls:
                await asyncio.sleep(10)
            yield Reply('It is 40 euros.').delta()

        flow, call, _ = opened(stream)
        started = time.perf_counter()
        said, done = await take_turn(call, flow.nodes, 'How much is it?', TurnBudget(first_part=0.2))
        assert len(calls) == 2 and time.perf_counter() - started < 1, len(calls)
        prompts = [p for m in call.messages for p in m.parts if isinstance(p, UserPromptPart)]
        assert [p.content for p in prompts] == ['How much is it?'], prompts
        if expected:
            assert call.spoken[-1].text == expected and done.status == 'answered', (said, done)
        else:
            assert said == call.locale.exhausted and done.status == 'exhausted', (said, done)


async def a_retried_request_ends_on_the_callers_words():
    """E2."""
    sent: list[list] = []

    async def stream(messages, info):
        sent.append(messages)
        if len(sent) == 1:
            await asyncio.sleep(10)
        yield Reply('It is 40 euros.').delta()

    flow, call, heard = opened(stream)
    said, _ = await take_turn(call, flow.nodes, 'How much is it?', TurnBudget(first_part=0.2))
    last = sent[-1]
    assert call.spoken[-1].text == 'It is 40 euros.' and not set(heard) & set(call.locale.holding), (
        said,
        heard,
    )
    said_before = [p.content for m in last for p in m.parts if isinstance(p, TextPart)]
    assert isinstance(last[-1], ModelRequest) and not set(said_before) & set(call.locale.holding), last
    assert [p.content for p in last[-1].parts if isinstance(p, UserPromptPart)] == ['How much is it?'], last

    sent.clear()
    flow, call, _ = opened(stream)
    call.messages = [
        ModelRequest(parts=[UserPromptPart('Hello.')]),
        ModelResponse(parts=[TextPart('Hi, how can I help?')]),
    ]
    call.begin_turn()
    call.turn_started('How much is it?')
    async for _ in Turn(call, flow.nodes, TurnBudget())._speak(flow.nodes['desk'], None):
        pass
    assert isinstance(sent[-1][-1], ModelRequest), sent[-1]
    assert [p.content for p in sent[-1][-1].parts if isinstance(p, UserPromptPart)] == ['How much is it?']


async def a_slow_model_gets_no_holding_line():
    """E3."""
    budget = TurnBudget(hold_after=0.1)

    async def slow(messages, info):
        await asyncio.sleep(0.4)
        yield Reply('It is 40 euros.').delta()

    async def speaks_then_pauses(messages, info):
        yield {0: DeltaToolCall(name='reply', json_args='{"say": "Well, ')}
        await asyncio.sleep(0.4)
        yield {0: DeltaToolCall(json_args='it is 40 euros."}')}

    for stream in (slow, speaks_then_pauses):
        flow, call, _ = opened(stream)
        said, _ = await take_turn(call, flow.nodes, 'Hm.', budget)
        assert not any(s.origin == 'tool_filler' for s in call.spoken), call.spoken
        assert call.spoken[-1].text.endswith('40 euros.'), said


async def a_fast_tool_needs_no_holding_line():
    """E4."""

    async def stream(messages, info):
        if not any(p.part_kind == 'tool-return' for m in messages for p in m.parts):
            yield Call('options').delta()
            return
        await asyncio.sleep(0.2)
        yield Reply('I have Tuesday at nine.').delta()

    flow, call, heard = opened(stream)
    said, _ = await take_turn(call, flow.nodes, 'Which times?', TurnBudget(hold_after=10.0))
    assert [s.origin for s in call.spoken] == ['model'], call.spoken
    assert not set(heard) & set(call.locale.holding) and call.spoken[-1].text == 'I have Tuesday at nine.', (
        heard,
        said,
    )


async def a_late_report_does_not_cut_what_played():
    """E5: trace fbdd81b6, in the clinic: the runtime's late report cut a read-back that had played out.

    The aggregate end of speech (`note_drained`) decides turn-taking only,
    never approval (session 4f), so the yes is bound by the copy's own
    delivery evidence, here the text reader's acknowledgement.
    """
    moves = [
        Calls(Call('options', update={'patient': 'P-1234'})),
        Calls(Call('book', update={'slot': '0900'})),
        Calls(Call('book')),
    ]

    async def stream(messages, info):
        async for streamed in (moves.pop(0) if moves else Reply('Sorry?')).stream():
            yield streamed

    flow, call, _ = opened(stream)
    await take_turn(call, flow.nodes, 'P-1234, nine, please.')
    read_back = call.spoken[-1]
    assert read_back.text.startswith('Shall I book'), call.spoken
    call.note_reached()
    call._burst -= 60
    call.note_drained()
    call.note_heard('', segment_ids=(read_back.id,), interrupted=True)
    call.note_barge()
    assert not read_back.interrupted and read_back.playback == 'played', 'a late report cut what had played'
    call.acknowledge(read_back.id)
    await take_turn(call, flow.nodes, 'Yes.')
    assert call.services.bookings, 'a yes to a read-back that played and was delivered booked nothing'


async def the_step_is_told_the_date_in_its_own_timezone():
    """E6: trace bbe42ad4: a step that works with days is told today's date and the week ahead, from core."""
    told: list[str] = []

    async def stream(messages, info):
        told.append(str(info.instructions))
        yield Reply('It is 40 euros.').delta()

    clock = lambda: datetime(2026, 9, 28, 9, 30, tzinfo=ZoneInfo('America/New_York'))  # noqa: E731
    flow, call, _ = opened(stream, clock=clock)
    await take_turn(call, flow.nodes, 'How much is it?')
    assert told and 'Today is Monday 2026-09-28' in told[0] and '(America/New_York)' in told[0], told


async def a_provider_error_never_silences_the_turn():
    """E7: the model fails, and the caller hears the stumble line in the call's own language."""

    async def broken(messages, info):
        raise RuntimeError('the model is down')
        yield ''

    flow, call, _ = opened(broken)
    said, done = await take_turn(call, flow.nodes, 'How much is it?')
    assert done.status == 'failed' and call.spoken[-1].text == call.locale.stumble == said, (
        done.status,
        said,
    )

    text = (HERE / 'agent.yaml').read_text().replace('agents:', 'locale: spanish\nagents:', 1)
    spanish = load(text, root=HERE).compile(model=FunctionModel(stream_function=broken))
    call = spanish.open(ClinicState(), services=Backend())
    said, done = await take_turn(call, spanish.nodes, '¿Hola?')
    assert done.status == 'failed' and said == SPANISH.stumble, said


async def main() -> None:
    await a_request_that_sends_nothing_is_tried_again()
    await a_retried_request_ends_on_the_callers_words()
    await a_slow_model_gets_no_holding_line()
    await a_fast_tool_needs_no_holding_line()
    await a_late_report_does_not_cut_what_played()
    await the_step_is_told_the_date_in_its_own_timezone()
    await a_provider_error_never_silences_the_turn()
    print('engine ok: E1-E7, stalls, holding lines, late reports, the date and errors, on the public clinic')


if __name__ == '__main__':
    asyncio.run(main())
