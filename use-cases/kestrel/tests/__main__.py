"""Run: uv run python -m tests

Every offline module, with live model requests disabled rather than merely
unused: `ALLOW_MODEL_REQUESTS` is off before any module is imported, so a
test that forgot the scripted model raises instead of billing. A placeholder
model key is set because building a `GoogleModel` needs one even though
nothing here calls it. The claims desk is the in-memory fake (`kestrel.claims`):
no network.
"""

import os
import runpy

import pydantic_ai.models

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
os.environ.setdefault('GOOGLE_API_KEY', 'offline-test-placeholder')
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')

MODULES = ('tests.test_kestrel',)


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
