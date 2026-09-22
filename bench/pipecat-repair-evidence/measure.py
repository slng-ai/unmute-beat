"""Rebuild metrics.json: PYTHONPATH=. python bench/pipecat-repair-evidence/measure.py."""

import gzip
import json
from collections import Counter
from pathlib import Path

from bench.turns import _CACHED, _attribute, analyse, percentile

HERE = Path(__file__).parent
COHORTS = {
    'earliest_3.1': ['ec513a9973c75e4d7effe461113fdcd4', '421a85c24cd39615f2b9eed5ef768d1b'],
    'earlier_3.5': [
        'd19f5384acd01c5d600815698f0572fc',
        'a3fdf1fc7e681995db94fd76de32aae4',
        'b1e329439ef96a79a4e69a029146f03d',
        '710977c673077c2b6068d9bc985fea29',
    ],
    'regressed_3.5': [
        'db4a8f687ac508bf0eb371b294a362f6',
        '9667ac56ac0a6882a84160c3218c984b',
        'de4c934fee4c268a0a696f531f592537',
        '8953247f2ec9501797574acfea819478',
    ],
    'current_voice_3.5': [
        '532451c178735dfa1379c5fcb764067e',
        '14036c1370b57cd94c313b5942b79815',
        '3e1a3d340c99f52ce879923d38bb0612',
        '055ff5320eee2ce481654acbc7051225',
    ],
}


def distribution(values):
    return {
        'n': len(values),
        'p50': percentile(values, 0.5),
        'p90': percentile(values, 0.9),
        'max': max(values, default=None),
    }


def measure(spans):
    report = analyse(spans)
    requests = [s for s in spans if s['type'] == 'GENERATION' and s['name'] not in ('stt', 'tts')]
    usage = [s.get('usageDetails') or {} for s in requests]
    tools = [s for s in spans if s['type'] == 'TOOL']
    attempts = [s for s in spans if s['name'] == 'agents']
    return {
        'models': dict(Counter(s['model'] for s in requests)),
        'caller_turns': len(report.turns),
        'model_requests': len(requests),
        'resolved_requests': sum(report.counts),
        'unresolved_requests': report.unresolved_requests,
        'attempts': sum(report.attempts_per_turn.values()),
        'extra_attempts': report.superseded_attempts,
        'attempt_statuses': dict(Counter(str(_attribute(s, 'status')) for s in attempts)),
        'explicitly_interrupted_attempts': sum(_attribute(s, 'interrupted') is True for s in attempts),
        'repair_turns': len(report.repair_turns),
        'caller_to_bot_seconds': distribution(report.user_bot_latency),
        'request_ttft_seconds': distribution(report.request_ttft),
        'ttft_source': report.ttft_source,
        'first_output': dict(report.first_output),
        'input_tokens_resolved': report.input_tokens,
        'output_tokens_resolved': report.output_tokens,
        'reported_cached_tokens_resolved': report.cached_tokens,
        'requests_with_usage': sum(bool(u) for u in usage),
        'requests_with_explicit_cache_field': sum(any(k in u for k in _CACHED) for u in usage),
        'tool_events': len(tools),
        'tool_output_characters': distribution(
            [s['tool_output_characters'] for s in tools if 'tool_output_characters' in s]
        ),
        'tool_output_bytes': distribution(
            [s['tool_output_bytes'] for s in tools if 'tool_output_bytes' in s]
        ),
    }


if __name__ == '__main__':
    with gzip.open(HERE / 'trace-metrics-source.json.gz', 'rt') as file:
        traces = json.load(file)
    assert len({t for ids in COHORTS.values() for t in ids}) == 14
    result = {
        'quantiles': 'nearest rank ceil(p*n)-1; absent measurements stay absent',
        'per_trace': {t: measure(v['observations']) for t, v in traces.items()},
        'cohorts': {
            name: measure([s for t in ids for s in traces[t]['observations']])
            for name, ids in COHORTS.items()
        },
        'trace_ids': COHORTS,
    }
    (HERE / 'metrics.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result['cohorts'], indent=2))
