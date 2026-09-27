"""Arm the live worker fault only once two calls are active on different workers (session 4e, worker-failure pair).

    uv run python arm_after_placement.py --port 7890 --arm <file> --out <placement.json> [--wait 180]

Polls the runner's own call record (`/calls`, local only) every half second.
When exactly two calls are active and their workers differ, it writes the
two rows to `--out`, creates `--arm` (the file `LIVE_FAULT_ARM` names) and
exits 0. If the calls land on one worker, or two never become active within
`--wait` seconds, it writes what it saw, does not arm, and exits 1: the
check is then recorded as incomplete.
"""

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, required=True)
parser.add_argument('--arm', required=True)
parser.add_argument('--out', required=True)
parser.add_argument('--wait', type=float, default=180.0)
args = parser.parse_args()

started, seen = time.time(), []
while time.time() - started < args.wait:
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/calls', timeout=2) as response:
            seen = json.loads(response.read())
    except OSError:
        seen = []
    active = [row for row in seen if row.get('state') == 'active']
    if len(active) == 2:
        verdict = 'armed' if active[0].get('worker') != active[1].get('worker') else 'same worker: not armed'
        Path(args.out).write_text(
            json.dumps({'at': time.time(), 'verdict': verdict, 'active': active}, indent=1)
        )
        if verdict == 'armed':
            Path(args.arm).touch()
            print(verdict, [(r.get('call_sid'), r.get('worker')) for r in active])
            sys.exit(0)
        print(verdict, [(r.get('call_sid'), r.get('worker')) for r in active])
        sys.exit(1)
    time.sleep(0.5)
Path(args.out).write_text(
    json.dumps({'at': time.time(), 'verdict': 'two calls never active', 'seen': seen}, indent=1)
)
print('two calls never active')
sys.exit(1)
