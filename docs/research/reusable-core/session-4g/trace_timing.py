"""Per caller turn of a saved Langfuse trace: where the time went, each part kept apart.

    python3 trace_timing.py OBSERVATIONS.json

OBSERVATIONS.json is the `data` list of `langfuse api observations list --trace-id ... --json`, sorted by
start time. For each Pipecat `turn` span: its user-to-bot latency (Pipecat's own), the STT span's time to
first byte, each model request's duration, each tool's duration, and the first TTS span's time to first
byte. Nothing is added up across kinds, and nothing missing is written as zero.
"""

import json
import sys
from datetime import datetime
from pathlib import Path


def at(stamp: str) -> float:
    return datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()


def main() -> None:
    items = sorted(json.loads(Path(sys.argv[1]).read_text()), key=lambda o: o['startTime'])
    turns = [o for o in items if o['name'] == 'turn' and o['type'] == 'SPAN']
    print(
        '| turn start | caller said | user->bot s | STT ttfb s | model requests s | tools s | TTS ttfb s | first words |'
    )
    print('|---|---|---|---|---|---|---|---|')
    for turn in turns:
        start, end = at(turn['startTime']), at(turn['endTime']) if turn.get('endTime') else float('inf')
        inside = [o for o in items if start <= at(o['startTime']) < end]
        meta = turn.get('metadata') or {}
        latency = meta.get('turn.user_bot_latency_seconds')
        stt = [o for o in items if o['name'] == 'stt' and start - 0.5 <= at(o['endTime']) < end][:1]
        heard = stt[0].get('output') if stt else ''
        ttfb = (stt[0].get('metadata') or {}).get('metrics.ttfb') if stt else None
        models = [
            at(o['endTime']) - at(o['startTime'])
            for o in inside
            if o['type'] == 'GENERATION' and o['name'].startswith('chat')
        ]
        tools = [
            f'{o["name"]} {at(o["endTime"]) - at(o["startTime"]):.3f}' for o in inside if o['type'] == 'TOOL'
        ]
        tts = [o for o in inside if o['name'] == 'tts']
        tts_ttfb = (tts[0].get('metadata') or {}).get('metrics.ttfb') if tts else None
        said = tts[0].get('input') if tts else ''
        print(
            f'| {turn["startTime"][11:23]} | {str(heard)[:60]} | '
            f'{f"{latency:.2f}" if isinstance(latency, int | float) else "n/a"} | '
            f'{f"{ttfb:.2f}" if isinstance(ttfb, int | float) else "n/a"} | '
            f'{", ".join(f"{m:.2f}" for m in models) or "none"} | {"; ".join(tools) or "none"} | '
            f'{f"{tts_ttfb:.2f}" if isinstance(tts_ttfb, int | float) else "n/a"} | {str(said)[:50]} |'
        )


if __name__ == '__main__':
    main()
