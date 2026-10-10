"""`python -m studio.check path/to/agent.yaml`: the loader's verdict and the engine's own moves, as JSON.

`FlowSpec.problems()` imports the application's state and operation modules to check them, so it
runs here, in a process of its own: an application's imports never land in the engine, and one
that hangs or crashes takes nothing else down. The manifest's package folder's parent goes on
the path first, as each use case's own folder is its import root (`use-cases/salon` holds `salon`).

A flow with no problems is then compiled on Pydantic AI's `TestModel`, which builds every agent
and calls no provider, so the moves come from the graph the engine walks (`Flow.moves`): exits,
consults and their hand-backs, prerequisites and their returns, and receipts' `route:` targets.

    {"problems": ["step: ..."], "moves": {"step": ["step", ...]}}
"""

import json
import sys
from pathlib import Path

from pydantic_ai.models.test import TestModel

import core
from core.flow import Flow


def check(manifest: Path) -> dict[str, object]:
    sys.path.insert(0, str(manifest.resolve().parent.parent))
    spec = core.load(manifest)
    found = spec.problems()
    if found:
        return {'problems': found, 'moves': {}}
    flow = spec.compile(model=TestModel())
    return {'problems': [], 'moves': {step: sorted(to) for step, to in Flow.of(flow.nodes).moves.items()}}


if __name__ == '__main__':
    print(json.dumps(check(Path(sys.argv[1]))))
