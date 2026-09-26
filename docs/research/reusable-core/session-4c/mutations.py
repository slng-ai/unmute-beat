"""Turn off one Stage C repair at a time in a scratch copy and show its regression fails. The worktree is untouched.

    git archive HEAD | tar -x -C MUT_ROOT
    uv run --locked python mutations.py MUT_ROOT OUT.md

Stage B's harness (`../session-4b-runtime/mutations.py`), with Stage C's
repairs. Each row runs the regression on the pristine copy (it must pass)
and with the repair turned off (it must fail).
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])
OUT = Path(sys.argv[2])
CORE, SALON, TOOLS = 'core', 'use-cases/salon-graph', 'use-cases/concierge-tools'

MUTATIONS = [
    (
        'C0.1 a pending receipt is a refusal',
        'core/src/core/steps.py',
        "        return 'rejected' if refused or not self.changes else 'unknown'",
        "        return 'rejected'",
        CORE,
        'tests.test_outcomes',
    ),
    (
        'C0.1 a retry signal after dispatch is a refusal',
        'core/src/core/operations.py',
        "                    self._lost(call, intent, checked, audit, f'ModelRetry after dispatch: {error}')\n"
        "                    raise OutcomeUnknownError(f'{self.name}: a retry signal after dispatch') from error",
        "                    call.set_intent(intent.id, status='rejected')\n                    raise",
        CORE,
        'tests.test_outcomes',
    ),
    (
        'C0.2 no claim before the first await',
        'core/src/core/operations.py',
        '        call._claimed.add(intent.id)\n',
        '',
        CORE,
        'tests.test_outcomes',
    ),
    (
        'C0.3 a reconciled old receipt is said as current',
        'core/src/core/operations.py',
        '        if succeeded and resource and not call.is_latest(kind, resource, version):',
        '        if False:',
        CORE,
        'tests.test_outcomes',
    ),
    (
        'C0.3 a restore reopens a finished intent',
        'core/src/core/state.py',
        "            if intent.status not in ('dispatched', 'unknown') or (\n"
        '                held is not None and held.status not in OPEN_INTENT\n'
        '            ):',
        "            if intent.status not in ('dispatched', 'unknown'):",
        CORE,
        'tests.test_outcomes',
    ),
    (
        'C0.4 a resource is keyed by its id alone',
        'core/src/core/state.py',
        "                ('resource', filed.kind, filed.resource)",
        "                ('resource', filed.resource)",
        CORE,
        'tests.test_outcomes',
    ),
    (
        'C0.5 an estimate delivers without a simulation (core)',
        'core/src/core/state.py',
        "        evidence = self.heard_by in ('mark', 'ack') or (self.heard_by == 'local' and self.simulated)",
        "        evidence = self.heard_by in ('mark', 'ack', 'local')",
        CORE,
        'tests.test_delivery',
    ),
    (
        'C0.5 an estimate delivers without a simulation (adapter)',
        'core/src/core/state.py',
        "        evidence = self.heard_by in ('mark', 'ack') or (self.heard_by == 'local' and self.simulated)",
        "        evidence = self.heard_by in ('mark', 'ack', 'local')",
        SALON,
        'tests.test_marks',
    ),
    (
        'C0.6 a rendered step streams model text',
        'core/src/core/steps.py',
        '            model_speech=not self.rendered,',
        '            model_speech=True,',
        CORE,
        'tests.test_speech',
    ),
    (
        'C0.6 a rendered step may answer in text',
        'core/src/core/steps.py',
        '            *([] if self.rendered else [str]),',
        '            str,',
        CORE,
        'tests.test_speech',
    ),
    (
        'C1 the chair trigger is off',
        'use-cases/salon-graph/salon/diary.py',
        'CREATE TRIGGER IF NOT EXISTS one_per_chair_insert BEFORE INSERT ON bookings\nWHEN NEW.active = 1 AND EXISTS (',
        'CREATE TRIGGER IF NOT EXISTS one_per_chair_insert BEFORE INSERT ON bookings\nWHEN 0 AND EXISTS (',
        SALON,
        'tests.test_store',
    ),
    (
        'C1 reconciliation files no void',
        'use-cases/salon-graph/salon/diary.py',
        "            db.execute('INSERT INTO operations VALUES (?, ?)', (operation_token, json.dumps(void)))\n",
        '',
        SALON,
        'tests.test_store',
    ),
    (
        'C1 a stale revision is not checked',
        'use-cases/salon-graph/salon/diary.py',
        "            if action in ('move', 'cancel') and expected_revision not in (None, mine[booking_id].revision):",
        '            if False:',
        SALON,
        'tests.test_store',
    ),
    (
        'C2 admission has no ceiling',
        'use-cases/salon-graph/server.py',
        '            if busy >= MAX_CALLS:',
        '            if False:',
        SALON,
        'tests.test_runner',
    ),
    (
        "C2 a dead worker's call is never closed",
        'use-cases/salon-graph/server.py',
        "            if not self._alive(row['worker']):",
        '            if False:',
        SALON,
        'tests.test_runner',
    ),
    (
        'C2 a campaign restores over a later change',
        'use-cases/concierge-tools/coval_sim/runtime.py',
        '        if now == self.url:',
        '        if True:',
        TOOLS,
        'tests.test_coval_sim',
    ),
    (
        'C2 a campaign restores with calls still running',
        'use-cases/concierge-tools/coval_sim/runtime.py',
        '        while left := self.calls_left():',
        '        while (left := self.calls_left()) and False:',
        TOOLS,
        'tests.test_coval_sim',
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
        timeout=900,
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
