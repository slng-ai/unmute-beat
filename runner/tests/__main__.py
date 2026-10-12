"""Run: uv run python -m tests

Every offline module, with live model requests disabled before anything is imported.
"""

import os
import runpy

import pydantic_ai.models

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
os.environ.setdefault('GOOGLE_API_KEY', 'offline-test-placeholder')
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')

MODULES = ('tests.test_runner',)

failed = []
for module in MODULES:
    try:
        runpy.run_module(module, run_name='__main__')
    except BaseException as error:
        failed.append(module)
        print(f'{module} FAILED: {type(error).__name__}: {error}')
if failed:
    raise SystemExit(f'{len(failed)} of {len(MODULES)} modules failed: {", ".join(failed)}')
print(f'\nall {len(MODULES)} modules ok')
