"""What history projection costs per request as a call grows, without a profiler.

    cd use-cases/salon-graph && uv run python ../../docs/research/reusable-core/optimization/projection.py

Runs the long salon call (`bench.py`'s `salon-long`) and, after each turn,
times `project` and `paired` over the call's history as the next request's
first projection sees it. The engine projects twice per request (once for
the run's history, once in `prepare`), so a request pays about twice this.
"""

import asyncio
import runpy
import statistics
import time
from pathlib import Path

from core.history import paired, project

bench = runpy.run_path(str(Path(__file__).with_name('bench.py')), run_name='bench')
TIMES = 200


def per_call_us(fn) -> float:
    runs = []
    for _ in range(5):
        started = time.perf_counter()
        for _ in range(TIMES):
            fn()
        runs.append((time.perf_counter() - started) / TIMES * 1e6)
    return statistics.median(runs)


async def main() -> None:
    workload = bench['WORKLOADS']['salon-long'](bench['Probe']())
    call = workload.open()
    rows = []
    for index, (words, moves) in enumerate(workload.turns(), start=1):
        await workload.turn(call, words, moves)
        if index in (1, 6, 12, 18, 24, 30):
            both = per_call_us(lambda: paired(project(call, call.messages)))
            alone = per_call_us(lambda: project(call, call.messages))
            rows.append((index, len(call.messages), len(call.all_spoken), alone, both))
    print('| turn | messages | spoken segments | project us | paired(project) us |')
    print('|---|---|---|---|---|')
    for turn, messages, spoken, alone, both in rows:
        print(f'| {turn} | {messages} | {spoken} | {alone:.0f} | {both:.0f} |')


if __name__ == '__main__':
    asyncio.run(main())
