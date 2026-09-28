"""Break one protection at a time, run Faro's suite, and report whether the suite noticed. Restores every file."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(
    '/Users/nicoferdi/Documents/GitHub/slng_voice_harness/.claude/worktrees/cleanup/use-cases/faro-language-learning'
)
MUTATIONS = {
    'help is ignored': (
        'faro/functions.py',
        'helped = session.checkpoint.helped == waiting',
        'helped = False',
    ),
    'a missed item before is ignored': (
        'faro/functions.py',
        "if result == 'independent' and (helped or _missed_before(plan, session, waiting)):",
        "if result == 'independent' and helped:",
    ),
    'the country code check is off': (
        'faro/state.py',
        '    if not said_country_code(context',
        '    if False and not said_country_code(context',
    ),
    'answers need not be the caller words': ('faro/agent.yaml', '    quoted: [answer]\n', ''),
    'learner facts before confirmation': (
        'faro/state.py',
        '        if not self.confirmed and any(known):',
        '        if False and not self.confirmed and any(known):',
    ),
    'a coach answer is not assisted back in the lesson': (
        'faro/functions.py',
        "        await _write(faro, session, checkpoint.model_copy(update={'helped': waiting}))",
        '        pass',
    ),
}
env = {**os.environ, 'GOOGLE_API_KEY': 'x', 'PYDANTIC_AI_NO_BANNER': '1'}
for name, (file, old, new) in MUTATIONS.items():
    path = ROOT / file
    original = path.read_text()
    assert old in original, (name, old)
    path.write_text(original.replace(old, new))
    try:
        result = subprocess.run(
            [sys.executable, '-m', 'tests.test_faro'],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        path.write_text(original)
    tail = [line for line in result.stderr.splitlines() if 'Error' in line or 'assert' in line][-1:] or ['']
    print(f'{"caught" if result.returncode else "MISSED"}: {name} :: {tail[0][:160]}')
