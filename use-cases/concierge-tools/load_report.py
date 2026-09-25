"""Judge Coval load-test calls from Langfuse and the write records, with no Coval metric.

    uv run --env-file ../../.env python load_report.py RUN_ID [RUN_ID ...] --out DIR

Coval only places and records the calls (`coval_sim run --no-metrics`). Each
call's Twilio ID is its Langfuse session; this reads every observation of it
and says, per call and in total:

* outcome: the writes, each with the question it answered, and the bookings;
* consent: writes with no matching question put on an earlier turn (must be 0);
* speech: model text in the same reply as an operation call (the operation
  says its own words, so the caller hears them twice), model text opening
  with a holding line, and holding lines per caller turn;
* requests: per caller turn, completed and cancelled, and the chains where a
  reply that only recorded a field was followed by a request that only asked
  the operation's read-back;
* latency: turn latency (`turn.user_bot_latency_seconds`), model request
  time, and for each reply that spoke, the time from its first token to its
  end: what holding its text until the reply is complete would cost;
* endpointing: pieces that continued a caller turn the runtime had already
  ended (the caller paused and went on), and turns the caller cut into;
* isolation: every call's first booking is BK0001, its own diary's first.

Writes `report.md` and `report.json` into DIR. Bills nothing: it reads.
"""

import argparse
import ast
import json
import os
import statistics
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

from coval_sim.coval import CovalClient
from coval_sim.models import Simulation
from ledger import Obs, fetch, is_request, quantile, read_call

OPERATIONS = frozenset(
    {'lookup_customer', 'book_appointment', 'move_appointment', 'cancel_appointment', 'record_complaint'}
)
"""The salon's operations (`use-cases/salon-graph/salon/agent.yaml`): the tool calls that speak their own words."""
BOOKINGS = frozenset({'book_appointment', 'move_appointment', 'cancel_appointment'})
HOLDING = ('Let me have a look.', 'One moment.')
"""English `Locale.holding` (`core/src/core/locale.py`), as text: this tool imports no build."""
LIMIT_SECONDS = 180
"""`coval_sim`'s `MAX_CALL_SECONDS`: a call this long was ended by the bot's limit, not by the caller."""
FIRST_BOOKING = 'BK0001'


def _listed(value: Any) -> list[Any]:
    """A list attribute, which Langfuse returns as a list or as its Python text."""
    if isinstance(value, str):
        try:
            value = ast.literal_eval(value)
        except ValueError, SyntaxError:
            return []
    return list(value) if isinstance(value, (list, tuple)) else []


def _reply(output: Any) -> dict[str, Any]:
    """A chat request's output as `{content, tool_calls}`; a cancelled request has none."""
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            return {'content': output}
    return output if isinstance(output, dict) else {}


def _tools(reply: dict[str, Any]) -> list[str]:
    return [call.get('function', {}).get('name', '') for call in reply.get('tool_calls') or []]


def _seconds(o: Obs) -> float | None:
    return (o.end - o.start).total_seconds() if o.end else None


@dataclass
class CallReport:
    run: str
    simulation: str
    call: str | None
    status: str
    seconds: float | None = None
    writes: list[dict[str, Any]] = field(default_factory=list)
    bookings: list[str] = field(default_factory=list)
    """What each booking, move or cancel receipt said, in order."""
    first_booking: str | None = None
    unmatched: list[dict[str, Any]] = field(default_factory=list)
    caller_turns: int = 0
    completed: int = 0
    cancelled: int = 0
    input_tokens: int = 0
    echo: list[str] = field(default_factory=list)
    """Model text said in the same reply as an operation call."""
    filler_copies: int = 0
    """Model replies that open with a holding line: the model saying the engine's words."""
    holding: dict[int, int] = field(default_factory=dict)
    """Caller turn -> holding lines said in it."""
    chains: dict[str, int] = field(default_factory=dict)
    patterns: list[str] = field(default_factory=list)
    latency: list[float] = field(default_factory=list)
    requests: list[float] = field(default_factory=list)
    tails: list[float] = field(default_factory=list)
    statuses: dict[str, int] = field(default_factory=dict)
    pieces: int = 0
    """Runs that answered a second or later piece of one caller turn (`turn_pieces` over 1)."""
    interrupted: int = 0
    """Runtime turns the caller cut into (`turn.was_interrupted`)."""
    problems: list[str] = field(default_factory=list)


def judge(run: str, sim: Simulation, obs: list[Obs]) -> CallReport:
    """One call's report, from its Langfuse observations."""
    report = CallReport(run, sim.simulation_id, sim.call_sid, sim.status)
    if sim.status != 'COMPLETED':
        report.problems.append(f'call {sim.status}: {sim.error_message or "no error message"}')
    if not obs:
        report.problems.append('no Langfuse session for this call')
        return report
    ledger = read_call(sim.call_sid or sim.simulation_id, obs)
    report.caller_turns, report.cancelled = ledger.caller_turns, ledger.cancelled
    report.completed, report.input_tokens = sum(ledger.completed.values()), ledger.input_tokens
    for write in ledger.writes:
        audit, receipt = write.audit, write.audit.get('receipt') or {}
        report.writes.append({
            'action': write.name, 'status': write.status, 'asked_turn': audit.get('asked_turn'),
            'turn': audit.get('turn'), 'heard': audit.get('heard'), 'receipt': receipt,
        })  # fmt: skip
        if write.name in BOOKINGS and write.status == 'ok':
            report.bookings.append(str(receipt.get('spoken', '')))
            report.first_booking = report.first_booking or receipt.get('booking_id')
    report.unmatched = [w.audit for w in ledger.unmatched()]

    chats = sorted((o for o in obs if is_request(o)), key=lambda o: o.start)
    for chat in chats:
        reply = _reply(chat.output)
        text, tools = str(reply.get('content') or ''), set(_tools(reply))
        if text.strip() and tools & OPERATIONS:
            report.echo.append(f'{"+".join(sorted(tools & OPERATIONS))}: {text.strip()[:120]}')
        if text.strip().startswith(HOLDING):
            report.filler_copies += 1
        if chat.output is not None and (seconds := _seconds(chat)) is not None:
            report.requests.append(seconds)

    holding: Counter[int] = Counter()
    chains: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    for span in sorted(
        (o for o in obs if o.type == 'AGENT' and 'speech_segments' in o.attrs), key=lambda o: o.start
    ):
        statuses[str(span.attrs.get('status'))] += 1
        report.pieces += str(span.attrs.get('turn_pieces', '1')) not in ('0', '1')
        for segment, origin in zip(
            _listed(span.attrs.get('speech_segments')),
            _listed(span.attrs.get('speech_origins')),
            strict=False,
        ):
            parts = str(segment).split(':')
            if origin == 'tool_filler' and len(parts) == 3 and parts[1].isdigit():
                holding[int(parts[1])] += 1
        inside = [c for c in chats if c.start >= span.start and (span.end is None or c.start <= span.end)]
        firsts, ttfts = (
            _listed(span.attrs.get('request_first_output')),
            _listed(span.attrs.get('request_ttft')),
        )
        for first, ttft, chat in zip(firsts, ttfts, inside, strict=False):
            if first == 'text' and (seconds := _seconds(chat)) is not None:
                report.tails.append(max(0.0, seconds - float(ttft)))
        kinds = [set(_tools(_reply(c.output))) or {'text'} for c in inside if c.output is not None]
        report.patterns.append(' > '.join('+'.join(sorted(k)) for k in kinds))
        for before, after in pairwise(kinds):
            if before == {'collect'} and after & OPERATIONS:
                chains[f'collect > {"+".join(sorted(after & OPERATIONS))}'] += 1
    report.holding, report.chains, report.statuses = dict(holding), dict(chains), dict(statuses)
    report.latency = [
        float(o.attrs['turn.user_bot_latency_seconds'])
        for o in obs
        if o.name == 'turn' and o.attrs.get('turn.user_bot_latency_seconds') is not None
    ]
    report.interrupted = sum(
        1 for o in obs if o.name == 'turn' and str(o.attrs.get('turn.was_interrupted')).lower() == 'true'
    )
    conversation = next((o for o in obs if o.name == 'conversation'), None)
    report.seconds = _seconds(conversation) if conversation else None

    if report.unmatched:
        report.problems.append(f'{len(report.unmatched)} write(s) with no matching question: consent')
    if len(report.bookings) != 1:
        report.problems.append(f'{len(report.bookings)} booking writes, expected 1')
    if report.first_booking not in (None, FIRST_BOOKING):
        report.problems.append(
            f'first booking {report.first_booking}, not {FIRST_BOOKING}: calls share a diary'
        )
    if report.seconds is not None and report.seconds >= LIMIT_SECONDS - 1:
        report.problems.append(f'ended by the {LIMIT_SECONDS} s limit')
    if report.echo:
        report.problems.append(f'{len(report.echo)} reply(s) spoke beside an operation')
    if any(n > 1 for n in holding.values()):
        report.problems.append(
            f'{sum(n > 1 for n in holding.values())} caller turn(s) with repeated holding lines'
        )
    return report


def _p(values: list[float], p: float) -> str:
    return f'{quantile(values, p):.2f}' if values else '-'


def render(reports: list[CallReport]) -> str:
    """The markdown report: one row per call, then the totals."""
    lines = [
        '| Run | Call | Bookings | Writes (unmatched) | Echo | Filler copies | Max holding / turn | '
        'collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Problems |',
        '|---|---|---|---|---|---|---|---|---|---|---|---|',
    ]
    for r in reports:
        per_turn = f'{r.completed / r.caller_turns:.2f}' if r.caller_turns else '-'
        lines.append(
            f'| `{r.run[:8]}` | `{r.simulation[:8]}` `{(r.call or "-")[:10]}` | {"; ".join(r.bookings) or "none"} | '
            f'{len(r.writes)} ({len(r.unmatched)}) | {len(r.echo)} | {r.filler_copies} | '
            f'{max(r.holding.values(), default=0)} | {sum(r.chains.values())} | {per_turn} | {r.cancelled} | '
            f'{_p(r.latency, 0.5)} / {_p(r.latency, 0.9)} | {"; ".join(r.problems) or "none"} |'
        )
    every = lambda name: [x for r in reports for x in getattr(r, name)]  # noqa: E731
    turns, completed = sum(r.caller_turns for r in reports), sum(r.completed for r in reports)
    started = completed + sum(r.cancelled for r in reports)
    chains = Counter()
    patterns = Counter(p for r in reports for p in r.patterns if p)
    for r in reports:
        chains.update(r.chains)
    passed = sum(not r.problems for r in reports)
    lines += [
        '',
        f'Calls: {len(reports)}, with no problem: {passed}.',
        f'Writes: {sum(len(r.writes) for r in reports)}, with no matching question: '
        f'{sum(len(r.unmatched) for r in reports)}.',
        f'Calls with exactly one booking: {sum(len(r.bookings) == 1 for r in reports)}. '
        f'First booking {FIRST_BOOKING} in every call that booked: '
        f'{all(r.first_booking in (None, FIRST_BOOKING) for r in reports)}.',
        f'Model text beside an operation: {sum(len(r.echo) for r in reports)} in '
        f'{sum(bool(r.echo) for r in reports)} calls. Replies opening with a holding line: '
        f'{sum(r.filler_copies for r in reports)}.',
        f'Caller turns with more than one holding line: {sum(n > 1 for r in reports for n in r.holding.values())}.',
        f'Requests: {completed} completed over {turns} caller turns '
        f'({completed / turns if turns else 0:.2f} per turn), {started - completed} cancelled of {started} started.',
        f'Chains code could skip: {dict(chains) or "none"}.',
        f'Endpointing: {sum(r.pieces for r in reports)} runs answered a continuation piece; '
        f'{sum(r.interrupted for r in reports)} runtime turns were cut into by the caller.',
        f'Turn latency: p50 {_p(every("latency"), 0.5)} s, p90 {_p(every("latency"), 0.9)} s, '
        f'n={len(every("latency"))}.',
        f'Model request time: p50 {_p(every("requests"), 0.5)} s, p90 {_p(every("requests"), 0.9)} s.',
        f'First token to end of a reply that spoke: p50 {_p(every("tails"), 0.5)} s, '
        f'p90 {_p(every("tails"), 0.9)} s, n={len(every("tails"))}.',
        f'Input tokens per call: {statistics.mean(r.input_tokens for r in reports) if reports else 0:.0f}.',
        '',
        'Most common request chains per hop:',
        *[f'- {n} x `{p}`' for p, n in patterns.most_common(12)],
    ]
    for r in reports:
        for echo in r.echo:
            lines.append(f'- echo in `{r.simulation[:8]}`: {echo}')
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('runs', nargs='+', help='Coval run IDs')
    parser.add_argument('--out', type=Path, required=True, help='where report.md and report.json go')
    args = parser.parse_args(argv)
    coval = CovalClient(os.environ.get('COVAL_API_KEY', ''))
    reports = [
        judge(run, sim, fetch(sim.call_sid) if sim.call_sid else [])
        for run in args.runs
        for sim in coval.simulations(run)
    ]
    args.out.mkdir(parents=True, exist_ok=True)
    text = render(reports)
    (args.out / 'report.md').write_text(text + '\n')
    (args.out / 'report.json').write_text(json.dumps([asdict(r) for r in reports], indent=1) + '\n')
    print(text)
    return 1 if any(r.unmatched for r in reports) else 0


if __name__ == '__main__':
    sys.exit(main())
