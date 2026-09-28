"""Read a memory run's `samples.json` (`evals.load ramp`): worker memory, live calls, tasks and calls over time.

Usage: `python samples.py <step folder> [every seconds]`. Prints one row per
`every` seconds (default 30) and the calls-over mark, then per worker the
memory at the start, at the end of the calls, at its peak and at the end of
the idle period. Memory is each worker's RSS from `ps`; `FlowCall` is the
worker's own live-object count (`server.live_counts`, with `--live`).
"""

import json
import sys
from pathlib import Path

folder = Path(sys.argv[1])
every = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
samples = json.loads((folder / 'samples.json').read_text())
start = samples[0]['at']
marker = folder / 'calls_over.json'
over = json.loads(marker.read_text())['at'] if marker.exists() else None
rows = [s for s in samples if 'workers' in s]
last_shown = -every
series: dict[int, list[tuple[float, float]]] = {}
for s in rows:
    t = s['at'] - start
    for pid, mb in s['rss_mb'].items():
        if str(pid) in {str(p) for p in s['workers']}:
            series.setdefault(int(pid), []).append((t, mb))
    if t - last_shown >= every:
        last_shown = t
        cells = []
        for pid, w in sorted(s['workers'].items()):
            live = w.get('live', {})
            cells.append(
                f'{pid}: {s["rss_mb"].get(str(pid), s["rss_mb"].get(int(pid), 0)):.0f} MB, '
                f'calls {len(w["active"])}, tasks {w["tasks"]}, FlowCall {live.get("FlowCall", "-")}'
            )
        print(f'{t:6.0f}s  ' + ' | '.join(cells))
if over is not None:
    print(f'calls over at {over - start:.0f}s')
for pid, points in sorted(series.items()):
    during = [mb for t, mb in points if over is None or t + start <= over]
    after = [mb for t, mb in points if over is not None and t + start > over]
    print(
        f'worker {pid}: start {points[0][1]:.0f} MB, end of calls {during[-1] if during else float("nan"):.0f} MB, '
        f'peak {max(mb for _, mb in points):.0f} MB, end of idle {after[-1] if after else float("nan"):.0f} MB'
    )
