"""N12 on the frozen protocol, several runs side by side: each turn's median `last_ms`, native and harness, at delay 0.

    python n12_compare.py RUN_DIR

`RUN_DIR` holds one `<tree>-<round>/timing.json` per `matrix time` run. The
frozen rule is read as `matrix` reads it: a turn passes when the harness
median is within max(5 ms, 25% of native) of the native median, and the
slowest harness turn must stay under 50 ms. Absolute and relative results
are printed apart.
"""

import json
import sys
from pathlib import Path


def main() -> None:
    runs = {p.parent.name: json.loads(p.read_text()) for p in sorted(Path(sys.argv[1]).glob('*/timing.json'))}
    names = list(runs)
    turns = len(next(iter(runs.values()))['delays']['0.0']['native']['per_turn'])
    print('| turn | ' + ' | '.join(f'{n} native / harness ms' for n in names) + ' |')
    print('|---|' + '---|' * len(names))
    for i in range(turns):
        cells = []
        for n in names:
            arms = runs[n]['delays']['0.0']
            cells.append(
                f'{arms["native"]["per_turn"][i]["last_ms"]:.1f} / {arms["harness"]["per_turn"][i]["last_ms"]:.1f}'
            )
        print(f'| {i + 1} | ' + ' | '.join(cells) + ' |')
    cells = [
        f'{runs[n]["delays"]["0.0"]["native"]["trace_ms"]:.1f} / {runs[n]["delays"]["0.0"]["harness"]["trace_ms"]:.1f}'
        for n in names
    ]
    print('| trace | ' + ' | '.join(cells) + ' |')
    print()
    print(
        '| run | relative rule: turns within | trace within | absolute: slowest harness turn ms | under 50 ms | passed |'
    )
    print('|---|---|---|---|---|---|')
    for n in names:
        v = runs[n]['verdict']
        within = ', '.join(str(i + 1) for i, ok in enumerate(v['turns_within_tolerance']) if ok)
        print(
            f'| {n} | {within} | {v["trace_within_tolerance"]} | {v["harness_turn_max_ms"]:.1f} | '
            f'{v["harness_turns_within_max"]} | {v["passed"]} |'
        )


if __name__ == '__main__':
    main()
