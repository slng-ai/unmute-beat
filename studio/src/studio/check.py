"""`python -m studio.check path/to/agent.yaml`: the loader's verdict, the engine's own moves, and each operation, as JSON.

`FlowSpec.problems()` imports the application's state and operation modules to check them, so it
runs here, in a process of its own: an application's imports never land in the engine, and one
that hangs or crashes takes nothing else down. The manifest's package folder's parent goes on
the path first, as each use case's own folder is its import root (`use-cases/salon` holds `salon`).

A flow with no problems is then compiled on Pydantic AI's `TestModel`, which builds every agent
and calls no provider, so the moves come from the graph the engine walks (`Flow.moves`): exits,
consults and their hand-backs, prerequisites and their returns, and receipts' `route:` targets.
Each operation is reported with its closing lines (`say`, `recap`) and the fields its receipts
have, which is what the editor checks a closing line's placeholders against.

    {"problems": ["step: ..."], "moves": {"step": ["step", ...]}, "operations": [{"name": ..., ...}]}
"""

import json
import sys
import typing
from pathlib import Path
from typing import Any

from pydantic import BaseModel, RootModel
from pydantic_ai.models.test import TestModel

import core
from core.flow import Flow


def receipt_fields(receipt: type[BaseModel]) -> list[str]:
    """Every field a receipt of this type can have: a union's members' together, in the order they come."""
    if issubclass(receipt, RootModel):
        root = receipt.model_fields['root'].annotation
        while typing.get_origin(root) is typing.Annotated:
            root = typing.get_args(root)[0]
        members = [m for m in typing.get_args(root) if isinstance(m, type) and issubclass(m, BaseModel)]
    else:
        members = [receipt]
    names: list[str] = []
    for member in members:
        names += [name for name in member.model_fields if name not in names]
    return names


def operations(flow: core.StepFlow) -> list[dict[str, Any]]:
    found = []
    for op in flow.operations():
        receipt = getattr(op, 'receipt_type', None)
        found.append(
            {
                'name': op.name,
                'say': getattr(op, 'say', ''),
                'recap': op.recap,
                'changes': op.changes,
                'approval': 'readback' if op.needs_approval else 'none',
                'fields': receipt_fields(receipt) if isinstance(receipt, type) else [],
                'route': op.route,
            }
        )
    return found


def check(manifest: Path) -> dict[str, object]:
    sys.path.insert(0, str(manifest.resolve().parent.parent))
    spec = core.load(manifest)
    found = spec.problems()
    if found:
        return {'problems': found, 'moves': {}, 'operations': []}
    flow = spec.compile(model=TestModel())
    return {
        'problems': [],
        'moves': {step: sorted(to) for step, to in Flow.of(flow.nodes).moves.items()},
        'operations': operations(flow),
    }


if __name__ == '__main__':
    print(json.dumps(check(Path(sys.argv[1]))))
