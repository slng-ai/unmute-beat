"""Timings per turn and per model request, read back from the traces in the local Jaeger, with what is slow.

A bot that runs with `OTEL_EXPORTER_OTLP_ENDPOINT` pointed at the studio (`core.observability`)
sends its spans to Jaeger. This module reads them back over Jaeger's query API and turns them into
the numbers a person testing an agent needs to find what is slow: per caller turn, the voice-to-voice
time and each STT, LLM and TTS request inside it; per model, its p50 and p95 and how often it was over
its limit. Every number comes from a span; nothing is estimated.

Where each number comes from:

    turn             Pipecat's `turn` span: `turn.number`, `turn.user_bot_latency_seconds`
                     (voice to voice: the caller stopped, the bot's first audio), `turn.was_interrupted`
    stt, tts         Pipecat's `stt` and `tts` spans: `metrics.ttfb` (seconds), the model and provider
    llm              Pipecat's `llm` spans with `metrics.ttfb`, or Pydantic AI's model requests
                     (`gen_ai.operation.name` = `chat`), whose spans carry a duration but no first token

A request belongs to the turn span it descends from, or else to the turn whose time it falls in.

ponytail: the LLM's first token is measured only where Pipecat runs the LLM. The salon's agents run on
Pydantic AI, so their requests are judged on duration until the engine's own `model.request` and first
`model.delta` events (studio.events) give the first token. Limits are defaults for every agent; per
agent limits come with the config page.
"""

import json
import statistics
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any, Literal

from pydantic import BaseModel

type Kind = Literal['stt', 'llm', 'tts']
type Level = Literal['ok', 'slow', 'bad']


class Limit(BaseModel):
    slow: float
    """Above this, the number is shown as slow."""
    bad: float
    """Above this, it is over its limit: the turn or request is flagged."""

    def level(self, value: float) -> Level:
        return 'bad' if value > self.bad else 'slow' if value > self.slow else 'ok'


class Limits(BaseModel):
    """What counts as slow, in milliseconds, except requests per turn, which is a count."""

    voice_to_voice: Limit = Limit(slow=1000, bad=1500)
    stt_first_byte: Limit = Limit(slow=250, bad=400)
    llm_first_token: Limit = Limit(slow=600, bad=700)
    llm_request: Limit = Limit(slow=1000, bad=1500)
    """For an LLM request with no first-token time: its whole duration."""
    tts_first_byte: Limit = Limit(slow=300, bad=400)
    requests_per_turn: Limit = Limit(slow=1, bad=2)


class Flag(BaseModel):
    level: Literal['slow', 'bad']
    what: str
    """In words: `LLM first token 912 ms`."""
    value: float
    limit: float
    span: str = ''


class Request(BaseModel):
    """One STT, LLM or TTS request."""

    kind: Kind
    model: str
    provider: str
    duration_ms: float
    first_byte_ms: float | None
    """Time to first byte (STT, TTS) or first token (LLM), where the span says."""
    error: bool
    span: str
    level: Level


class TurnTiming(BaseModel):
    trace: str
    turn: int
    voice_to_voice_ms: float | None
    interrupted: bool
    requests: list[Request]
    flags: list[Flag]
    level: Level


class ModelStats(BaseModel):
    kind: Kind
    model: str
    provider: str
    calls: int
    p50_ms: float
    p95_ms: float
    errors: int
    over_limit: float
    """The share of its requests over the limit, 0 to 1."""


class Summary(BaseModel):
    turns: int
    voice_to_voice_avg_ms: float | None
    voice_to_voice_p95_ms: float | None
    over_limit: float
    """The share of turns with a flag over its limit, 0 to 1."""
    errors: int


class Metrics(BaseModel):
    source: str
    limits: Limits
    summary: Summary
    turns: list[TurnTiming]
    models: list[ModelStats]


def tags(span: dict[str, Any]) -> dict[str, Any]:
    return {tag['key']: tag.get('value') for tag in span.get('tags', [])}


def _kind(span: dict[str, Any], tag: dict[str, Any]) -> Kind | None:
    name = span.get('operationName', '')
    if name in ('stt', 'tts', 'llm'):
        return name
    return 'llm' if tag.get('gen_ai.operation.name') == 'chat' else None


def _error(tag: dict[str, Any]) -> bool:
    return tag.get('error') in (True, 'true') or tag.get('otel.status_code') == 'ERROR'


def turns(trace: dict[str, Any], limits: Limits) -> list[TurnTiming]:
    """Every caller turn in one Jaeger trace, with its requests and flags, in turn order."""
    spans = {span['spanID']: span for span in trace.get('spans', [])}
    parent = {
        sid: next(
            (ref['spanID'] for ref in span.get('references', []) if ref.get('refType') == 'CHILD_OF'), None
        )
        for sid, span in spans.items()
    }
    turn_spans = {sid: span for sid, span in spans.items() if span.get('operationName') == 'turn'}

    def owner(sid: str) -> str | None:
        seen = set()
        at = parent.get(sid)
        while at and at not in seen:
            if at in turn_spans:
                return at
            seen.add(at)
            at = parent.get(at)
        start = spans[sid].get('startTime', 0)
        return next(
            (
                tid
                for tid, t in turn_spans.items()
                if t.get('startTime', 0) <= start <= t.get('startTime', 0) + t.get('duration', 0)
            ),
            None,
        )

    requests: dict[str, list[Request]] = defaultdict(list)
    for sid, span in spans.items():
        tag = tags(span)
        kind = _kind(span, tag)
        if kind is None or (turn := owner(sid)) is None:
            continue
        ttfb = tag.get('metrics.ttfb')
        first = float(ttfb) * 1000 if isinstance(ttfb, int | float) else None
        duration = span.get('duration', 0) / 1000
        limit = {
            'stt': limits.stt_first_byte,
            'tts': limits.tts_first_byte,
            'llm': limits.llm_first_token if first is not None else limits.llm_request,
        }[kind]
        error = _error(tag)
        requests[turn].append(
            Request(
                kind=kind,
                model=str(tag.get('gen_ai.request.model') or tag.get('gen_ai.response.model') or ''),
                provider=str(tag.get('gen_ai.provider.name') or tag.get('gen_ai.system') or ''),
                duration_ms=duration,
                first_byte_ms=first,
                error=error,
                span=sid,
                level='bad' if error else limit.level(first if first is not None else duration),
            )
        )

    found = []
    for tid, span in turn_spans.items():
        tag = tags(span)
        latency = tag.get('turn.user_bot_latency_seconds')
        v2v = float(latency) * 1000 if isinstance(latency, int | float) else None
        mine = sorted(requests[tid], key=lambda r: spans[r.span].get('startTime', 0))
        flags = _flags(v2v, mine, limits)
        levels = [f.level for f in flags]
        found.append(
            TurnTiming(
                trace=trace.get('traceID', ''),
                turn=int(tag.get('turn.number') or 0),
                voice_to_voice_ms=v2v,
                interrupted=bool(tag.get('turn.was_interrupted')),
                requests=mine,
                flags=flags,
                level='bad' if 'bad' in levels else 'slow' if 'slow' in levels else 'ok',
            )
        )
    return sorted(found, key=lambda t: t.turn)


def _flags(v2v: float | None, requests: list[Request], limits: Limits) -> list[Flag]:
    flags: list[Flag] = []
    if v2v is not None and (level := limits.voice_to_voice.level(v2v)) != 'ok':
        limit = limits.voice_to_voice.bad if level == 'bad' else limits.voice_to_voice.slow
        flags.append(Flag(level=level, what=f'voice to voice {v2v:.0f} ms', value=v2v, limit=limit))
    names = {'stt': 'STT first byte', 'tts': 'TTS first audio', 'llm': 'LLM first token'}
    for r in requests:
        if r.error:
            flags.append(
                Flag(
                    level='bad',
                    what=f'{r.kind.upper()} error · {r.model}',
                    value=r.duration_ms,
                    limit=0,
                    span=r.span,
                )
            )
            continue
        if r.level == 'ok':
            continue
        value = r.first_byte_ms if r.first_byte_ms is not None else r.duration_ms
        label = names[r.kind] if r.first_byte_ms is not None else 'LLM request'
        limit = {
            'stt': limits.stt_first_byte,
            'tts': limits.tts_first_byte,
            'llm': limits.llm_first_token if r.first_byte_ms is not None else limits.llm_request,
        }[r.kind]
        flags.append(
            Flag(
                level=r.level,
                what=f'{label} {value:.0f} ms · {r.model}',
                value=value,
                limit=limit.bad if r.level == 'bad' else limit.slow,
                span=r.span,
            )
        )
    llm = sum(1 for r in requests if r.kind == 'llm')
    if (level := limits.requests_per_turn.level(llm)) != 'ok':
        limit = limits.requests_per_turn.bad if level == 'bad' else limits.requests_per_turn.slow
        flags.append(Flag(level=level, what=f'{llm} LLM requests in one turn', value=llm, limit=limit))
    return flags


def _p(values: list[float], q: float) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method='inclusive')[round(q * 100) - 1]


def models(timings: Iterable[TurnTiming]) -> list[ModelStats]:
    """Per kind and model: calls, p50 and p95 (first byte where known, else duration), errors, share over limit."""
    grouped: dict[tuple[Kind, str, str], list[Request]] = defaultdict(list)
    for t in timings:
        for r in t.requests:
            grouped[(r.kind, r.model, r.provider)].append(r)
    stats = []
    for (kind, model, provider), rs in grouped.items():
        values = [r.first_byte_ms if r.first_byte_ms is not None else r.duration_ms for r in rs]
        stats.append(
            ModelStats(
                kind=kind,
                model=model,
                provider=provider,
                calls=len(rs),
                p50_ms=_p(values, 0.5),
                p95_ms=_p(values, 0.95),
                errors=sum(r.error for r in rs),
                over_limit=sum(r.level == 'bad' for r in rs) / len(rs),
            )
        )
    return sorted(stats, key=lambda s: -s.p95_ms)


def summary(timings: list[TurnTiming]) -> Summary:
    v2v = [t.voice_to_voice_ms for t in timings if t.voice_to_voice_ms is not None]
    return Summary(
        turns=len(timings),
        voice_to_voice_avg_ms=statistics.fmean(v2v) if v2v else None,
        voice_to_voice_p95_ms=_p(v2v, 0.95) if v2v else None,
        over_limit=sum(t.level == 'bad' for t in timings) / len(timings) if timings else 0,
        errors=sum(r.error for t in timings for r in t.requests),
    )


type Fetch = Callable[[str], dict[str, Any]]


def fetch_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def collect(
    jaeger: str,
    *,
    service: str = 'slng-voice-harness',
    lookback: str = '1h',
    limit: int = 50,
    limits: Limits | None = None,
    fetch: Fetch = fetch_json,
) -> Metrics:
    """The recent traces of `service` in Jaeger, as turns, per-model stats and a summary.

    Raises `urllib.error.URLError` (or what `fetch` raises) when Jaeger does not answer.
    """
    limits = limits or Limits()
    query = urllib.parse.urlencode({'service': service, 'lookback': lookback, 'limit': limit})
    found = fetch(f'{jaeger}/api/traces?{query}')
    timings = [t for trace in found.get('data') or [] for t in turns(trace, limits)]
    return Metrics(
        source=jaeger, limits=limits, summary=summary(timings), turns=timings, models=models(timings)
    )
