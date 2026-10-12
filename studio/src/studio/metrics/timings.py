"""Timings per turn and per model request, read back from the traces in the local Jaeger, with what is slow.

A bot that runs with `OTEL_EXPORTER_OTLP_ENDPOINT` pointed at the studio (`core.observability`)
sends its spans to Jaeger, on Pipecat or on LiveKit. This module reads them back (`studio.metrics.jaeger`) and turns them into
the numbers a person testing an agent needs to find what is slow: per caller turn, the voice-to-voice
time and each STT, LLM and TTS request inside it; per model, its p90s and how often it was over its
limit. Every number comes from a span; nothing is estimated. The overview can be narrowed to one
agent (`slng.agent.id` on every span, `core.observability.setup(agent=)`); one run's view is its
own trace, which names its agent. The tiles are p90s; a model swapped between runs is two rows.

Where each number comes from:

    turn             Pipecat's `turn` span: `turn.number`, `turn.user_bot_latency_seconds`
                     (voice to voice: the caller stopped, the bot's first audio), `turn.was_interrupted`;
                     or on LiveKit, unmute-core's `turn` span (`core.livekit.CallTrace`), the same
                     numbers as `turn.e2e_seconds` and `turn.interrupted`
    stt, tts         `stt` and `tts` spans, Pipecat's or unmute-core's: `metrics.ttfb` (seconds), the
                     model and provider
    llm              Pipecat's `llm` spans with `metrics.ttfb`, or Pydantic AI's model requests
                     (`gen_ai.operation.name` = `chat`) with their duration and, when they streamed, the
                     time to first chunk Pydantic AI writes on them (`gen_ai.client.operation.time_to_first_chunk`)

A request belongs to the turn span it descends from, or else to the turn whose time it falls in.

ponytail: a model request that did not stream (no first chunk) is judged on its duration and leaves
its first-token cell empty rather than passing the total off as one. Limits are defaults for every
agent.
"""

import statistics
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from studio.metrics.jaeger import Jaeger, Traces
from studio.metrics.models import (
    Flag,
    Kind,
    Limits,
    Metrics,
    ModelStats,
    Request,
    Run,
    Summary,
    TurnTiming,
)


class SpanTags:
    """One span's tags, read as the timings need them."""

    FIRST_CHUNK = 'gen_ai.client.operation.time_to_first_chunk'

    def __init__(self, span: dict[str, Any]) -> None:
        self.span = span
        self.tags = {tag['key']: tag.get('value') for tag in span.get('tags', [])}

    def kind(self) -> Kind | None:
        """The request this span is (`stt`, `tts`, `llm`, or a Pydantic AI chat), or None."""
        name = self.span.get('operationName', '')
        if name in ('stt', 'tts', 'llm'):
            return name
        return 'llm' if self.tags.get('gen_ai.operation.name') == 'chat' else None

    def error(self) -> bool:
        """Whether the span failed."""
        return self.tags.get('error') in (True, 'true') or self.tags.get('otel.status_code') == 'ERROR'

    def milliseconds(self, key: str) -> float | None:
        """A tag in seconds as milliseconds, or None when it is missing or not a number."""
        value = self.tags.get(key)
        return float(value) * 1000 if isinstance(value, int | float) and not isinstance(value, bool) else None

    def first_byte(self, kind: Kind) -> float | None:
        """Time to first byte, or for an LLM that streamed, to its first chunk."""
        first = self.milliseconds('metrics.ttfb')
        return self.milliseconds(self.FIRST_CHUNK) if first is None and kind == 'llm' else first

    def model(self) -> tuple[str, str]:
        """The model and provider the span names."""
        tags = self.tags
        model = tags.get('gen_ai.request.model') or tags.get('gen_ai.response.model') or ''
        return str(model), str(tags.get('gen_ai.provider.name') or tags.get('gen_ai.system') or '')


class TraceTimings:
    """Every caller turn in one trace, with its requests and flags, in turn order.

    A request is judged on its first byte (first token); an LLM request with none, on its duration.
    An STT or TTS request with no first byte has nothing to judge: its length is the audio's, not a wait.
    """

    NAMES: ClassVar[dict[Kind, str]] = {
        'stt': 'STT first byte',
        'tts': 'TTS first audio',
        'llm': 'LLM first token',
    }

    def __init__(self, trace: dict[str, Any], limits: Limits) -> None:
        self.trace = trace
        self.limits = limits
        self.id = trace.get('traceID', '')
        self.run, self.agent = Traces.labels(trace)
        self.spans = {span['spanID']: span for span in trace.get('spans', [])}
        self.parents = {sid: self._parent(span) for sid, span in self.spans.items()}
        self.turn_spans = {
            sid: span for sid, span in self.spans.items() if span.get('operationName') == 'turn'
        }

    @staticmethod
    def _parent(span: dict[str, Any]) -> str | None:
        """The span's parent, if it names one."""
        return next(
            (ref['spanID'] for ref in span.get('references', []) if ref.get('refType') == 'CHILD_OF'), None
        )

    def turns(self) -> list[TurnTiming]:
        """Every turn, with the requests that belong to it."""
        owned: dict[str, list[Request]] = defaultdict(list)
        for sid, span in self.spans.items():
            request = self._request(sid, span)
            if request is not None and (turn := self._owner(sid)) is not None:
                owned[turn].append(request)
        found = [
            self._turn(tid, span, sorted(owned[tid], key=lambda r: r.at))
            for tid, span in self.turn_spans.items()
        ]
        return sorted(found, key=lambda turn: turn.turn)

    def _owner(self, sid: str) -> str | None:
        """The turn a span belongs to: the turn span it descends from, or the one whose time it falls in."""
        seen: set[str] = set()
        at = self.parents.get(sid)
        while at and at not in seen:
            if at in self.turn_spans:
                return at
            seen.add(at)
            at = self.parents.get(at)
        start = self.spans[sid].get('startTime', 0)
        return next(
            (
                tid
                for tid, turn in self.turn_spans.items()
                if turn.get('startTime', 0) <= start <= turn.get('startTime', 0) + turn.get('duration', 0)
            ),
            None,
        )

    def _request(self, sid: str, span: dict[str, Any]) -> Request | None:
        """The span as a request, judged against its limit; None when it is no request."""
        tags = SpanTags(span)
        kind = tags.kind()
        if kind is None:
            return None
        first, duration, error = tags.first_byte(kind), span.get('duration', 0) / 1000, tags.error()
        measured = first if first is not None else duration if kind == 'llm' else None
        limit = self.limits.for_request(kind, streamed=first is not None)
        model, provider = tags.model()
        return Request(
            kind=kind,
            model=model,
            provider=provider,
            duration_ms=duration,
            first_byte_ms=first,
            error=error,
            span=sid,
            level='bad' if error else 'ok' if measured is None else limit.level(measured),
            at=int(span.get('startTime', 0)),
            trace=self.id,
        )

    def _turn(self, tid: str, span: dict[str, Any], requests: list[Request]) -> TurnTiming:
        """One turn span, its requests and its flags."""
        tags = SpanTags(span)
        v2v = tags.milliseconds('turn.user_bot_latency_seconds')
        if v2v is None:
            v2v = tags.milliseconds('turn.e2e_seconds')
        flags = self._flags(v2v, requests)
        levels = {flag.level for flag in flags}
        return TurnTiming(
            trace=self.id,
            run=self.run,
            agent=self.agent,
            turn=int(tags.tags.get('turn.number') or 0),
            voice_to_voice_ms=v2v,
            interrupted=bool(tags.tags.get('turn.was_interrupted') or tags.tags.get('turn.interrupted')),
            requests=requests,
            flags=flags,
            level='bad' if 'bad' in levels else 'slow' if 'slow' in levels else 'ok',
        )

    def _flags(self, v2v: float | None, requests: list[Request]) -> list[Flag]:
        """What in a turn is over its limit: voice to voice, each request, and how many LLM requests it took."""
        limits, flags = self.limits, []
        if v2v is not None and (level := limits.voice_to_voice.level(v2v)) != 'ok':
            flags.append(
                Flag(
                    level=level,
                    what=f'voice to voice {v2v:.0f} ms',
                    value=v2v,
                    limit=limits.voice_to_voice.crossed(level),
                )
            )
        flags += [
            self._request_flag(request) for request in requests if request.error or request.level != 'ok'
        ]
        llm = sum(request.kind == 'llm' for request in requests)
        if (level := limits.requests_per_turn.level(llm)) != 'ok':
            flags.append(
                Flag(
                    level=level,
                    what=f'{llm} LLM requests in one turn',
                    value=llm,
                    limit=limits.requests_per_turn.crossed(level),
                )
            )
        return flags

    def _request_flag(self, request: Request) -> Flag:
        """One request over its limit, or in error."""
        if request.error:
            return Flag(
                level='bad',
                what=f'{request.kind.upper()} error · {request.model}',
                value=request.duration_ms,
                limit=0,
                span=request.span,
            )
        streamed = request.first_byte_ms is not None
        value = request.first_byte_ms if request.first_byte_ms is not None else request.duration_ms
        label = self.NAMES[request.kind] if streamed else 'LLM request'
        level: Literal['slow', 'bad'] = 'bad' if request.level == 'bad' else 'slow'
        limit = self.limits.for_request(request.kind, streamed).crossed(level)
        return Flag(
            level=level,
            what=f'{label} {value:.0f} ms · {request.model}',
            value=value,
            limit=limit,
            span=request.span,
        )

    def run_row(self, turns: int) -> Run:
        """The trace as a run, as the Metrics page lists it."""
        started = min((span.get('startTime', 0) for span in self.spans.values()), default=0)
        return Run(trace=self.id, run=self.run, agent=self.agent, started=Report.iso(started), turns=turns)


class Report:
    """The Metrics page's answer for a set of traces: every turn, the runs, per-model stats and the tiles."""

    ORDER: ClassVar[dict[Kind, int]] = {'stt': 0, 'llm': 1, 'tts': 2}

    def __init__(self, source: str, scope: Literal['overview', 'run'], agent: str, limits: Limits) -> None:
        self.source = source
        self.scope = scope
        self.agent = agent
        self.limits = limits

    def of(self, traces: list[dict[str, Any]]) -> Metrics:
        """The report, one studio run's traces read as one."""
        timed = [
            (timings, timings.turns())
            for timings in (TraceTimings(trace, self.limits) for trace in Traces.by_run(traces))
        ]
        turns = [turn for _, found in timed for turn in found]
        runs = sorted(
            (timings.run_row(len(found)) for timings, found in timed),
            key=lambda run: run.started,
            reverse=True,
        )
        return Metrics(
            source=self.source,
            scope=self.scope,
            agent=self.agent,
            limits=self.limits,
            summary=self.summary(turns),
            runs=runs,
            turns=turns,
            models=self.models(turns),
        )

    @staticmethod
    def p90(values: list[float]) -> float | None:
        """The 90th percentile, inclusive, or None for no values: the number a tile shows."""
        if len(values) < 2:
            return values[0] if values else None
        return statistics.quantiles(values, n=10, method='inclusive')[8]

    @staticmethod
    def iso(microseconds: int) -> str:
        """Jaeger's microseconds as an ISO time."""
        return datetime.fromtimestamp(microseconds / 1e6, UTC).isoformat(timespec='seconds')

    def models(self, turns: Iterable[TurnTiming]) -> list[ModelStats]:
        """Per kind, model and provider: requests, runs, first byte and total at p90, errors, share over limit."""
        grouped: dict[tuple[Kind, str, str], list[Request]] = defaultdict(list)
        for request in (request for turn in turns for request in turn.requests):
            grouped[(request.kind, request.model, request.provider)].append(request)
        stats = [
            self._model(kind, model, provider, requests)
            for (kind, model, provider), requests in grouped.items()
        ]
        return sorted(stats, key=lambda stat: (self.ORDER[stat.kind], stat.first_seen))

    def _model(self, kind: Kind, model: str, provider: str, requests: list[Request]) -> ModelStats:
        """One model's row."""
        return ModelStats(
            kind=kind,
            model=model,
            provider=provider,
            requests=len(requests),
            runs=len({request.trace for request in requests}),
            first_byte_p90_ms=self.p90([r.first_byte_ms for r in requests if r.first_byte_ms is not None]),
            total_p90_ms=self.p90([r.duration_ms for r in requests]) or 0,
            errors=sum(r.error for r in requests),
            over_limit=sum(r.level == 'bad' for r in requests) / len(requests),
            first_seen=self.iso(min(r.at for r in requests)),
            last_seen=self.iso(max(r.at for r in requests)),
        )

    def summary(self, turns: list[TurnTiming]) -> Summary:
        """The tiles over these turns."""
        requests = [request for turn in turns for request in turn.requests]
        served = {kind: {r.model for r in requests if r.kind == kind} for kind in self.ORDER}

        def first(kind: Kind) -> float | None:
            """A kind's first byte at p90."""
            return self.p90(
                [r.first_byte_ms for r in requests if r.kind == kind and r.first_byte_ms is not None]
            )

        mixed: list[Kind] = [kind for kind in ('stt', 'llm', 'tts') if len(served[kind]) > 1]
        return Summary(
            turns=len(turns),
            runs=len({turn.trace for turn in turns}),
            voice_to_voice_p90_ms=self.p90(
                [t.voice_to_voice_ms for t in turns if t.voice_to_voice_ms is not None]
            ),
            llm_first_token_p90_ms=first('llm'),
            tts_first_audio_p90_ms=first('tts'),
            stt_first_byte_p90_ms=first('stt'),
            over_limit=sum(turn.level == 'bad' for turn in turns) / len(turns) if turns else 0,
            errors=sum(r.error for r in requests),
            mixed=mixed,
        )


class MetricsReader:
    """The Metrics page's questions, asked of Jaeger: the overview, one studio run, one trace."""

    def __init__(self, jaeger: Jaeger, limits: Limits | None = None) -> None:
        self.jaeger = jaeger
        self.limits = limits or Limits()

    def collect(self, *, agent: str = '', run: str = '', lookback: str = '1h', limit: int = 50) -> Metrics:
        """Recent traces, for one agent or all; or with `run`, that studio run's view, every trace it wrote as one.

        Raises `urllib.error.URLError` (or what the fetch raises) when Jaeger does not answer.
        """
        if run:
            traces = self.jaeger.search(lookback, limit, {'slng.run.id': run})
            return Report(self.jaeger.url, 'run', '', self.limits).of(traces)
        traces = self.jaeger.search(lookback, limit, {'slng.agent.id': agent} if agent else {})
        return Report(self.jaeger.url, 'overview', agent, self.limits).of(traces)

    def collect_run(self, trace: str) -> Metrics:
        """One run's view: its trace alone, which names its own agent. Raises `LookupError` when Jaeger has none."""
        found = self.jaeger.trace(trace)
        if not found:
            raise LookupError(f'Jaeger has no trace {trace!r}')
        return Report(self.jaeger.url, 'run', '', self.limits).of(found)

    def trace_of_run(self, run: str, lookback: str = '24h') -> str:
        """The trace id of a studio run (`slng.run.id`), or '' when Jaeger has none for it yet."""
        found = Traces.by_run(self.jaeger.search(lookback, 20, {'slng.run.id': run}))
        return str(found[0]['traceID']) if found else ''
