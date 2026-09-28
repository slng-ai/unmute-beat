"""Time every Diary method the N12 booking trace calls, in the harness arm, to see where a turn's time goes."""

import asyncio
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.getcwd())
import pydantic_ai.models

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
from evals import matrix  # noqa: E402
from salon.diary import Diary  # noqa: E402

spent: dict[str, list[float]] = defaultdict(list)
for name in [n for n in dir(Diary) if not n.startswith('__') and callable(getattr(Diary, n))]:
    original = getattr(Diary, name)

    def timed(*args, _original=original, _name=name, **kwargs):
        started = time.perf_counter()
        try:
            return _original(*args, **kwargs)
        finally:
            spent[_name].append((time.perf_counter() - started) * 1000)

    setattr(Diary, name, timed)


async def main():
    for _ in range(5):
        await matrix.timing(0.0)


asyncio.run(main())
for name, values in sorted(spent.items(), key=lambda kv: -sum(kv[1])):
    print(
        f'{name:28} calls/run {len(values) / 5:5.1f}  ms/run {sum(values) / 5:6.2f}  max {max(values):6.2f}'
    )
