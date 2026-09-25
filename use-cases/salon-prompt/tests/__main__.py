"""Run: uv run python -m tests

Every offline module. Each one also runs on its own,
`python -m tests.test_baseline`.

The baseline has no Pydantic AI, so there is no `ALLOW_MODEL_REQUESTS` to
turn off: the tests call its tools on a diary and never start the service.
A placeholder model key is set because building the service reads one;
`setdefault`, so a real key in the environment is left alone and still not used.

No pytest, no fixtures, no network. Each module prints `ok` or raises on its
first failure, and one broken module does not hide the state of the others.
"""

import os
import runpy

os.environ.setdefault('GOOGLE_API_KEY', 'offline-test-placeholder')

MODULES = ('tests.test_baseline',)


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
