"""N12 per turn, the parent revision against this branch: median time to first output, native and harness, in ms.

    uv run python docs/research/reusable-core/cleanup/n12-compare.py

Reads `matrix/parent-timing.json` (origin/main fc2cfa4, run by this session
on the same machine) and `matrix/timing.json` (this branch).
"""

import json
from pathlib import Path

HERE = Path(__file__).parent / 'matrix'
runs = {
    name: json.loads((HERE / file).read_text())
    for name, file in (('parent', 'parent-timing.json'), ('cleanup', 'timing.json'))
}
print('| delay | turn | native (parent) | harness (parent) | native (cleanup) | harness (cleanup) |')
print('|---|---|---|---|---|---|')
for delay in runs['parent']['delays']:
    turns = len(runs['parent']['delays'][delay]['native']['per_turn'])
    for turn in range(turns):
        cells = []
        for name in ('parent', 'cleanup'):
            arms = runs[name]['delays'][delay]
            cells += [f'{arms[arm]["per_turn"][turn]["first_ms"]:.1f}' for arm in ('native', 'harness')]
        print(f'| {delay} | {turn + 1} | ' + ' | '.join(cells) + ' |')
for name, run in runs.items():
    print(f'{name}: {json.dumps(run["verdict"])}')
