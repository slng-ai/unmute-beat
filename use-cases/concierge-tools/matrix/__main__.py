"""The N1-N13 native comparison: run each arm's checks in its own environment and compare, row by row.

    uv run python -m matrix pin                 # the native reference's digest, for scenarios.v1.json
    uv run python -m matrix run [N1 N9 ...]     # the rows' checks, both arms; writes matrix.json and matrix.md
    uv run python -m matrix time                # N12: paired timings on the booking trace; writes timing.json

Run from `use-cases/concierge-tools`. The native arm is the pinned
salon-pipecat-flows; the harness arm is salon-graph plus core. Each arm's
checks run in a process started in that member's own folder
(`run_checks.py`), so the two salon packages never meet. Offline: every
model is scripted and real requests are refused. The exit code is 1 when a
harness check fails, when a check has no result, or when the reference
changed: zero unauthorized or duplicate writes is a gate, not a score.
"""

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SCENARIOS = HERE / 'scenarios.v2.json'
OUT = ROOT / 'data' / 'matrix'


class Arm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    member: str
    revision: str = ''
    files_sha256: str = ''


class Row(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str
    native_expect: str
    native: list[str]
    harness: list[str]


class Tolerance(BaseModel):
    model_config = ConfigDict(extra='forbid')
    overhead_ms: float
    overhead_share: float
    harness_turn_max_ms: float


class Timing(BaseModel):
    model_config = ConfigDict(extra='forbid')
    warmup: int
    repetitions: int
    delays: list[float]
    tolerance: Tolerance


class Scenarios(BaseModel):
    model_config = ConfigDict(extra='forbid')
    notes: list[str]
    version: int
    native: Arm
    harness: Arm
    rows: dict[str, Row]
    timing: Timing


def digest(folder: Path) -> str:
    """One sha256 over every runtime file of a build: its path and its bytes, in path order."""
    skip = {'evals', 'tests', '__pycache__', '.venv'}
    total = hashlib.sha256()
    for path in sorted(
        p for p in folder.rglob('*') if p.is_file() and not skip & set(p.relative_to(folder).parts)
    ):
        total.update(str(path.relative_to(folder)).encode() + b'\0' + path.read_bytes() + b'\0')
    return total.hexdigest()


def invoke_one(member: str, ref: str, timeout: float = 600) -> dict[str, Any]:
    """Run one check in a fresh process in its member's own environment."""
    command = ['uv', 'run', '--directory', str(ROOT / member), 'python', str(HERE / 'run_checks.py'), ref]
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'check': ref, 'passed': False, 'error': f'no result within {timeout} s'}
    lines = [line for line in run.stdout.splitlines() if line.startswith('MATRIX ')]
    if not lines:
        return {
            'check': ref,
            'passed': False,
            'error': f'no result (exit {run.returncode}): {run.stderr[-800:]}',
        }
    return json.loads(lines[-1].removeprefix('MATRIX '))[0]


def invoke(member: str, refs: list[str]) -> dict[str, dict[str, Any]]:
    """Run checks in one member's own environment, each in a process of its own, four at a time.

    One process per check, because checks share module state: two lines
    patching the same classifier at once restore it out of order, and the
    patch outlives them.
    """
    with ThreadPoolExecutor(4) as pool:
        return {r['check']: r for r in pool.map(lambda ref: invoke_one(member, ref), refs)}


def split(harness: Scenarios, refs: list[str]) -> dict[str, list[str]]:
    """Each harness check by the member it runs in: `core/...` in core, the rest in the harness member."""
    members: dict[str, list[str]] = {}
    for ref in refs:
        member, check = (
            ('core', ref.removeprefix('core/')) if ref.startswith('core/') else (harness.harness.member, ref)
        )
        members.setdefault(member, []).append(check)
    return members


def evaluate(spec: Scenarios, native: dict[str, dict], harness: dict[str, dict]) -> dict[str, dict[str, Any]]:
    """Each row's results. A row passes only when every harness check passed; a missing result is a failure."""
    rows = {}
    for name, row in spec.rows.items():
        mine = [
            harness.get(ref.removeprefix('core/'), {'check': ref, 'passed': False, 'error': 'no result'})
            for ref in row.harness
        ]
        theirs = [
            native.get(ref, {'check': ref, 'passed': False, 'error': 'no result'}) for ref in row.native
        ]
        native_result = (
            ('pass' if all(r['passed'] for r in theirs) else 'fail') if theirs else 'not exercised'
        )
        rows[name] = {
            'title': row.title,
            'status': 'pass' if mine and all(r['passed'] for r in mine) else 'fail',
            'harness': mine,
            'native': theirs,
            'native_result': native_result,
            'native_as_expected': native_result == row.native_expect or native_result == 'not exercised',
        }
    return rows


def table(rows: dict[str, dict[str, Any]]) -> str:
    lines = [
        '| Row | Native | Harness | Checks (native / harness) | Status |',
        '|---|---|---|---|---|',
    ]
    for name, row in rows.items():
        harness_passed = sum(r['passed'] for r in row['harness'])
        native_passed = sum(r['passed'] for r in row['native'])
        failed = [r['check'] for r in [*row['harness']] if not r['passed']]
        lines.append(
            f'| {name} | {row["native_result"]} | {"pass" if row["status"] == "pass" else "fail"} '
            f'| {native_passed}/{len(row["native"])} / {harness_passed}/{len(row["harness"])} '
            f'| {row["status"]}{" (harness failed: " + ", ".join(failed) + ")" if failed else ""} |'
        )
    return '\n'.join(lines) + '\n'


def within(harness_ms: float, native_ms: float, tolerance: Tolerance) -> bool:
    """The frozen N12 tolerance: harness no slower than native by more than max(5 ms, 25 % of native)."""
    return harness_ms <= native_ms + max(tolerance.overhead_ms, tolerance.overhead_share * native_ms)


def load() -> Scenarios:
    return Scenarios.model_validate_json(SCENARIOS.read_text())


def pinned(spec: Scenarios) -> bool:
    return digest(ROOT / spec.native.member) == spec.native.files_sha256


def run(names: list[str], out: Path) -> int:
    spec = load()
    if not pinned(spec):
        print(
            f'refused: {spec.native.member} is not the pinned reference ({digest(ROOT / spec.native.member)})'
        )
        return 1
    rows = {name: row for name, row in spec.rows.items() if not names or name in names}
    spec = spec.model_copy(update={'rows': rows})
    native = invoke(spec.native.member, sorted({ref for row in rows.values() for ref in row.native}))
    harness: dict[str, dict] = {}
    for member, refs in split(spec, sorted({ref for row in rows.values() for ref in row.harness})).items():
        harness |= invoke(member, refs)
    results = evaluate(spec, native, harness)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'matrix.json').write_text(
        json.dumps({'native_files_sha256': spec.native.files_sha256, 'rows': results}, indent=2, default=str)
    )
    (out / 'matrix.md').write_text(table(results))
    print(table(results))
    return 0 if all(row['status'] == 'pass' for row in results.values()) else 1


def one_timing(member: str, delay: float) -> dict[str, Any]:
    result = invoke_one(member, f'scenario:timing:{delay}')
    if not result['passed']:
        raise RuntimeError(f'{member} timing run failed: {result.get("error")}')
    return result['observation']


def summarise(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Per turn, the median dispatch-to-first-output and dispatch-to-last-output, in ms, and the raw values."""
    turns = len(samples[0]['turns'])
    per_turn = []
    for index in range(turns):
        first = [s['turns'][index]['first_output'] * 1000 for s in samples]
        last = [s['turns'][index]['last_output'] * 1000 for s in samples]
        per_turn.append(
            {
                'first_ms': statistics.median(first),
                'last_ms': statistics.median(last),
                'raw_first_ms': first,
                'raw_last_ms': last,
            }
        )
    trace = [sum(t['last_output'] for t in s['turns']) * 1000 for s in samples]
    return {
        'per_turn': per_turn,
        'trace_ms': statistics.median(trace),
        'raw_trace_ms': trace,
        'requests': samples[0]['requests'],
    }


def verdict(native: dict[str, Any], harness: dict[str, Any], tolerance: Tolerance) -> dict[str, Any]:
    """The frozen N12 rule on the zero-delay run: every turn's median within tolerance, and every harness turn under its maximum.

    Per turn, because the plan froze it on turn wall time. The trace total is
    reported beside it, and cannot excuse a turn that breaches.
    """
    turns = [
        within(mine['last_ms'], theirs['last_ms'], tolerance)
        for mine, theirs in zip(harness['per_turn'], native['per_turn'], strict=True)
    ]
    slowest = max(max(t['raw_last_ms']) for t in harness['per_turn'])
    result = {
        'turns_within_tolerance': turns,
        'trace_within_tolerance': within(harness['trace_ms'], native['trace_ms'], tolerance),
        'harness_turn_max_ms': slowest,
        'harness_turns_within_max': slowest <= tolerance.harness_turn_max_ms,
    }
    result['passed'] = all(turns) and result['trace_within_tolerance'] and result['harness_turns_within_max']
    return result


def time_it(out: Path) -> int:
    spec = load()
    if not pinned(spec):
        print('refused: the native reference is not the pinned one')
        return 1
    arms = {'native': spec.native.member, 'harness': spec.harness.member}
    report: dict[str, Any] = {'protocol': spec.timing.model_dump(), 'delays': {}}
    ok = True
    for delay in spec.timing.delays:
        samples: dict[str, list[dict]] = {'native': [], 'harness': []}
        for index in range(spec.timing.warmup + spec.timing.repetitions):
            order = ['native', 'harness'] if index % 2 == 0 else ['harness', 'native']
            for arm in order:
                observed = one_timing(arms[arm], delay)
                if index >= spec.timing.warmup:
                    samples[arm].append(observed)
            print(
                f'delay {delay}: repetition {index + 1} of {spec.timing.warmup + spec.timing.repetitions}',
                flush=True,
            )
        native, harness = summarise(samples['native']), summarise(samples['harness'])
        report['delays'][str(delay)] = {'native': native, 'harness': harness}
        if delay == 0.0:
            report['verdict'] = verdict(native, harness, spec.timing.tolerance)
            ok = report['verdict']['passed']
    out.mkdir(parents=True, exist_ok=True)
    (out / 'timing.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report.get('verdict'), indent=2))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='matrix', description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('pin')
    running = commands.add_parser('run')
    running.add_argument('rows', nargs='*')
    running.add_argument('--out', type=Path, default=OUT)
    timing = commands.add_parser('time')
    timing.add_argument('--out', type=Path, default=OUT)
    args = parser.parse_args(argv)
    if args.command == 'pin':
        print(digest(ROOT / load().native.member))
        return 0
    if args.command == 'run':
        return run(args.rows, args.out)
    return time_it(args.out)


if __name__ == '__main__':
    sys.exit(main())
