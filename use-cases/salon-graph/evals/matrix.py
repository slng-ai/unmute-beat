"""The harness arm's N1-N13 scenarios, on the real pipeline (`evals.line`). Each returns an observation.

The same scenarios, by name and by acceptance condition, as the native arm
(`use-cases/salon-pipecat-flows/evals/matrix.py`). The caller's words are the
same; what each build's model is scripted to do differs, because the builds
ask their models different things: the native model calls `answer` for a yes,
the harness reads a yes with a classifier. A scripted yes tests
authorisation, not whether a model understands consent.

    uv run python -m evals.matrix booking     # one scenario, JSON on stdout
"""

import asyncio
import json
import sys
import time
from datetime import datetime
from typing import Any

from pipecat.frames.frames import BotStartedSpeakingFrame, InterruptionFrame
from pipecat.processors.frame_processor import FrameDirection
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from core.history import paired
from evals.line import Line, Reply
from salon.diary import TZ, Diary

PHONE = '+34600111222'
MONDAY = datetime(2026, 9, 21, 8, 0, tzinfo=TZ)
WANTS = {'to': 'booking', 'because': 'a haircut', 'booking': {'service': 'haircut', 'when': 'Tuesday'}}

TURN_REQUESTS = 8
"""The per-turn request bound both builds share (native `TOOL_CALLS_PER_TURN`, harness `TurnBudget`).

`empty_output` passes on a bounded, audible, write-free recovery. Its first
version passed only at two requests or fewer, a number copied from the
native build's own repair (one rerun, then a fixed line), not the row's
rule; the harness spent three. Both counts are reported as measured.
"""


def diary() -> Diary:
    books = Diary()
    books.set_clock(MONDAY)
    return books


def bookings(line: Line) -> list[dict[str, Any]]:
    return line.diary.bookings_for(PHONE, MONDAY.date())


async def to_offer(line: Line) -> list[dict[str, Any]]:
    """The caller asks for a haircut before saying who they are, is verified, and is offered Tuesday's times."""
    return [
        await line.turn('I want a haircut on Tuesday.', Reply(calls=[('hand_over', WANTS)])),
        await line.turn('+34 600 111 222', Reply(calls=[('ask_number', {'phone': PHONE})])),
        await line.turn('Yes.'),
    ]


async def to_read_back(line: Line) -> list[dict[str, Any]]:
    turns = await to_offer(line)
    turns.append(await line.turn('Nine, please.'))
    return turns


def nine_on_tuesday(line: Line) -> Reply:
    """The booking step's model reading Tuesday at nine back again."""
    slots = line.diary.find_slots(PHONE, 'Tuesday', 'haircut', MONDAY, offered_to=line.call.id)['slots']
    slot = next(s['slot_id'] for s in slots if s['time'] == '09:00')
    return Reply(calls=[('ask_booking', {'action': 'book', 'slot_id': slot, 'booking_id': ''})])


def requests(turns: list[dict[str, Any]]) -> dict[str, int]:
    return {
        'agent': sum(t['agent_requests'] for t in turns),
        'classifier': sum(t['classifier_requests'] for t in turns),
    }


async def booking(delay: float = 0.05) -> dict[str, Any]:
    """N2/N3/N4/N12: the whole booking, with the request made before identity. One booking, on the read-back."""
    async with Line(delay=delay, diary=diary()) as line:
        turns = await to_read_back(line)
        turns.append(await line.turn('Yes.'))
        booked = bookings(line)
        return {
            'passed': len(booked) == 1 and booked[0]['time'] == '09:00',
            'bookings': len(booked),
            'turns': turns,
            'requests': requests(turns),
        }


async def replay() -> dict[str, Any]:
    """N2: a second yes after the booking is not a second booking."""
    async with Line(diary=diary()) as line:
        await to_read_back(line)
        await line.turn('Yes.')
        again = await line.turn('Yes, book it.')
        return {'passed': len(bookings(line)) == 1, 'bookings': len(bookings(line)), 'after': again}


async def unplayed_read_back() -> dict[str, Any]:
    """N9: the read-back was generated and handed on, and its audio never played. A yes must book nothing."""
    async with Line(diary=diary()) as line:
        await to_offer(line)
        line.playout.playback = 'none'
        await line.turn('Nine, please.')
        line.playout.playback = 'full'
        again = await line.turn('Yes.', nine_on_tuesday(line))
        return {'passed': not bookings(line), 'bookings': len(bookings(line)), 'after': again}


async def partial_read_back() -> dict[str, Any]:
    """N9: the caller cut into the read-back. A yes to what they did not hear must book nothing."""
    async with Line(diary=diary()) as line:
        await to_offer(line)
        line.playout.playback = 'none'
        await line.turn('Nine, please.')
        line.playout.playback = 'full'
        await line.worker.queue_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await asyncio.sleep(0.05)
        await line.worker.queue_frame(InterruptionFrame())
        await asyncio.sleep(0.1)
        again = await line.turn('Yes.', nine_on_tuesday(line))
        return {'passed': not bookings(line), 'bookings': len(bookings(line)), 'after': again}


async def cut_receipt() -> dict[str, Any]:
    """N8: the caller cuts into the receipt; the booking stands, once, and the next yes does not repeat it."""
    async with Line(diary=diary()) as line:
        await to_read_back(line)
        line.playout.playback = 'none'
        await line.turn('Yes.')
        line.playout.playback = 'full'
        await line.worker.queue_frame(InterruptionFrame())
        await asyncio.sleep(0.1)
        after = await line.turn(
            'Sorry, is it booked?', Reply(text='Yes, you are booked for Tuesday at nine.')
        )
        return {'passed': len(bookings(line)) == 1, 'bookings': len(bookings(line)), 'after': after}


async def empty_output() -> dict[str, Any]:
    """N6: the model answers with nothing, twice. Bounded: one repair, then a fixed line, never silence."""
    async with Line(diary=diary()) as line:
        await to_offer(line)
        turn = await line.turn('What else do you have?', Reply(), Reply(), Reply())
        return {
            'passed': bool(turn['heard']) and not bookings(line) and turn['requests'] <= TURN_REQUESTS,
            'heard': turn['heard'],
            'requests': turn['requests'],
            'agent_requests': turn['agent_requests'],
        }


async def bad_slot() -> dict[str, Any]:
    """N1: the model names a slot that does not exist. Nothing is read back, nothing is booked."""
    async with Line(diary=diary()) as line:
        await to_offer(line)
        bad = Reply(
            calls=[
                ('ask_booking', {'action': 'book', 'slot_id': '2026-09-22T0915|haircut', 'booking_id': ''})
            ]
        )
        turn = await line.turn(
            'Quarter past nine.', bad, Reply(calls=[('booking_guidance', {'topic': 'time'})])
        )
        return {
            'passed': not bookings(line) and 'Shall I book' not in turn['heard'],
            'heard': turn['heard'],
            'node': turn['node'],
        }


async def two_calls() -> dict[str, Any]:
    """N11: two calls at once on the one set of agents: each books its own, neither sees the other's."""

    async def one(pick: str, time_: str) -> tuple[int, str]:
        async with Line(diary=diary()) as line:
            await to_offer(line)
            await line.turn(pick)
            await line.turn('Yes.')
            return len(bookings(line)), bookings(line)[0]['time'] if bookings(line) else ''

    first, second = await asyncio.gather(one('Nine, please.', '09:00'), one('Ten, please.', '10:00'))
    return {'passed': first == (1, '09:00') and second == (1, '10:00'), 'calls': [first, second]}


async def barge_during_entry_line() -> dict[str, Any]:
    """N7: the caller cuts into the line a step speaks on entry; their next words are still taken and routed."""
    async with Line(diary=diary()) as line:
        line.playout.playback = 'none'
        await line.turn('I want a haircut on Tuesday.', Reply(calls=[('hand_over', WANTS)]))
        line.playout.playback = 'full'
        await line.worker.queue_frame(InterruptionFrame())
        await asyncio.sleep(0.1)
        turn = await line.turn('+34 600 111 222', Reply(calls=[('ask_number', {'phone': PHONE})]))
        return {
            'passed': turn['node'] == 'verify' and 'read that back' in turn['heard'],
            'node': turn['node'],
            'heard': turn['heard'],
        }


async def provider_messages_in_context() -> dict[str, Any]:
    """N10: provider-specific parts and a call a cancellation orphaned, in the history: no crash, no orphan sent."""
    async with Line(diary=diary()) as line:
        line.call.messages.extend(
            [
                ModelRequest(parts=[UserPromptPart('Hello.')]),
                ModelResponse(
                    parts=[
                        ThinkingPart('', signature='c2lnbmF0dXJl', provider_name='google-vertex'),
                        ToolCallPart('booking_guidance', {'topic': 'day'}, tool_call_id='kept'),
                        ToolCallPart('booking_guidance', {'topic': 'day'}, tool_call_id='orphan'),
                    ],
                    provider_name='google-vertex',
                ),
                ModelRequest(parts=[ToolReturnPart('booking_guidance', 'Which day?', tool_call_id='kept')]),
                ModelResponse(parts=[TextPart('Which day would you like?')]),
            ]
        )
        sent = paired(line.call.messages)
        ids = [p.tool_call_id for m in sent for p in m.parts if isinstance(p, ToolCallPart)]
        turn = await line.turn('I want a haircut on Tuesday.', Reply(calls=[('hand_over', WANTS)]))
        return {
            'passed': ids == ['kept'] and turn['node'] == 'verify',
            'sent_calls': ids,
            'node': turn['node'],
        }


async def timing(delay: float = 0.0) -> dict[str, Any]:
    """N12: the booking trace, timed turn by turn, with every scripted model answering after `delay` seconds."""
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
