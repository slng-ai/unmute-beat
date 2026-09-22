"""Replay the move approval three times: python -m bench.approval-evidence.replay.

A scripted first response fixes the exact pending question from the voice trace;
real Gemini processes the caller's approval. This is not microphone acceptance.
"""

import asyncio
import json

from bench.scenarios import Counter
from salon import agents, diary
from tests.test_consent import CLOCK, PHONE, _context, _heard, _salon, _slot, outputs, scripted
from utils.arm import settings


async def main():
    print(
        json.dumps(
            {
                'settings': settings(),
                'audio': False,
                'scenario_clock': CLOCK.isoformat(),
                'scenario_fixture': 'fresh per repeat',
            }
        ),
        flush=True,
    )
    for repeat in range(3):
        call = _salon()
        _heard(call)
        call.turn_started('a haircut on Friday at ten please')
        friday = _slot(call, 'friday', at='10:00')
        await agents.save_booking(_context(call), 'book', friday['slot_id'], '')
        held = diary.bookings_for(PHONE, CLOCK.date())[0]
        monday = _slot(call, 'next Monday', at='10:00')
        processor = Counter(call, agents.AGENTS)
        with scripted(
            booking=outputs(
                'ask_booking', action='move', slot_id=monday['slot_id'], booking_id=held['booking_id']
            )
        ):
            await processor._turn('Move it to next Monday at ten.')
        await processor._turn('Go for it.')
        after = diary.bookings_for(PHONE, CLOCK.date())
        ok = (
            len(after) == 1
            and after[0]['booking_id'] == held['booking_id']
            and (after[0]['date'], after[0]['time']) == ('2026-09-28', '10:00')
            and call.state.writes == 2
            and call._asked is None
            and processor._finished.status == 'answered'
        )
        print(
            json.dumps(
                {
                    'repeat': repeat,
                    'result': 'ok' if ok else 'failed',
                    'requests': processor.requests(),
                    'speech': [s.generated for s in call.spoken],
                    'bookings': after,
                }
            ),
            flush=True,
        )
        assert ok


if __name__ == '__main__':
    asyncio.run(main())
