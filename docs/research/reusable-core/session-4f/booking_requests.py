"""Booking's model requests per caller turn, and what each `options` result led to, from saved live spans.

Reads the session 4e observation exports (`session-4e/live/*.observations.json`) and needs no network:

    python3 booking_requests.py ../session-4e/live/s4e-*.observations.json

For each caller turn that ran the booking step, it prints the order of model requests (`llm`) and the tools
they called, then counts what the request right after an `options` result did: `wait` alone means that
continuation decided nothing the application could not have done (`wait` looks the offer up itself).
"""

import collections
import json
import sys
from pathlib import Path

TOOLS = {
    'collect',
    'options',
    'wait',
    'book_appointment',
    'move_appointment',
    'cancel_appointment',
    'hand_over',
    'goodbye',
}


def ancestors(span: dict, by_id: dict) -> list[dict]:
    found = []
    while span.get('parent') not in (None, 'None') and span['parent'] in by_id:
        span = by_id[span['parent']]
        found.append(span)
    return found


def turns(path: str) -> list[list[str]]:
    spans = json.loads(Path(path).read_text())
    by_id = {s['id']: s for s in spans}
    grouped: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for span in spans:
        kind = 'llm' if span['name'].startswith('chat ') else span['name'] if span['name'] in TOOLS else ''
        if not kind:
            continue
        up = ancestors(span, by_id)
        if not any(a['name'] == 'booking run' for a in up):
            continue
        turn = next((a for a in up if a['name'] == 'turn'), None)
        if turn is not None:
            grouped[turn['id']].append((span['start'], kind))
    return [[kind for _, kind in sorted(steps)] for steps in grouped.values()]


def main() -> None:
    after_options: collections.Counter[str] = collections.Counter()
    requests: collections.Counter[int] = collections.Counter()
    for path in sys.argv[1:]:
        print(path)
        for sequence in turns(path):
            print('  ', ' '.join(sequence))
            requests[sequence.count('llm')] += 1
            for i, kind in enumerate(sequence):
                if kind != 'options':
                    continue
                rest = sequence[i + 1 :]
                if 'llm' not in rest:
                    after_options['no further request'] += 1
                    continue
                start = rest.index('llm') + 1
                nxt = rest[start:]
                end = nxt.index('llm') if 'llm' in nxt else len(nxt)
                after_options[' + '.join(nxt[:end]) or 'text'] += 1
    print('booking turns by model requests:', dict(sorted(requests.items())))
    print('what the request after an options result called:', dict(after_options))


if __name__ == '__main__':
    main()
