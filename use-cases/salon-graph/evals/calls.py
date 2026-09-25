"""The live text checkpoint: four scripted salon calls on the real engine and the real models.

    uv run --env-file ../../.env python -m evals.calls              # T1 T2 T3 T4, billable
    uv run --env-file ../../.env python -m evals.calls T2 --out /tmp/calls

Each case is one call through `core.turn.run_turn` with the salon's real
`AGENTS`, a fresh `Diary` on a fixed clock, and no audio. The caller is a
script that reads only what was delivered to it: each rule names words to
listen for in the agent's last reply and what to say back. A reply no rule
matches stops the case as `off-script`: the script did not expect it, which
is a finding, not a pass. No caller model is used.

Nothing the models say decides a case. Its check reads the diary, the
evaluator's own record of every diary call, and the `write` spans core leaves
for every attempted write.

Delivery is acknowledged explicitly. After each turn the evaluator hands the
turn's new speech segments to the text caller and acknowledges each by id
(`CallContext.acknowledge`, the text runtime's playback signal), and records
which segment ids that marked delivered. A returned string is never treated
as heard.

Limits, from the Verification contract (`docs/plans/reusable-voice-core-sessions.md`):
12 caller turns per case, 100 target model requests and 250,000 tokens across
the four, one attempt each, no retries. A limit hit stops the case as
`exhausted`, which is not a pass. The limits are checked between turns, so one
turn can overshoot by at most its own `TurnBudget` (8 requests).

ponytail: requests and tokens are counted from the model spans Pydantic AI
already emits, collected in memory. A request whose span never ended, such as
one cancelled mid-stream, is not counted; the Langfuse session holds the full
record.
"""

import argparse
import asyncio
import json
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider

from core.observability import setup
from core.policies import TurnBudget
from core.turn import FixedSpeech, TextDelta, TurnFinished, greet, receive, run_turn
from salon.agents import AGENTS, GREETING
from salon.diary import TZ, Diary
from salon.models.state import SalonCall, open_call

CLOCK = datetime(2026, 9, 28, 8, 30, tzinfo=TZ)
"""Monday 28 September 2026, before opening: Tuesday is the 29th, Wednesday the 30th, Friday 2 October."""
TODAY: date = CLOCK.date()
PHONE = '+34600111222'
"""The diary's seed customer."""
SAID_PHONE = 'My number is plus three four, six zero zero, one one one, two two two.'
MAX_TURNS = 12
MAX_REQUESTS = 100
MAX_TOKENS = 250_000
CLAIMS = ("you're all set", 'is booked', "that's booked", 'booked for', 'booked you', 'is confirmed')
"""Words that tell the caller a booking exists, checked against what the call had proven when it said them.

ponytail: an English list, for the evaluator's own cross-check. The business
check is the diary; this only flags speech that ran ahead of the evidence.
"""


@dataclass(frozen=True)
class Rule:
    """What the caller says when they hear any of `hear` in the agent's last reply.

    `say` is used in order, one entry per match, the last one repeated.
    `last` marks the caller's goodbye: the case ends after the agent answers it.
    """

    hear: tuple[str, ...]
    say: tuple[str, ...]
    last: bool = False


@dataclass
class Record:
    """Everything one case produced, saved as JSON. Times are wall-clock seconds from the case's start."""

    case: str
    session: str
    turns: list[dict[str, Any]] = field(default_factory=list)
    diary_calls: list[dict[str, Any]] = field(default_factory=list)
    writes: list[dict[str, Any]] = field(default_factory=list)
    final: dict[str, Any] = field(default_factory=dict)
    requests: int = 0
    tokens: dict[str, int] = field(default_factory=lambda: {'input': 0, 'output': 0})
    trace_ids: list[str] = field(default_factory=list)
    result: str = ''
    problems: list[str] = field(default_factory=list)

    def bookings(self) -> list[dict]:
        return self.final.get('bookings', [])

    def calls_to(self, name: str) -> list[dict[str, Any]]:
        return [c for c in self.diary_calls if c['name'] == name]


@dataclass(frozen=True)
class Case:
    """One scripted call and the business check that decides it."""

    id: str
    version: int
    goal: str
    opening: str
    rules: tuple[Rule, ...]
    check: Callable[[Record], list[str]]
    prepare: Callable[[Diary], None] = lambda diary: None
    """A fault put into this case's diary before the call starts."""

    @property
    def name(self) -> str:
        return f'{self.id}.v{self.version}'


class Spans(SpanProcessor):
    """Every span that ends, kept in memory: the model requests, their tokens and the `write` audits."""

    def __init__(self) -> None:
        self.ended: list[ReadableSpan] = []

    def on_end(self, span: ReadableSpan) -> None:
        self.ended.append(span)

    def take(self) -> list[ReadableSpan]:
        taken, self.ended = self.ended, []
        return taken


def spans() -> Spans:
    """The collector, on whichever tracer provider this process exports to (Langfuse's, or a local one).

    `setup` goes first, so Langfuse's provider exists before this looks. In
    the session 1 checkpoint it went second: this installed a local provider,
    OpenTelemetry refused Langfuse's, and no text call reached Langfuse.
    """
    setup()
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    collector = Spans()
    provider.add_span_processor(collector)
    return collector


def attributes(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def is_request(span: ReadableSpan) -> bool:
    """A provider request: Pydantic AI's `chat` span, one per request, agent step or classifier alike."""
    return attributes(span).get('gen_ai.operation.name') == 'chat'


def union(intervals: list[tuple[float, float]]) -> float:
    """Seconds covered by at least one interval: readings and a step's request often run side by side."""
    total, end = 0.0, float('-inf')
    for start, stop in sorted(intervals):
        if stop > end:
            total += stop - max(start, end)
            end = stop
    return total


def watch(diary: Diary, record: Record, start: float) -> None:
    """Time every diary call on this instance and write it down: the backend's share of each turn.

    Only the diary's own call can raise. Recording what it answered never
    does: the first live run of this checkpoint read a key the answer did not
    have, and the evaluator broke the diary it was meant to watch.
    """
    for name in ('find_slots', 'save_booking', 'outcome_of', 'bookings_for', 'save_complaint'):
        real = getattr(diary, name, None)
        if real is None:
            continue

        def timed(*args, _real=real, _name=name, **kwargs):
            entry: dict[str, Any] = {'name': _name, 'start': time.time() - start}
            record.diary_calls.append(entry)
            try:
                result = _real(*args, **kwargs)
            except Exception as error:
                entry['error'] = f'{type(error).__name__}: {error}'
                raise
            finally:
                entry['end'] = time.time() - start
            entry['returned'] = result is not None
            if isinstance(result, dict) and isinstance(result.get('slots'), list):
                entry['offered'] = [
                    (result.get('date'), s.get('time')) for s in result['slots'] if isinstance(s, dict)
                ]
            return result

        setattr(diary, name, timed)


def reply(rules: tuple[Rule, ...], heard: str, used: dict[int, int]) -> tuple[str, bool] | None:
    """The caller's next words to what they last heard, or None if the script has nothing for it."""
    text = heard.lower()
    for index, rule in enumerate(rules):
        if any(words in text for words in rule.hear):
            count = used.get(index, 0)
            used[index] = count + 1
            return rule.say[min(count, len(rule.say) - 1)], rule.last
    return None


def acknowledge(call: SalonCall) -> list[dict[str, Any]]:
    """Hand this turn's new speech to the text caller, and say which segment ids that marked heard in full."""
    before = {s.id: s.playback for s in call.spoken}
    receive(call)
    return [
        {
            'id': s.id,
            'origin': s.origin,
            'text': s.text,
            'before': before.get(s.id),
            'after': s.playback,
            'interrupted': s.interrupted,
            'delivered': s.delivered,
        }
        for s in call.spoken
    ]


async def run_case(
    case: Case,
    collector: Spans,
    *,
    agents: dict | None = None,
    max_turns: int = MAX_TURNS,
    requests_left: int = MAX_REQUESTS,
    tokens_left: int = MAX_TOKENS,
) -> Record:
    """One call, one attempt. The record says passed, failed, exhausted or off-script, and why."""
    agents = AGENTS if agents is None else agents
    session = f'text-{case.name}-{uuid.uuid4().hex[:8]}'
    setup(session=session, trace_name='salon text call')
    record = Record(case=case.name, session=session)
    start = time.time()
    diary = Diary(clock=CLOCK)
    case.prepare(diary)
    watch(diary, record, start)
    call = open_call('', diary=diary)
    heard: list[str] = []

    async def say(text: str) -> None:
        heard.append(text)

    async def end() -> None:
        pass

    call._say, call._end = say, end
    collector.take()
    last = greet(call, GREETING)
    record.turns.append({'turn': 0, 'caller': '', 'said': last, 'delivered': acknowledge(call)})
    words, closing, used = case.opening, False, {}
    for number in range(1, max_turns + 1):
        turn_start = time.time() - start
        before, parts, done = len(heard), [], None
        async for event in run_turn(call, agents, words, TurnBudget()):
            if isinstance(event, TextDelta | FixedSpeech):
                parts.append(event.text)
            elif isinstance(event, TurnFinished):
                done = event
        turn_end = time.time() - start
        said = ' '.join([*heard[before:], ''.join(parts)]).strip()
        readings = [
            {'kind': r.kind, 'labels': r.labels, 'outcome': r.outcome, 'seconds': r.duration}
            for r in call.requests
        ]
        delivered = acknowledge(call)
        ended = collector.take()
        requests = [s for s in ended if is_request(s)]
        record.writes += [
            json.loads(str(attributes(s)['write.audit'])) for s in ended if 'write.audit' in attributes(s)
        ]
        record.trace_ids += sorted(
            {format(s.context.trace_id, '032x') for s in ended if s.context} - set(record.trace_ids)
        )
        tokens_in = sum(int(attributes(s).get('gen_ai.usage.input_tokens', 0)) for s in requests)
        tokens_out = sum(int(attributes(s).get('gen_ai.usage.output_tokens', 0)) for s in requests)
        record.requests += len(requests)
        record.tokens['input'] += tokens_in
        record.tokens['output'] += tokens_out
        model = [((s.start_time or 0) / 1e9 - start, (s.end_time or 0) / 1e9 - start) for s in requests]
        backend = [
            (c['start'], c['end']) for c in record.diary_calls if c['start'] >= turn_start and 'end' in c
        ]
        wall = turn_end - turn_start
        proven = call.state.appointment is not None
        record.turns.append({
            'turn': number,
            'started': round(turn_start, 3),
            'caller': words,
            'said': said,
            'delivered': delivered,
            'status': done.status if done else 'none',
            'readings': readings,
            'requests': len(requests),
            'tokens': {'input': tokens_in, 'output': tokens_out},
            'seconds': {
                'wall': round(wall, 3),
                'model': round(union(model), 3),
                'backend': round(union(backend), 3),
                'core': round(max(0.0, wall - union(model + backend)), 3),
            },
            'bookings_after': len(diary.bookings_for(PHONE, TODAY)),
            'appointment_known': proven,
            'agent_after': call.state.agent,
        })  # fmt: skip
        if not proven and any(claim in said.lower() for claim in CLAIMS):
            record.problems.append(
                f'turn {number}: said a booking exists before the call had its receipt: {said!r}'
            )
        last = said
        if closing or call._ended:
            break
        if record.requests >= requests_left or sum(record.tokens.values()) >= tokens_left:
            record.result = 'exhausted'
            record.problems.append(
                f'allowance reached after turn {number}: {record.requests} requests, {sum(record.tokens.values())} tokens'
            )
            break
        answer = reply(case.rules, last, used)
        if answer is None:
            record.result = 'off-script'
            record.problems.append(f'turn {number}: the script has no reply to {last!r}')
            break
        words, closing = answer
    else:
        record.result = 'exhausted'
        record.problems.append(f'{max_turns} caller turns used without the call ending')
    record.final = {
        'bookings': diary.bookings_for(PHONE, TODAY),
        'state': call.state.model_dump(mode='json'),
    }
    found = case.check(record)
    if record.result:
        record.problems += [f'(check, not decisive) {p}' for p in found]
    else:
        record.problems += found
        record.result = 'failed' if record.problems else 'passed'
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.force_flush()
    return record


def one_booking(record: Record, service: str, day: str) -> list[str]:
    """Exactly one booking for the seed customer, of this service on this date, at a time the diary offered."""
    held = record.bookings()
    if len(held) != 1:
        return [f'expected exactly one booking, found {len(held)}: {held}']
    booked = held[0]
    problems = []
    if (booked['service'], booked['date']) != (service, day):
        problems.append(f'expected a {service} on {day}, found a {booked["service"]} on {booked["date"]}')
    offers = {tuple(o) for c in record.calls_to('find_slots') for o in c.get('offered', [])}
    if (booked['date'], booked['time']) not in offers:
        problems.append(f'{booked["date"]} {booked["time"]} was booked but never offered to this call')
    if record.final['state'].get('verified_phone') != PHONE:
        problems.append(
            f'the caller was never identified: verified_phone {record.final["state"].get("verified_phone")!r}'
        )
    return problems


def booked_on(record: Record) -> list[dict[str, Any]]:
    """The booking writes that landed, each with the question it answered."""
    return [w for w in record.writes if w.get('action') == 'save_booking' and w.get('status') == 'ok']


def played(record: Record, words: str) -> bool:
    """Whether a segment with exactly these words was delivered to the caller and played to its end."""
    return any(
        s['text'] == words and s['after'] == 'played' and not s['interrupted']
        for turn in record.turns
        for s in turn['delivered']
    )


def check_t1(record: Record) -> list[str]:
    problems = one_booking(record, 'haircut', '2026-10-02')
    if record.bookings() and int(record.bookings()[0]['time'][:2]) >= 12:
        problems.append(f'asked for the morning, booked {record.bookings()[0]["time"]}')
    return problems + [
        f'booked on a question that was not played in full: {w["question"]!r}'
        for w in booked_on(record)
        if not played(record, w['question'])
    ]


def check_t2(record: Record) -> list[str]:
    """No Tuesday write, ever; the Wednesday booking only on a yes to a Wednesday read-back that played in full."""
    problems = one_booking(record, 'haircut', '2026-09-30')
    conditional = next((t for t in record.turns if t['caller'].lower().startswith('yes, but')), None)
    if conditional is None:
        problems.append('the script never reached "yes, but make it Wednesday": nothing was tested')
    elif conditional['bookings_after']:
        problems.append(f'turn {conditional["turn"]}: "yes, but make it Wednesday" booked something')
    for write in (w for w in record.writes if w.get('action') == 'save_booking'):
        if '2026-09-29' in json.dumps(write):
            problems.append(f'a write for Tuesday was attempted: {write["args"]}')
    for write in booked_on(record):
        if 'wednesday' not in write['question'].lower():
            problems.append(f'booked on a read-back that was not for Wednesday: {write["question"]!r}')
        if not played(record, write['question']):
            problems.append(f'booked on a read-back that was not played in full: {write["question"]!r}')
    return problems


def check_t3(record: Record) -> list[str]:
    """Verification first, then the booking asked for at the start, with its details, on a yes of its own.

    v2: v1 named the service `colour`, which is not the diary's id, and did
    not look at what was offered on the way back from verification, so a
    detour that kept the day but dropped the time passed.
    """
    problems = one_booking(record, 'hair-color', '2026-10-01')
    if record.bookings() and record.bookings()[0]['time'] != '14:00':
        problems.append(f'asked for two in the afternoon, booked {record.bookings()[0]["time"]}')
    confirmed = next((t for t in record.turns if t['caller'] == "Yes, that's my number."), None)
    if confirmed is None:
        problems.append('the script never confirmed the number: the detour was not exercised')
        return problems
    if not any(t in confirmed['said'] for t in ('2:00 PM', '14:00', 'two o', '2 PM')):
        problems.append(
            f'the reply to the verification yes did not offer the requested 2 PM: {confirmed["said"]!r}'
        )
    elif confirmed['bookings_after']:
        problems.append(f'turn {confirmed["turn"]}: the yes to the phone read-back also booked')
    problems += [
        f'the booking was authorised by a {w["about"]!r} question, not a booking read-back'
        for w in booked_on(record)
        if w.get('about') != 'booking'
    ]
    return problems


def check_t4(record: Record) -> list[str]:
    """The first write lands but its answer is lost. One booking; the outcome asked for before any second write.

    v2 of the case adds the caller's reply to the agent saying the save went
    wrong: v1 had none, and its one live run stopped there, off-script.
    """
    problems = one_booking(record, 'haircut', '2026-10-02')
    saves = record.calls_to('save_booking')
    if not saves or 'error' not in saves[0]:
        return [*problems, 'the injected lost answer never happened: nothing was tested']
    asked = [c for c in record.calls_to('outcome_of') if c['start'] >= saves[0]['end']]
    if len(saves) > 1 and not any(c['end'] <= saves[1]['start'] for c in asked):
        problems.append('a second write went out before the outcome of the first was asked for')
    if not any(c.get('returned') for c in asked):
        problems.append('the lost outcome was never recovered from the diary')
    return problems


def lose_first_answer(diary: Diary) -> None:
    """T4's fault: the first booking lands, its answer is lost, and the diary cannot say so the first time it is asked."""
    real_save, real_outcome = diary.save_booking, diary.outcome_of
    count = {'saves': 0, 'asked': 0}

    def save_booking(*args: Any, **kwargs: Any) -> dict:
        count['saves'] += 1
        result = real_save(*args, **kwargs)
        if count['saves'] == 1:
            raise TimeoutError('the diary did not answer')
        return result

    def outcome_of(token: str) -> dict | None:
        count['asked'] += 1
        return None if count['asked'] == 1 else real_outcome(token)

    setattr(diary, 'save_booking', save_booking)  # noqa: B010
    setattr(diary, 'outcome_of', outcome_of)  # noqa: B010


PHONE_RULES = (
    Rule(('phone number', 'your number', 'country code'), (SAID_PHONE,)),
    Rule(('have i got that right',), ("Yes, that's my number.",)),
)
GOODBYE = Rule(
    ('anything else', 'what else', 'something else'), ("No, that's everything. Goodbye.",), last=True
)

CASES: dict[str, Case] = {
    case.id: case
    for case in (
        Case(
            'T1',
            1,
            'A known customer books a haircut on Friday morning.',
            "Hi, I'd like to book a haircut on Friday morning, please.",
            (
                *PHONE_RULES,
                Rule(('which time',), ('The first one, please.',)),
                Rule(('shall i book',), ('Yes, please.',)),
                GOODBYE,
            ),
            check_t1,
        ),
        Case(
            'T2',
            1,
            'The caller picks a Tuesday time, then answers its read-back with "yes, but make it Wednesday".',
            "Hi, I'd like a haircut on Tuesday.",
            (
                *PHONE_RULES,
                Rule(('which time',), ('The first one, please.',)),
                Rule(('shall i book',), ('Yes, but make it Wednesday.', 'Yes, please.')),
                GOODBYE,
            ),
            check_t2,
        ),
        Case(
            'T3',
            2,
            'The caller asks for a colour on Thursday at two before saying who they are.',
            'Hi, can I book a colour on Thursday at two in the afternoon?',
            (
                *PHONE_RULES,
                Rule(('which time',), ("Two o'clock, please.",)),
                Rule(('shall i book',), ('Yes, please.',)),
                GOODBYE,
            ),
            check_t3,
        ),
        Case(
            'T4',
            2,
            'A normal booking whose first write lands but whose answer is lost.',
            "Hi, I'd like to book a haircut on Friday morning, please.",
            (
                *PHONE_RULES,
                Rule(('which time',), ('The first one, please.',)),
                Rule(
                    ('glitch', 'checks on this', 'did not save', "didn't save", 'not saved', 'went through'),
                    ('Okay. Can you check now whether it was booked?',),
                ),
                Rule(('shall i book', 'try again', 'not sure', 'not clear'), ('Yes, please.',)),
                GOODBYE,
            ),
            check_t4,
            lose_first_answer,
        ),
    )
}


async def checkpoint(
    names: list[str], out: Path, requests: int = MAX_REQUESTS, tokens: int = MAX_TOKENS
) -> int:
    """Run the named cases one after the other, sharing one request and token allowance. Returns the failures.

    `requests` and `tokens` are what is left of the checkpoint's allowance, for
    a checkpoint split across invocations: never more than the contract's.
    """
    collector = spans()
    requests, tokens, failures = min(requests, MAX_REQUESTS), min(tokens, MAX_TOKENS), 0
    out.mkdir(parents=True, exist_ok=True)
    for name in names:
        if requests <= 0 or tokens <= 0:
            print(f'{name}: not run, the checkpoint allowance is spent', flush=True)
            failures += 1
            continue
        record = await run_case(CASES[name], collector, requests_left=requests, tokens_left=tokens)
        requests -= record.requests
        tokens -= sum(record.tokens.values())
        path = out / f'{record.case}.json'
        path.write_text(json.dumps(asdict(record), indent=2, default=str))
        failures += record.result != 'passed'
        print(
            f'{record.case}: {record.result}  turns {len(record.turns) - 1}  requests {record.requests}  '
            f'tokens {record.tokens["input"]}+{record.tokens["output"]}  session {record.session}  -> {path}',
            flush=True,
        )
        for problem in record.problems:
            print(f'    {problem}', flush=True)
    print(f'allowance left: {requests} requests, {tokens} tokens', flush=True)
    return failures


def recheck(paths: list[Path]) -> int:
    """Run today's check of each saved record's case again, with no model: a check fixed after a run is not a rerun.

    Only the check is new; the call is the one that was recorded. A record
    whose case's script has changed since is still rechecked, and says both
    versions, so nothing reads it as a run of the new script.
    """
    failures = 0
    for path in paths:
        data = json.loads(path.read_text())
        record = Record(**data)
        case = CASES[record.case.split('.')[0]]
        problems = case.check(record)
        failures += bool(problems) or data['result'] in ('off-script', 'exhausted')
        verdict = (
            data['result']
            if data['result'] in ('off-script', 'exhausted')
            else 'failed'
            if problems
            else 'passed'
        )
        print(f'{record.case} rechecked with {case.name}: {verdict}', flush=True)
        for problem in problems:
            print(f'    {problem}', flush=True)
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m evals.calls', description='The live text checkpoint.')
    parser.add_argument('cases', nargs='*', metavar='CASE', help=f'any of {", ".join(CASES)}; default all')
    parser.add_argument('--out', type=Path, default=Path(tempfile.mkdtemp(prefix='salon-calls-')))
    parser.add_argument(
        '--requests-left', type=int, default=MAX_REQUESTS, help='what is left of the allowance'
    )
    parser.add_argument('--tokens-left', type=int, default=MAX_TOKENS, help='what is left of the allowance')
    parser.add_argument('--recheck', type=Path, nargs='+', help='saved records to check again, no model')
    args = parser.parse_args(argv)
    if args.recheck:
        return recheck(args.recheck)
    unknown = [c for c in args.cases if c not in CASES]
    if unknown:
        parser.error(f'unknown case {", ".join(unknown)}')
    return asyncio.run(checkpoint(args.cases or list(CASES), args.out, args.requests_left, args.tokens_left))


if __name__ == '__main__':
    sys.exit(main())
