"""The bench results as Markdown tables: one run folder, or a baseline and a candidate side by side.

    python summary.py BASELINE_DIR [CANDIDATE_DIR]

Medians, with p90 and the spread where they matter. Nothing is rounded
before it is compared.
"""

import json
import sys
from pathlib import Path

WORKLOADS = ('desk', 'salon', 'salon-long', 'faro')


def read(folder: Path, mode: str, workload: str) -> dict | None:
    path = folder / f'{mode}-{workload}.json'
    return json.loads(path.read_text())['result'] if path.exists() else None


def ms(stat: dict | None) -> str:
    return '-' if not stat else f'{stat["median"]:.1f} (p90 {stat["p90"]:.1f}, sd {stat["stdev"]:.1f})'


def table(header: list[str], rows: list[list[str]]) -> str:
    lines = ['| ' + ' | '.join(header) + ' |', '|' + '---|' * len(header)]
    lines += ['| ' + ' | '.join(row) + ' |' for row in rows]
    return '\n'.join(lines)


def compile_rows(folders: list[Path]) -> str:
    rows = []
    for w in WORKLOADS:
        for folder in folders:
            r = read(folder, 'compile', w)
            if r:
                rows.append(
                    [
                        w,
                        folder.name,
                        ms(r['cold_compile_ms']),
                        ms(r['warm_compile_ms']),
                        ms(r['warm_load_ms']),
                    ]
                )
    return table(['workload', 'run', 'cold compile ms', 'warm compile ms', 'load ms'], rows)


def turn_rows(folders: list[Path]) -> str:
    rows = []
    for w in WORKLOADS:
        for folder in folders:
            r = read(folder, 'turns', w)
            if not r:
                continue
            warm = r['warm']
            cold = sum(t['wall_ms'] for t in r['cold_call'])
            first = [t['median'] for t in warm['first_output_ms'] if t]
            rows.append(
                [
                    w,
                    folder.name,
                    str(sum(warm['requests'])),
                    f'{cold:.1f}',
                    ms(warm['call_wall_ms']),
                    ms(warm['call_cpu_ms']),
                    f'{max(t["median"] for t in warm["wall_ms"]):.1f}',
                    f'{sum(first) / len(first):.1f}' if first else '-',
                ]
            )
    return table(
        [
            'workload',
            'run',
            'requests',
            'cold call ms',
            'warm call wall ms',
            'warm call CPU ms',
            'slowest turn ms',
            'mean first output ms',
        ],
        rows,
    )


def payload_rows(folders: list[Path]) -> str:
    rows = []
    for w in WORKLOADS:
        for folder in folders:
            r = read(folder, 'payload', w)
            if not r:
                continue
            t = r['totals']
            rows.append(
                [
                    w,
                    folder.name,
                    str(t['requests']),
                    str(t['instructions']),
                    str(t['context']),
                    str(t['history']),
                    str(t['schemas']),
                    str(r['warmup_requests']),
                ]
            )
    return table(
        [
            'workload',
            'run',
            'requests',
            'instructions B',
            'of which context B',
            'history B',
            'tool schemas B',
            'warm-up requests',
        ],
        rows,
    )


def memory_rows(folders: list[Path]) -> str:
    rows = []
    for w in WORKLOADS:
        for folder in folders:
            r = read(folder, 'memory', w)
            if not r:
                continue
            d, c, o, rss = r['definitions'], r['completed'], r['open'], r['rss_mb']
            rows.append(
                [
                    w,
                    folder.name,
                    f'{d["alive"]}/{d["compiled"]}',
                    f'{d["retained_kib"]:.0f}',
                    f'{c["alive"]}/{c["calls"]}',
                    f'{c["retained_kib"]:.0f}',
                    f'{o["held_kib_each"]:.0f}',
                    f'{o["alive_after_release"]}/{o["active"]}',
                    f'{rss["start"]:.0f} -> {rss["end"]:.0f}',
                ]
            )
    return table(
        [
            'workload',
            'run',
            'discarded flows alive',
            'retained by compiles KiB',
            'completed calls alive',
            'retained by calls KiB',
            'open call KiB each',
            'released calls alive',
            'RSS MB',
        ],
        rows,
    )


def profile_rows(folders: list[Path]) -> str:
    rows = []
    for w in WORKLOADS:
        for folder in folders:
            r = read(folder, 'profile', w)
            if r:
                rows.append(
                    [
                        w,
                        folder.name,
                        f'{r["per_call_own_ms_total"]:.1f}',
                        f'{r["per_call_own_ms_core"]:.1f}',
                        f'{100 * r["per_call_own_ms_core"] / r["per_call_own_ms_total"]:.0f}%',
                    ]
                )
    return table(['workload', 'run', 'profiled ms/call', 'in core ms/call', 'core share'], rows)


def main() -> None:
    folders = [Path(p) for p in sys.argv[1:]]
    for title, part in (
        ('Compile', compile_rows),
        ('Turns (scripted, simulated delivery)', turn_rows),
        ('Payload per call (bytes, not tokens)', payload_rows),
        ('Python memory (tracemalloc) and RSS', memory_rows),
        ('Profile (cProfile own time, inflated by the profiler)', profile_rows),
    ):
        print(f'### {title}\n\n{part(folders)}\n')


if __name__ == '__main__':
    main()
