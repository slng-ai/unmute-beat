"""Known defects: checks that fail today on purpose, kept out of `python -m tests`.

Run: uv run python -m tests.defects

Each check states the behaviour the harness should have. It prints
`KNOWN DEFECT` and the session that owns the repair while the defect is still
there, and `fixed` once it is gone. The exit code is the number of defects
still present, so session 2 can see its repair land, and then move the check
into `tests/__main__.py`, where a regression fails the suite.

Not in the suite, because a suite that is red by design stops being read, and
not deleted, because a defect with no reproducible check is only a rumour.
"""

from collections.abc import Callable

DEFECTS: dict[str, tuple[Callable[[], bool], int]] = {}
"""Each defect, its check (True while the defect is present) and the session that owns the repair.

Session 1's three (a read-back never recorded, one queued and never played,
and core loading `.env` at import) were repaired in session 2 and now fail
the suite if they return: `tests.test_delivery` and `tests.test_package`.
"""


def main() -> int:
    present = 0
    for name, (check, session) in DEFECTS.items():
        if check():
            present += 1
            print(f'KNOWN DEFECT (session {session}): {name}')
        else:
            print(f'fixed: {name}')
    return present


if __name__ == '__main__':
    raise SystemExit(main())
