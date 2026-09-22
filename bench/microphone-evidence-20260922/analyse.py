"""Rebuild the four-call microphone review with the existing latency reader."""

import gzip
import importlib
import json
from collections import Counter
from pathlib import Path

from bench.turns import _attribute, _ttft, analyse, seconds_between, turn_of

HERE = Path(__file__).parent
measurement = importlib.import_module('bench.pipecat-repair-evidence.measure')


def metrics(spans: list[dict]) -> dict:
    """Keep server timing, service timing and provider requests separate."""
    report = analyse(spans)
    result = measurement.measure(spans)
    result['zero_model_turns'] = report.zero_model_turns
    result['requests_per_caller_turn'] = sum(report.counts) / len(report.turns)
    result['first_submitted_text_seconds'] = measurement.distribution(
        [value for s in spans if isinstance(value := _attribute(s, 'turn_first_text_out'), (int, float))]
    )
    result['service_ttfb_seconds'] = {
        name: measurement.distribution(
            [value for s in spans if s['name'] == name and (value := _ttft(s)) is not None]
        )
        for name in ('stt', 'tts')
    }
    return result


def turns(spans: list[dict]) -> list[dict]:
    """Locate the final transcript-to-agent gap without calling it model time."""
    report = analyse(spans)
    by_id = {s['id']: s for s in spans}
    rows = []
    for turn in sorted(report.turns, key=lambda t: by_id[t]['startTime']):
        children = sorted(
            [s for s in spans if turn_of(s, by_id, set(report.turns)) == turn],
            key=lambda s: s['startTime'],
        )
        answered = [s for s in children if s['name'] == 'agents' and _attribute(s, 'status') == 'answered']
        assert len(answered) == 1
        attempt = answered[0]
        transcripts = [s for s in children if s['name'] == 'stt' and s['endTime'] <= attempt['startTime']]
        assert transcripts
        rows.append(
            {
                'trace_id': by_id[turn]['traceId'],
                'turn_id': turn,
                'turn_number': _attribute(by_id[turn], 'turn.number'),
                'caller_to_bot_seconds': _attribute(by_id[turn], 'turn.user_bot_latency_seconds'),
                'final_stt_to_answering_attempt_seconds': seconds_between(
                    transcripts[-1]['endTime'], attempt['startTime']
                ),
                'attempt_to_first_submitted_text_seconds': _attribute(attempt, 'turn_first_text_out'),
                'model_requests_including_interrupted': report.requests_per_turn[turn],
            }
        )
    return rows


def main() -> None:
    """Check complete retrieval and outcome counts, then write reproducible metrics."""
    with gzip.open(HERE / 'source.json.gz', 'rt') as stream:
        traces = json.load(stream)
    assert len(traces) == 4
    spans = [s for trace in traces.values() for s in trace['observations']]
    assert len(spans) == len({s['id'] for s in spans}) == 244
    for trace in traces.values():
        assert not trace['retrieval']['truncated']
        assert trace['retrieval']['fetchedItems'] == len(trace['observations'])
    result = {
        'quantiles': 'nearest rank ceil(p*n)-1; per-call p90 with n<10 equals the maximum',
        'per_trace': {key: metrics(trace['observations']) for key, trace in traces.items()},
        'pooled': metrics(spans),
        'turns': turns(spans),
        'historical_cohorts': json.loads((HERE.parent / 'pipecat-repair-evidence/metrics.json').read_text())[
            'cohorts'
        ],
        'application_receipts': dict(
            Counter(action for trace in traces.values() for action in trace['call']['application_actions'])
        ),
    }
    assert result['pooled']['caller_turns'] == len(result['turns']) == 29
    assert result['pooled']['model_requests'] == 36
    assert result['pooled']['zero_model_turns'] == 4
    assert result['pooled']['request_ttft_seconds']['n'] == 33
    assert result['pooled']['first_output'] == {'tool': 30, 'text': 3}
    assert sum(trace['call']['state']['writes'] for trace in traces.values()) == 6
    assert all(trace['call']['state']['unresolved'] is None for trace in traces.values())
    (HERE / 'metrics.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result['pooled'], indent=2))


if __name__ == '__main__':
    main()
