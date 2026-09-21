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
import subprocess
import sys
import time
from collections import Counter as Tally
from datetime import UTC, date, datetime
from importlib.metadata import version

from dotenv import load_dotenv
from pipecat.frames.frames import LLMTextFrame, TTSSpeakFrame

load_dotenv(override=True)
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')

from salon import diary  # noqa: E402
from salon.state import open_call  # noqa: E402
from voice.harness import TurnBudget  # noqa: E402
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
    'manager': ('I want to speak to a manager',),
    'knowledge': (
        'how much is a hair colour?',
        'and do you have parking?',
        "no, that's all",
    ),
}


# Scenarios whose outcome cannot be compared across arms, and why. They still
# run -- how an arm handles a request it cannot serve is worth watching -- but
# a row from one of these is not evidence about the other arm's latency.
#
# `manager`: this agency has no manager transfer and the native one offers
# `to_manager`, so the two are answering different questions.
# `knowledge`: the salon's documents are in the prompt here and behind a
# retrieval tool there, so a faster knowledge turn measures the inlining and
# not the architecture.
NOT_COMPARABLE = frozenset({'manager', 'knowledge'})


def _held(service: str = '', weekday: str = '') -> str:
    """'' when this caller holds exactly the one booking described, else why not."""
    held = diary.bookings_for(PHONE, diary.now().date())
    if len(held) != 1:
        return f'{len(held)} bookings held, expected exactly 1'
    only = held[0]
    if service and only['service'] != service:
        return f'booked {only["service"]}, expected {service}'
    if weekday and (day := date.fromisoformat(only['date']).strftime('%A')) != weekday:
        return f'booked on {day}, expected {weekday}'
    return ''


def _nothing_written() -> str:
    """'' when the call wrote nothing at all, which is the right answer to some of these."""
    held = diary.bookings_for(PHONE, diary.now().date())
    if held or diary.complaints():
        return f'{len(held)} bookings and {len(diary.complaints())} complaints, expected none'
    return ''


def _one_complaint() -> str:
    logged = diary.complaints()
    if len(logged) != 1:
        return f'{len(logged)} complaints recorded, expected exactly 1'
    return '' if logged[0]['phone'] == PHONE else 'the complaint is under the wrong number'


# The judge, per scenario, and it reads the salon rather than the transcript.
# A row that says the turn answered says the agency spoke; only this says it
# did the thing. Every one of them returns '' for a pass.
CHECKS = {
    'book': lambda: _held('haircut'),
    'book_then_change_day': lambda: _held('haircut', 'Wednesday'),
    'change_after_booking': lambda: _held('haircut', 'Saturday'),
    'question_then_book': lambda: _held('blowout'),
    'complaint': _one_complaint,
    'manager': _nothing_written,
    'knowledge': _nothing_written,
}
assert set(CHECKS) == set(SCENARIOS), 'every scenario is judged, or the run cannot be scored'


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
        self.first_text = None

    async def push_frame(self, frame, direction=None):
        if isinstance(frame, LLMTextFrame):
            self.spoke = True
            self.first_text = self.first_text or time.perf_counter()
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


def _earliest(clock: float, counter: Counter, call) -> float | None:
    """When the caller first heard anything at all, model speech or a fixed line."""
    heard = [s.at for s in call.spoken]
    if counter.first_text:
        heard.append(counter.first_text)
    return round(min(heard) - clock, 3) if heard else None


async def run_scenario(name: str, said: tuple[str, ...], repeat: int, write) -> list[dict]:
    """One scenario, one row per caller turn, each written as it finishes.

    `write` is called with every row the moment the turn is over, so a run
    that crashes on turn four still leaves turns one to three on disk. They
    used to be held in memory until every scenario had finished, which is the
    one moment a benchmark can least afford to lose.
    """
    from salon.agents import AGENTS

    diary.reset()
    call = open_call(PHONE)
    counter = Counter(call, AGENTS)
    rows = []
    for index, turn in enumerate(said):
        counter.reset()
        started = time.monotonic()
        clock = time.perf_counter()
        row: dict[str, object] = {
            'scenario': name,
            'turn': index,
            'run': repeat,
            'said': turn,
            'at': datetime.now(UTC).isoformat(),
            'call': call.id,
            'comparable': name not in NOT_COMPARABLE,
        }
        try:
            await counter._turn(turn)
        except Exception as error:
            # Kept, not dropped. A turn that failed is a result, and a
            # benchmark that reports only the turns that worked is reporting
            # the wrong population.
            row |= {
                'status': 'crashed',
                'detail': f'{type(error).__name__}: {error}'[:200],
                'seconds': time.monotonic() - started,
            }
            rows.append(row)
            write(row)
            break
        done = counter._finished
        timed = [r for r in call.requests if r.ttft is not None]
        rows.append(
            row
            | {
                # How the turn ended, from the core's own verdict rather than
                # from the absence of an exception. An apologising turn spoke,
                # and it did not answer: `good` used to accept any row that
                # carried a request count, so a caller hearing "sorry, could
                # you say that again" scored as a completed turn.
                'status': done.status if done else 'unknown',
                'detail': done.detail if done else '',
                # The turn's own usage, reset by the processor at the top of each
                # turn, so this is that turn's requests and not a running total.
                'runs': counter.requests(),
                'seconds': time.monotonic() - started,
                # Per provider request, timed at the model boundary, with what
                # each one produced first. A tool call is not speech.
                'request_ttft': [r.ttft for r in timed],
                'request_first_output': [r.kind for r in timed],
                # Anything at all reaching the caller, model speech or a fixed
                # line. A goodbye goes out as a fixed line, so counting only
                # model text reported every hang-up turn as silence.
                'spoke': counter.spoke or counter.fixed > 0,
                'fixed_lines': counter.fixed,
                # Two numbers, not one. A turn that says "let me check"
                # quickly and answers slowly beats a turn that just answers,
                # on any benchmark that records only the first of these.
                'first_speech': _earliest(clock, counter, call),
                'first_model_word': round(counter.first_text - clock, 3) if counter.first_text else None,
                # What the caller was actually sent, in order. The fixed half
                # is nowhere in the model's history, so a transcript rebuilt
                # from history is missing every confirmation.
                'said_back': [[s.kind, s.text] for s in call.spoken],
                'step': call.state.agent,
                'hops': len(done.handoffs) + 1 if done else None,
            }
        )
        write(rows[-1])
        if counter.ended:
            break

    # The deterministic judge, asked once the conversation is over: what is in
    # the diary, not what the agent said about it. A scenario that spoke
    # beautifully and booked nothing has not passed.
    outcome: dict[str, object] = {
        'scenario': name,
        'run': repeat,
        'turn': None,
        'call': call.id,
        'at': datetime.now(UTC).isoformat(),
        'business': CHECKS[name]() or 'ok',
        'comparable': name not in NOT_COMPARABLE,
    }
    rows.append(outcome)
    write(outcome)
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


def settings() -> dict:
    """What this arm was, recorded beside its numbers.

    A latency difference between two arms means nothing without the two
    configurations it is a difference between, and "the repository at the
    time" is not a record anybody can pair a later run against. Unknown stays
    unknown: the fields this process cannot see are the ones that block a
    strict matched-configuration claim, and leaving them out is how that
    block goes unnoticed.
    """
    from salon import agents

    revision = subprocess.run(
        ['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, check=False
    ).stdout.strip()
    dirty = subprocess.run(
        ['git', 'status', '--porcelain'], capture_output=True, text=True, check=False
    ).stdout.strip()
    return {
        'revision': revision or 'unknown',
        'uncommitted_changes': bool(dirty),
        'python': sys.version.split()[0],
        'pydantic_ai': version('pydantic-ai-slim'),
        'model': str(agents.MODEL),
        'model_settings': str(agents.SETTINGS),
        'budget': str(TurnBudget()),  # what the processor is built with
        'speak_by': agents.SPEAK_BY,
        'timezone': str(diary.TZ),
        'clock': datetime.now(UTC).isoformat(),
        'seed_customers': sorted(diary._customers),
        # Not recorded here because this process cannot see them, and each one
        # is a dimension the two arms have to match on before a difference
        # between them means anything.
        'unknown': ['provider endpoint', 'region', 'stt/tts route', 'transport', 'audio settings'],
    }


async def main() -> None:
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    label = sys.argv[2] if len(sys.argv) > 2 else ('D-pinned' if os.getenv('BENCH_PINNED') else 'D')

    raw = pathlib.Path('bench/raw') / f'{label}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl'
    raw.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    with raw.open('w') as handle:
        # Every row hits the disk as its turn ends. A crash on the last
        # scenario of the last repeat used to take every row with it.
        def write(row: dict) -> None:
            handle.write(json.dumps(row) + '\n')
            handle.flush()

        write({'arm': label, 'settings': settings()})
        for run in range(repeats):
            for name, said in SCENARIOS.items():
                print(f'  run {run + 1}/{repeats}  {name}', file=sys.stderr)
                rows.extend(await run_scenario(name, said, run, write))
    print(f'  {len(rows) + 1} rows -> {raw}', file=sys.stderr)

    turns = [r for r in rows if r.get('turn') is not None]
    # Answered, and nothing else. An exhausted turn apologised, an interrupted
    # one was talked over, a crashed one never spoke: each of those is a
    # result and none of them is a turn that worked.
    good = [r for r in turns if r.get('status') == 'answered']
    other = [r for r in turns if r.get('status') != 'answered']
    scored = [r for r in rows if r.get('turn') is None and 'business' in r]
    failed_business = [r for r in scored if r['business'] != 'ok']
    runs = [float(r['runs']) for r in good]
    seconds = [r['seconds'] for r in good]
    silent = [r for r in good if not r['spoke']]

    print(
        f'\n=== configuration {label}: {repeats} x {len(SCENARIOS)} scenarios, {len(turns)} caller turns ==='
    )
    print(f'  turns that answered         {len(good)}/{len(turns)}')
    if other:
        by_status = Tally(r.get('status') for r in other)
        print(f'  turns that did not          {dict(by_status)}   <- not successes, and not dropped')
        for row in other[:5]:
            print(
                f'    {row["scenario"]} turn {row["turn"]}: {row.get("status")} {row.get("detail", "")[:80]}'
            )
    print(
        f'  scenarios that did the job  {len(scored) - len(failed_business)}/{len(scored)}   <- read off the diary, not the transcript'
    )
    for row in failed_business:
        print(f'    {row["scenario"]} run {row["run"]}: {row["business"]}')
    if not runs:
        print('  nothing answered, so there is nothing to average')
        return
    print(f'  model requests per turn     mean {statistics.fmean(runs):.2f}   {spread(runs)}')
    print(f'  seconds per turn            mean {statistics.fmean(seconds):.2f}   {spread(seconds)}')
    print(f'  turns that said nothing     {len(silent)}/{len(good)}   <- a caller sitting in silence')
    first = [t for r in good for t in r.get('request_ttft', [])]
    if first:
        kinds = Tally(k for r in good for k in r.get('request_first_output', []))
        print(f'  request time to first output {spread(first)}   {dict(kinds)}')

    print('\n  by turn, mean requests (the ledger is about the second turn of a booking):')
    for name in SCENARIOS:
        per = [r for r in good if r['scenario'] == name]
        by_turn: dict[int, list[float]] = {}
        for row in per:
            by_turn.setdefault(row['turn'], []).append(float(row['runs']))
        line = '  '.join(f'{turn}:{statistics.fmean(v):.1f}' for turn, v in sorted(by_turn.items()))
        marker = '' if name not in NOT_COMPARABLE else '   <- not comparable across arms'
        print(f'    {name:<22} {line}{marker}')
    print('\n  Paired runs are the point: these numbers mean something only beside a')
    print('  contemporaneous run of the other arm on the same scenarios and settings.')


if __name__ == '__main__':
    # Guarded, unlike the test modules, because this one makes real model
    # requests and writes files. Importing it -- which a linter, an editor or
    # a stray `import bench.scenarios` will do -- used to run the whole
    # benchmark.
    asyncio.run(main())
