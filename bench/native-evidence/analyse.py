"""Summarize the frozen 20-pair transcript cohort without dropping failed calls.

Run: python bench/native-evidence/analyse.py. Reads the retained gzip archive if
the ignored raw folder is absent. Bootstrap units are calls, never isolated turns.
"""

import gzip
import hashlib
import json
import math
import random
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def median(values):
    return statistics.median(values) if values else None


def quantile(values, fraction):
    return sorted(values)[max(0, math.ceil(fraction * len(values)) - 1)] if values else None


def metrics(calls):
    rows = [row for call in calls for row in call['rows']]
    fields = {
        'full_turn_seconds': [row['seconds'] for row in rows],
        'first_speech_seconds_including_filler': [
            row['first_speech_seconds'] for row in rows if row.get('first_speech_seconds') is not None
        ],
        'request_first_output_seconds': [value for row in rows for value in row.get('ttft', [])],
    }
    return {
        'calls': len(calls),
        'turns': len(rows),
        'requests': sum(row['requests'] for row in rows),
        'requests_per_turn': sum(row['requests'] for row in rows) / len(rows) if rows else None,
        **{
            key: {'n': len(values), 'p50': median(values), 'p90_nearest_rank': quantile(values, 0.9)}
            for key, values in fields.items()
        },
    }


def interval(pairs):
    if not pairs:
        return None
    rng = random.Random(20260921)
    changes = [[], []]
    for _ in range(10_000):
        sample = rng.choices(pairs, k=len(pairs))
        native = [row['seconds'] for pair in sample for row in pair[0]['rows']]
        poc = [row['seconds'] for pair in sample for row in pair[1]['rows']]
        changes[0].append(statistics.median(poc) - statistics.median(native))
        changes[1].append(quantile(poc, 0.9) - quantile(native, 0.9))
    return {
        key: {'low_95': quantile(values, 0.025), 'high_95': quantile(values, 0.975)}
        for key, values in zip(('p50_poc_minus_native', 'p90_poc_minus_native'), changes, strict=True)
    }


def main():
    raw = ROOT / 'bench/raw/native-matched-v2'
    archive = HERE / 'calls.json.gz'
    if raw.exists():
        calls = {p.stem: json.loads(p.read_text()) for p in raw.glob('*.json')}
        assert len(calls) == 200, f'Incomplete cohort: {len(calls)}/200 calls'
        archive.write_bytes(gzip.compress(json.dumps(calls, sort_keys=True).encode(), mtime=0))
    else:
        calls = json.loads(gzip.decompress(archive.read_bytes()))
    snapshot = json.loads((HERE / 'snapshot-v2.json').read_text())
    changed = [
        name
        for name, digest in snapshot['hashes'].items()
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest
    ]
    build = ROOT / 'bench/raw/native-reference/examples/salon-concierge'
    native_changed = (
        [
            name
            for name, digest in snapshot['native_manifest']['hashes'].items()
            if hashlib.sha256((build / name).read_bytes()).hexdigest() != digest
        ]
        if build.exists()
        else None
    )
    results = {
        'source_changes_since_freeze': changed,
        'native_build_changes_since_freeze': native_changed,
        'scenarios': {},
    }
    for name in ('hours', 'complaint', 'complaint_verify', 'booking', 'move'):
        pairs = [
            (calls[f'{name}-{index:02d}-native'], calls[f'{name}-{index:02d}-poc']) for index in range(20)
        ]
        good = [pair for pair in pairs if not any(call['errors'] for call in pair)]
        entry = {'successful_pairs': len(good), 'full_turn_difference_ci': interval(good)}
        for arm, column in (('native', 0), ('poc', 1)):
            arm_calls = [pair[column] for pair in pairs]
            entry[arm] = {
                'passed': sum(not call['errors'] for call in arm_calls),
                'failed': sum(bool(call['errors']) for call in arm_calls),
                'failure_reasons': dict(Counter(error for call in arm_calls for error in call['errors'])),
                'all_calls': metrics(arm_calls),
                'successful_pairs_only': metrics([pair[column] for pair in good]),
            }
        results['scenarios'][name] = entry
    (HERE / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    print(
        json.dumps(
            {
                name: {
                    'native': entry['native']['passed'],
                    'poc': entry['poc']['passed'],
                    'pairs': entry['successful_pairs'],
                }
                for name, entry in results['scenarios'].items()
            },
            indent=2,
        )
    )


if __name__ == '__main__':
    main()
