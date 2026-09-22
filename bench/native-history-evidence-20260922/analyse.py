"""Reproduce historical native Pipecat metrics without pooling models or arms."""

import gzip
import importlib
import json
from pathlib import Path

from bench.turns import _ttft

HERE = Path(__file__).parent
measurement = importlib.import_module('bench.pipecat-repair-evidence.measure')
COHORTS = {
    'native_gemini31_20260921': ['787cfb6233eb03adda2fd399a02d23ec', 'f1c4f999e516a7792dfdbed72fa8262d'],
    'native_gpt56_20260916': ['b50d268c197cb9bb12693bccd76d487b', 'f24292e7ef17eb57497f11cadbb81821'],
}


def metrics(spans: list[dict]) -> dict:
    """Report orphan requests; never interpret missing ancestry as avoided work."""
    result = measurement.measure(spans)
    requests = [s for s in spans if s['name'] == 'llm']
    result['all_request_reported_ttfb_seconds'] = measurement.distribution(
        [value for s in requests if (value := _ttft(s)) is not None]
    )
    result['requests_per_caller_turn'] = (
        None if result['unresolved_requests'] else len(requests) / result['caller_turns']
    )
    result['caller_turns_without_latency'] = result['caller_turns'] - result['caller_to_bot_seconds']['n']
    return result


def main() -> None:
    """Check complete retrieval and the material ordering finding, then save metrics."""
    with gzip.open(HERE / 'source.json.gz', 'rt') as file:
        traces = json.load(file)
    spans = [s for t in traces.values() for s in t['observations']]
    assert len(spans) == len({s['id'] for s in spans}) == 207
    assert all(not t['retrieval']['truncated'] for t in traces.values())
    assert all(t['retrieval']['fetchedItems'] == len(t['observations']) for t in traces.values())
    by_id = {s['id']: s for s in spans}
    save = by_id['6ea73abbe69ea1ec']
    readback = by_id['e46e8a011256dd81']
    agreement = by_id['bc23b96f201c1469']
    assert save['input']['confirmed'] is True
    assert save['output'][0]['status'] == 'ok'
    assert save['startTime'] < readback['startTime'] < agreement['startTime']
    assert save['endTime'] < agreement['startTime']
    assert agreement['output'] == 'Yes, please.'
    result = {
        'per_trace': {key: metrics(t['observations']) for key, t in traces.items()},
        'cohorts': {
            name: metrics([s for key in ids for s in traces[key]['observations']])
            for name, ids in COHORTS.items()
        },
        'current_poc': json.loads((HERE.parent / 'microphone-evidence-20260922/metrics.json').read_text())[
            'pooled'
        ],
    }
    assert result['cohorts']['native_gemini31_20260921']['model_requests'] == 26
    assert result['cohorts']['native_gpt56_20260916']['unresolved_requests'] == 6
    (HERE / 'metrics.json').write_text(json.dumps(result, indent=2) + '\n')
    print('207 observations; complete retrieval; model-separated metrics; premature-save ordering verified.')


if __name__ == '__main__':
    main()
