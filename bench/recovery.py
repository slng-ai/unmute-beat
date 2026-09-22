"""Explicit regression suite: python -m bench.recovery --repeats 3 --label fixed.

Real model, no audio. Each scenario starts from an identical in-memory fixture;
JSONL retains every utterance, answer, request count and exact diary outcome.
"""

import argparse
import asyncio
import hashlib
import json
import pathlib
import time
from datetime import UTC, datetime

from pydantic_ai import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.messages import ModelMessagesTypeAdapter

from bench.scenarios import CLOCK, PHONE, Counter
from salon import diary
from salon.agents import AGENTS
from salon.state import open_call
from utils.arm import outcome, settings

SCENARIOS = {
    'select_01': ('01:00, please.',),
    'select_pm': ('1:00 PM',),
    'select_word': ('one, please.',),
    'move_same_time': ('Can you move my haircut to Saturday, the same— time?',),
    'complaint_verify': (
        'My haircut was completely uneven.',
        'My number is +34 600 111 222.',
        'Absolutely.',
        'It was yesterday.',
        'I would like a free redo, please.',
        'Yes, that is right.',
    ),
    'complaint_verified': (
        'My haircut yesterday was completely uneven. Can I get it redone?',
        'Yes, a free redo please.',
        'Yes, that is right.',
    ),
    'no_thanks': ('No, thanks.',),
    'goodbye_with_a_request': ('Thanks, but can you tell me when you close on Friday?',),
}


def fixture(name):
    diary.reset()
    diary.set_clock(CLOCK)
    call = open_call('' if name == 'complaint_verify' else PHONE)
    if name != 'complaint_verify':
        call.commit(customer_verified='existing', verified_phone=PHONE)
    if name.startswith('select_') or name == 'move_same_time':
        found = diary.find_slots(PHONE, 'Friday', 'haircut', diary.now(), offered_to=call.id)
        slots = found['slots']
        if name == 'move_same_time':
            slot = next(s for s in slots if s['time'] == '09:00')
            diary.save_booking(PHONE, 'book', slot['slot_id'], '', False, diary.now(), offered_to=call.id)
            answer = 'Your haircut is booked for Friday at nine AM. Anything else?'
        else:
            call.commit(agent='booking')
            answer = 'For your haircut on Friday, I have 1 PM available. Would you like that time?'
        call.messages = [
            ModelRequest(parts=[UserPromptPart(content='I want a haircut on Friday.')]),
            ModelResponse(
                parts=[ToolCallPart('read_diary', {'when': 'Friday', 'service': 'haircut'}, 'fixture-read')]
            ),
            ModelRequest(parts=[ToolReturnPart('read_diary', found, 'fixture-read')]),
            ModelResponse(parts=[TextPart(content=answer)]),
        ]
    elif name.startswith('complaint_'):
        call.commit(agent='concierge' if name == 'complaint_verify' else 'complaints')
    else:
        call.messages = [ModelResponse(parts=[TextPart(content='Anything else I can help with?')])]
    return call


def judge(name, call, rows, ended):
    actual = outcome(PHONE)
    bookings, complaints = actual['bookings'], actual['complaints']
    if any(r['status'] != 'answered' for r in rows):
        return 'a turn did not complete'
    if name.startswith('select_'):
        if len(bookings) != 1 or bookings[0]['time'] != '13:00' or call.state.writes != 1:
            return 'did not book the selected 13:00 slot'
        if rows[0]['requests'] != 1:
            return 'selection required another model request'
    elif name == 'move_same_time':
        if len(bookings) != 1 or bookings[0]['date'] != '2026-09-26' or bookings[0]['time'] != '09:00':
            return 'did not move the existing booking to Saturday at 09:00'
    elif name.startswith('complaint_'):
        if len(complaints) != 1 or call.state.writes != 1:
            return 'complaint was not recorded exactly once'
        receipts = [s for r in rows for s in r['speech'] if complaints[0]['reference'] in s['text']]
        if len(receipts) != 1:
            return 'complaint receipt missing or repeated'
        if bookings:
            return 'complaint changed the diary'
    elif name == 'no_thanks' and not ended:
        return 'ordinary goodbye did not close the call'
    elif name == 'goodbye_with_a_request' and ended:
        return 'hung up on an outstanding question'
    return 'ok'


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--label', default='recovery')
    parser.add_argument('--scenario', action='append', choices=SCENARIOS)
    args = parser.parse_args()
    path = pathlib.Path('bench/raw') / f'{args.label}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    failed = 0
    with path.open('w') as file:

        def write(row):
            file.write(json.dumps(row) + '\n')
            file.flush()

        write(
            {
                'arm': args.label,
                'settings': settings() | {'fixture': 'fresh', 'salon_clock': CLOCK.isoformat()},
                'suite': 'recovery',
                'suite_sha256': hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
                'audio': False,
            }
        )
        for repeat in range(args.repeats):
            for name in args.scenario or SCENARIOS:
                call = fixture(name)
                processor = Counter(call, AGENTS)
                rows = []
                for turn, words in enumerate(SCENARIOS[name]):
                    start = time.perf_counter()
                    await processor._turn(words)
                    row = {
                        'scenario': name,
                        'repeat': repeat,
                        'turn': turn,
                        'said': words,
                        'status': processor._finished.status if processor._finished else 'missing',
                        'requests': processor.requests(),
                        'seconds': time.perf_counter() - start,
                        'speech': [{'text': s.text, 'kind': s.kind} for s in call.spoken],
                    }
                    rows.append(row)
                    write(row)
                result = judge(name, call, rows, processor.ended)
                failed += result != 'ok'
                write(
                    {
                        'scenario': name,
                        'repeat': repeat,
                        'result': result,
                        'outcome': outcome(PHONE),
                        'failed_protocol': ModelMessagesTypeAdapter.dump_python(call._protocol, mode='json')
                        if result != 'ok'
                        else None,
                    }
                )
                print(f'{repeat + 1} {name}: {result}', flush=True)
    print(f'{failed} failures; evidence: {path}')
    return bool(failed)


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
