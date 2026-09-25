"""Run named checks inside one build's own environment, and print what each did, as JSON.

Started by the matrix driver with `uv run --directory <member> python <this file> CHECK...`,
so it imports that member's code and nothing else. A check is either an
existing assert function (`tests.test_x:name`: passed if it returns) or a
line scenario from the member's `evals.matrix` (`scenario:name[:delay]`:
passed as its observation says). `@scripted-readings` runs a check under the
salon suite's scripted readings, as its module's own `__main__` does. Real model requests are refused before
anything is imported, and every check runs on its own event loop.
"""

import asyncio
import contextlib
import importlib
import inspect
import json
import os
import sys
import time
import traceback

import pydantic_ai.models

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
os.environ.setdefault('GOOGLE_API_KEY', 'offline-test-placeholder')
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')
sys.path.insert(0, os.getcwd())


def run(ref: str) -> dict:
    started = time.perf_counter()
    result: dict = {'check': ref}
    try:
        if ref.startswith('scenario:'):
            _, name, *delay = ref.split(':')
            scenarios = importlib.import_module('evals.matrix').SCENARIOS
            kwargs = {'delay': float(delay[0])} if delay else {}
            observation = asyncio.run(scenarios[name](**kwargs))
            result.update(passed=bool(observation.get('passed')), observation=observation)
        else:
            target, _, wrap = ref.partition('@')
            module, _, name = target.partition(':')
            fn = getattr(importlib.import_module(module), name)
            with contextlib.ExitStack() as stack:
                if wrap == 'scripted-readings':
                    stack.enter_context(importlib.import_module('tests.classifier_model').classifier_model())
                outcome = fn()
                if inspect.isawaitable(outcome):
                    asyncio.run(outcome)
            result.update(passed=True)
    except Exception as error:
        result.update(
            passed=False,
            error=f'{type(error).__name__}: {error}'[:2000],
            trace=traceback.format_exc()[-3000:],
        )
    result['seconds'] = round(time.perf_counter() - started, 3)
    return result


if __name__ == '__main__':
    results = [run(ref) for ref in sys.argv[1:]]
    print('MATRIX ' + json.dumps(results, default=str))
