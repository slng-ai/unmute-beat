"""What `collect` now spends making the offers that no longer stand: the salon's two choice functions, locally.

    uv run --directory use-cases/salon-graph python ../../docs/research/reusable-core/session-4g/offer_cost.py

`core.steps._offer` for `slot` (find_times: one SQLite read and the day's grid) and `appointment`
(held_bookings), on a fresh diary on the fixed clock, each in its worker thread as on a call. Wall time per
call, 300 after 30 warm-up. This is tool/backend time on this machine, not model or network time.
"""

import asyncio
import os
import statistics
import sys
import time

import pydantic_ai.models

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
os.environ.setdefault('GOOGLE_API_KEY', 'offline-test-placeholder')
sys.path.insert(0, os.getcwd())

from core.steps import _offer  # noqa: E402
from tests.test_business import at_booking  # noqa: E402


async def main() -> None:
    _, flow, call, _ = await at_booking(service='haircut', booking_day='Friday', booking_time='09:00')
    choices = flow.by_id['booking'].choices
    samples = []
    for index in range(330):
        started = time.perf_counter()
        await _offer(call, choices, rendered=True)
        if index >= 30:
            samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    print(
        f'_offer, slot and appointment: median {statistics.median(samples):.2f} ms, '
        f'p90 {samples[int(len(samples) * 0.9)]:.2f} ms, max {samples[-1]:.2f} ms, n={len(samples)}, '
        f'load {os.getloadavg()[0]:.2f}'
    )


asyncio.run(main())
