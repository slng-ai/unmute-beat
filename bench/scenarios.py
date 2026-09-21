"""One scenario set, run identically, counting what a caller actually waits for.

Run: uv run --env-file .env python -m bench.scenarios [repeats] [label]
     BENCH_PINNED=1 uv run --env-file .env python -m bench.scenarios 3 D

Configurations A and B live in another package and are measured off Langfuse.
C and D are this harness, and here the harness is the instrument: the turn's
own shared usage object counts provider requests directly, so the number of
requests a caller waited through is read off the thing that made them. No
traces, no ingestion delay, no span tree to walk, and nothing to get wrong
about which observation belonged to which turn.

Two counting methods have already been wrong here. Frames: one response
envelope is one frame and can be several requests, so every tool turn was
undercounted. Then `ModelResponse` history items: closer, but a cancelled run
and a turn that resets history both lose responses that really were paid for,
and a turn is not the same length as the history it leaves behind. The usage
object is the provider's own count, shared across every run in the turn.

Every row is written to JSONL, failures included, because a summary printed
to a terminal is not evidence and a failed run that leaves no trace is a
result quietly dropped.

The scenarios are written once, here, and are the same ones the other two
configurations get driven with. Real model, no audio, no transport.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import pathlib
import statistics
import sys
import time
from datetime import UTC, datetime

from dotenv import load_dotenv
from pipecat.frames.frames import LLMTextFrame, TTSSpeakFrame

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

    def __init__(self, call, agents):
        super().__init__(call, agents)
        self.ended = False
        call._end = self._hang_up
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
        """Provider requests in the turn that just ran, off the turn's own tally.

        The processor threads one `RunUsage` through every run in a turn, so
        this is the provider's count of requests actually made -- not frames,
        which undercount a tool turn, and not `ModelResponse` history items,
        which a cancelled run or a history reset quietly loses.
        """
        return self._finished.requests if self._finished else 0


async def run_scenario(name: str, said: tuple[str, ...], repeat: int = 0) -> list[dict]:
    from salon.agents import AGENTS

    diary.reset()
    call = open_call(PHONE)
    counter = Counter(call, AGENTS)
    rows = []
    for index, turn in enumerate(said):
        counter.reset()
        started = time.monotonic()
        row = {'scenario': name, 'turn': index, 'run': repeat,
               'said': turn, 'at': datetime.now(UTC).isoformat()}
        try:
            await counter._turn(turn)
        except Exception as error:
            # Kept, not dropped. A turn that failed is a result, and a
            # benchmark that reports only the turns that worked is reporting
            # the wrong population.
            rows.append(row | {'error': f'{type(error).__name__}: {error}'[:200],
                               'seconds': time.monotonic() - started})
            break
        rows.append(row | {
            # The turn's own usage, reset by the processor at the top of each
            # turn, so this is that turn's requests and not a running total.
            'runs': counter.requests(),
            'seconds': time.monotonic() - started,
            # Anything at all reaching the caller, model speech or a fixed line.
            # A goodbye goes out as a fixed line, so counting only model text
            # reported every hang-up turn as silence.
            'spoke': counter.spoke or counter.fixed > 0,
            'fixed_lines': counter.fixed,
            'step': call.state.agent,
        })
        if counter.ended:
            break
    return rows


def spread(values: list[float]) -> str:
    """p50, p90 and max under one stated convention.

    Nearest rank, `ceil(p*n) - 1`, the same as `bench.turns` -- two figures in
    one write-up computed two ways cannot be compared, and the previous form
    here was neither nearest rank nor the same as the other module's.
    """
    if not values:
        return 'no data'
    ordered = sorted(values)
    p = lambda f: ordered[min(len(ordered) - 1, max(0, math.ceil(f * len(ordered)) - 1))]  # noqa: E731
    return f'p50 {p(0.5):.2f}  p90 {p(0.9):.2f}  max {ordered[-1]:.2f}  (nearest rank)'


async def main() -> None:
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    label = sys.argv[2] if len(sys.argv) > 2 else ('D-pinned' if os.getenv('BENCH_PINNED') else 'D')

    rows: list[dict] = []
    for run in range(repeats):
        for name, said in SCENARIOS.items():
            print(f'  run {run + 1}/{repeats}  {name}', file=sys.stderr)
            rows.extend(await run_scenario(name, said, repeat=run))

    # Written before anything is printed, so a crash in the summary cannot
    # lose a run that really happened.
    raw = pathlib.Path('bench/raw') / f'{label}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl'
    raw.parent.mkdir(parents=True, exist_ok=True)
    with raw.open('w') as handle:
        for row in rows:
            handle.write(json.dumps(row) + '\n')
    print(f'  {len(rows)} rows -> {raw}', file=sys.stderr)

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


if __name__ == '__main__':
    # Guarded, unlike the test modules, because this one makes real model
    # requests and writes files. Importing it -- which a linter, an editor or
    # a stray `import bench.scenarios` will do -- used to run the whole
    # benchmark.
    asyncio.run(main())
