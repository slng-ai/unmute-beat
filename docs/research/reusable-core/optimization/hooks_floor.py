"""What Pydantic AI charges per request for each `Hooks` capability on an agent, with no core at all.

    uv run python ../docs/research/reusable-core/optimization/hooks_floor.py   (from core/)

A bare agent on a scripted model streams one short reply, as a step agent's
request does, with 0, 1, 2, 5 and 8 `Hooks` capabilities attached, each
with one no-op `before_model_request` hook. The difference per capability
is the SDK's own dispatch, the cost core adds each time it attaches a
separate `Hooks` object instead of one.
"""

import asyncio
import statistics
import time

from pydantic_ai import Agent
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.models.function import AgentInfo, FunctionModel

REQUESTS = 400
ROUNDS = 7


async def reply(messages, info: AgentInfo):
    yield 'Two o clock is free.'


def hooks() -> Hooks:
    h = Hooks()

    @h.on.before_model_request
    async def unchanged(ctx, request_context):
        return request_context

    return h


async def per_request_us(count: int) -> float:
    agent = Agent(FunctionModel(stream_function=reply), capabilities=[hooks() for _ in range(count)])
    for _ in range(20):
        async with agent.run_stream_events('hello') as events:
            async for _ in events:
                pass
    started = time.perf_counter()
    for _ in range(REQUESTS):
        async with agent.run_stream_events('hello') as events:
            async for _ in events:
                pass
    return (time.perf_counter() - started) / REQUESTS * 1e6


async def main() -> None:
    counts = (0, 1, 2, 5, 8)
    runs = {c: [] for c in counts}
    for _ in range(ROUNDS):
        for c in counts:
            runs[c].append(await per_request_us(c))
    base = statistics.median(runs[0])
    for c in counts:
        median = statistics.median(runs[c])
        print(
            f'{c} Hooks: {median:7.1f} us per run (min {min(runs[c]):.1f}, max {max(runs[c]):.1f}), '
            f'+{median - base:6.1f} us over none'
        )


if __name__ == '__main__':
    asyncio.run(main())
