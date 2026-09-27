"""Per worker, RSS beside the Python heap tracemalloc sees, over a `--live --tracemalloc` run.

Usage: `python heap.py <step folder> [every seconds]`. Each row: seconds since
start, then per worker `RSS MB / Python heap MB / live calls / active calls`.
"""

import json
import sys
from pathlib import Path

folder = Path(sys.argv[1])
every = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
samples = json.loads((folder / 'samples.json').read_text())
start = samples[0]['at']
shown = -every
for s in samples + [dict(samples[-1], at=samples[-1]['at'] + every)]:
    t = s['at'] - start
    if t - shown < every:
        continue
    shown = t
    cells = [
        f'{pid}: {s["rss_mb"].get(pid, 0):.0f} / {w.get("live", {}).get("python_heap_mb", "-")} / '
        f'{w.get("live", {}).get("FlowCall", "-")} / {len(w["active"])}'
        for pid, w in sorted(s['workers'].items())
    ]
    print(f'{t:5.0f}s  ' + ' | '.join(cells))
