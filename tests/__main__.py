"""Run: uv run python -m tests

Every offline module, with live model requests disabled rather than merely
unused. Each one also runs on its own -- `python -m tests.test_salon` -- and
that is what you want while you are working on one thing.

The difference between the two is worth knowing. Run on its own, a module is
safe because every agent it touches is overridden with a scripted model: safe
by convention, and a module that forgot one override would quietly call a
provider. Run through here, `ALLOW_MODEL_REQUESTS` is off before any scenario
module is imported, so a missed override raises instead of billing. That has
already caught one.

No pytest, no fixtures, no network. Each module prints `ok` or raises on its
first failure.
"""

import os
import runpy

import pydantic_ai.models

# Before the modules are imported, because importing one builds its agents.
pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

# And a placeholder key, because building a `GoogleModel` needs credentials
# even though nothing here will ever call it -- without one the salon's agents
# raise `DefaultCredentialsError` at import. `setdefault`, so a real key in the
# environment is left alone; it will not be used either way.
os.environ.setdefault('GOOGLE_API_KEY', 'offline-test-placeholder')
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')

# The comments are aligned on purpose; the formatter would unalign them.
# fmt: off
MODULES = (
    'tests.test_graph',   # the graph: routing, the waiting step, the schema
    'tests.test_salon',   # the salon agency end to end
    'tests.test_consent', # the caller's own words, and what may act on them
    'tests.test_traces',  # one call is one trace
    'tests.test_state',   # validated state, and the phone policy
    'tests.test_diary',   # the service oracle: can it say no
    'tests.test_flow',    # guards and conflicts, enforced at execution
    'tests.test_budget',  # one allowance per caller turn
    'tests.test_recovery', # live regressions and actual Pipecat queue cancellation
    'tests.test_history', # C3 projections at each provider request and late playback
    'tests.test_optimization', # local actions keep validation without provider requests
    'tests.test_core',    # the core and the Pipecat adapter agree
)
# fmt: on


def main() -> int:
    failed = []
    for module in MODULES:
        print(f'\n--- {module} ---', flush=True)
        try:
            runpy.run_module(module, run_name='__main__')
        except BaseException as error:
            # Carry on rather than stopping: one broken module should not hide
            # the state of the other nine, and the summary below is the thing
            # worth reading.
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
