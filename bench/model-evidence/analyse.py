"""Summarize the declared 20-pair model comparison, retaining failures.

Run: python -m bench.model-evidence.analyse
"""

import gzip
import hashlib
import importlib.util
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('native_analysis', ROOT / 'bench/native-evidence/analyse.py')
assert spec and spec.loader
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def main():
    raw = ROOT / 'bench/raw/model-matched-20260922'
    archive = HERE / 'calls.json.gz'
    calls = (
        {path.stem: json.loads(path.read_text()) for path in raw.glob('*.json')}
        if raw.exists()
        else json.loads(gzip.decompress(archive.read_bytes()))
    )
    assert len(calls) == 200, f'Incomplete cohort: {len(calls)}/200 calls'
    snapshot = json.loads((HERE / 'snapshot.json').read_text())
    changed = [
        name
        for name, digest in snapshot['hashes'].items()
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest
    ]
    assert not changed, f'Frozen sources changed: {changed}'
    for call in calls.values():
        if call.get('settings'):
            for key, value in snapshot['matching_settings'].items():
                assert call['settings'][key] == value, (call['model'], key)
            assert call['settings']['model'] == f'google-cloud:{call["model"]}'
    archive.write_bytes(gzip.compress(json.dumps(calls, sort_keys=True).encode(), mtime=0))
    results = {'source_changes_since_freeze': changed, 'scenarios': {}}
    for scenario in ('hours', 'complaint', 'complaint_verify', 'booking', 'move'):
        pairs = [
            tuple(calls[f'{scenario}-{index:02d}-gemini-{model}-flash-lite'] for model in ('3.1', '3.5'))
            for index in range(20)
        ]
        good = [pair for pair in pairs if not any(call['errors'] for call in pair)]
        interval = analysis.interval(good)
        entry = {
            'successful_pairs': len(good),
            'full_turn_difference_ci': {
                key.replace('poc_minus_native', '35_minus_31'): value for key, value in interval.items()
            }
            if interval
            else None,
        }
        for column, model in enumerate(('3.1', '3.5')):
            arm = [pair[column] for pair in pairs]
            entry[model] = {
                'passed': sum(not call['errors'] for call in arm),
                'failed': sum(bool(call['errors']) for call in arm),
                'failure_reasons': dict(Counter(error for call in arm for error in call['errors'])),
                'all_calls': analysis.metrics(arm),
                'successful_pairs_only': analysis.metrics([pair[column] for pair in good]),
            }
        results['scenarios'][scenario] = entry
    (HERE / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    print(
        json.dumps(
            {
                name: {model: value[model]['passed'] for model in ('3.1', '3.5')}
                for name, value in results['scenarios'].items()
            }
        )
    )


if __name__ == '__main__':
    main()
