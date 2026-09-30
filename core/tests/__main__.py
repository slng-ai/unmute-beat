"""Run: uv run python -m tests

Every offline module, with live model requests disabled rather than merely
unused. Each one also runs on its own, `python -m tests.test_history`, which is
what you want while working on one thing.

The difference is worth knowing. Run on its own, a module is safe because
every agent it touches is overridden with a scripted model: safe by
convention, and a module that forgot one override would quietly call a
provider. Run through here, `ALLOW_MODEL_REQUESTS` is off before any scenario
module is imported, so a missed override raises instead of billing.

A placeholder model key is set because building a `GoogleModel` needs
credentials even though nothing here will ever call it; `setdefault`, so a
real key in the environment is left alone and still not used.

No pytest, no fixtures, no network. Each module prints `ok` or raises on its
first failure, and one broken module does not hide the state of the others.
"""

import os
import runpy

import pydantic_ai.models

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
os.environ.setdefault('GOOGLE_API_KEY', 'offline-test-placeholder')
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')

MODULES = (
    'tests.test_classes',
    'tests.test_history',
    'tests.test_engine',
    'tests.test_clock',
    'tests.test_values',
    'tests.test_actions',
    'tests.test_context',
    'tests.test_structured',
    'tests.test_manifest',
    'tests.test_package',
    'tests.test_delivery',
    'tests.test_public',
    'tests.test_session',
    'tests.test_sdk',
    'tests.test_authoring',
    'tests.test_nodes',
    'tests.test_intents',
    'tests.test_tickets',
    'tests.test_flow_rules',
    'tests.test_outcomes',
    'tests.test_webrtc',
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
        print(f'{len(failed)} of {len(MODULES)} modules failed:')
        for module in failed:
            print(f'  {module}')
        return 1
    print(f'all {len(MODULES)} modules ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
