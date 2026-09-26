"""Rescore every session-4 Coval call with the repaired `load_report.py`, offline, and count what it proves.

    cd use-cases/concierge-tools
    uv run python ../../docs/research/reusable-core/session-4b/rescore.py EXPORT.json

EXPORT.json is `load_report.py --save` over the 31 session-4 runs: every
observation of every call, speech included, so it stays private and is not
committed. `evidence.json` beside this file is committed: the facts (recording
length; how each call ended, observed or inferred, with its log lines) and the
reviewed labels. This writes
`load/<group>/report.{md,json}` and `inventory.md` beside it, and reads the
session-4 reports only to take each run's group. It launches nothing.
"""

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
OLD = HERE.parent / 'session-4' / 'load'
sys.path.insert(0, str(Path.cwd()))

from coval_sim.models import Simulation  # noqa: E402
from ledger import from_json  # noqa: E402
from load_report import DIMENSIONS, CallReport, Evidence, as_json, judge, render  # noqa: E402

GROUPS = {
    'complex': ('harness', 'interruptive', 180),
    'normal-5': ('harness', 'normal', 180),
    'stress-5': ('harness', 'interruptive', 180),
    'baseline': ('harness', 'mixed', 180),
    'fixed': ('harness', 'mixed', 180),
    'endpointing': ('harness', 'mixed', 180),
    'gate1': ('harness', 'normal', 300),
    'gate1-r2': ('harness', 'normal', 300),
    'gate2': ('harness', 'interruptive', 300),
    'native': ('native', 'mixed', 300),
}
"""Session-4 run folder -> build, scenario and call ceiling (`session-4/coval_evals.py`)."""
CASES = {'M5Z8LjVyQZPuhSubmt26w7': 'normal', 'GRdmreeb7SP4G8yegtMntC': 'interruptive'}


def main(export: Path) -> None:
    evidence = Evidence.model_validate_json((HERE / 'evidence.json').read_text())
    rows = {row['simulation']['simulation_id']: row for row in json.loads(export.read_text())}
    everything: list[tuple[str, str, CallReport]] = []
    for group, (build, _, limit) in GROUPS.items():
        old = json.loads((OLD / group / 'report.json').read_text())
        reports = []
        for call in old:
            row = rows[call['simulation']]
            sim = Simulation.model_validate(row['simulation'])
            report = judge(row['run'], sim, [from_json(o) for o in row['observations']], limit, evidence)
            reports.append(report)
            everything.append((group, build, report))
        out = HERE / 'load' / group
        out.mkdir(parents=True, exist_ok=True)
        (out / 'report.md').write_text(render(reports) + '\n')
        (out / 'report.json').write_text(json.dumps([as_json(r) for r in reports], indent=1) + '\n')
    ends = {
        sim: f'{facts.ended_by}{"" if facts.observed else " (inferred)"}'
        for sim, facts in evidence.facts.items()
        if facts.ended_by
    }
    (HERE / 'inventory.md').write_text('\n'.join(inventory(everything, rows, ends)) + '\n')
    print(f'rescored {len(everything)} calls')


def inventory(
    calls: list[tuple[str, str, CallReport]], rows: dict[str, dict], ends: dict[str, str]
) -> list[str]:
    """The counts: placed, connected, terminal, evidence-complete, and each dimension, by build and scenario."""
    lines = [
        '# Session 4 calls, rescored',
        '',
        'Built by `rescore.py` from the private export and `evidence.json`. Every placed call is counted once, in',
        'its build and scenario; failures and unknowns stay in every denominator.',
        '',
        '| Build | Scenario | Placed | Coval completed | Conversation span | Recording measured | Ending observed '
        '| Active after replay: 0 / 1 / 2+ | Ended by | Evidence complete | '
        + ' | '.join(f'{d}: pass / fail / unknown' for d in DIMENSIONS)
        + ' |',
        '|---|---|---|---|---|---|---|---|---|---|' + '---|' * len(DIMENSIONS),
    ]
    cohorts: dict[tuple[str, str], list[CallReport]] = {}
    for _, build, report in calls:
        case = rows[report.simulation]['simulation'].get('test_case_id')
        cohorts.setdefault((build, CASES.get(case or '', 'unknown case')), []).append(report)
    cohorts[('all', 'all')] = [r for _, _, r in calls]
    for (build, scenario), reports in cohorts.items():
        active = Counter(min(len(r.active), 2) for r in reports)
        endings = Counter(ends.get(r.simulation) or 'unknown' for r in reports)
        ended = ', '.join(f'{name} {n}' for name, n in endings.most_common())
        cells = []
        for d in DIMENSIONS:
            seen = Counter(getattr(r, d).verdict for r in reports)
            cells.append(f'{seen["pass"]} / {seen["fail"]} / {seen["unknown"]}')
        lines.append(
            f'| {build} | {scenario} | {len(reports)} | {sum(r.status == "COMPLETED" for r in reports)} '
            f'| {sum(r.span_seconds is not None for r in reports)} | {sum(r.media_seconds is not None for r in reports)} '
            f'| {sum(not any("how the call ended" in m or "inferred" in m for m in r.missing) for r in reports)} '
            f'| {active[0]} / {active[1]} / {active[2]} | {ended} '
            f'| {sum(not r.missing for r in reports)} | ' + ' | '.join(cells) + ' |'
        )
    return lines


if __name__ == '__main__':
    main(Path(sys.argv[1]))
