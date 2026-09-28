"""Turn off one Stage B repair at a time in a scratch copy and show its regression fails. The worktree is untouched.

Run with the workspace interpreter: uv run --locked python mutations.py MUT_ROOT OUT.md
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])
OUT = Path(sys.argv[2])

MUTATIONS = [
    (
        'B1 a cancellation after dispatch is recorded as a refusal',
        'core/src/core/operations.py',
        "                    self._lost(call, intent, checked, audit, 'cancelled after dispatch')",
        "                    call.set_intent(intent.id, status='rejected')",
        'core',
        'tests.test_intents',
    ),
    (
        'B1 an invalid reconciliation answer clears the guard',
        'core/src/core/steps.py',
        '                data = found.model_dump() if isinstance(found, BaseModel) else found\n',
        "                call._unknown.pop(intent.id, None)\n                call.set_intent(intent.id, status='rejected')\n"
        '                data = found.model_dump() if isinstance(found, BaseModel) else found\n',
        'core',
        'tests.test_intents',
    ),
    (
        'B1 a read is recorded as an intent to reconcile',
        'core/src/core/operations.py',
        '        if not self.changes:\n            return\n        services = getattr(call',
        '        services = getattr(call',
        'core',
        'tests.test_tickets',
    ),
    (
        'B2 a later change does not supersede an earlier receipt',
        'core/src/core/state.py',
        '            latest.pop(key, None)\n            latest[key] = filed',
        '            latest.setdefault(key, filed)',
        'core',
        'tests.test_tickets',
    ),
    (
        'B2 a lookup counts as a standing change',
        'core/src/core/state.py',
        '            if not (filed.changes and filed.succeeded) or filed.identity != self.identity():',
        '            if not filed.succeeded or filed.identity != self.identity():',
        'core',
        'tests.test_tickets',
    ),
    (
        "B2 a withheld clause cuts the step's previous fixed line",
        'core/src/core/turn.py',
        "            if model := [s for s in call.spoken if s.kind == 'model']:\n"
        '                note_spoken(call, model[-1].text, interrupted=True, segment_ids=(model[-1].id,))',
        '            note_spoken(call, _delivered(call), interrupted=True)',
        'core',
        'tests.test_tickets',
    ),
    (
        'B3 the aggregate stop signal delivers',
        'core/src/core/state.py',
        "            and self.heard_by in ('mark', 'local', 'ack')\n            and self.played_at is not None\n",
        "            and self.heard_by in ('mark', 'local', 'ack', 'signal')\n",
        'core',
        'tests.test_delivery',
    ),
    (
        'B3 a clear does not void pending marks',
        'core/src/core/state.py',
        "                segment.cleared = True\n                self._log_playback('cleared'",
        "                self._log_playback('cleared'",
        'use-cases/salon-graph',
        'tests.test_marks',
    ),
    (
        'B3 the reply may begin before the mark',
        'core/src/core/state.py',
        '        return segment.played_at <= min(onsets) - self._answer_lead',
        '        return True',
        'use-cases/salon-graph',
        'tests.test_marks',
    ),
    (
        'B3 a mark with no media before it delivers',
        'core/src/core/state.py',
        '        if media == 0 and (segment := self._marked(name)) is not None:',
        '        if False and (segment := self._marked(name)) is not None:',
        'core',
        'tests.test_delivery',
    ),
    (
        'B4 read-backs are not bounded',
        'core/src/core/steps.py',
        'READBACKS = 2\n',
        'READBACKS = 99\n',
        'use-cases/salon-graph',
        'tests.test_recovery',
    ),
    (
        'B4 each joined piece gets a fresh budget',
        'core/src/core/turn.py',
        '            self.usage = call._turn_usage[1]',
        '            pass',
        'use-cases/salon-graph',
        'tests.test_recovery',
    ),
    (
        'B4 the ceiling owner does not cancel the timer',
        'core/src/core/processor.py',
        '            task.cancel()\n            with suppress(asyncio.CancelledError):\n                await task',
        '            pass',
        'use-cases/salon-graph',
        'tests.test_ceiling',
    ),
    (
        'B5 a reconciled flag alone excuses a write',
        'use-cases/concierge-tools/load_report.py',
        '        if not earlier:\n            continue\n        found.add(i)',
        '        found.add(i)',
        'use-cases/concierge-tools',
        'tests.test_load_report',
    ),
]


def run(member: str, module: str) -> int:
    env = {
        **os.environ,
        'PYTHONPATH': os.pathsep.join([str(ROOT / 'core' / 'src'), str(ROOT / member)]),
        'GOOGLE_API_KEY': 'offline-test-placeholder',
        'PYDANTIC_AI_NO_BANNER': '1',
    }
    done = subprocess.run(
        [
            sys.executable,
            '-c',
            f'import pydantic_ai.models as m; m.ALLOW_MODEL_REQUESTS = False; import runpy; runpy.run_module("{module}", run_name="__main__")',
        ],
        cwd=ROOT / member,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return done.returncode


rows = ['| Repair turned off | Regression | Exit with repair | Exit without |', '|---|---|---:|---:|']
failures = 0
for name, file, old, new, member, module in MUTATIONS:
    path = ROOT / file
    pristine = path.read_text()
    assert old in pristine, (name, 'the text to turn off is not in the file')
    clean = run(member, module)
    path.write_text(pristine.replace(old, new, 1))
    try:
        broken = run(member, module)
    finally:
        path.write_text(pristine)
    ok = clean == 0 and broken != 0
    failures += not ok
    rows.append(f'| {name} | `{member}` `{module}` | {clean} | {broken} |')
    print(name, clean, broken, 'ok' if ok else 'NOT DISCRIMINATING', flush=True)
OUT.write_text('\n'.join(rows) + '\n')
sys.exit(failures)
