"""Judge Coval calls from Langfuse and the write records, with no Coval metric.

    uv run --env-file ../../.env python load_report.py RUN_ID [RUN_ID ...] --out DIR [--evidence FILE]
    uv run python load_report.py --saved EXPORT.json --out DIR [--evidence FILE]

Coval only places and records the calls (`coval_sim run --no-metrics`). Each
call's Twilio ID is its Langfuse session; this reads every observation of it.
`--save` keeps what was read, and `--saved` judges that export again offline.

Each call gets six separate answers. None stands in for another, and each is
pass, fail or unknown. Unknown is never counted as a pass:

* outcome: the appointments still active once every receipt is replayed in
  order by booking ID (a move changes one, a cancel removes one, the same
  receipt twice is one), against what the caller finally asked for;
* authorization: every write that needed a yes had a question about it on an
  earlier turn (a label check, deterministic), and the words heard accepted
  that exact proposal (a reviewed label);
* delivery: the proposal played in full before the answer. The runtime's own
  playback estimate cannot certify itself: only a transport acknowledgement in
  the audit or a reviewed recording can;
* completion: what the agent said it had done at the end (a reviewed label)
  is backed by the replayed effects: a lookup receipt backs no booking;
* operational: the call completed, and how it ended (measured from bot logs
  or the recording). A trace span's length is not a call's length;
* evidence: every fact the other five could not establish, by name.

Reviewed labels and measured facts come from `--evidence`, a JSON file
(`Evidence`). Each review names its reviewer (`human` or `agent`) and its
source; an agent's label is not a human one, and the report says which a
pass rests on. Without evidence, a call can fail on deterministic facts but
never pass on semantic ones.

Diagnostics that are not verdicts stay as before: model text beside an
operation, holding lines, request chains, latency, endpointing, and the first
booking ID (BK0001 in a fresh diary per call: expected, not an isolation test).

Writes `report.md` and `report.json` into DIR. Exit status: 0 when every call
passes all five, 1 when any call fails one, 3 when none fails but some answer
is unknown. Bills nothing: it reads.
"""

import argparse
import ast
import json
import os
import re
import statistics
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, time
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from coval_sim.coval import CovalClient
from coval_sim.models import Simulation
from ledger import (
    Obs,
    Write,
    _matches,
    fetch,
    from_json,
    is_request,
    needs_consent,
    quantile,
    read_call,
    to_json,
)

OPERATIONS = frozenset(
    {'lookup_customer', 'book_appointment', 'move_appointment', 'cancel_appointment', 'record_complaint'}
)
"""The salon's operations (`use-cases/salon-graph/salon/agent.yaml`): the tool calls that speak their own words."""
BOOKINGS = frozenset({'book_appointment', 'move_appointment', 'cancel_appointment', 'save_booking'})
"""Booking writes: the harness's operations, and the native build's one tool (`use-cases/salon-pipecat-flows`).
Only `booked_after` reads it; effects are replayed from the receipts' shape, whatever the action's name."""
HOLDING = ('Let me have a look.', 'One moment.')
"""English `Locale.holding` (`core/src/core/locale.py`), as text: this tool imports no build."""
LIMIT_SECONDS = 180
"""`coval_sim`'s `MAX_CALL_SECONDS`, the default for `--call-seconds`."""
FIRST_BOOKING = 'BK0001'
EFFECTS = {'booked': 'book', 'moved': 'move', 'cancelled': 'cancel'}
"""A booking receipt's status, and the change it makes (`Saved` in both builds' `salon/models/receipts.py`)."""
SLOT = re.compile(r'^(\d{4}-\d{2}-\d{2})T(\d{2})(\d{2})\|')
"""The salon diary's slot ID: `2026-10-02T0900|haircut`."""
AUDITED = ('about', 'question', 'asked_turn', 'turn', 'heard')
"""What a write's audit must say before its consent can be judged at all."""
EXIT_FAILED, EXIT_INCOMPLETE = 1, 3

Verdict = Literal['pass', 'fail', 'unknown']
_RANK = {'pass': 0, 'unknown': 1, 'fail': 2}


class _Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class WriteReview(_Strict):
    """What a reviewer found about one write, keyed `action@turn` in `CallReview.writes`."""

    accepted: bool | None = None
    """Whether the words heard accepted this exact proposal: not a time pick, repeated digits, a question or noise."""
    delivered: bool | None = None
    """Whether the proposal played in full before the answer, from the recording or its timing."""
    note: str = ''


class Final(_Strict):
    """The caller's final, disambiguated request: what one active appointment must match."""

    service: str
    date: date
    time: time
    phone: str | None = None


class CallReview(_Strict):
    """A reviewed reading of one call. Semantic, so it names who read it and from what."""

    reviewer: Literal['human', 'agent']
    source: str = Field(min_length=1)
    """What was read: `langfuse trace`, `coval transcript`, `recording`, and where."""
    final: Final | None = None
    no_request: bool = False
    """The caller left without asking for a booking: then none may be active."""
    writes: dict[str, WriteReview] = {}
    claims: list[Literal['booked', 'moved', 'cancelled', 'nothing_done']] | None = None
    """What the agent said, by the end, it had done. None: its closing was not reviewed. [] : it claimed nothing."""
    note: str = ''


class Facts(_Strict):
    """Measured facts about one call, from outside the trace. Deterministic, so they need no reviewer."""

    media_seconds: float | None = None
    """The length of Coval's recording: the connected media, as Coval kept it."""
    ended_by: (
        Literal['caller', 'agent', 'ceiling', 'provider_stall', 'simulator_stall', 'transport', 'test_runner']
        | None
    ) = None
    source: str = ''
    """Where they were measured: which log or recording."""


class Case(_Strict):
    """What a Coval test case asks for, for every call of it."""

    bookings: int = Field(ge=0)
    service: str | None = None


class Evidence(_Strict):
    """Everything `--evidence` supplies: reviews and facts by simulation ID, expectations by test case ID."""

    cases: dict[str, Case] = {}
    reviews: dict[str, CallReview] = {}
    facts: dict[str, Facts] = {}


@dataclass
class Check:
    """One dimension's answer: fail if any part failed, else unknown if any part is, else pass."""

    failed: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    """What could not be established: the call's missing evidence."""
    basis: list[str] = field(default_factory=list)
    """`deterministic`, `review:agent` or `review:human`, for the parts that passed."""

    def add(self, verdict: Verdict, why: str = '', basis: str = 'deterministic') -> None:
        if verdict == 'fail':
            self.failed.append(why)
        elif verdict == 'unknown':
            self.unknown.append(why)
        elif basis not in self.basis:
            self.basis.append(basis)

    @property
    def verdict(self) -> Verdict:
        return 'fail' if self.failed else 'unknown' if self.unknown else 'pass'

    def __str__(self) -> str:
        return self.verdict + (f' ({", ".join(self.basis)})' if self.verdict == 'pass' and self.basis else '')

    def as_json(self) -> dict[str, Any]:
        return {'verdict': self.verdict, **asdict(self)}


@dataclass
class Effects:
    """What the receipts did to the business, replayed in order. Reconstructed from receipts, not a backend snapshot."""

    active: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Booking ID -> the appointment as its latest receipt left it, with the caller's number where known."""
    applied: list[str] = field(default_factory=list)
    """Each change in order: `book BK0001`, `move BK0001`, `cancel BK0001`."""
    repeats: int = 0
    """Receipts seen again with nothing new: a replay of the same write, not a second appointment."""
    unknown: list[str] = field(default_factory=list)
    """Why the replay cannot be trusted: an outcome nobody knows, or a receipt that does not fit."""


def effects(writes: list[Write]) -> Effects:
    """Replay the booking receipts of one call, in order, by booking ID.

    A receipt is a booking's when it has a booking status and ID, whatever the
    action that returned it is called. A failed write changed nothing; a write
    whose outcome is unknown makes the whole replay unknown, as does a receipt
    naming another booking than its write did, or a move or cancel of a booking
    this call never booked (it may have existed before; only a backend
    snapshot could say).
    """
    out, phone = Effects(), None
    for w in writes:
        audit, receipt = w.audit, w.audit.get('receipt') or {}
        args = audit.get('args') or {}
        at = f'{w.name}@{audit.get("turn")}'
        if w.status == 'unknown':
            out.unknown.append(f'{at}: outcome unknown')
            continue
        if w.status != 'ok':
            continue
        if not receipt:
            out.unknown.append(f'{at}: landed, but no receipt says what it did')
            continue
        phone = receipt.get('customer_phone') or phone
        change, booking = EFFECTS.get(str(receipt.get('status'))), receipt.get('booking_id')
        if change is None or not booking:
            continue
        if args.get('booking_id') and args['booking_id'] != booking:
            out.unknown.append(f'{at}: receipt names {booking}, the write named {args["booking_id"]}')
            continue
        record = {
            'service': receipt.get('service'),
            **_when(receipt, args),
            'phone': receipt.get('phone') or args.get('phone') or phone,
        }
        held = out.active.get(booking)
        if change == 'book' and held is not None:
            if held == record:
                out.repeats += 1
            else:
                out.unknown.append(f'{at}: {booking} booked twice with different details')
            continue
        if change != 'book' and held is None:
            out.unknown.append(f'{at}: {change} of {booking}, which this call never booked')
            continue
        out.applied.append(f'{change} {booking}')
        if change == 'cancel':
            del out.active[booking]
        else:
            out.active[booking] = record
    return out


def _when(receipt: dict[str, Any], args: dict[str, Any]) -> dict[str, str | None]:
    """The day and time an appointment is on: the receipt's own fields, else the slot the write named, else None.

    The native receipt carries `date` and `time`; the harness's carries only
    words (`spoken`), so its slot argument is read instead: the salon's slot
    ID is `YYYY-MM-DDTHHMM|service` (`salon/diary.py`). Neither: unknown, never
    a guess from the spoken words.
    """
    if receipt.get('date') and receipt.get('time'):
        return {'date': receipt['date'], 'time': receipt['time']}
    if hit := SLOT.match(str(args.get('slot') or '')):
        return {'date': hit[1], 'time': f'{hit[2]}:{hit[3]}:00'}
    return {'date': None, 'time': None}


def _reviewed(review: CallReview | None) -> str:
    return f'review:{review.reviewer}' if review else ''


def outcome(done: Effects, review: CallReview | None, case: Case | None) -> Check:
    """Was the requested final action achieved, for the right caller, service, day and time?"""
    check = Check()
    if done.unknown:
        for why in done.unknown:
            check.add('unknown', why)
        return check
    requested = (
        0
        if review and review.no_request
        else case.bookings
        if case
        else 1
        if review and review.final
        else None
    )
    if requested is None:
        check.add('unknown', 'what the caller asked for: no case expectation and no reviewed final request')
        return check
    active = list(done.active.values())
    if len(active) != requested:
        check.add('fail', f'{len(active)} active appointment(s), {requested} requested')
        return check
    check.add('pass', basis=_reviewed(review) if review and review.no_request else 'deterministic')
    if not active:
        return check
    want = review.final if review else None
    service = want.service if want else case.service if case else None
    for held in active:
        if service and held['service'] != service:
            check.add('fail', f'service {held["service"]}, asked for {service}')
    if want is None:
        check.add('unknown', 'the final day and time asked for: not reviewed')
        return check
    for held in active:
        if held['date'] is None:
            check.add('unknown', 'the day and time booked: neither its receipt nor its write says')
        elif (held['date'], held['time']) != (want.date.isoformat(), want.time.isoformat()):
            check.add('fail', f'booked {held["date"]} {held["time"]}, asked for {want.date} {want.time}')
        if want.phone and held['phone'] is None:
            check.add('unknown', 'whose appointment it is: no number in its write or an identity receipt')
        elif want.phone and held['phone'] != want.phone:
            check.add('fail', f'booked for {held["phone"]}, the caller is {want.phone}')
    check.add('pass', basis=_reviewed(review))
    return check


def _key(w: Write) -> str:
    return f'{w.name}@{w.audit.get("turn")}'


def authorization(writes: list[Write], review: CallReview | None) -> Check:
    """Did the caller authorise each write that needed it: this exact proposal, not a pick, digits or noise?"""
    check = Check()
    for w in writes:
        need, key = needs_consent(w.audit), _key(w)
        if need is None:
            check.add(
                'unknown', f'{key}: whether it needed a yes (no mapping, no needs_approval in its audit)'
            )
            continue
        if not need:
            continue
        if lacking := [k for k in AUDITED if k not in w.audit]:
            check.add('unknown', f'{key}: its audit lacks {", ".join(lacking)}')
            continue
        if not _matches(w.audit):
            check.add('fail', f'{key}: no question about it put on an earlier turn')
            continue
        answer = review.writes.get(key) if review else None
        heard = w.audit.get('heard')
        if answer is None or answer.accepted is None:
            check.add('unknown', f'{key}: whether {heard!r} accepted the proposal (not reviewed)')
        elif not answer.accepted:
            check.add('fail', f'{key}: {heard!r} did not accept the proposal')
        else:
            check.add('pass', basis=_reviewed(review))
    if not (check.failed or check.unknown or check.basis):
        check.basis.append('no write needed a yes')
    return check


def delivery(writes: list[Write], review: CallReview | None) -> Check:
    """Did each proposal that authorised a write play in full before its answer, on evidence outside the runtime?"""
    check = Check()
    for w in writes:
        if not needs_consent(w.audit):
            continue
        key, answer = _key(w), review.writes.get(_key(w)) if review else None
        if w.audit.get('delivery') == 'acknowledged':
            check.add('pass')
        elif answer is None or answer.delivered is None:
            check.add(
                'unknown', f'{key}: no evidence the proposal played in full (the runtime estimate is not)'
            )
        elif not answer.delivered:
            check.add('fail', f'{key}: the proposal had not played in full when it was answered')
        else:
            check.add('pass', basis=_reviewed(review))
    if not (check.failed or check.unknown or check.basis):
        check.basis.append('no proposal authorised a write')
    return check


def completion(done: Effects, review: CallReview | None) -> Check:
    """Did the agent claim only effects the replayed receipts back?"""
    check = Check()
    if review is None or review.claims is None:
        check.add('unknown', 'what the agent said it had done: its closing was not reviewed')
        return check
    if done.unknown:
        check.add('unknown', 'what was done: the replay is unknown')
        return check
    changes = {a.split()[0] for a in done.applied}
    backed = {
        'booked': 'book' in changes and bool(done.active),
        'moved': 'move' in changes and bool(done.active),
        'cancelled': 'cancel' in changes,
        'nothing_done': not done.applied,
    }
    for claim in review.claims:
        if backed[claim]:
            check.add('pass', basis=_reviewed(review))
        else:
            check.add('fail', f'said {claim}; the receipts show {", ".join(done.applied) or "no change"}')
    if not review.claims:
        check.add('pass', basis=_reviewed(review))
    return check


def operational(
    sim: Simulation, traced: bool, facts: Facts | None, span: float | None, limit: float
) -> Check:
    """Did the call connect and end normally? From Coval's status and measured facts, never from a span's length."""
    check = Check()
    if sim.status in ('FAILED', 'ERROR', 'CANCELLED'):
        check.add('fail', f'call {sim.status}: {sim.error_message or "no error message"}')
    elif sim.status != 'COMPLETED':
        check.add('unknown', f'call {sim.status}: not finished')
    if not traced:
        check.add('unknown', 'no Langfuse session for this call')
    ended = facts.ended_by if facts else None
    spanned = f'{span:.0f} s' if span is not None else 'none'
    if ended is None:
        check.add(
            'unknown', f'how the call ended: not measured (the trace span, {spanned}, is not its length)'
        )
    elif ended == 'ceiling' and facts and facts.media_seconds is not None and facts.media_seconds < limit - 5:
        check.add(
            'unknown',
            f'a ceiling was logged, but the recording is {facts.media_seconds:.0f} s of {limit:.0f}',
        )
    elif ended in ('caller', 'agent'):
        check.add('pass')
    else:
        check.add('fail', f'ended by {ended.replace("_", " ")}')
    return check


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


DIMENSIONS = ('outcome', 'authorization', 'delivery', 'completion', 'operational')


@dataclass
class CallReport:
    run: str
    simulation: str
    call: str | None
    status: str
    span_seconds: float | None = None
    """The Langfuse conversation span: when tracing opened and closed, not how long the call was connected."""
    media_seconds: float | None = None
    """Coval's recording, when `--evidence` measured it."""
    writes: list[dict[str, Any]] = field(default_factory=list)
    bookings: list[str] = field(default_factory=list)
    """What each booking, move or cancel receipt said, in order: receipts, not appointments (see `active`)."""
    active: list[dict[str, Any]] = field(default_factory=list)
    """The appointments still active once the receipts are replayed."""
    applied: list[str] = field(default_factory=list)
    first_booking: str | None = None
    unmatched: list[dict[str, Any]] = field(default_factory=list)
    outcome: Check = field(default_factory=Check)
    authorization: Check = field(default_factory=Check)
    delivery: Check = field(default_factory=Check)
    completion: Check = field(default_factory=Check)
    operational: Check = field(default_factory=Check)
    missing: list[str] = field(default_factory=list)
    """Every fact a dimension could not establish: the call's evidence is complete when this is empty."""
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
    booked_after: float | None = None
    """Seconds from the call's start to its first booking write: how long the task took."""
    problems: list[str] = field(default_factory=list)
    """Diagnostics, not verdicts: speech and isolation symptoms worth reading."""

    @property
    def verdict(self) -> Verdict:
        return max((getattr(self, d).verdict for d in DIMENSIONS), key=_RANK.__getitem__)


def judge(
    run: str,
    sim: Simulation,
    obs: list[Obs],
    limit: float = LIMIT_SECONDS,
    evidence: Evidence | None = None,
) -> CallReport:
    """One call's report, from its Langfuse observations and whatever `evidence` says about it."""
    evidence = evidence or Evidence()
    review, facts = evidence.reviews.get(sim.simulation_id), evidence.facts.get(sim.simulation_id)
    case = evidence.cases.get(sim.test_case_id or '')
    report = CallReport(run, sim.simulation_id, sim.call_sid, sim.status)
    report.media_seconds = facts.media_seconds if facts else None
    ledger = read_call(sim.call_sid or sim.simulation_id, obs)
    conversation = next((o for o in obs if o.name == 'conversation'), None)
    report.span_seconds = _seconds(conversation) if conversation else None
    done = effects(ledger.writes)
    report.active, report.applied = list(done.active.values()), done.applied
    report.outcome = (
        outcome(done, review, case) if obs else Check(unknown=['no trace: no receipts to replay'])
    )
    report.authorization = authorization(ledger.writes, review)
    report.delivery = delivery(ledger.writes, review)
    report.completion = completion(done, review)
    report.operational = operational(sim, bool(obs), facts, report.span_seconds, limit)
    report.missing = [f'{d}: {why}' for d in DIMENSIONS for why in getattr(report, d).unknown]
    if not obs:
        return report
    report.caller_turns, report.cancelled = ledger.caller_turns, ledger.cancelled
    report.completed, report.input_tokens = sum(ledger.completed.values()), ledger.input_tokens
    for write in ledger.writes:
        audit, receipt = write.audit, write.audit.get('receipt') or {}
        report.writes.append({
            'action': write.name, 'status': write.status, 'about': audit.get('about'),
            'question': audit.get('question'), 'asked_turn': audit.get('asked_turn'), 'turn': audit.get('turn'),
            'heard': audit.get('heard'), 'args': audit.get('args'), 'receipt': receipt,
        })  # fmt: skip
        if write.status == 'ok' and receipt.get('status') in EFFECTS:
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
    booking_writes = sorted(
        (
            o
            for o in obs
            if o.name == 'write'
            and any(f'"{name}"' in str(o.attrs.get('write.audit', '')) for name in BOOKINGS)
        ),
        key=lambda o: o.start,
    )
    if conversation is not None and booking_writes:
        report.booked_after = (booking_writes[0].start - conversation.start).total_seconds()

    if report.first_booking not in (None, FIRST_BOOKING):
        report.problems.append(
            f'first booking {report.first_booking}, not {FIRST_BOOKING}: calls share a diary'
        )
    if report.echo:
        report.problems.append(f'{len(report.echo)} reply(s) spoke beside an operation')
    if any(n > 1 for n in holding.values()):
        report.problems.append(
            f'{sum(n > 1 for n in holding.values())} caller turn(s) with repeated holding lines'
        )
    return report


def _p(values: list[float], p: float) -> str:
    return f'{quantile(values, p):.2f}' if values else '-'


def _active(r: CallReport) -> str:
    return '; '.join(f'{a["service"]} {a["date"]} {a["time"]}' for a in r.active) or 'none'


def render(reports: list[CallReport]) -> str:
    """The markdown report: the verdicts per call, their totals, then the diagnostics."""
    lines = [
        '| Run | Call | Active after replay | Receipts | Outcome | Authorization | Delivery | Completion | '
        'Operational | Failed | Unknown |',
        '|---|---|---|---|---|---|---|---|---|---|---|',
    ]
    for r in reports:
        failed = [f'{d}: {why}' for d in DIMENSIONS for why in getattr(r, d).failed]
        lines.append(
            f'| `{r.run[:8]}` | `{r.simulation[:8]}` `{(r.call or "-")[:10]}` | {_active(r)} | '
            f'{", ".join(r.applied) or "none"} | '
            + ' | '.join(str(getattr(r, d)) for d in DIMENSIONS)
            + f' | {"; ".join(failed) or "-"} | {"; ".join(r.missing) or "-"} |'
        )
    lines += [
        '',
        '| Dimension | Pass | Fail | Unknown | Passes resting on an agent review |',
        '|---|---|---|---|---|',
    ]
    for d in DIMENSIONS:
        seen = Counter(getattr(r, d).verdict for r in reports)
        agent = sum(
            getattr(r, d).verdict == 'pass' and 'review:agent' in getattr(r, d).basis for r in reports
        )
        lines.append(f'| {d} | {seen["pass"]} | {seen["fail"]} | {seen["unknown"]} | {agent} |')
    complete = sum(not r.missing for r in reports)
    passed = sum(r.verdict == 'pass' for r in reports)
    lines += [
        '',
        f'Calls: {len(reports)}. Every dimension passed: {passed}. Any dimension failed: '
        f'{sum(r.verdict == "fail" for r in reports)}. Evidence complete: {complete} of {len(reports)}.',
        f'Calls with exactly one active appointment after replay: {sum(len(r.active) == 1 for r in reports)}. '
        f'Receipts replayed as a repeat or a change of an earlier one are not counted as appointments; '
        'this is reconstructed from trace receipts, not checked against a backend snapshot.',
        '',
        '## Diagnostics',
        '',
        '| Call | Writes (label mismatch) | Echo | Filler copies | Max holding / turn | collect > op | '
        'Requests / turn | Cancelled | Turn latency p50 / p90 | Span s | Recording s | Notes |',
        '|---|---|---|---|---|---|---|---|---|---|---|---|',
    ]
    for r in reports:
        per_turn = f'{r.completed / r.caller_turns:.2f}' if r.caller_turns else '-'
        span = f'{r.span_seconds:.0f}' if r.span_seconds is not None else '-'
        media = f'{r.media_seconds:.0f}' if r.media_seconds is not None else '-'
        lines.append(
            f'| `{r.simulation[:8]}` | {len(r.writes)} ({len(r.unmatched)}) | {len(r.echo)} | {r.filler_copies} | '
            f'{max(r.holding.values(), default=0)} | {sum(r.chains.values())} | {per_turn} | {r.cancelled} | '
            f'{_p(r.latency, 0.5)} / {_p(r.latency, 0.9)} | {span} | {media} | {"; ".join(r.problems) or "-"} |'
        )
    every = lambda name: [x for r in reports for x in getattr(r, name)]  # noqa: E731
    turns, completed = sum(r.caller_turns for r in reports), sum(r.completed for r in reports)
    started = completed + sum(r.cancelled for r in reports)
    chains = Counter()
    patterns = Counter(p for r in reports for p in r.patterns if p)
    for r in reports:
        chains.update(r.chains)
    lines += [
        '',
        f'Writes: {sum(len(r.writes) for r in reports)}, with no question about them on an earlier turn: '
        f'{sum(len(r.unmatched) for r in reports)} (a label check, not consent).',
        f'First booking {FIRST_BOOKING} in every call that booked: '
        f'{all(r.first_booking in (None, FIRST_BOOKING) for r in reports)} '
        '(expected with a fresh diary per call; not an isolation or contention test).',
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
        f'Time to first booking write: p50 '
        f'{_p([r.booked_after for r in reports if r.booked_after is not None], 0.5)} s, '
        f'p90 {_p([r.booked_after for r in reports if r.booked_after is not None], 0.9)} s.',
        '',
        'Most common request chains per hop:',
        *[f'- {n} x `{p}`' for p, n in patterns.most_common(12)],
    ]
    for r in reports:
        for echo in r.echo:
            lines.append(f'- echo in `{r.simulation[:8]}`: {echo}')
    return '\n'.join(lines)


def exit_status(reports: list[CallReport]) -> int:
    """0 only when every call passed every dimension: a failure is 1, missing evidence with no failure is 3."""
    if any(r.verdict == 'fail' for r in reports):
        return EXIT_FAILED
    return EXIT_INCOMPLETE if any(r.verdict == 'unknown' for r in reports) or not reports else 0


def as_json(r: CallReport) -> dict[str, Any]:
    out = asdict(r)
    out.update({d: getattr(r, d).as_json() for d in DIMENSIONS}, verdict=r.verdict)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('runs', nargs='*', help='Coval run IDs (none with --saved)')
    parser.add_argument('--out', type=Path, required=True, help='where report.md and report.json go')
    parser.add_argument(
        '--call-seconds', type=float, default=LIMIT_SECONDS, help='the call limit the runs had'
    )
    parser.add_argument(
        '--evidence', type=Path, help='reviews, measured facts and case expectations (Evidence)'
    )
    parser.add_argument('--saved', type=Path, help='judge this export instead of reading Coval and Langfuse')
    parser.add_argument(
        '--save', type=Path, help='also write what was read, for --saved later (private: speech)'
    )
    args = parser.parse_args(argv)
    if bool(args.runs) == bool(args.saved):
        parser.error('give run IDs or --saved, not both')
    try:
        evidence = Evidence.model_validate_json(args.evidence.read_text()) if args.evidence else Evidence()
    except ValidationError as error:
        parser.error(f'--evidence {args.evidence}: {error}')
    if args.saved:
        calls = [
            (
                row['run'],
                Simulation.model_validate(row['simulation']),
                [from_json(o) for o in row['observations']],
            )
            for row in json.loads(args.saved.read_text())
        ]
    else:
        coval = CovalClient(os.environ.get('COVAL_API_KEY', ''))
        calls = [
            (run, sim, fetch(sim.call_sid) if sim.call_sid else [])
            for run in args.runs
            for sim in coval.simulations(run)
        ]
    if args.save:
        rows = [
            {'run': run, 'simulation': sim.model_dump(mode='json'), 'observations': [to_json(o) for o in obs]}
            for run, sim, obs in calls
        ]
        args.save.write_text(json.dumps(rows) + '\n')
    reports = [judge(run, sim, obs, args.call_seconds, evidence) for run, sim, obs in calls]
    args.out.mkdir(parents=True, exist_ok=True)
    text = render(reports)
    (args.out / 'report.md').write_text(text + '\n')
    (args.out / 'report.json').write_text(json.dumps([as_json(r) for r in reports], indent=1) + '\n')
    print(text)
    return exit_status(reports)


if __name__ == '__main__':
    sys.exit(main())
