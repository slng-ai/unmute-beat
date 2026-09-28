"""Session 4g's revert checks: put each repair back alone, run its regressions, and expect them to fail.

Run from the repository root on a clean tree: `python3 docs/research/reusable-core/session-4g/reverts.py`.
Each revert edits one file, runs the named checks through `each.py` in the member they live in, and
restores the file from git. The output is `reverts.txt` beside this file.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent
STEPS = 'core/src/core/steps.py'
TURN = 'core/src/core/turn.py'

REVERTS = [
    (
        'A: a pick judged only on the offer standing before the update (no _record)',
        STEPS,
        [
            (
                'unoffered, made = await self._record(flow, call, asked)',
                'unoffered, made = flow.record(call, asked, self.choices), {}',
            )
        ],
        [
            ('use-cases/salon-graph', 'tests.test_contract', ['a_time_and_its_slot']),
            ('core', 'tests.test_tickets', ['a_pick_counts_on_the_offer']),
        ],
    ),
    (
        'B: a receipt said again answers the turn (answers=False removed)',
        TURN,
        [('lambda: _say_again(call, told), answers=False)', 'lambda: _say_again(call, told))')],
        [
            ('use-cases/salon-graph', 'tests.test_contract', ['a_request_over_a_cut_receipt']),
            ('core', 'tests.test_tickets', ['a_request_over_a_cut_receipt']),
            ('core', 'tests.test_nodes', ['an_unheard_receipt_is_said_again']),
        ],
    ),
    (
        'C: a refused value read only from the wrapped argument shape',
        STEPS,
        [("values = sent.get('values', sent) or {}", "values = sent.get('values') or {}")],
        [('use-cases/salon-graph', 'tests.test_contract', ['a_refused_value'])],
    ),
    (
        'D: the rendered step keeps its options tool',
        STEPS,
        [('if self.choices and not self.rendered:', 'if self.choices:')],
        [('use-cases/salon-graph', 'tests.test_contract', ['a_cancel_of_a_held_booking'])],
    ),
    (
        'E: wait and say_again add to a receipt said again on the same piece',
        STEPS,
        [
            ('answered = call.receipt_said_again()', 'answered = False'),
            ('if words and call.receipt_said_again():', 'if False:'),
        ],
        [
            ('use-cases/salon-graph', 'tests.test_contract', ['an_agreement_over_a_cut_receipt']),
            ('core', 'tests.test_tickets', ['a_request_over_a_cut_receipt']),
        ],
    ),
    (
        'F: a refused pick is not offered again by code',
        STEPS,
        [('if name not in call._offers or name in refused:', 'if name not in call._offers:')],
        [('core', 'tests.test_tickets', ['a_rendered_step_offers_in_the_desks_words'])],
    ),
]


def run(member: str, module: str, names: list[str]) -> str:
    each = HERE / 'each.py'
    done = subprocess.run(
        ['uv', 'run', '--directory', member, 'python', str(each), module, *names],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=os.environ | {'LOGURU_LEVEL': 'CRITICAL', 'PYDANTIC_AI_NO_BANNER': '1'},
    )
    return '\n'.join(line for line in done.stdout.splitlines() if line.startswith(('PASS', 'FAIL')))


def main() -> int:
    lines, missed = [], 0
    for title, path, edits, checks in REVERTS:
        source = ROOT / path
        original = source.read_text()
        text = original
        for old, new in edits:
            assert old in text, (title, old)
            text = text.replace(old, new)
        source.write_text(text)
        try:
            lines.append(f'## {title}')
            for member, module, names in checks:
                result = run(member, module, names)
                lines.append(result or f'(no result from {member} {module} {names})')
                if 'FAIL' not in result:
                    missed += 1
                    lines.append('   ^ NOT CAUGHT')
        finally:
            source.write_text(original)
    subprocess.run(['git', 'diff', '--exit-code', '--stat', STEPS, TURN], cwd=ROOT, check=True)
    (HERE / 'reverts.txt').write_text('\n'.join(lines) + f'\n\nnot caught: {missed}\n')
    print('\n'.join(lines), f'\nnot caught: {missed}')
    return missed


if __name__ == '__main__':
    sys.exit(main())
