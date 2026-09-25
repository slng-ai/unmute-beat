"""Run: uv run python -m tests

Every offline module. Each one also runs on its own,
`python -m tests.test_ledger`.

No model, no Coval and no Langfuse here: the tests read recorded traces and
scripted Coval replies. No pytest, no fixtures, no network. Each module prints
`ok` or raises on its first failure, and one broken module does not hide the
state of the others.
"""

import runpy

MODULES = ('tests.test_coval_sim', 'tests.test_ledger', 'tests.test_audio_timing')


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
        print(f'{len(failed)} of {len(MODULES)} modules failed:')
        for module in failed:
            print(f'  {module}')
        return 1
    print(f'all {len(MODULES)} modules ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
