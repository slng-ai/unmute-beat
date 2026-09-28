"""The native arm's N1-N13 scenarios, on the real pipeline (`evals.line`). Each returns an observation.

Every scenario checks the harness's acceptance condition for its row, the
same condition the harness arm checks, and reports `passed` for it. A native
gap is therefore a native `passed: False`, recorded, never a softer rule.
The native reference itself (`graph_salon/`, `flow.yaml`, `bot.py`) is not
changed; this file only drives it.

    uv run python -m evals.matrix booking     # one scenario, JSON on stdout
"""

import asyncio
import json
import sys
import time
from typing import Any, cast

from pipecat.flows import FlowManager, NodeConfig
from pipecat.frames.frames import InterruptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMSpecificMessage
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair

from evals.line import KNOWN, MONDAY, Line, Reply
from graph_salon import GraphSalon
from graph_salon.flow import FLOW
from salon.diary import Diary

OFFER = 'For a haircut on Tuesday, I have 9:00 AM, 9:30 AM and 10:00 AM. Which time works for you?'

TURN_REQUESTS = 8
"""The per-turn request bound both builds share (native `TOOL_CALLS_PER_TURN`, harness `TurnBudget`).

`empty_output` passes on a bounded, audible, write-free recovery. Its first
version passed only at two requests or fewer, a number copied from the
native build's own repair (one rerun, then a fixed line), not the row's
rule; the harness spent three. Both counts are reported as measured.
"""


def bookings(line: Line) -> list[dict[str, Any]]:
    return line.diary.bookings_for(KNOWN, MONDAY.date())


def nine_on_tuesday(line: Line) -> str:
    slots = line.diary.find_slots(KNOWN, 'Tuesday', 'haircut', MONDAY)['slots']
    return next(s['slot_id'] for s in slots if s['time'] == '09:00')


async def to_offer(line: Line) -> list[dict[str, Any]]:
    """The caller asks for a haircut before saying who they are, is verified, and is offered Tuesday's times."""
    return [
        await line.turn('I want a haircut on Tuesday.', Reply(calls=[('hand_over', {'to': 'booking'})])),
        await line.turn('+34 600 111 222', Reply(calls=[('ask_number', {'phone': KNOWN})])),
        await line.turn(
            'Yes.',
            Reply(calls=[('answer', {'reply': 'yes'})]),
            Reply(calls=[('read_diary', {'day': 'Tuesday', 'service': 'haircut'})]),
            Reply(text=OFFER),
        ),
    ]


async def to_read_back(line: Line) -> list[dict[str, Any]]:
    turns = await to_offer(line)
    pick = Reply(calls=[('ask_booking', {'action': 'book', 'slot_id': nine_on_tuesday(line)})])
    turns.append(await line.turn('Nine, please.', pick))
    return turns


async def booking(delay: float = 0.05) -> dict[str, Any]:
    """N2/N3/N4/N12: the whole booking, with the request made before identity. One booking, on the read-back."""
    async with Line(delay=delay) as line:
        turns = await to_read_back(line)
        turns.append(await line.turn('Yes.', Reply(calls=[('answer', {'reply': 'yes'})])))
        booked = bookings(line)
        return {
            'passed': len(booked) == 1 and booked[0]['time'] == '09:00',
            'bookings': len(booked),
            'turns': turns,
            'requests': {'agent': sum(t['requests'] for t in turns), 'classifier': 0},
        }


async def replay() -> dict[str, Any]:
    """N2: a second yes after the booking is not a second booking."""
    async with Line() as line:
        await to_read_back(line)
        await line.turn('Yes.', Reply(calls=[('answer', {'reply': 'yes'})]))
        again = await line.turn('Yes, book it.', Reply(calls=[('answer', {'reply': 'yes'})]))
        return {'passed': len(bookings(line)) == 1, 'bookings': len(bookings(line)), 'after': again}


async def unplayed_read_back() -> dict[str, Any]:
    """N9: the read-back was generated and handed on, and its audio never played. A yes must book nothing.

    The native build has no playback evidence to consult: `SalonCall.approve`
    checks the proposal's kind, caller and turn.
    """
    async with Line() as line:
        await to_read_back(line)
        await line.turn('Yes.', Reply(calls=[('answer', {'reply': 'yes'})]))
        return {'passed': not bookings(line), 'bookings': len(bookings(line))}


async def partial_read_back() -> dict[str, Any]:
    """N9: the caller cut into the read-back. A yes to what they did not hear must book nothing."""
    async with Line() as line:
        await to_read_back(line)
        await line.worker.queue_frame(InterruptionFrame())
        await asyncio.sleep(0.1)
        await line.turn('Yes.', Reply(calls=[('answer', {'reply': 'yes'})]))
        return {'passed': not bookings(line), 'bookings': len(bookings(line))}


async def cut_receipt() -> dict[str, Any]:
    """N8: the caller cuts into the receipt; the booking stands, once, and the next yes does not repeat it."""
    async with Line() as line:
        await to_read_back(line)
        await line.turn('Yes.', Reply(calls=[('answer', {'reply': 'yes'})]))
        await line.worker.queue_frame(InterruptionFrame())
        await asyncio.sleep(0.1)
        after = await line.turn('Sorry, is it booked?', Reply(calls=[('answer', {'reply': 'yes'})]))
        return {'passed': len(bookings(line)) == 1, 'bookings': len(bookings(line)), 'after': after}


async def empty_output() -> dict[str, Any]:
    """N6: the model answers with nothing, twice. Bounded: one repair, then a fixed line, never silence."""
    async with Line() as line:
        await to_offer(line)
        turn = await line.turn('What else do you have?', Reply(), Reply(), Reply())
        return {
            'passed': bool(turn['heard']) and not bookings(line) and turn['requests'] <= TURN_REQUESTS,
            'heard': turn['heard'],
            'requests': turn['requests'],
        }


async def bad_slot() -> dict[str, Any]:
    """N1: the model names a slot that does not exist. Nothing is read back, nothing is booked."""
    async with Line() as line:
        await to_offer(line)
        bad = Reply(calls=[('ask_booking', {'action': 'book', 'slot_id': '2026-09-22T0915|haircut'})])
        turn = await line.turn('Quarter past nine.', bad, Reply(text='That time is not free.'))
        return {
            'passed': not bookings(line) and 'Shall I book' not in turn['heard'],
            'heard': turn['heard'],
            'node': turn['node'],
        }


async def two_calls() -> dict[str, Any]:
    """N11: two calls at once on the one flow config: each books its own, neither sees the other's."""

    async def one(time_: str) -> tuple[int, str]:
        async with Line() as line:
            await to_offer(line)
            slots = line.diary.find_slots(KNOWN, 'Tuesday', 'haircut', MONDAY)['slots']
            slot = next(s['slot_id'] for s in slots if s['time'] == time_)
            await line.turn('That one.', Reply(calls=[('ask_booking', {'action': 'book', 'slot_id': slot})]))
            await line.turn('Yes.', Reply(calls=[('answer', {'reply': 'yes'})]))
            return len(bookings(line)), bookings(line)[0]['time'] if bookings(line) else ''

    first, second = await asyncio.gather(one('09:00'), one('10:00'))
    return {'passed': first == (1, '09:00') and second == (1, '10:00'), 'calls': [first, second]}


async def barge_during_entry_line() -> dict[str, Any]:
    """N7: the caller cuts into the line a node speaks on entry; their next words are still taken and routed."""
    async with Line() as line:
        await line.turn('I want a haircut on Tuesday.', Reply(calls=[('hand_over', {'to': 'booking'})]))
        await line.worker.queue_frame(InterruptionFrame())
        await asyncio.sleep(0.1)
        turn = await line.turn('+34 600 111 222', Reply(calls=[('ask_number', {'phone': KNOWN})]))
        return {
            'passed': turn['node'] == 'confirm' and 'read that back' in turn['heard'],
            'node': turn['node'],
            'heard': turn['heard'],
        }


async def action_frame_dropped() -> dict[str, Any]:
    """N7, the native lesson: a node change must not wait forever for an `ActionFinishedFrame` a barge-in drops.

    Pipecat's own `tts_say` holds the node change until that frame reaches
    the end of the pipeline; the pipeline here never runs, as a barge-in
    drops the frame. The build's custom `say` action waits for nothing.
    """

    async def enter(action: dict[str, Any]) -> bool:
        salon = GraphSalon(Diary(clock=MONDAY), 'call-1', Line().llm)
        pair = LLMContextAggregatorPair(salon.context)
        salon.attach(PipelineWorker(Pipeline([pair.user(), salon.llm, pair.assistant()])), pair)
        await asyncio.wait_for(salon.start(), 2)
        manager = salon._manager
        assert isinstance(manager, FlowManager)
        node = cast(NodeConfig, {**FLOW.node('verify_listen'), 'pre_actions': [action]})
        try:
            await asyncio.wait_for(manager.set_node_from_config(node), 2)
        except TimeoutError:
            return False
        return True

    native_say = await enter(
        {'type': 'say', 'handler': FLOW.node('verify_listen')['pre_actions'][0]['handler']}
    )
    pipecat_tts_say = await enter({'type': 'tts_say', 'text': 'Which phone number should I use?'})
    return {
        'passed': native_say,
        'custom_say_completes': native_say,
        'tts_say_completes_within_2s': pipecat_tts_say,
    }


async def provider_messages_in_context() -> dict[str, Any]:
    """N10: a provider-specific record in the context (Gemini's function call) must not crash the next tool."""
    async with Line() as line:
        record = {
            'role': 'model',
            'parts': [{'function_call': {'name': 'hand_over', 'args': {'to': 'booking'}}}],
        }
        line.salon.context.add_message(LLMSpecificMessage(llm='google', message=record))
        turn = await line.turn(
            'I want a haircut on Tuesday.', Reply(calls=[('hand_over', {'to': 'booking'})])
        )
        return {'passed': turn['node'] == 'verify_listen', 'node': turn['node']}


async def timing(delay: float = 0.0) -> dict[str, Any]:
    """N12: the booking trace, timed turn by turn, with the scripted model answering after `delay` seconds."""
    started = time.perf_counter()
    result = await booking(delay)
    result['seconds'] = time.perf_counter() - started
    return result


SCENARIOS = {
    fn.__name__: fn
    for fn in (
        booking,
        replay,
        unplayed_read_back,
        partial_read_back,
        cut_receipt,
        empty_output,
        bad_slot,
        two_calls,
        barge_during_entry_line,
        action_frame_dropped,
        provider_messages_in_context,
        timing,
    )
}


def main() -> None:
    name, *rest = sys.argv[1:]
    kwargs = {'delay': float(rest[0])} if rest else {}
    print(json.dumps(asyncio.run(SCENARIOS[name](**kwargs)), default=str))


if __name__ == '__main__':
    main()
