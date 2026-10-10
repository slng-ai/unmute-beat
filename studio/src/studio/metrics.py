"""Timings per turn and per model request, read back from the traces in the local Jaeger, with what is slow.

A bot that runs with `OTEL_EXPORTER_OTLP_ENDPOINT` pointed at the studio (`core.observability`)
sends its spans to Jaeger. This module reads them back over Jaeger's query API and turns them into
the numbers a person testing an agent needs to find what is slow: per caller turn, the voice-to-voice
time and each STT, LLM and TTS request inside it; per model, its p50 and p95 and how often it was over
its limit. Every number comes from a span; nothing is estimated. The overview can be narrowed to one
agent (`slng.agent.id` on every span, `core.observability.setup(agent=)`); one run's view is its
own trace, which names its agent. The tiles are p90s; a model swapped between runs is two rows.

Where each number comes from:

    turn             Pipecat's `turn` span: `turn.number`, `turn.user_bot_latency_seconds`
                     (voice to voice: the caller stopped, the bot's first audio), `turn.was_interrupted`
    stt, tts         Pipecat's `stt` and `tts` spans: `metrics.ttfb` (seconds), the model and provider
    llm              Pipecat's `llm` spans with `metrics.ttfb`, or Pydantic AI's model requests
                     (`gen_ai.operation.name` = `chat`) with their duration and, when they streamed, the
                     time to first chunk Pydantic AI writes on them (`gen_ai.client.operation.time_to_first_chunk`)

A request belongs to the turn span it descends from, or else to the turn whose time it falls in.

ponytail: a model request that did not stream (no first chunk) is judged on its duration and leaves
its first-token cell empty rather than passing the total off as one. Limits are defaults for every
agent.
"""

import json
import statistics
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
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
    at: int = 0
    """When it started, in microseconds since the epoch (Jaeger's own unit)."""
    trace: str = ''


class TurnTiming(BaseModel):
    trace: str
    run: str = ''
    agent: str = ''
    turn: int
    voice_to_voice_ms: float | None
    interrupted: bool
    requests: list[Request]
    flags: list[Flag]
    level: Level


class ModelStats(BaseModel):
    """One model's requests, keyed by kind, model and provider: a model swapped between runs is two rows."""

    kind: Kind
    model: str
    provider: str
    requests: int
    runs: int
    """How many runs (traces) it served in."""
    first_byte_p90_ms: float | None
    """Time to first byte (STT, TTS) or first token (LLM) at p90; None when no request of it says."""
    total_p90_ms: float
    errors: int
    over_limit: float
    """The share of its requests over the limit, 0 to 1."""
    first_seen: str
    last_seen: str
    """ISO times of its first and last request in the period."""


class Summary(BaseModel):
    turns: int
    runs: int
    voice_to_voice_p90_ms: float | None
    llm_first_token_p90_ms: float | None
    tts_first_audio_p90_ms: float | None
    stt_first_byte_p90_ms: float | None
    over_limit: float
    """The share of turns with a flag over its limit, 0 to 1."""
    errors: int
    mixed: list[Kind] = []
    """The kinds served by more than one model in the period: their numbers mix models, the table splits them."""


class Run(BaseModel):
    """One run, as Jaeger holds it: one trace, from its conversation span."""

    trace: str
    run: str
    """The studio's run id (`slng.run.id`); '' for a bot started outside the studio's runner."""
    agent: str
    """`slng.agent.id`; '' for a bot whose launcher did not name its agent."""
    started: str
    turns: int


class Metrics(BaseModel):
    source: str
    scope: Literal['overview', 'run']
    agent: str = ''
    """The agent the overview is filtered to; '' for every agent. A run's view names its own agent in `runs`."""
    limits: Limits
    summary: Summary
    runs: list[Run]
    turns: list[TurnTiming]
    models: list[ModelStats]


def tags(span: dict[str, Any]) -> dict[str, Any]:
    return {tag['key']: tag.get('value') for tag in span.get('tags', [])}


FIRST_CHUNK = 'gen_ai.client.operation.time_to_first_chunk'
"""What Pydantic AI writes on a model request span that streamed: its time to first chunk, in seconds."""


def _kind(span: dict[str, Any], tag: dict[str, Any]) -> Kind | None:
    name = span.get('operationName', '')
    if name in ('stt', 'tts', 'llm'):
        return name
    return 'llm' if tag.get('gen_ai.operation.name') == 'chat' else None


def _error(tag: dict[str, Any]) -> bool:
    return tag.get('error') in (True, 'true') or tag.get('otel.status_code') == 'ERROR'


def _seconds(value: Any) -> float | None:
    return float(value) * 1000 if isinstance(value, int | float) and not isinstance(value, bool) else None


def labels(trace: dict[str, Any]) -> tuple[str, str]:
    """The run and agent a trace belongs to: the first span carrying each (every span of a studio run does)."""
    run = agent = ''
    for span in trace.get('spans', []):
        tag = tags(span)
        run = run or str(tag.get('slng.run.id') or '')
        agent = agent or str(tag.get('slng.agent.id') or '')
        if run and agent:
            break
    return run, agent


def turns(trace: dict[str, Any], limits: Limits) -> list[TurnTiming]:
    """Every caller turn in one Jaeger trace, with its requests and flags, in turn order."""
    spans = {span['spanID']: span for span in trace.get('spans', [])}
    run, agent = labels(trace)
    trace_id = trace.get('traceID', '')
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
        first = _seconds(tag.get('metrics.ttfb'))
        if first is None and kind == 'llm':
            first = _seconds(tag.get(FIRST_CHUNK))
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
                at=int(span.get('startTime', 0)),
                trace=trace_id,
            )
        )

    found = []
    for tid, span in turn_spans.items():
        tag = tags(span)
        v2v = _seconds(tag.get('turn.user_bot_latency_seconds'))
        mine = sorted(requests[tid], key=lambda r: r.at)
        flags = _flags(v2v, mine, limits)
        levels = [f.level for f in flags]
        found.append(
            TurnTiming(
                trace=trace_id,
                run=run,
                agent=agent,
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


def p90(values: list[float]) -> float | None:
    """The 90th percentile, inclusive, or None for no values: the number a tile shows."""
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=10, method='inclusive')[8]


def _iso(microseconds: int) -> str:
    return datetime.fromtimestamp(microseconds / 1e6, UTC).isoformat(timespec='seconds')


def models(timings: Iterable[TurnTiming]) -> list[ModelStats]:
    """Per kind, model and provider: requests, runs, first byte and total at p90, errors, share over limit.

    Keyed by the model each span names, so swapping a model between runs gives two rows, each with
    the period it covers (`first_seen`, `last_seen`), rather than one number mixing the two.
    """
    grouped: dict[tuple[Kind, str, str], list[Request]] = defaultdict(list)
    for t in timings:
        for r in t.requests:
            grouped[(r.kind, r.model, r.provider)].append(r)
    stats = []
    for (kind, model, provider), rs in grouped.items():
        firsts = [r.first_byte_ms for r in rs if r.first_byte_ms is not None]
        stats.append(
            ModelStats(
                kind=kind,
                model=model,
                provider=provider,
                requests=len(rs),
                runs=len({r.trace for r in rs}),
                first_byte_p90_ms=p90(firsts),
                total_p90_ms=p90([r.duration_ms for r in rs]) or 0,
                errors=sum(r.error for r in rs),
                over_limit=sum(r.level == 'bad' for r in rs) / len(rs),
                first_seen=_iso(min(r.at for r in rs)),
                last_seen=_iso(max(r.at for r in rs)),
            )
        )
    order = {'stt': 0, 'llm': 1, 'tts': 2}
    return sorted(stats, key=lambda s: (order[s.kind], s.first_seen))


def summary(timings: list[TurnTiming]) -> Summary:
    requests = [r for t in timings for r in t.requests]

    def first(kind: Kind) -> float | None:
        return p90([r.first_byte_ms for r in requests if r.kind == kind and r.first_byte_ms is not None])

    served: dict[Kind, set[str]] = defaultdict(set)
    for r in requests:
        served[r.kind].add(r.model)
    return Summary(
        turns=len(timings),
        runs=len({t.trace for t in timings}),
        voice_to_voice_p90_ms=p90([t.voice_to_voice_ms for t in timings if t.voice_to_voice_ms is not None]),
        llm_first_token_p90_ms=first('llm'),
        tts_first_audio_p90_ms=first('tts'),
        stt_first_byte_p90_ms=first('stt'),
        over_limit=sum(t.level == 'bad' for t in timings) / len(timings) if timings else 0,
        errors=sum(r.error for r in requests),
        mixed=[kind for kind in ('stt', 'llm', 'tts') if len(served[kind]) > 1],
    )


def run_of(trace: dict[str, Any], timings: list[TurnTiming]) -> Run:
    run, agent = labels(trace)
    started = min((span.get('startTime', 0) for span in trace.get('spans', [])), default=0)
    return Run(
        trace=trace.get('traceID', ''),
        run=run,
        agent=agent,
        started=_iso(started),
        turns=len(timings),
    )


type Fetch = Callable[[str], dict[str, Any]]


def fetch_json(url: str) -> dict[str, Any]:
    """One Jaeger API v3 answer, whole: a streamed answer is several JSON objects, merged into one `result`.

    Jaeger says "no traces found" as a 404; that is an empty answer here, not an error.
    """
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            text = response.read().decode()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {'result': {'resourceSpans': []}}
        raise
    decoder, at, spans = json.JSONDecoder(), 0, []
    while at < len(text):
        while at < len(text) and text[at].isspace():
            at += 1
        if at >= len(text):
            break
        found, at = decoder.raw_decode(text, at)
        spans += (found.get('result') or {}).get('resourceSpans') or []
    return {'result': {'resourceSpans': spans}}


def _value(value: dict[str, Any]) -> Any:
    """An OTLP attribute value as a plain one: ints arrive as strings in OTLP JSON."""
    for key, found in value.items():
        if key == 'intValue':
            return int(found)
        if key == 'arrayValue':
            return [_value(v) for v in found.get('values', [])]
        if key == 'kvlistValue':
            return {kv['key']: _value(kv['value']) for kv in found.get('values', [])}
        return found
    return None


def traces_of(answer: dict[str, Any]) -> list[dict[str, Any]]:
    """Jaeger API v3's OTLP JSON as traces of spans: id, name, start and duration in microseconds, parent, tags."""
    traces: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for resource in (answer.get('result') or {}).get('resourceSpans') or []:
        for scope in resource.get('scopeSpans') or []:
            for span in scope.get('spans') or []:
                tag = [{'key': a['key'], 'value': _value(a['value'])} for a in span.get('attributes') or []]
                if (span.get('status') or {}).get('code') in (2, 'STATUS_CODE_ERROR'):
                    tag.append({'key': 'otel.status_code', 'value': 'ERROR'})
                start, end = int(span['startTimeUnixNano']), int(span['endTimeUnixNano'])
                parent = span.get('parentSpanId')
                traces[span['traceId']].append(
                    {
                        'spanID': span['spanId'],
                        'operationName': span.get('name', ''),
                        'startTime': start // 1000,
                        'duration': (end - start) // 1000,
                        'references': [{'refType': 'CHILD_OF', 'spanID': parent}] if parent else [],
                        'tags': tag,
                    }
                )
    return [{'traceID': trace, 'spans': spans} for trace, spans in traces.items()]


_UNITS = {'m': 60, 'h': 3600, 'd': 86400}


def _window(lookback: str) -> tuple[str, str]:
    """`1h`, `24h`, `7d` as the start and end times API v3 asks for."""
    seconds = int(lookback[:-1]) * _UNITS[lookback[-1]] if lookback[-1:] in _UNITS else 3600
    now = datetime.now(UTC)
    return (
        (now - timedelta(seconds=seconds)).isoformat(timespec='seconds').replace('+00:00', 'Z'),
        now.isoformat(timespec='seconds').replace('+00:00', 'Z'),
    )


def _search(jaeger: str, service: str, lookback: str, limit: int, attributes: dict[str, str]) -> str:
    start, end = _window(lookback)
    query = {
        'query.service_name': service,
        'query.start_time_min': start,
        'query.start_time_max': end,
        'query.num_traces': limit,
    }
    if attributes:
        query['query.attributes'] = json.dumps(attributes)
    return f'{jaeger}/api/v3/traces?{urllib.parse.urlencode(query)}'


def _read(
    source: str, scope: Literal['overview', 'run'], agent: str, traces: list[dict[str, Any]], limits: Limits
) -> Metrics:
    per_trace = [(trace, turns(trace, limits)) for trace in traces]
    timings = [t for _, found in per_trace for t in found]
    runs = sorted((run_of(trace, found) for trace, found in per_trace), key=lambda r: r.started, reverse=True)
    return Metrics(
        source=source,
        scope=scope,
        agent=agent,
        limits=limits,
        summary=summary(timings),
        runs=runs,
        turns=timings,
        models=models(timings),
    )


def collect(
    jaeger: str,
    *,
    agent: str = '',
    service: str = 'slng-voice-harness',
    lookback: str = '1h',
    limit: int = 50,
    limits: Limits | None = None,
    fetch: Fetch = fetch_json,
) -> Metrics:
    """The overview: recent traces of `service` in Jaeger, for one agent or all, as turns, per-model stats and a summary.

    One agent is asked of Jaeger itself (API v3's `query.attributes={"slng.agent.id": agent}`), so a
    busy runner's other agents never crowd the chosen one out of `limit`. Raises
    `urllib.error.URLError` (or what `fetch` raises) when Jaeger does not answer.
    """
    limits = limits or Limits()
    found = fetch(_search(jaeger, service, lookback, limit, {'slng.agent.id': agent} if agent else {}))
    return _read(jaeger, 'overview', agent, traces_of(found), limits)


def collect_run(
    jaeger: str, trace: str, *, limits: Limits | None = None, fetch: Fetch = fetch_json
) -> Metrics:
    """One run's view: its trace alone, which names its own agent. Raises as `collect` does."""
    found = traces_of(fetch(f'{jaeger}/api/v3/traces/{urllib.parse.quote(trace)}'))
    if not found:
        raise LookupError(f'Jaeger has no trace {trace!r}')
    return _read(jaeger, 'run', '', found, limits or Limits())


def trace_of_run(
    jaeger: str,
    run: str,
    *,
    service: str = 'slng-voice-harness',
    lookback: str = '24h',
    fetch: Fetch = fetch_json,
) -> str:
    """The trace id of a studio run (`slng.run.id`), or '' when Jaeger has none for it yet."""
    found = traces_of(fetch(_search(jaeger, service, lookback, 1, {'slng.run.id': run})))
    return str(found[0]['traceID']) if found else ''
