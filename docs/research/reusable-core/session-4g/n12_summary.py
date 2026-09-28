"""Print N12's per-turn medians and request counts from a `matrix time` timing.json. `python3 n12_summary.py FILE`."""

import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text())
for delay, arms in data['delays'].items():
    for arm in ('harness', 'native'):
        found = arms[arm]
        turns = [round(t['first_ms'], 1) for t in found['per_turn']]
        rest = {
            k: (round(v, 1) if isinstance(v, float) else v)
            for k, v in found.items()
            if not isinstance(v, list | dict)
        }
        print(delay, arm, turns, rest)
print(data['verdict'])
