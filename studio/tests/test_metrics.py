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


def call(
    trace: str = 'tr1', agent: str = 'sage-and-stone', llm: str = 'gemini-3.5-flash-lite', at: int = 0
) -> dict:
    """Two turns: a quick one, and one whose LLM request and voice-to-voice are both over their limits."""
    model = {'gen_ai__request__model': llm, 'gen_ai__system': 'google'}
    names = {'slng__agent__id': agent, 'slng__run__id': f'run_{trace}'}
    found = {
        'traceID': trace,
        'spans': [
            span('conv', 'conversation', 0, 20000, **names),
            span('t1', 'turn', 0, 3000, 'conv', turn__number=1, turn__user_bot_latency_seconds=0.91),
            span('s1', 'stt', 180, 120, 't1', gen_ai__request__model='slng-stt', gen_ai__provider__name='slng', metrics__ttfb=0.12),
            span('a1', 'invoke_agent concierge', 300, 560, 't1'),
            span('c1', 'chat gemini-3.5-flash-lite', 300, 520, 'a1', gen_ai__operation__name='chat', **model),
            span('v1', 'tts', 640, 700, 't1', gen_ai__request__model='s2.1-pro', gen_ai__provider__name='fish', metrics__ttfb=0.27),
            span('t2', 'turn', 4000, 3000, 'conv', turn__number=2, turn__user_bot_latency_seconds=1.62, turn__was_interrupted=True),
            span('s2', 'stt', 4210, 130, None, gen_ai__request__model='slng-stt', gen_ai__provider__name='slng', metrics__ttfb=0.45),
            span('c2', 'chat gemini-3.5-flash-lite', 4340, 1640, 't2', gen_ai__operation__name='chat', **model),
            span('c3', 'chat gemini-3.5-flash-lite', 6000, 400, 't2', gen_ai__operation__name='chat', otel__status_code='ERROR', **model),
            span('c4', 'chat gemini-3.5-flash-lite', 6500, 900, 't2', gen_ai__operation__name='chat', gen_ai__client__operation__time_to_first_chunk=0.31, **model),
            span('v2', 'tts', 6600, 3000, 't2', gen_ai__request__model='s2.1-pro', gen_ai__provider__name='fish'),
        ],
    }  # fmt: skip
    for one in found['spans']:
        one['startTime'] += at * US
    return found


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
    assert [r.span for r in second.requests] == ['s2', 'c2', 'c3', 'c4', 'v2'], (
        'an stt span with no parent joins the turn it falls in'
    )
    assert second.requests[-2].first_byte_ms == 310, 'a streamed model request has its first chunk'
    assert second.requests[-1].level == 'ok' and not any('3000' in f.what for f in second.flags), (
        'a TTS request with no first byte is not judged on the length of its audio'
    )
    assert (first.run, first.agent) == ('run_tr1', 'sage-and-stone'), 'each turn names its run and agent'

    said = [f.what for f in second.flags]
    assert said[0] == 'voice to voice 1620 ms', said
    assert (
        'STT first byte 450 ms · slng-stt' in said and 'LLM request 1640 ms · gemini-3.5-flash-lite' in said
    ), said
    assert 'LLM error · gemini-3.5-flash-lite' in said and '3 LLM requests in one turn' in said, said
    assert all(f.level == 'bad' for f in second.flags if 'requests in one turn' not in f.what)


def per_model_and_summary():
    timings = metrics.turns(call(), metrics.Limits())
    stats = {(s.kind, s.model): s for s in metrics.models(timings)}
    llm = stats[('llm', 'gemini-3.5-flash-lite')]
    assert (llm.requests, llm.runs, llm.errors, round(llm.over_limit, 2)) == (4, 1, 1, 0.5), llm
    assert llm.first_byte_p90_ms == 310, (
        'only requests that streamed have a first token: no total passed off as one'
    )
    assert stats[('stt', 'slng-stt')].first_byte_p90_ms == 417, stats[('stt', 'slng-stt')]
    assert [s.kind for s in metrics.models(timings)] == ['stt', 'llm', 'tts'], 'STT, LLM, TTS in that order'
    whole = metrics.summary(timings)
    assert (whole.turns, whole.runs, whole.over_limit, whole.errors, whole.mixed) == (2, 1, 0.5, 1, []), whole
    assert whole.voice_to_voice_p90_ms == 1549, whole
    assert (whole.llm_first_token_p90_ms, whole.tts_first_audio_p90_ms) == (310, 270), whole


def a_swapped_model_is_two_rows():
    """The LLM changed between two runs: the table keeps them apart, and the overview says the period mixes them."""
    before = metrics.turns(call('tr1', llm='gemini-3.1-flash-lite'), metrics.Limits())
    after = metrics.turns(call('tr2', llm='gemini-3.5-flash-lite', at=86_400_000), metrics.Limits())
    rows = [s for s in metrics.models(before + after) if s.kind == 'llm']
    assert [r.model for r in rows] == ['gemini-3.1-flash-lite', 'gemini-3.5-flash-lite'], 'oldest first'
    assert rows[0].last_seen < rows[1].first_seen and all(r.runs == 1 for r in rows), rows
    assert metrics.summary(before + after).mixed == ['llm']


def otlp(*traces: dict) -> dict:
    """Traces written as Jaeger's API v3 answers them: OTLP JSON, nanosecond strings, typed attribute values."""

    def value(v: object) -> dict:
        if isinstance(v, bool):
            return {'boolValue': v}
        if isinstance(v, int):
            return {'intValue': str(v)}
        if isinstance(v, float):
            return {'doubleValue': v}
        return {'stringValue': str(v)}

    spans = [
        {
            'traceId': trace['traceID'],
            'spanId': s['spanID'],
            'parentSpanId': s['references'][0]['spanID'] if s['references'] else '',
            'name': s['operationName'],
            'startTimeUnixNano': str(s['startTime'] * 1000),
            'endTimeUnixNano': str((s['startTime'] + s['duration']) * 1000),
            'attributes': [
                {'key': t['key'], 'value': value(t['value'])}
                for t in s['tags']
                if t['key'] != 'otel.status_code'
            ],
            'status': {'code': 2} if any(t['key'] == 'otel.status_code' for t in s['tags']) else {},
        }
        for trace in traces
        for s in trace['spans']
    ]
    return {
        'result': {
            'resourceSpans': [
                {'resource': {'attributes': []}, 'scopeSpans': [{'scope': {'name': 'x'}, 'spans': spans}]}
            ]
        }
    }


def api_v3_becomes_the_same_spans():
    """Jaeger 2's query API (v3, OTLP JSON) read back into the same traces the timing code reads."""
    [back] = metrics.traces_of(otlp(call()))
    original = call()
    assert back['traceID'] == 'tr1' and len(back['spans']) == len(original['spans'])
    assert metrics.turns(back, metrics.Limits()) == metrics.turns(original, metrics.Limits()), (
        'the same turns'
    )


def collect_reads_jaegers_query_api():
    asked: list[str] = []

    def fetch(url: str) -> dict:
        asked.append(url)
        return otlp(call(), call('tr2', agent='faro'))

    found = metrics.collect('http://127.0.0.1:16686', fetch=fetch)
    assert asked[0].startswith(
        'http://127.0.0.1:16686/api/v3/traces?query.service_name=slng-voice-harness&'
    ), asked
    assert 'query.num_traces=50' in asked[0] and 'query.attributes' not in asked[0], asked
    assert found.scope == 'overview' and found.summary.runs == 2 and found.summary.turns == 4
    assert {r.agent for r in found.runs} == {'sage-and-stone', 'faro'}

    asked.clear()
    metrics.collect('http://127.0.0.1:16686', agent='faro', fetch=fetch)
    assert 'query.attributes=%7B%22slng.agent.id%22%3A+%22faro%22%7D' in asked[0], (
        'one agent is asked of Jaeger itself'
    )

    def one(url: str) -> dict:
        asked.append(url)
        return otlp(call('tr9', agent='faro'))

    run = metrics.collect_run('http://127.0.0.1:16686', 'tr9', fetch=one)
    assert asked[-1] == 'http://127.0.0.1:16686/api/v3/traces/tr9'
    assert run.scope == 'run' and run.agent == '' and [r.agent for r in run.runs] == ['faro'], (
        "a run's view has no filter: it names its own agent"
    )


if __name__ == '__main__':
    a_turn_holds_its_own_requests_and_flags()
    per_model_and_summary()
    a_swapped_model_is_two_rows()
    api_v3_becomes_the_same_spans()
    collect_reads_jaegers_query_api()
    print('metrics ok: turns from spans, flags over limits, p90s, per model and swaps, one agent, one run')
