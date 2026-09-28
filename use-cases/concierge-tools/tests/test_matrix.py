"""The matrix driver, offline: a failed or missing harness check fails its row, the pin guards the reference.

No build is run here: the rows are evaluated from canned results, the way
the driver evaluates the ones the builds report.
"""

import tempfile
from pathlib import Path

from matrix.__main__ import Tolerance, digest, evaluate, load, verdict, within


def results(refs, passed=True, **extra):
    return {ref.removeprefix('core/'): {'check': ref, 'passed': passed, **extra} for ref in refs}


def every_row_is_read_from_the_versioned_file():
    spec = load()
    assert spec.version == 1 and set(spec.rows) == {f'N{i}' for i in range(1, 14)} - {'N12'}, set(spec.rows)
    assert spec.rows['N9'].native_expect == 'fail', (
        'the native gap on unheard read-backs is expected, not hidden'
    )
    assert len(spec.native.files_sha256) == 64


def a_failed_or_missing_harness_check_fails_its_row():
    spec = load()
    everything = [ref for row in spec.rows.values() for ref in row.harness]
    native = results([ref for row in spec.rows.values() for ref in row.native])
    rows = evaluate(spec, native, results(everything))
    assert all(row['status'] == 'pass' for row in rows.values()), rows
    broken = results(everything) | results(['scenario:replay'], passed=False, observation={'bookings': 2})
    assert evaluate(spec, native, broken)['N2']['status'] == 'fail', 'a duplicate booking passed'
    missing = {ref: r for ref, r in results(everything).items() if ref != 'scenario:unplayed_read_back'}
    assert evaluate(spec, native, missing)['N9']['status'] == 'fail', 'a check with no result passed'


def a_native_gap_is_recorded_as_expected():
    spec = load()
    harness = results([ref for row in spec.rows.values() for ref in row.harness])
    native = results([ref for row in spec.rows.values() for ref in row.native]) | results(
        ['scenario:unplayed_read_back'], passed=False
    )
    row = evaluate(spec, native, harness)['N9']
    assert row['native_result'] == 'fail' and row['native_as_expected'] and row['status'] == 'pass', row


def the_pin_changes_with_the_reference():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        (root / 'bot.py').write_text('print(1)')
        (root / 'evals').mkdir()
        (root / 'evals' / 'matrix.py').write_text('x = 1')
        before = digest(root)
        (root / 'evals' / 'matrix.py').write_text('x = 2')
        assert digest(root) == before, 'the adapter is not part of the reference'
        (root / 'bot.py').write_text('print(2)')
        assert digest(root) != before, 'a changed reference kept its pin'


def the_frozen_tolerance():
    tolerance = Tolerance(overhead_ms=5, overhead_share=0.25, harness_turn_max_ms=50)
    assert within(14.9, 10, tolerance) and not within(15.1, 10, tolerance)
    assert within(124.9, 100, tolerance) and not within(125.1, 100, tolerance)

    def arm(*turns_ms):
        return {'per_turn': [{'last_ms': t, 'raw_last_ms': [t]} for t in turns_ms], 'trace_ms': sum(turns_ms)}

    faster_overall = verdict(arm(7.0, 30.0), arm(12.5, 8.0), tolerance)
    assert faster_overall['trace_within_tolerance'] and not faster_overall['passed'], (
        'a faster trace excused a turn over its tolerance'
    )
    assert verdict(arm(7.0, 30.0), arm(11.9, 8.0), tolerance)['passed']


def main() -> None:
    every_row_is_read_from_the_versioned_file()
    a_failed_or_missing_harness_check_fails_its_row()
    a_native_gap_is_recorded_as_expected()
    the_pin_changes_with_the_reference()
    the_frozen_tolerance()
    print('matrix ok: rows, missing results, native gaps, pin, tolerance')


if __name__ == '__main__':
    main()
