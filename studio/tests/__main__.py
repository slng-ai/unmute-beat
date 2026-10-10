"""Run: uv run python -m tests

Every studio module, offline: no Docker, no model, no network. Each one also runs on its own,
`python -m tests.test_discovery`. Asserts, no pytest; one failing module hides no other.
"""

import runpy

MODULES = (
    'tests.test_discovery',
    'tests.test_events',
    'tests.test_metrics',
    'tests.test_boot',
    'tests.test_server',
    'tests.test_cli',
    'tests.test_editor',
)


def main() -> int:
    failed = []
    for module in MODULES:
        print(f'\n--- {module} ---', flush=True)
        try:
            runpy.run_module(module, run_name='__main__')
        except BaseException as error:
            print(f'{module} FAILED: {type(error).__name__}: {error}', flush=True)
            failed.append(module)
    print()
    if failed:
        print(f'{len(failed)} of {len(MODULES)} modules failed:', *failed, sep='\n  ')
        return 1
    print(f'all {len(MODULES)} modules ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
