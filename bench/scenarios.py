"""One scenario set, run identically, counting what a caller actually waits for.

Run: uv run --env-file .env python -m bench.scenarios [repeats] [label]
     BENCH_PINNED=1 uv run --env-file .env python -m bench.scenarios 3 D

Configurations A and B live in another package and are measured off Langfuse.
C and D are this harness, and here the harness is the instrument: the processor
pushes exactly one `LLMFullResponseStartFrame` per model run, so the number of
requests a caller waited through is a count of frames, not a query. No traces,
no ingestion delay, no span tree to walk, and nothing to get wrong about which
observation belonged to which turn.

The scenarios are written once, here, and are the same ones the other two
configurations get driven with. Real model, no audio, no transport.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time

from dotenv import load_dotenv
from pipecat.frames.frames import LLMTextFrame, TTSSpeakFrame
from pydantic_ai import ModelResponse

load_dotenv(override=True)
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')

from salon import diary  # noqa: E402
from salon.state import open_call  # noqa: E402
from voice.processor import PydanticAgentProcessor  # noqa: E402

PHONE = '+34600111222'

# One set, six shapes of call. `booking` is tagged on the turns the LLM-call
# ledger is actually about: the turn after the caller agrees to their number,
# where today a request is spent deciding where they go and nobody speaks.
SCENARIOS: dict[str, tuple[str, ...]] = {
    'book': (
        'can I book a haircut for Monday',
        "yes that's right",
        'the nine o clock one please',
        "no that's all, thanks",
    ),
    'book_then_change_day': (
        "I'd like a haircut next Friday",
        'yes, that is my number',
        'actually, make it Wednesday instead',
        'the first one',
        'no thanks, bye',
    ),
    # The shape the live trace of 2026-09-20 had, and the one the free edge is
    # for: the caller changes a booking *after* it is saved, so they are at the
    # front desk and already identified. That turn cost four requests on the
    # real call -- one of them the front desk saying "booking" and nothing else,
    # 3.37s, the worst turn in the call. Every other scenario here changes the
    # booking before it is saved, when the caller is already in the step, which
    # is why none of them exercised this.
    'change_after_booking': (
        'can I book a haircut for Friday',
        "yes that's right",
        'nine o clock please',
        'actually, can you move it to Saturday?',
        'same time',
        "that's all, thanks",
    ),
    'question_then_book': (
        'what time do you close on Saturday?',
        'great, can I book a blowout for tomorrow',
        "yes that's me",
        "whatever's earliest",
        "that's everything, bye",
    ),
    'complaint': (
        'my colour came out completely wrong last week',
        'I want a refund',
        'yes, that is my number',
        'yes please, write it down',
    ),
    'manager': (
        'I want to speak to a manager',
    ),
    'knowledge': (
        'how much is a hair colour?',
        'and do you have parking?',
        "no, that's all",
    ),
}


class Counter(PydanticAgentProcessor):
    """The processor with its downstream replaced by a tally."""

    def __init__(self, state, agents):
        super().__init__(state, agents)
        self.ended = False
        state._end = self._hang_up
        self.reset()

    async def _hang_up(self):
        self.ended = True

    def reset(self):
        self.spoke = False
        self.fixed = 0

    async def push_frame(self, frame, direction=None):
        if isinstance(frame, LLMTextFrame):
            self.spoke = True
        elif isinstance(frame, TTSSpeakFrame):
            self.fixed += 1

    def requests(self) -> int:
        """Model requests so far this call, counted off the agents' own history.

        Not response envelopes: one agent run that calls a tool and then answers
        is one envelope and **two** requests, and the ledger this benchmark
        exists to settle is denominated in requests. Counting frames undercounted
        every tool turn in the first draft of this script.
        """
        return sum(isinstance(m, ModelResponse) for m in self._state.messages)


async def run_scenario(name: str, said: tuple[str, ...]) -> list[dict]:
    from salon.agents import AGENTS

    diary.reset()
    state = open_call(PHONE)
    counter = Counter(state, AGENTS)
    rows = []
    for index, turn in enumerate(said):
        counter.reset()
        before = counter.requests()
        started = time.monotonic()
        try:
            await counter._turn(turn)
        except Exception as error:
            rows.append({'scenario': name, 'turn': index, 'error': str(error)[:120]})
            break
        rows.append({
            'scenario': name,
            'turn': index,
            'runs': counter.requests() - before,
            'seconds': time.monotonic() - started,
            # Anything at all reaching the caller, model speech or a fixed line.
            # A goodbye goes out as a fixed line, so counting only model text
            # reported every hang-up turn as silence.
            'spoke': counter.spoke or counter.fixed > 0,
            'step': state.agent,
        })
        if counter.ended:
            break
    return rows


def spread(values: list[float]) -> str:
    if not values:
        return '-'
    ordered = sorted(values)
    p = lambda f: ordered[min(len(ordered) - 1, max(0, round(f * len(ordered) + 0.5) - 1))]  # noqa: E731
    return f'p50 {p(0.5):.2f}  p90 {p(0.9):.2f}  max {ordered[-1]:.2f}'


async def main() -> None:
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    label = sys.argv[2] if len(sys.argv) > 2 else ('D-pinned' if os.getenv('BENCH_PINNED') else 'D')

    rows: list[dict] = []
    for run in range(repeats):
        for name, said in SCENARIOS.items():
            print(f'  run {run + 1}/{repeats}  {name}', file=sys.stderr)
            rows.extend(await run_scenario(name, said))

    good = [r for r in rows if 'runs' in r]
    errors = [r for r in rows if 'error' in r]
    runs = [float(r['runs']) for r in good]
    seconds = [r['seconds'] for r in good]
    silent = [r for r in good if not r['spoke']]

    print(f'\n=== configuration {label}: {repeats} x {len(SCENARIOS)} scenarios, '
          f'{len(good)} caller turns ===')
    print(f'  model requests per turn     mean {statistics.fmean(runs):.2f}   {spread(runs)}')
    print(f'  seconds per turn            mean {statistics.fmean(seconds):.2f}   {spread(seconds)}')
    print(f'  turns that said nothing     {len(silent)}/{len(good)}'
          '   <- a caller sitting in silence')
    if errors:
        print(f'  turns that failed           {len(errors)}')
        for row in errors[:5]:
            print(f'    {row["scenario"]} turn {row["turn"]}: {row["error"]}')

    print('\n  by turn, mean requests (the ledger is about the second turn of a booking):')
    for name in SCENARIOS:
        per = [r for r in good if r['scenario'] == name]
        by_turn = {}
        for row in per:
            by_turn.setdefault(row['turn'], []).append(float(row['runs']))
        line = '  '.join(f'{turn}:{statistics.fmean(v):.1f}' for turn, v in sorted(by_turn.items()))
        print(f'    {name:<22} {line}')


asyncio.run(main())
