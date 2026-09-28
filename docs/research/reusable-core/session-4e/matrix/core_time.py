"""Time core's own steps inside the N12 booking trace (harness arm), per run, wall time on perf_counter."""

import asyncio
import functools
import inspect
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.getcwd())
import pydantic_ai.models

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
import core.history  # noqa: E402
import core.operations  # noqa: E402
import core.state  # noqa: E402
import core.steps  # noqa: E402
import core.turn  # noqa: E402
from evals import matrix  # noqa: E402

spent: dict[str, list[float]] = defaultdict(list)


def wrap(owner, name, label):
    original = getattr(owner, name)
    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def timed(*args, **kwargs):
            started = time.perf_counter()
            try:
                return await original(*args, **kwargs)
            finally:
                spent[label].append((time.perf_counter() - started) * 1000)
    else:

        @functools.wraps(original)
        def timed(*args, **kwargs):
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                spent[label].append((time.perf_counter() - started) * 1000)

    setattr(owner, name, timed)


wrap(core.history, 'project', 'history.project')
wrap(core.turn, 'project', 'turn.project')
wrap(core.history, 'paired', 'history.paired')
wrap(core.steps.FlowCall, 'collect', 'FlowCall.collect')
wrap(core.steps.FlowCall, 'validated', 'FlowCall.validated')
wrap(core.state.CallContext, 'commit', 'CallContext.commit')
wrap(core.operations.Operation, 'run', 'Operation.run')
wrap(core.state.CallContext, 'speak_now', 'CallContext.speak_now')
wrap(core.state.CallContext, 'say', 'CallContext.say')
wrap(core.steps, '_offer', 'steps._offer')
wrap(core.steps, 'context', 'steps.context')
wrap(core.turn.Turn, '_speak', 'Turn._speak')

RUNS = 10


async def main():
    for _ in range(RUNS):
        await matrix.timing(0.0)


asyncio.run(main())
for name, values in sorted(spent.items(), key=lambda kv: -sum(kv[1])):
    print(
        f'{name:26} calls/run {len(values) / RUNS:6.1f}  ms/run {sum(values) / RUNS:7.2f}  max {max(values):6.2f}'
    )
