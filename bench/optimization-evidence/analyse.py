"""Summarize the separately frozen optimization cohort; reuse the original metrics."""

import gzip
import hashlib
import importlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

baseline = importlib.import_module('bench.native-evidence.analyse')
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def main():
    raw = ROOT / 'bench/raw/code-optimization-matched'
    archive = HERE / 'calls.json.gz'
    if raw.exists():
        calls = {p.stem: json.loads(p.read_text()) for p in raw.glob('*.json')}
        assert len(calls) == 200, f'Incomplete cohort: {len(calls)}/200'
        archive.write_bytes(gzip.compress(json.dumps(calls, sort_keys=True).encode(), mtime=0))
    else:
        calls = json.loads(gzip.decompress(archive.read_bytes()))
    snapshot = json.loads((HERE / 'snapshot.json').read_text())
    changed = [
        p for p, h in snapshot['hashes'].items() if hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != h
    ]
    base = ROOT / 'bench/raw/native-reference/examples/salon-concierge'
    native_changed = (
        [
            p
            for p, h in snapshot['native_manifest']['hashes'].items()
            if hashlib.sha256((base / p).read_bytes()).hexdigest() != h
        ]
        if base.exists()
        else None
    )
    result = {'source_changes': changed, 'native_changes': native_changed, 'scenarios': {}}
    for scenario in ('hours', 'complaint', 'complaint_verify', 'booking', 'move'):
        pairs = [(calls[f'{scenario}-{i:02d}-native'], calls[f'{scenario}-{i:02d}-poc']) for i in range(20)]
        good = [pair for pair in pairs if not any(call['errors'] for call in pair)]
        ci = baseline.interval(good)
        entry: dict[str, Any] = {'successful_pairs': len(good), 'full_turn_difference_ci': ci}
        for arm, column in (('native', 0), ('poc', 1)):
            values = [pair[column] for pair in pairs]
            entry[arm] = {
                'passed': sum(not call['errors'] for call in values),
                'failure_reasons': dict(Counter(e for call in values for e in call['errors'])),
                'all_calls': baseline.metrics(values),
                'successful_pairs_only': baseline.metrics([pair[column] for pair in good]),
            }
        entry['zero_regression_gate'] = bool(
            not changed
            and not native_changed
            and ci
            and entry['poc']['passed'] >= entry['native']['passed']
            and all(value['high_95'] <= 0 for value in ci.values())
        )
        result['scenarios'][scenario] = entry
    (HERE / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    for scenario, entry in result['scenarios'].items():
        print(
            scenario,
            'POC/native',
            entry['poc']['passed'],
            entry['native']['passed'],
            'zero regression:',
            entry['zero_regression_gate'],
        )
    assert not changed and not native_changed, 'Measured source changed during collection'


if __name__ == '__main__':
    main()
