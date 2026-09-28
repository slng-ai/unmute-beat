"""Build `coval-evals.md`: every session-4 Coval call, one row each, with its Coval and Langfuse links.

    python docs/research/reusable-core/session-4/coval_evals.py

Reads the `report.json` that `use-cases/concierge-tools/load_report.py` wrote
for each run under `load/`, and the review notes below, which come from
reading the calls in Langfuse (`ledger.md`). Writes nothing else.
"""

import json
from pathlib import Path

HERE = Path(__file__).parent
COVAL = (
    'https://app.coval.dev/slng-ltd-1787040174713992753/workspace/01M09YA4S1SC1Q8AHKHP2R9A0E/simulated/runs'
)
LANGFUSE = 'https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/sessions'

GROUPS = (
    (
        'complex',
        'Complex stress, calls 1-2',
        'harness 044057e (call 1), 52eb245 (call 2)',
        'profile v1, 180 s',
    ),
    ('normal-5', 'Normal x5, calls 3-7', 'harness 52eb245', 'profile v1, 180 s, 5 at once'),
    ('stress-5', 'Interruptive x5, calls 8-12', 'harness 52eb245', 'profile v1, 180 s, 5 at once'),
    ('baseline', 'Load baseline, 20 at once', 'harness 52eb245', 'bot default speech, 180 s'),
    ('fixed', 'Load after fixes F1-F4, 20 at once', 'harness a04c0ff', 'bot default speech, 180 s'),
    ('endpointing', 'Endpointing, VAD 0.6 s', 'harness a04c0ff', 'bot default speech, 180 s, 8 at once'),
    ('gate1', 'Gate 1, normal x20', 'harness 3fd1bea', 'profile v1, 300 s, 10 at once'),
    ('gate1-r2', 'Gate 1 rerun, normal x20', 'harness 11dd3eb', 'profile v1, 300 s, 10 at once'),
    ('gate2', 'Gate 2, interruptive x20', 'harness 83d4be8', 'profile v1, 300 s, 10 at once'),
    (
        'native',
        'Native Pipecat flows, 10 normal + 8 interruptive',
        'salon-pipecat-flows, pinned',
        'profile v1, 300 s, 1 at a time; 2 interruptive calls never placed (tunnel service timeout)',
    ),
)
"""Run folder under `load/`, title, build and revision, speech and limit."""

NOTES = {
    'fymMyWTi': 'Consent fault (critical): the number read-back was queued behind a holding line and counted as heard; fixed in 52eb245. The audit itself was fooled, so the automatic columns say nothing is wrong.',
    'NdkBKGCN': 'Saturday booked on a heard yes; the cut receipt was said again by code, no second write.',
    'jEisR2g6': "Repeated holding lines talked over the caller's first number.",
    'Khs7tVuS': 'Simulated caller went silent after 31 s and never spoke again; still in progress at Coval. Inconclusive.',
    'UD4fMgUZ': 'Friday booked on a heard yes, then the caller asked for Saturday; 85 s lost to a misheard number; the call hit 180 s at the move read-back.',
    'QeJTxmKm': 'Caller said yes before the read-back, then stayed silent 70 s after it: deadlock until 180 s.',
    'gyaSM3Ax': 'Still mid-conversation at the 180 s limit.',
    'Dui5EEvb': 'Hit the 180 s limit as the caller said yes.',
    '7oRuKHUE': 'Simulated caller never gave its digits; hung up at 128 s.',
    'b7BSSwtX': 'Booked. The concierge said "I\'ve got you down" before any booking and the harness corrected it out loud; the concierge is now guarded.',
    'ArS5ZMnu': '129 s on the number; hit 180 s at the booking read-back.',
    'cYMMixAc': 'Critical: after the caller\'s yes, the model said goodbye with "your haircut is booked" and nothing was booked. Goodbye claim guard added in 11dd3eb.',
    'BWqBhyLj': "Booked after five read-backs: the 8 s silence reminder kept talking over the caller's yes. Reminder moved to 20 s in 11dd3eb.",
    'cBrx4NhR': 'Verify\'s model opened with "I\'ll connect you with someone"; the caller thanked and left. Code now asks for the number (`asks:`, 83d4be8).',
    'YzkhPWRp': 'Native, wrong: booked Friday on "nine works for me, thanks" (a pick, not a yes to a read-back), then booked Saturday as a second appointment.',
    '3CyVWZmp': 'Native, wrong: booked Friday on "silence underscore tool" (Coval tool noise read by speech-to-text), then booked Saturday as a second appointment.',
    'C26pPHK6': 'The caller said yes 0.3 s into the read-back (not heard, not counted), said yes in pieces for 12 s, then said goodbye before the read-back was answered.',
}
"""Review notes, by the first 8 characters of the simulation ID: what reading the call in Langfuse showed."""


def rows() -> list[str]:
    lines = []
    for folder, title, build, setup in GROUPS:
        report = HERE / 'load' / folder / 'report.json'
        if not report.exists():
            continue
        calls = json.loads(report.read_text())
        booked = sum(len(c['bookings']) == 1 for c in calls)
        unmatched = sum(len(c['unmatched']) for c in calls)
        lines += [
            '',
            f'## {title}',
            '',
            f'{build}; {setup}. Exactly one booking: {booked} of {len(calls)}. Writes without a matching question: {unmatched}.',
            '',
            '| # | Coval | Langfuse | Booking | Writes (unmatched) | Turn latency p50 | Time to booking | Automatic problems | Review |',
            '|---|---|---|---|---|---|---|---|---|',
        ]
        for n, c in enumerate(calls, 1):
            p50 = sorted(c['latency'])[len(c['latency']) // 2] if c['latency'] else None
            session = f'[{c["call"][:10]}]({LANGFUSE}/{c["call"]})' if c['call'] else '-'
            booked_after = f'{c["booked_after"]:.0f} s' if c.get('booked_after') is not None else '-'
            lines.append(
                f'| {n} | [{c["simulation"][:8]}]({COVAL}/{c["run"]}/results/{c["simulation"]}) | {session} '
                f'| {"; ".join(c["bookings"]) or "none"} | {len(c["writes"])} ({len(c["unmatched"])}) '
                f'| {f"{p50:.2f} s" if p50 is not None else "-"} | {booked_after} '
                f'| {"; ".join(c["problems"]) or "none"} | {NOTES.get(c["simulation"][:8], "")} |'
            )
    return lines


def main() -> None:
    head = [
        '# Session 4: every Coval call',
        '',
        "Built by `coval_evals.py` from each run's `load/<run>/report.json` (`use-cases/concierge-tools/load_report.py`).",
        'The automatic columns come from Langfuse and the write records; Coval scored nothing but a turn count.',
        'The review column is what reading the call showed. A booking is correct when it is on the day the caller',
        "finally chose. Calls longer than the run's limit are marked by the report, not failed by it.",
        '',
        '> **Erratum, 26 September 2026.** These columns come from the session-4 `load_report.py`, which counted',
        '> booking receipts as appointments, read a matching question label as consent, never checked native',
        "> `confirm_customer`, and read a trace span as the call's length. The review notes for `YzkhPWRp` and",
        '> `3CyVWZmp` ("booked Saturday as a second appointment") are wrong: each moved the same BK0001. `iPFgi5qq`',
        '> did not reach the limit: its recording is 49.7 s. The rows are kept as they were. The rescored calls are',
        '> in [session 4b](../session-4b/inventory.md) ([report](../session-4b-evaluator.md)).',
    ]
    (HERE / 'coval-evals.md').write_text('\n'.join([*head, *rows()]) + '\n')


if __name__ == '__main__':
    main()
