"""Check each Coval qualification set holds exactly the manifest's ten cases, word for word. Free: reads only, through
the same `coval test-cases list` call `coval_sim plan` makes.

    cd use-cases/concierge-tools && PYTHONPATH=.:../../docs/qualification/final-qualification \
        uv run python ../../docs/qualification/final-qualification/verify_sets.py ENV

Also writes `fixtures.json` (case ID to the caller's number), which `coval_sim run --fixtures` binds calls with.
Exit 0 when both sets match.
"""

import json
import sys
from pathlib import Path

from make_sets import CASES, key

from coval_sim.coval import CovalClient


def main() -> int:
    coval = CovalClient(key(Path(sys.argv[1])))
    doc = json.loads(CASES.read_text())
    problems, fixtures = [], {}
    for name, cohort in doc['cohorts'].items():
        listed = coval._call(
            'test-cases', 'list', '--test-set-id', cohort['coval_test_set'], '--page-size', '100'
        )
        remote = {c['id']: c for c in listed or []}
        wanted = {c['coval_test_case']: c for c in cohort['cases']}
        if set(remote) != set(wanted):
            problems.append(f'{name}: set holds {sorted(remote)}, manifest {sorted(wanted)}')
        for case_id, case in wanted.items():
            got = remote.get(case_id, {})
            if got.get('input_str') != case['input']:
                problems.append(f'{name} {case["key"]}: input differs')
            fixtures[case_id] = case['phone']
        print(name, cohort['coval_test_set'], len(remote), 'cases')
    (CASES.parent / 'fixtures.json').write_text(json.dumps(fixtures, indent=1) + '\n')
    print('\n'.join(problems) or 'both sets match the manifest')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
