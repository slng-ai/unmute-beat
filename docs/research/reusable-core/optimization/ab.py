"""Alternating A/B rounds of `bench.py turns`: each round's median warm call, and the median of the rounds.

    python ab.py RUN_DIR

`RUN_DIR` holds one folder per tree and round (`<tree>-<round>/turns-<workload>.json`),
as the alternating runner writes them. Rounds alternate trees so that load
changing on the machine falls on both.
"""

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def main() -> None:
    root = Path(sys.argv[1])
    rounds: dict[tuple[str, str], list[float]] = defaultdict(list)
    for path in sorted(root.glob('*-*/turns-*.json')):
        tree = path.parent.name.rsplit('-', 1)[0]
        workload = path.stem.removeprefix('turns-')
        warm = json.loads(path.read_text())['result']['warm']
        rounds[workload, tree].append(warm['call_wall_ms']['median'])
    trees = sorted({tree for _, tree in rounds})
    print(
        '| workload | ' + ' | '.join(f'{t} rounds (median ms)' for t in trees) + ' | change of the median |'
    )
    print('|---|' + '---|' * len(trees) + '---|')
    for workload in sorted({w for w, _ in rounds}):
        cells = [rounds[workload, t] for t in trees]
        medians = [statistics.median(c) for c in cells]
        change = 100 * (medians[-1] - medians[0]) / medians[0]
        row = [
            f'{", ".join(f"{v:.1f}" for v in c)} (median {m:.1f})'
            for c, m in zip(cells, medians, strict=True)
        ]
        print(f'| {workload} | ' + ' | '.join(row) + f' | {change:+.1f}% |')


if __name__ == '__main__':
    main()
