"""Run each named regression of one test module on its own and print pass or the first failure.

For the revert checks and the reproducers: `python each.py tests.test_contract g1 g2 ...` from a member's
folder, with names as a prefix of the function names. Offline, like the suites: model requests are off.
"""

import asyncio
import importlib
import inspect
import os
import sys
import tempfile
import traceback
from pathlib import Path

import pydantic_ai.models

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
os.environ.setdefault('GOOGLE_API_KEY', 'offline-test-placeholder')
sys.path.insert(0, os.getcwd())


def main() -> int:
    module = importlib.import_module(sys.argv[1])
    wanted = sys.argv[2:]
    failed = 0
    for name, fn in inspect.getmembers(module, inspect.isfunction):
        if fn.__module__ != module.__name__ or not any(name.startswith(w) for w in wanted):
            continue
        try:
            params = inspect.signature(fn).parameters
            with tempfile.TemporaryDirectory() as folder:
                result = fn(Path(folder)) if params else fn()
                if inspect.iscoroutine(result):
                    asyncio.run(result)
            print(f'PASS {name}')
        except Exception as error:
            failed += 1
            frame = traceback.extract_tb(error.__traceback__)[-1]
            print(f'FAIL {name}: {type(error).__name__}: {str(error)[:300]} (line {frame.lineno})')
    return failed


if __name__ == '__main__':
    sys.exit(main())
