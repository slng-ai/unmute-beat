"""Core overhead on one scripted salon booking, the same on the parent and on the candidate: `python bench.py [runs]`.

Run from `use-cases/salon-graph` of either tree, with that tree's
environment, so each measures its own core. The scripted provider answers at
once, so every millisecond here is the harness: the turn engine, the
operation boundary, validation and history. Three caller turns: ask for a
Friday haircut (collect, options), pick two o'clock (the read-back), and
yes (the booking). No goodbye: its contract changed in stage B, so the
comparison stops before it.

Prints one JSON object: cold (process start to a compiled flow) and, per
turn, median wall and CPU milliseconds over the runs, model requests, and
the sizes of the last request's instructions and message history.
"""

import json
import os
import statistics
import subprocess
import sys
import time

os.environ.setdefault('GOOGLE_API_KEY', 'offline-bench')
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')


def cold() -> float:
    """Seconds from a fresh interpreter to the salon's compiled flow, the median of three."""
    code = (
        'import time; t = time.perf_counter(); '
        'import pydantic_ai.models; pydantic_ai.models.ALLOW_MODEL_REQUESTS = False; '
        'from salon.app import SPEC; from pydantic_ai.models.test import TestModel; '
        'SPEC.compile(model=TestModel()); print(time.perf_counter() - t)'
    )
    runs = [
        float(subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True).stdout)
        for _ in range(3)
    ]
    return statistics.median(runs)


def warm(runs: int) -> list[dict]:
    import asyncio

    import pydantic_ai.models
    from loguru import logger
    from pydantic_ai.messages import ModelMessagesTypeAdapter

    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    logger.remove()
    import core
    from tests.script import PHONE, opened, slot

    turns = (
        (
            'A haircut on Friday at two, please.',
            (
                ('collect', {'service': 'haircut', 'booking_day': 'Friday', 'booking_time': '14:00'}),
                ('call', 'options'),
                ('say', 'I have two in the afternoon. Shall I take it?'),
            ),
        ),
        ('That one.', (('collect', {'slot': slot('2026-10-02', '14:00')}), ('call', 'book_appointment'))),
        ('Yes, please.', (('call', 'book_appointment'),)),
    )
    samples: list[list[dict]] = [[] for _ in turns]

    async def once() -> None:
        script, flow, call, diary = opened(verified_phone=PHONE, customer_status='existing')
        call.commit(agent='booking')
        for index, (words, moves) in enumerate(turns):
            script.moves = list(moves)
            before = len(script.requests)
            wall, cpu = time.perf_counter(), time.process_time()
            reply = await core.text_turn(flow, call, words)
            wall, cpu = time.perf_counter() - wall, time.process_time() - cpu
            call.acknowledge(*(s.id for s in reply.segments))
            last = script.requests[-1]
            samples[index].append(
                {
                    'wall_ms': wall * 1000,
                    'cpu_ms': cpu * 1000,
                    'requests': len(script.requests) - before,
                    'instructions_bytes': len(last.instructions.encode()),
                    'history_bytes': len(ModelMessagesTypeAdapter.dump_json(last.messages)),
                }
            )
        assert diary.bookings_for(PHONE, diary.now().date()), 'the scripted booking did not book'

    async def all_runs() -> None:
        for _ in range(3):
            await once()
        for turn in samples:
            turn.clear()
        for _ in range(runs):
            await once()

    asyncio.run(all_runs())
    return [
        {
            'turn': index + 1,
            'wall_ms': round(statistics.median(s['wall_ms'] for s in turn), 2),
            'cpu_ms': round(statistics.median(s['cpu_ms'] for s in turn), 2),
            'requests': turn[0]['requests'],
            'instructions_bytes': turn[0]['instructions_bytes'],
            'history_bytes': turn[0]['history_bytes'],
        }
        for index, turn in enumerate(samples)
    ]


if __name__ == '__main__':
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    print(json.dumps({'cold_seconds': round(cold(), 3), 'turns': warm(count)}, indent=1))
