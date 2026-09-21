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
import random
import statistics
import sys
import time
from collections import Counter as Tally
from datetime import UTC, datetime

from dotenv import load_dotenv
from pipecat.frames.frames import LLMTextFrame, TTSSpeakFrame

load_dotenv(override=True)
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')

from salon import diary  # noqa: E402
from salon.state import open_call  # noqa: E402
from utils.arm import settings  # noqa: E402
from voice.processor import PydanticAgentProcessor  # noqa: E402

PHONE = '+34600111222'

# One set, six shapes of call. `booking` is tagged on the turns the LLM-call
# ledger is actually about: the turn after the caller agrees to their number,
# where today a request is spent deciding where they go and nobody speaks.
SCENARIOS: dict[str, tuple[str, ...]] = {
    # The five caller utterances of the native reference call, trace
    # f1c4f999e516a7792dfdbed72fa8262d of 2026-09-21 09:15:54Z, word for word.
    # It is the only scenario here whose request counts can be put beside
    # another arm's, because it is the only one both arms have actually been
    # driven with -- every other entry measures this harness against itself.
    #
    # Two divergences are known in advance and are findings, not faults to
    # paper over: the reference identity is a US number this agency refuses,
    # so the seeded Spanish one stands in; and the reference was offered
    # 09:00, 11:30 and 15:00 where this diary offers a half-hour grid capped
    # at twelve, which on a Friday haircut stops at 14:30. What the caller
    # asking for three o'clock then gets is the thing to read in the row.
    'reference': (
        'Hey, I would like to book a haircut appointment on Friday.',
        "That's correct.",
        "3 o'clock in the afternoon.",
        'That would be lovely, yes.',
        "No, I'm good, thanks.",
    ),
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
    # Five turns, not four. The complaint needs an owner, so the flow is
    # complaints -> verify -> complaints before anything can be written down,
    # and a four-turn script ran out of caller before `record_complaint`
    # could fire. Every arm failed it for the same reason, which makes it a
    # scenario that measures nothing rather than a difference between them.
    'complaint': (
        'my colour came out completely wrong last week',
        'I want a refund',
        'yes, that is my number',
        'yes please, write it down',
        "that's right, go ahead",
        "no, that's everything",
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
# `reference`: the only conversation both arms have been driven with, and the
# fixtures underneath it are not the same. The native run was offered 15:00
# and this diary's twelve Friday haircut slots stop at 14:30, so the caller
# asking for three o'clock is answered differently before the model has done
# anything -- and availability changes tool work and requests, not only which
# slot is booked. The identity differs too. It stays in the suite and its
# requests are still printed beside the native ones, as a replay to read
# rather than a result to pool.
NOT_COMPARABLE = frozenset({'manager', 'knowledge', 'reference'})

# What the native arm spent on the `reference` conversation, one number per
# caller turn, counted off trace f1c4f999e516a7792dfdbed72fa8262d by
# `bench.turns`. Written down here so the comparison is against a measurement
# and not against a memory of one. Ten requests over five caller turns.
NATIVE_REFERENCE = (2, 3, 1, 2, 2)


# The salon's clock for a benchmark run: Monday 21 September 2026, 10:00 in
# Madrid. Pinned, because "Monday" and "tomorrow" resolve against it and an
# expected date cannot be written down against a clock that moves. Every date
# below is what these utterances mean at this instant.
CLOCK = datetime(2026, 9, 21, 10, 0, tzinfo=diary.TZ)

# What the salon must hold when each scenario is over. Not "a booking
# exists": the booking the caller asked for, on the day they named, at the
# time they chose, written on the turn they agreed to it.
#
# The check this replaces counted bookings and looked at the service, so one
# haircut on Friday at 10:30 passed a scenario that asked for Monday at 9:00
# -- the wrong appointment, for the right customer, scored as a pass. That is
# the failure a benchmark is least able to notice and most likely to reward
# with a good latency number, because doing the wrong thing is quicker.
#
# `wrote_on_turn` is the consent assertion. In `book`, turn 3 is the caller
# saying yes; a write that lands on turn 2 is a booking made on a choice of
# time, before anybody agreed to it.
EXPECTED: dict[str, dict] = {
    'reference': {
        'service': 'haircut',
        'date': '2026-09-25',
        # The caller asks for three o'clock. This diary offers 09:00 to 14:30
        # in half hours and nothing later, so the nearest thing to what they
        # asked for is 14:30 and that is what a correct answer books.
        #
        # This used to pin no time at all, on the grounds that the fixture
        # differs. It does -- but "no time" passes a run that books 09:00,
        # six hours from what the caller said, and a booking at the wrong
        # time is the failure this oracle exists to catch. The fixture
        # mismatch is recorded by keeping the scenario out of the comparable
        # population, not by declining to check it.
        'time': '14:30',
        'writes': 1,
        'wrote_on_turn': 3,
    },
    # Turn 2 is "the nine o clock one please", and this agency books on it:
    # `prompts/salon/booking.md` says to ask the confirming question *only*
    # when the details are not already agreed, and an unambiguous choice of
    # an offered time is agreement. The native arm reads the booking back and
    # waits for a separate yes.
    #
    # Both are defensible and they are not the same policy, which is why the
    # turn count of a booking is **not** comparable between the two arms
    # until one of them changes. Written down here rather than discovered in
    # a latency table: a policy that books a turn earlier finishes the call
    # a turn sooner, and that is a different product, not a faster one.
    'book': {'service': 'haircut', 'date': '2026-09-28', 'time': '09:00', 'writes': 1, 'wrote_on_turn': 2},
    'book_then_change_day': {
        'service': 'haircut',
        'date': '2026-09-23',
        'time': '09:00',
        'writes': 1,
        'wrote_on_turn': 3,  # "the first one", after a day correction
    },
    'change_after_booking': {
        'service': 'haircut',
        'date': '2026-09-26',
        'time': '09:00',
        'writes': 2,  # the booking, and then the move
        'wrote_on_turn': 2,
    },
    'question_then_book': {
        'service': 'blowout',
        'date': '2026-09-22',
        'time': '09:00',
        'writes': 1,
        'wrote_on_turn': 3,  # "whatever's earliest", which is a choice
    },
    # No turn pinned. The identity detour can come before or after the
    # caller agrees to the summary, so which turn the write lands on is the
    # model's sequencing rather than the contract. What the contract is about
    # -- one complaint, under this number, and nothing booked -- is covered
    # by the fields above it.
    'complaint': {'complaints': 1, 'writes': 1},
    'manager': {},
    'knowledge': {},
}
assert set(EXPECTED) == set(SCENARIOS), 'every scenario states its outcome, or it cannot be scored'
# A booking has a time, and the oracle has to say which one. `reference` left
# it out on the grounds that the fixture differs from the native arm's -- and
# then passed a run that booked 09:00 against a caller who asked for three in
# the afternoon. A fixture that does not match is recorded by keeping the
# scenario out of the comparable population, not by declining to check it.
assert all('time' in want for want in EXPECTED.values() if want.get('service')), (
    'a scenario expecting a booking must say at what time, or the wrong appointment passes'
)


def judge(name: str, call, timeline: list[dict]) -> str:
    """'' when the salon holds exactly what this scenario should have produced.

    Reads the diary and the call state, never the transcript: what the agent
    said it did is the thing under test.
    """
    want = EXPECTED[name]
    held = diary.bookings_for(PHONE, CLOCK.date())
    logged = [c for c in diary.complaints() if c['phone'] == PHONE]

    wanted_bookings = 1 if want.get('service') else 0
    if len(held) != wanted_bookings:
        return f'{len(held)} bookings, expected {wanted_bookings}'
    if len(logged) != want.get('complaints', 0):
        return f'{len(logged)} complaints, expected {want.get("complaints", 0)}'
    for field in ('service', 'date', 'time'):
        if held and field in want and held[0][field] != want[field]:
            return f'{field} is {held[0][field]}, expected {want[field]}'
    if call.state.writes != want.get('writes', 0):
        return f'{call.state.writes} accepted writes, expected {want.get("writes", 0)}'
    if call.state.unresolved:
        return 'the call ended with a write whose outcome was never established'

    # When the first write landed, which is the only thing here that tells a
    # booking made on a caller's yes from one made on their naming a time.
    wrote = next((row['turn'] for row in timeline if row.get('wrote')), None)
    if (expected := want.get('wrote_on_turn')) is not None and wrote != expected:
        return f'the first write landed on turn {wrote}, expected turn {expected}'
    return ''


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


def _out(clock: float, call, kind: str = '') -> float | None:
    """When text of this kind was first handed to TTS, relative to the turn's start.

    Handed to TTS, not heard: playout is a boundary this process cannot see,
    and the row's name says so. `kind` empty means the first of any kind --
    a filler going out quickly is not an answer going out quickly.
    """
    out = [s.at for s in call.spoken if not kind or s.kind == kind]
    return round(min(out) - clock, 3) if out else None


async def run_scenario(name: str, said: tuple[str, ...], repeat: int, write) -> list[dict]:
    """One scenario, one row per caller turn, each written as it finishes.

    `write` is called with every row the moment the turn is over, so a run
    that crashes on turn four still leaves turns one to three on disk. They
    used to be held in memory until every scenario had finished, which is the
    one moment a benchmark can least afford to lose.
    """
    from salon.agents import AGENTS

    diary.reset()
    diary.set_clock(CLOCK)
    call = open_call(PHONE)
    counter = Counter(call, AGENTS)
    rows = []
    for index, turn in enumerate(said):
        counter.reset()
        started = time.monotonic()
        clock = time.perf_counter()
        written_before = call.state.writes
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
                'first_text_out': _out(clock, call),
                'first_model_text_out': _out(clock, call, 'model'),
                # What the caller was actually sent, in order. The fixed half
                # is nowhere in the model's history, so a transcript rebuilt
                # from history is missing every confirmation.
                # Both halves, in order. The model's own answer was missing
                # entirely, so a turn where it spoke read back as a turn that
                # said nothing -- and nothing in the row could show whether
                # consent had been asked for.
                'said_back': [[s.kind, s.text] for s in call.spoken],
                'step': call.state.agent,
                'hops': len(done.handoffs) + 1 if done else None,
                # Whether this turn is the one that wrote. The row that says
                # a booking exists cannot say the caller had agreed to it;
                # the turn it landed on can.
                'wrote': call.state.writes > written_before,
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
        'business': judge(name, call, rows) or 'ok',
        'comparable': name not in NOT_COMPARABLE,
    }
    rows.append(outcome)
    write(outcome)
    return rows


def confidence(calls: list[list[float]], fraction: float = 0.9, draws: int = 2000) -> tuple[float, float]:
    """A 95% interval for a quantile, resampled over calls rather than turns.

    The turns of one call are not independent of each other -- one slow
    handoff, one caller who has to repeat themselves, and every later turn of
    that call carries it. Resampling turns treats them as if they were, which
    narrows the interval to a width nobody measured. So the unit of resampling
    is the call, which is also the unit two arms get paired on.
    """
    random.seed(0)
    got = []
    for _ in range(draws):
        picked = [random.choice(calls) for _ in calls]
        got.append(_quantile([s for call in picked for s in call], fraction))
    return _quantile(got, 0.025), _quantile(got, 0.975)


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]


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
    scored = [r for r in rows if r.get('turn') is None and 'business' in r]
    failed_business = [r for r in scored if r['business'] != 'ok']
    # Answered, and nothing else. An exhausted turn apologised, an interrupted
    # one was talked over, a crashed one never spoke: each of those is a
    # result and none of them is a turn that worked.
    replied = [r for r in turns if r.get('status') == 'answered']
    other = [r for r in turns if r.get('status') != 'answered']
    # Which calls may be pooled into a headline: a scenario both arms can be
    # driven with, on a run where the salon ended up holding the right thing.
    # Turns from a call that booked the wrong appointment are not evidence
    # about latency -- doing the wrong thing is quicker -- and `manager` and
    # `knowledge` are not evidence about this comparison at all. Everything
    # is still reported, and every row is still on disk; what is partitioned
    # is the number a decision would be taken on.
    comparable = {
        (r['scenario'], r['run'])
        for r in scored
        if r['business'] == 'ok' and r['scenario'] not in NOT_COMPARABLE
    }
    good = [r for r in replied if (r['scenario'], r['run']) in comparable]
    runs = [float(r['runs']) for r in good]
    seconds = [r['seconds'] for r in good]
    silent = [r for r in good if not r['spoke']]

    print(
        f'\n=== configuration {label}: {repeats} x {len(SCENARIOS)} scenarios, {len(turns)} caller turns ==='
    )
    print(f'  turns that answered         {len(replied)}/{len(turns)}')
    if other:
        by_status = Tally(r.get('status') for r in other)
        print(f'  turns that did not          {dict(by_status)}   <- not successes, and not dropped')
        for row in other[:5]:
            print(
                f'    {row["scenario"]} turn {row["turn"]}: {row.get("status")} {row.get("detail", "")[:80]}'
            )
    print(
        f'  scenarios that did the job  {len(scored) - len(failed_business)}/{len(scored)}   '
        '<- the exact expected booking, off the diary, not the transcript'
    )
    for row in failed_business:
        print(f'    {row["scenario"]} run {row["run"]}: {row["business"]}')
    print(
        f'  turns in the headline       {len(good)}/{len(turns)}   '
        '<- comparable scenarios, on the runs that did the job'
    )
    if not runs:
        print('  nothing answered, so there is nothing to average')
        return
    print(f'  model requests per turn     mean {statistics.fmean(runs):.2f}   {spread(runs)}')
    print(f'  seconds per turn            mean {statistics.fmean(seconds):.2f}   {spread(seconds)}')
    print(f'  turns that said nothing     {len(silent)}/{len(good)}   <- a caller sitting in silence')
    by_call: dict[tuple, list[float]] = {}
    for row in good:
        by_call.setdefault((row['scenario'], row['run']), []).append(row['seconds'])
    if len(by_call) > 1:
        low, high = confidence(list(by_call.values()))
        print(
            f'  p90 seconds/turn, 95% CI    [{low:.2f}, {high:.2f}] over {len(by_call)} calls   '
            "<- resampled over calls, because a call's turns are correlated"
        )
    first = [t for r in good for t in r.get('request_ttft', [])]
    if first:
        kinds = Tally(k for r in good for k in r.get('request_first_output', []))
        print(f'  request time to first output {spread(first)}   {dict(kinds)}')
    out = [r['first_text_out'] for r in good if r.get('first_text_out') is not None]
    answer = [r['first_model_text_out'] for r in good if r.get('first_model_text_out') is not None]
    if out:
        print(f'  first text out              {spread(out)}   <- any of it, filler included')
    if answer:
        print(f'  first model text out        {spread(answer)}   <- the answer, not the filler')
    print('                              both are text handed to TTS, not audio the caller heard')

    print('\n  by turn, mean requests (the ledger is about the second turn of a booking):')
    for name in SCENARIOS:
        # Every answered turn here, headline or not: the table is describing
        # what each scenario cost, and a scenario left out of the aggregate
        # still has to be readable.
        per = [r for r in replied if r['scenario'] == name]
        by_turn: dict[int, list[float]] = {}
        for row in per:
            by_turn.setdefault(row['turn'], []).append(float(row['runs']))
        line = '  '.join(f'{turn}:{statistics.fmean(v):.1f}' for turn, v in sorted(by_turn.items()))
        marker = '' if name not in NOT_COMPARABLE else '   <- not comparable across arms'
        print(f'    {name:<22} {line}{marker}')
    # Its own selection, because `reference` is deliberately out of the
    # comparable population and `good` therefore does not contain it. Runs
    # that did the job, so a replay that booked the wrong time is not
    # averaged into what the conversation costs.
    did_the_job = {r['run'] for r in scored if r['scenario'] == 'reference' and r['business'] == 'ok'}
    replayed = [r for r in replied if r['scenario'] == 'reference' and r['run'] in did_the_job]
    ours = [
        statistics.fmean([float(r['runs']) for r in replayed if r['turn'] == n])
        for n in range(len(SCENARIOS['reference']))
        if any(r['turn'] == n for r in replayed)
    ]
    if len(ours) == len(NATIVE_REFERENCE):
        print('\n  the reference conversation, the same five utterances, requests per turn:')
        print(f'    native f1c4f999   {list(NATIVE_REFERENCE)}   total {sum(NATIVE_REFERENCE)}')
        print(f'    this harness      {[round(n, 1) for n in ours]}   total {sum(ours):.1f}')
        print('    NOT a comparison. The two arms were offered different times for the slot the')
        print('    caller asks for and identified the caller differently, and availability')
        print('    changes how many requests a turn takes. Read it as a replay until the')
        print('    fixtures, the consent policy and the identity match on both sides.')

    print('\n  Paired runs are the point: these numbers mean something only beside a')
    print('  contemporaneous run of the other arm on the same scenarios and settings.')


if __name__ == '__main__':
    # Guarded, unlike the test modules, because this one makes real model
    # requests and writes files. Importing it -- which a linter, an editor or
    # a stray `import bench.scenarios` will do -- used to run the whole
    # benchmark.
    asyncio.run(main())
