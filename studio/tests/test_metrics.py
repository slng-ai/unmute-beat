"""Run: uv run python -m tests.test_metrics

Turn and request timings from a trace shaped as Jaeger's query API returns it, with the span names
and attributes Pipecat and Pydantic AI write (checked against pipecat.utils.tracing): a turn span with
`turn.user_bot_latency_seconds`, `stt` and `tts` spans with `metrics.ttfb` in seconds, and a Pydantic AI
model request (`gen_ai.operation.name` = `chat`) with a duration and no first token.
"""

from studio import metrics

US = 1000
"""Jaeger times are microseconds; these spans are written in milliseconds times this."""


def span(sid: str, name: str, start: int, duration: int, parent: str | None = None, **tags: object) -> dict:
    return {
        'spanID': sid,
        'operationName': name,
        'startTime': start * US,
        'duration': duration * US,
        'references': [{'refType': 'CHILD_OF', 'spanID': parent}] if parent else [],
        'tags': [{'key': key.replace('__', '.'), 'value': value} for key, value in tags.items()],
    }


def call() -> dict:
    """Two turns: a quick one, and one whose LLM request and voice-to-voice are both over their limits."""
    model = {'gen_ai__request__model': 'gemini-3.5-flash-lite', 'gen_ai__system': 'google'}
    return {
        'traceID': 'tr1',
        'spans': [
            span('conv', 'conversation', 0, 20000),
            span('t1', 'turn', 0, 3000, 'conv', turn__number=1, turn__user_bot_latency_seconds=0.91),
            span('s1', 'stt', 180, 120, 't1', gen_ai__request__model='slng-stt', gen_ai__provider__name='slng', metrics__ttfb=0.12),
            span('a1', 'invoke_agent concierge', 300, 560, 't1'),
            span('c1', 'chat gemini-3.5-flash-lite', 300, 520, 'a1', gen_ai__operation__name='chat', **model),
            span('v1', 'tts', 640, 700, 't1', gen_ai__request__model='s2.1-pro', gen_ai__provider__name='fish', metrics__ttfb=0.27),
            span('t2', 'turn', 4000, 3000, 'conv', turn__number=2, turn__user_bot_latency_seconds=1.62, turn__was_interrupted=True),
            span('s2', 'stt', 4210, 130, None, gen_ai__request__model='slng-stt', gen_ai__provider__name='slng', metrics__ttfb=0.45),
            span('c2', 'chat gemini-3.5-flash-lite', 4340, 1640, 't2', gen_ai__operation__name='chat', **model),
            span('c3', 'chat gemini-3.5-flash-lite', 6000, 400, 't2', gen_ai__operation__name='chat', otel__status_code='ERROR', **model),
        ],
    }  # fmt: skip


def a_turn_holds_its_own_requests_and_flags():
    first, second = metrics.turns(call(), metrics.Limits())
    assert (first.turn, first.voice_to_voice_ms, first.level, first.flags) == (1, 910, 'ok', []), first
    assert [r.kind for r in first.requests] == ['stt', 'llm', 'tts'], 'the agent run span is no request'
    stt, llm, tts = first.requests
    assert (stt.first_byte_ms, stt.model, stt.provider) == (120, 'slng-stt', 'slng')
    assert (llm.first_byte_ms, llm.duration_ms, llm.provider) == (None, 520, 'google'), (
        'no first token: judged on duration'
    )
    assert tts.first_byte_ms == 270

    assert second.interrupted and second.level == 'bad'
    assert [r.span for r in second.requests] == ['s2', 'c2', 'c3'], (
        'an stt span with no parent joins the turn it falls in'
    )
    said = [f.what for f in second.flags]
    assert said[0] == 'voice to voice 1620 ms', said
    assert (
        'STT first byte 450 ms · slng-stt' in said and 'LLM request 1640 ms · gemini-3.5-flash-lite' in said
    ), said
    assert 'LLM error · gemini-3.5-flash-lite' in said and '2 LLM requests in one turn' in said, said
    assert all(f.level == 'bad' for f in second.flags if 'requests in one turn' not in f.what)


def per_model_and_summary():
    timings = metrics.turns(call(), metrics.Limits())
    stats = {(s.kind, s.model): s for s in metrics.models(timings)}
    llm = stats[('llm', 'gemini-3.5-flash-lite')]
    assert (llm.calls, llm.errors, round(llm.over_limit, 2)) == (3, 1, 0.67), llm
    assert stats[('stt', 'slng-stt')].p50_ms == 285, stats[('stt', 'slng-stt')]
    whole = metrics.summary(timings)
    assert (whole.turns, whole.voice_to_voice_avg_ms, whole.over_limit, whole.errors) == (2, 1265, 0.5, 1), (
        whole
    )


def collect_reads_jaegers_query_api():
    asked: list[str] = []

    def fetch(url: str) -> dict:
        asked.append(url)
        return {'data': [call()]}

    found = metrics.collect('http://127.0.0.1:16686', fetch=fetch)
    assert asked == ['http://127.0.0.1:16686/api/traces?service=slng-voice-harness&lookback=1h&limit=50'], (
        asked
    )
    assert found.summary.turns == 2 and found.models[0].p95_ms >= found.models[-1].p95_ms, (
        'slowest model first'
    )


if __name__ == '__main__':
    a_turn_holds_its_own_requests_and_flags()
    per_model_and_summary()
    collect_reads_jaegers_query_api()
    print('metrics ok: turns from spans, flags over limits, per model, summary, the query')
