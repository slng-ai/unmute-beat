"""Arm the worker fault only once two calls are carried by two different runner processes (session 4f).

    uv run python -m evals.placement --port 7890 --arm <file> --out <placement.json> [--wait 180] [--campaign X]

Session 4e's `arm_after_placement.py`, moved here so the offline check (F6)
and the live pair use the same code, and made stricter. It polls the
runner's own record (`/calls`, local only; every runner sharing the
`RUNNER_DB` answers with every call) every half second. It arms only when
exactly two calls are active and the media of each was claimed by a
different worker pid on a different listening port (`served_port`): where
the stream really arrived, not where the webhook went. Then it writes both
rows to `--out`, creates `--arm` (the file `LIVE_FAULT_ARM` names) and exits
0. Two calls on one process, or two never active within `--wait` seconds,
write what was seen, arm nothing and exit 1: the check is incomplete.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path
from typing import Any


def calls(port: int) -> list[dict[str, Any]]:
    """The runner file's rows, through a runner's local `/calls`; empty while it does not answer."""
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/calls', timeout=2) as response:
            return json.loads(response.read())
    except OSError:
        return []


def verdict(active: list[dict[str, Any]]) -> str:
    """'armed' when two active calls were claimed by different workers on different ports, else why not."""
    if len(active) != 2:
        return f'{len(active)} calls active'
    first, second = active
    if first.get('worker') == second.get('worker'):
        return 'same worker: not armed'
    if not first.get('served_port') or first.get('served_port') == second.get('served_port'):
        return 'same runner process: not armed'
    return 'armed'


def watch(port: int, arm: Path, out: Path, wait: float, campaign: str = '') -> bool:
    """Poll until two calls are active, then arm the fault only if they sit on two processes. True when armed."""
    started, seen = time.time(), []
    while time.time() - started < wait:
        seen = [row for row in calls(port) if not campaign or row.get('campaign') == campaign]
        active = [row for row in seen if row.get('state') == 'active']
        if len(active) == 2:
            outcome = verdict(active)
            out.write_text(json.dumps({'at': time.time(), 'verdict': outcome, 'active': active}, indent=1))
            placed = [(row.get('call_sid'), row.get('worker'), row.get('served_port')) for row in active]
            print(outcome, placed, flush=True)
            if outcome == 'armed':
                arm.touch()
                return True
            return False
        time.sleep(0.5)
    out.write_text(
        json.dumps({'at': time.time(), 'verdict': 'two calls never active', 'seen': seen}, indent=1)
    )
    print('two calls never active', flush=True)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--arm', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--wait', type=float, default=180.0)
    parser.add_argument('--campaign', default='')
    args = parser.parse_args()
    return 0 if watch(args.port, args.arm, args.out, args.wait, args.campaign) else 1


if __name__ == '__main__':
    raise SystemExit(main())
