"""Does the model speak in the same response as a tool call? (RESEARCH R1)

Run: uv run --env-file .env python -m bench.preamble [runs] [model]

The architecture's "per-turn neutral" claim rests on one of two things, and they
are different claims:

  * the model emits a text preamble beside its tool call, so it covers its own
    gap and the caller hears something while the tool runs; or
  * it does not, and the gap is covered by the fixed `enter:` and filler lines
    this harness speaks -- which are ours, not the model's.

Nobody should have to guess which. This asks the real agents, on the real model,
with no audio and no runtime, and prints a number. It writes nothing and it is
not a test: it can only be answered by a live model, so it fails on a bad day
rather than asserting.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter

from dotenv import load_dotenv
from pydantic_ai import ModelResponse, TextPart
from pydantic_ai.messages import ToolCallPart

load_dotenv(override=True)
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')

from salon.agents import AGENTS  # noqa: E402
from salon.state import SalonCall, SalonState  # noqa: E402

# One turn per row, each one a turn that has to call something before it can
# answer. The interesting column is whether any text arrived with the call.
TURNS = (
    ('concierge', 'I need to book a haircut for Monday', {}),
    ('concierge', 'what time do you close on Saturday?', {}),
    (
        'booking',
        'can I get a haircut on Monday morning?',
        {'customer_verified': 'existing', 'customer_phone': '+34600111222'},
    ),
    (
        'complaints',
        'my colour came out completely wrong, can I get a refund?',
        {'customer_verified': 'existing', 'customer_phone': '+34600111222'},
    ),
)


async def one(step: str, said: str, facts: dict, model: str | None):
    """Run one turn and say what arrived in the response that carried the call."""
    node = AGENTS[step]
    call = SalonCall(state=SalonState(agent=step, **facts))
    override = node.agent.override(model=model) if model else None
    if override:
        override.__enter__()
    try:
        result = await node.agent.run(said, deps=call, output_type=node.outputs(call.state))
    finally:
        if override:
            override.__exit__(None, None, None)

    for message in result.all_messages():
        if not isinstance(message, ModelResponse):
            continue
        calls = [p for p in message.parts if isinstance(p, ToolCallPart)]
        if not calls:
            continue
        text = ''.join(p.content for p in message.parts if isinstance(p, TextPart)).strip()
        return calls[0].tool_name, text
    return '', ''


async def main() -> None:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    model = sys.argv[2] if len(sys.argv) > 2 else None
    tally: Counter[str] = Counter()
    samples: list[str] = []

    for _ in range(runs):
        for step, said, facts in TURNS:
            try:
                tool, text = await one(step, said, facts, model)
            except Exception as error:  # a live model, so a bad day is a result too
                tally['error'] += 1
                samples.append(f'  error   {step}: {error}')
                continue
            if not tool:
                tally['spoke, no call'] += 1
            elif text:
                tally['call WITH preamble'] += 1
                samples.append(f'  {step}/{tool}: {text!r}')
            else:
                tally['call, silent'] += 1

    total = sum(tally.values())
    print(f'\n=== {runs} runs x {len(TURNS)} turns, model={model or "the agency default"} ===')
    for label, count in tally.most_common():
        print(f'  {count:3d}/{total}  {label}')
    if samples:
        print('\npreambles and errors seen:')
        print('\n'.join(samples[:12]))
    covered = tally['call WITH preamble']
    calls = covered + tally['call, silent']
    print(
        f'\nR1: the model covered its own gap on {covered}/{calls} tool turns.'
        if calls
        else '\nR1: no tool turn happened; nothing to say.'
    )
    if calls and not covered:
        print('So the `enter:` and filler lines are load-bearing, not belt-and-braces.')


if __name__ == '__main__':
    # Guarded, unlike the test modules, because this one makes real model
    # requests and writes files. Importing it -- which a linter, an editor or
    # a stray `import bench.scenarios` will do -- used to run the whole
    # benchmark.
    asyncio.run(main())
