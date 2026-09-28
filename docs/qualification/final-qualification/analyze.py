"""Per-call model work and latency for one cohort, from each call's Langfuse session. Reads only; bills nothing.

    cd use-cases/concierge-tools && PYTHONPATH=. uv run --env-file ../../.env \
        python ../../docs/qualification/final-qualification/analyze.py normal RUN_ID PRIVATE_DIR > metrics-normal.json

The call SIDs are the runner's own record of the cohort's calls (`PRIVATE_DIR/state-<cohort>/runner.db`). Every
observation of every call is read with pagination (`ledger.fetch`) and kept in `PRIVATE_DIR/traces/`, which is
private: it holds what callers said. What this prints holds no speech, only counts and times.

Definitions:

* A request is one provider generation (`ledger.is_request`). One with no usage was cancelled, or its usage never
  arrived, and is counted apart. Tokens are Langfuse's split, counted once: `input` (not cached),
  `input_cached_tokens` and `output` (plus `output_reasoning_tokens`). Cost is each request's `operation.cost`.
* Time to any audio, per caller turn: Pipecat's `turn.user_bot_latency_seconds`, from the voice detector's stop
  to the bot's first audio leaving the server. Server side: it misses both network legs.
* Time to useful audio: the same, moved to the first audio of the turn that is not a holding line
  (`Locale.holding`), by the gap between the two TTS first bytes (`start + metrics.ttfb`). A turn whose only
  audio is a holding line has no useful audio and is counted apart. A holding line never shortens it.
* A filler turn is one whose first audio is a holding line.
* Model share, per turn: the summed wall time of that turn's completed requests, to show where the wait goes.
  Endpointing and transcription come from Pipecat's own breakdown in the bot log, which is kept privately.
"""

import json
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

from ledger import fetch, from_json, is_request, read_call, to_json

HOLDING = ('Let me have a look.', 'One moment.')


def quantile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, round(p * (len(ordered) - 1))))], 3)


def at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def call_metrics(sid: str, rows: list[dict]) -> dict:
    obs = [from_json(r) for r in rows]
    requests = [o for o in obs if is_request(o)]
    done = [o for o in requests if o.usage]
    tokens = {
        'input_uncached': sum(o.usage.get('input', 0) + o.usage.get('input_cache_creation', 0) for o in done),
        'input_cached': sum(o.usage.get('input_cached_tokens', 0) for o in done),
        'output': sum(o.usage.get('output', 0) + o.usage.get('output_reasoning_tokens', 0) for o in done),
    }
    cost = sum(float(o.attrs.get('operation.cost') or 0) for o in done)
    tts = [o for o in obs if o.name == 'tts']
    turns = []
    for turn in sorted((o for o in obs if o.name == 'turn'), key=lambda o: o.start):
        latency = turn.attrs.get('turn.user_bot_latency_seconds')
        mine = sorted((t for t in tts if t.parent == turn.id), key=lambda t: t.start)
        first = mine[0] if mine else None
        useful = next((t for t in mine if str(t.attrs.get('text', '')).strip() not in HOLDING), None)
        entry = {'number': turn.attrs.get('turn.number'), 'any_audio_s': float(latency) if latency else None}
        entry['filler_first'] = bool(first and str(first.attrs.get('text', '')).strip() in HOLDING)
        entry['useful_audio_s'] = None
        if entry['any_audio_s'] is not None and first and useful:

            def heard(t):
                return t.start.timestamp() + float(t.attrs.get('metrics.ttfb') or 0)

            entry['useful_audio_s'] = round(entry['any_audio_s'] + heard(useful) - heard(first), 3)
        entry['tts_ttfb_s'] = (
            float(first.attrs['metrics.ttfb']) if first and first.attrs.get('metrics.ttfb') else None
        )
        inside = [o for o in done if turn.start <= o.start and (turn.end is None or o.start <= turn.end)]
        entry['requests'] = len(inside)
        entry['model_s'] = round(sum((o.end - o.start).total_seconds() for o in inside if o.end), 3)
        turns.append(entry)
    ledger_call = read_call(sid, obs)
    return {
        'call_sid': sid,
        'observations': len(obs),
        'caller_turns': ledger_call.caller_turns,
        'requests_completed': len(done),
        'requests_cancelled_or_unreported': len(requests) - len(done),
        'tokens': tokens,
        'cost_usd': round(cost, 6),
        'writes': [{'name': w.name, 'status': w.status} for w in ledger_call.writes],
        'turns': turns,
    }


def main() -> None:
    cohort, run_id, private = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    db = sqlite3.connect(f'file:{private / f"state-{cohort}" / "runner.db"}?mode=ro', uri=True)
    sids = [r[0] for r in db.execute('SELECT call_sid FROM calls ORDER BY admitted_at')]
    db.close()
    traces = private / 'traces'
    traces.mkdir(exist_ok=True)
    calls = []
    for sid in sids:
        rows = [to_json(o) for o in fetch(sid)]
        (traces / f'{sid}.json').write_text(json.dumps(rows))
        calls.append(call_metrics(sid, rows))
    turns = [t for c in calls for t in c['turns'] if t['any_audio_s'] is not None]
    useful = [t['useful_audio_s'] for t in turns if t['useful_audio_s'] is not None]
    anyaudio = [t['any_audio_s'] for t in turns]
    summary = {
        'cohort': cohort,
        'run_id': run_id,
        'calls': len(calls),
        'eligible_turns': len(turns),
        'any_audio_s': {'median': quantile(anyaudio, 0.5), 'p90': quantile(anyaudio, 0.9)},
        'useful_audio_s': {'n': len(useful), 'median': quantile(useful, 0.5), 'p90': quantile(useful, 0.9)},
        'filler_turns': sum(t['filler_first'] for t in turns),
        'turns_with_no_useful_audio': sum(t['useful_audio_s'] is None for t in turns),
        'requests_completed': sum(c['requests_completed'] for c in calls),
        'requests_cancelled_or_unreported': sum(c['requests_cancelled_or_unreported'] for c in calls),
        'tokens': {
            k: sum(c['tokens'][k] for c in calls) for k in ('input_uncached', 'input_cached', 'output')
        },
        'cost_usd': round(sum(c['cost_usd'] for c in calls), 6),
        'model_s_per_turn_median': quantile([t['model_s'] for t in turns], 0.5),
        'tts_ttfb_s_median': quantile([t['tts_ttfb_s'] for t in turns if t['tts_ttfb_s'] is not None], 0.5),
        'per_call_input_tokens_median': statistics.median(
            [c['tokens']['input_uncached'] + c['tokens']['input_cached'] for c in calls]
        )
        if calls
        else None,
    }
    print(json.dumps({'summary': summary, 'calls': calls}, indent=1))


if __name__ == '__main__':
    main()
