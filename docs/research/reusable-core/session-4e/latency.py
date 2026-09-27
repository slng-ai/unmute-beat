"""Where a live call's answer time went, turn by turn, from its saved Langfuse observations.

Usage (from `use-cases/concierge-tools`, which has `ledger`):

    uv run python ../../docs/research/reusable-core/session-4e/latency.py <observations.json>

Every span here comes from the bot's one process, so their start and end
times share one clock; nothing is subtracted across processes. Per core
turn span (the `PydanticAgentProcessor` turn: `model_requests` on it):

* `useful`: core's `turn_first_useful_text_out`, turn start to the first
  text handed to TTS that is not a holding line;
* `model`: the model requests (`chat` generations) that ended before it,
  their durations summed, and how many;
* `tools`: tool spans (domain functions, lookups, writes) ended before it;
* `core`: what is left of `useful`: core's own work and scheduling;
* `tts_ttfb`: Pipecat's time to first audio byte for the first TTS request
  of the turn (`metrics.ttfb`);
* `user_bot`: Pipecat's own measure for the turn the core span sits in, the
  caller's stop to the bot's first audio (`turn.user_bot_latency_seconds`),
  which also holds endpointing and transcription.

Pipecat's endpointing and transcription parts are in the bot log (`latency
breakdown:` lines), not here.
"""

import json
import sys
from datetime import datetime


def when(value: str | None) -> float | None:
    return datetime.fromisoformat(value).timestamp() if value else None


rows = json.load(open(sys.argv[1]))
for o in rows:
    o['t0'], o['t1'] = when(o['start']), when(o['end'])
turns = sorted((o for o in rows if 'model_requests' in o['attrs']), key=lambda o: o['t0'])
chats = [o for o in rows if o['type'] == 'GENERATION' and o['name'].startswith('chat') and o['t1']]
tools = [o for o in rows if o['type'] == 'TOOL' and o['t1']]
tts = sorted((o for o in rows if o['name'].startswith('tts')), key=lambda o: o['t0'])
pipecat_turns = [o for o in rows if o['name'] == 'turn' and o['t1']]
print('turn  agent     status       req  useful  model(n)     tools   core   tts_ttfb  user_bot')
totals = {'useful': [], 'model': [], 'tools': [], 'core': [], 'tts': []}
for index, turn in enumerate(turns, 1):
    attrs = turn['attrs']
    useful = float(attrs['turn_first_useful_text_out']) if 'turn_first_useful_text_out' in attrs else None
    edge = turn['t0'] + useful if useful is not None else turn['t1']
    before = [c for c in chats if c['t0'] >= turn['t0'] and c['t1'] <= edge]
    model = sum(c['t1'] - c['t0'] for c in before)
    tool = sum(t['t1'] - t['t0'] for t in tools if t['t0'] >= turn['t0'] and t['t1'] <= edge)
    core = useful - model - tool if useful is not None else None
    first_tts = next((t for t in tts if t['t0'] >= turn['t0']), None)
    ttfb = float(first_tts['attrs'].get('metrics.ttfb', 'nan')) if first_tts else None
    around = next((p for p in pipecat_turns if p['t0'] <= turn['t0'] <= p['t1']), None)
    user_bot = around['attrs'].get('turn.user_bot_latency_seconds') if around else None
    if useful is not None:
        totals['useful'].append(useful)
        totals['model'].append(model)
        totals['tools'].append(tool)
        totals['core'].append(core)
    if ttfb is not None:
        totals['tts'].append(ttfb)
    print(
        f'{index:4}  {attrs.get("agent", ""):8}  {attrs.get("status", ""):11} {attrs.get("model_requests", ""):>4}  '
        f'{useful if useful is not None else "-":>6}  {model:5.2f}({len(before)})  {tool:6.2f}  '
        f'{core if core is not None else float("nan"):5.2f}  {ttfb if ttfb is not None else float("nan"):8.2f}  '
        f'{user_bot or "-"}'
    )


def median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2] if ordered else float('nan')


print(
    'median  useful {:.2f}  model {:.2f}  tools {:.2f}  core {:.2f}  tts_ttfb {:.2f}'.format(
        *(median(totals[k]) for k in ('useful', 'model', 'tools', 'core', 'tts'))
    )
)
usage = [o['usage'] for o in chats]
print(
    f'model requests {len(chats)}; input tokens {sum(u.get("input", 0) for u in usage)}, '
    f'cached {sum(u.get("input_cached_tokens", 0) or u.get("cache_read_input_tokens", 0) for u in usage)}, '
    f'output {sum(u.get("output", 0) for u in usage)}'
)
