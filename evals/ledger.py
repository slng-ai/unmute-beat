"""One ledger of model work, latency and writes, read the same way from every voice build's Langfuse traces.

    uv run --env-file .env python -m evals.ledger harness CA... CA... -- graph trace:a8b9... -- livekit trace:4808...

Each group is a label and its calls. A call is `session:<id>` (a Twilio call
sid is the harness's session), `trace:<id>`, or a bare id, read as a session.
Nothing is billed: it only reads traces.

It knows three shapes of trace, and reads them with one set of rules:

* the harness: Pydantic AI `chat ...` generations under Pipecat's `turn` span;
* Pipecat's own LLM service (the graph build, the one-prompt baseline): `llm`
  generations under `turn`;
* LiveKit: `llm_request` generations, whose `llm_node` wrapper is skipped,
  under `agent_turn` and unmute's `turn`.

The rules, each one a mistake made once by hand:

* A request is one provider generation. Its input is Langfuse's `input` plus
  `input_cached_tokens`, counted once: Pydantic AI also reports the same
  tokens as `text_prompt_tokens`, and adding every key with "input" or
  "prompt" in it counted every harness token twice (24 September).
* A request with no usage at all was cancelled, or its usage never arrived.
  It is counted apart and never as zero tokens.
* Requests that overlap in time are one step of the caller's wait: the
  harness's readings run side by side. "Calls in a row" counts the steps.
* Latency is Pipecat's `turn.user_bot_latency_seconds`, the caller's silence
  before the bot speaks. LiveKit records no such figure; its latency is
  estimated as the gap from the end of the user turn to the next
  `agent_speaking`, plus LiveKit's own `lk.end_of_turn_delay`, and marked so.
* A write is a `save_booking` or `record_complaint` tool call, or a `write`
  span: the harness leaves one per attempt (`core.operations`) with the
  question it answered, the one-prompt baseline one per diary write
  (`salon.baseline.written`) without. A task is a write that succeeded, or
  whose result the trace does not show (LiveKit records no output for its
  tools). Only the harness's audit can be checked here; every other write
  is listed with the lines before it, for a person to read.
"""

import json
import math
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from typing import Any

MODELS = ('gemini', 'gpt', 'claude')
"""A generation from one of these is a model request. STT and TTS generations are not."""
CLASSIFIER = 'You classify the answer to a question'
"""The first words of every harness classifier's instructions (`core.classifiers.TASK`)."""
TASKS = frozenset({'save_booking', 'record_complaint'})
"""Tool calls that change the business: the work a call is for."""
FAILED = ('error', 'nothing was', 'refus', 'not saved')
"""Words in a tool's output that mean the write did not happen."""


@dataclass(frozen=True)
class Obs:
    """One Langfuse observation, reduced to what the ledger reads."""

    id: str
    parent: str | None
    type: str
    name: str
    start: datetime
    end: datetime | None = None
    model: str = ''
    input: Any = None
    output: Any = None
    usage: dict[str, int] = field(default_factory=dict)
    attrs: dict[str, Any] = field(default_factory=dict)
    """The observation's metadata and its OTel attributes, flattened into one dict."""

    @classmethod
    def of(cls, o: Any) -> Obs:
        meta = dict(o.metadata or {})
        attrs = {k: v for k, v in meta.items() if k not in ('attributes', 'resourceAttributes')}
        attrs.update(meta.get('attributes') or {})
        return cls(
            id=o.id,
            parent=o.parent_observation_id,
            type=o.type,
            name=o.name or '',
            start=o.start_time,
            end=o.end_time,
            model=o.model or '',
            input=o.input,
            output=o.output,
            usage=dict(o.usage_details or {}),
            attrs=attrs,
        )


def is_request(o: Obs) -> bool:
    return o.type == 'GENERATION' and o.name != 'llm_node' and any(m in o.model.lower() for m in MODELS)


def kind(o: Obs) -> str:
    return 'classifier' if CLASSIFIER in str(o.input) else 'step'


def tokens(o: Obs) -> tuple[int, int] | None:
    """(input, output) tokens, counted once; None when the request has no usage (cancelled, or never reported)."""
    fresh, cached, out = (
        o.usage.get('input', 0),
        o.usage.get('input_cached_tokens', 0),
        o.usage.get('output', 0),
    )
    return None if not (fresh or cached or out) else (fresh + cached, out)


def steps_in_a_row(requests: list[Obs]) -> int:
    """How many requests the caller waited for one after another: overlapping requests are one step."""
    steps, until = 0, None
    for o in sorted(requests, key=lambda o: o.start):
        end = o.end or o.start
        if until is None or o.start >= until:
            steps += 1
            until = end
        else:
            until = max(until, end)
    return steps


def quantile(values: list[float], p: float) -> float:
    """Nearest rank: the smallest value with at least `p` of the values at or below it."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


@dataclass
class Write:
    name: str
    status: str
    """ok, failed, or unknown when the trace does not show the result."""
    said_before: str
    heard_before: str
    audit: dict[str, Any] = field(default_factory=dict)


@dataclass
class Turn:
    shape: str
    """The requests in time order: C for a classifier, S for a step or conversational model, '-' for none."""
    in_a_row: int
    latency: float | None
    estimated: bool = False


@dataclass
class Call:
    ref: str
    caller_turns: int = 0
    completed: Counter = field(default_factory=Counter)
    cancelled: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    turns: list[Turn] = field(default_factory=list)
    writes: list[Write] = field(default_factory=list)

    @property
    def tasks(self) -> int:
        return sum(1 for w in self.writes if w.status != 'failed' and w.name in TASKS)

    def unmatched(self) -> list[Write]:
        """Audited writes with no question behind them, or a question about another action: none should exist."""
        return [w for w in self.writes if 'about' in w.audit and w.name in TASKS and not _matches(w.audit)]


ANSWERS = {'save_booking': ('booking', 'move_or_add'), 'record_complaint': ('complaint',)}
"""The questions whose yes can authorise each write."""


def _matches(audit: dict[str, Any]) -> bool:
    """Whether an audited write answered a question about it, put on an earlier turn than the answer.

    ponytail: the move-or-add question is worded by the model, so the
    application holds no text for it and an empty question passes there.
    Word it in code (as the read-backs are) to close that.
    """
    asked = audit.get('asked_turn')
    return (
        audit.get('about') in ANSWERS.get(audit.get('action', ''), ())
        and (bool(audit.get('question')) or audit.get('about') == 'move_or_add')
        and asked is not None
        and asked < audit.get('turn', 0)
    )


def _text(value: Any) -> str:
    return value if isinstance(value, str) else str(value or '')


def read_call(ref: str, observations: list[Obs]) -> Call:
    """The ledger of one call, from all of its observations."""
    obs = sorted(observations, key=lambda o: o.start)
    by_id = {o.id: o for o in obs}
    call = Call(ref)

    def turn_of(o: Obs) -> Obs | None:
        at = by_id.get(o.parent or '')
        while at is not None and at.name != 'turn':
            at = by_id.get(at.parent or '')
        return at

    per_turn: dict[str, list[Obs]] = {}
    for o in obs:
        if not is_request(o):
            continue
        used = tokens(o)
        if used is None:
            call.cancelled += 1
            continue
        call.completed[kind(o)] += 1
        call.input_tokens += used[0]
        call.output_tokens += used[1]
        if (turn := turn_of(o)) is not None:
            per_turn.setdefault(turn.id, []).append(o)

    livekit = any(o.name == 'user_turn' for o in obs)
    turns = [o for o in obs if o.name == 'turn']
    call.caller_turns = sum(1 for o in obs if o.name == 'user_turn') if livekit else len(turns)
    for turn in turns:
        requests = per_turn.get(turn.id, [])
        shape = ''.join('C' if kind(o) == 'classifier' else 'S' for o in requests) or '-'
        latency = turn.attrs.get('turn.user_bot_latency_seconds')
        call.turns.append(Turn(shape, steps_in_a_row(requests), None if latency is None else float(latency)))
    if livekit:
        speaking = [o for o in obs if o.name == 'agent_speaking']
        estimates = []
        for user in (o for o in obs if o.name == 'user_turn' and o.end):
            assert user.end is not None
            nxt = next((s for s in speaking if s.start >= user.end), None)
            if nxt is not None:
                delay = float(user.attrs.get('lk.end_of_turn_delay') or 0)
                estimates.append((nxt.start - user.end).total_seconds() + delay)
        for turn, estimate in zip([t for t in call.turns if t.latency is None], estimates, strict=False):
            turn.latency, turn.estimated = estimate, True

    said, heard = '', ''
    for o in obs:
        if o.name == 'tts' and o.input:
            said = _text(o.input)
        elif o.name == 'stt' and o.output:
            heard = _text(o.output)
        elif o.name == 'user_turn' and o.input:
            heard = _text(o.input)
        if o.name == 'write' and 'write.audit' in o.attrs:
            audit = o.attrs['write.audit']
            audit = json.loads(audit) if isinstance(audit, str) else dict(audit)
            call.writes.append(Write(audit['action'], audit['status'], said, heard, audit))
        elif o.type == 'TOOL' and o.name in TASKS:
            out = _text(o.output).lower()
            status = 'unknown' if o.output is None else 'failed' if any(w in out for w in FAILED) else 'ok'
            call.writes.append(Write(o.name, status, said, heard))
    if any(w.audit for w in call.writes):
        call.writes = [w for w in call.writes if w.audit]
    return call


def summary(label: str, calls: list[Call]) -> list[str]:
    """The report lines for one group of calls."""
    lines = [f'== {label}: {len(calls)} calls']
    for c in calls:
        lines.append(
            f'   {c.ref:40} turns={c.caller_turns:3} requests={sum(c.completed.values()):3} '
            f'(classifier {c.completed["classifier"]}, step {c.completed["step"]}) cancelled={c.cancelled:2} '
            f'in={c.input_tokens:6} out={c.output_tokens:5} tasks={c.tasks}'
        )
        for w in c.writes:
            audit = f' answered: {w.audit.get("question", "")!r}' if w.audit else ''
            lines.append(
                f'      {w.name} {w.status}; agent said {w.said_before[-90:]!r}; caller said '
                f'{w.heard_before[-90:]!r}{audit}'
            )
    if not calls:
        return lines
    turns = sum(c.caller_turns for c in calls)
    total_in = sum(c.input_tokens for c in calls)
    completed = sum(sum(c.completed.values()) for c in calls)
    per_call = [c.input_tokens for c in calls]
    tasks = sum(c.tasks for c in calls)
    lines.append(
        f'   input tokens per call: mean {statistics.mean(per_call):.0f} ({min(per_call)} to {max(per_call)}); '
        f'per caller turn {total_in / max(turns, 1):.0f}; output {sum(c.output_tokens for c in calls)}'
    )
    lines.append(
        f'   requests: {completed} completed over {turns} caller turns ({completed / max(turns, 1):.2f} per turn), '
        f'{sum(c.cancelled for c in calls)} cancelled or unreported'
    )
    lines.append(
        f'   tokens per completed task: {total_in / tasks:.0f} ({tasks} tasks)'
        if tasks
        else '   tokens per completed task: undefined, no task completed'
    )
    audited = [w for c in calls for w in c.writes if 'about' in w.audit and w.name in TASKS]
    if audited:
        bad = [w for c in calls for w in c.unmatched()]
        lines.append(
            f'   write audit: {len(audited) - len(bad)} of {len(audited)} writes answered a matching '
            'question put on an earlier turn'
        )
        for w in bad:
            lines.append(f'      NO MATCH {w.name}: {w.audit}')
    elif any(c.writes for c in calls):
        lines.append('   write audit: no application question in these traces; the writes above need reading')
    rows = [t for c in calls for t in c.turns]
    lines.append(
        '   calls in a row per turn: '
        + ', '.join(f'{n}: {k} turns' for n, k in sorted(Counter(t.in_a_row for t in rows).items()))
    )
    timed = [t for t in rows if t.latency is not None]
    if timed:
        mark = ' (estimated from LiveKit spans)' if any(t.estimated for t in timed) else ''
        values = [t.latency for t in timed if t.latency is not None]
        lines.append(
            f'   latency{mark}: p50 {statistics.median(values):.2f}s p90 {quantile(values, 0.9):.2f}s '
            f'max {max(values):.2f}s, {len(values)} of {len(rows)} turns timed'
        )
        by_shape: dict[str, list[float]] = {}
        for t in timed:
            key = (
                'no model'
                if t.shape == '-'
                else (
                    'classifier only'
                    if 'S' not in t.shape
                    else f'{t.in_a_row} in a row' + (', classifier first' if t.shape[0] == 'C' else '')
                )
            )
            by_shape.setdefault(key, []).append(t.latency or 0.0)
        for key, values in sorted(by_shape.items(), key=lambda kv: -len(kv[1])):
            lines.append(
                f'      {key:28} turns={len(values):3} p50 {statistics.median(values):.2f}s '
                f'max {max(values):.2f}s'
            )
    else:
        lines.append('   latency: missing, no turn in these traces records it')
    return lines


def groups(args: list[str]) -> list[tuple[str, list[str]]]:
    """`label ref ref -- label ref`: the label, then its calls, groups split by `--`."""
    out: list[tuple[str, list[str]]] = []
    for arg in args:
        if arg == '--' or not out:
            out.append(('', []))
            if arg == '--':
                continue
        label, refs = out[-1]
        if not label:
            out[-1] = (arg, refs)
        else:
            refs.append(arg)
    if not out or any(not label or not refs for label, refs in out):
        raise SystemExit(__doc__)
    return out


@cache
def _langfuse() -> Any:
    """One client for the whole run. The default timeout is too short for a session's traces."""
    from langfuse import Langfuse

    return Langfuse(timeout=60)


def fetch(ref: str) -> list[Obs]:
    lf = _langfuse()
    kind_, _, ident = ref.partition(':') if ':' in ref else ('session', '', ref)
    ids = [ident] if kind_ == 'trace' else [t.id for t in lf.api.trace.list(session_id=ident, limit=100).data]
    return [Obs.of(o) for tid in ids for o in lf.api.trace.get(tid).observations]


if __name__ == '__main__':
    for label, refs in groups(sys.argv[1:]):
        print('\n'.join(summary(label, [read_call(ref, fetch(ref)) for ref in refs])), flush=True)
