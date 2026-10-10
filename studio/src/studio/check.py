"""`python -m studio.check path/to/agent.yaml`: the loader's verdict, the engine's own moves, and each operation, as JSON.

`FlowSpec.problems()` imports the application's state and operation modules to check them, so it
runs here, in a process of its own: an application's imports never land in the engine, and one
that hangs or crashes takes nothing else down. The manifest's package folder's parent goes on
the path first, as each use case's own folder is its import root (`use-cases/salon` holds `salon`).

A flow with no problems is then compiled on Pydantic AI's `TestModel`, which builds every agent
and calls no provider, so the moves come from the graph the engine walks (`Flow.moves`): exits,
consults and their hand-backs, prerequisites and their returns, and receipts' `route:` targets.
Each operation is reported with its closing lines (`say`, `recap`: one line, or cases by result), the
fields a success has and the fields a refusal has, and the values a closed field can hold, which is what
the editor checks a closing line's placeholders and each case's `when` against.

    {"problems": ["step: ..."], "moves": {"step": ["step", ...]}, "operations": [{"name": ..., ...}]}
"""

import json
import sys
import typing
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import Any

from pydantic import BaseModel, RootModel
from pydantic_ai.models.test import TestModel

import core
from core.closing import Closing
from core.flow import Flow


def _members(receipt: type[BaseModel]) -> list[type[BaseModel]]:
    """The receipt models a return can be: a union's members, or the one model."""
    if not issubclass(receipt, RootModel):
        return [receipt]
    root = receipt.model_fields['root'].annotation
    while typing.get_origin(root) is typing.Annotated:
        root = typing.get_args(root)[0]
    return [m for m in typing.get_args(root) if isinstance(m, type) and issubclass(m, BaseModel)]


def _names(members: Sequence[type[BaseModel]]) -> list[str]:
    names: list[str] = []
    for member in members:
        names += [name for name in member.model_fields if name not in names]
    return names


def receipt_fields(receipt: type[BaseModel]) -> list[str]:
    """Every field a receipt of this type can have: a union's members' together, in the order they come."""
    return _names(_members(receipt))


def outcomes(op: Any) -> tuple[list[type[BaseModel]], list[type[BaseModel]]]:
    """The receipt models a success can be and the ones a refusal can be: a closing line names only its own result's.

    A union's `core.Rejection` members are its refusals and the rest its successes. One model with
    `succeeds` or `rejects` (`{status: booked}`) can be either, so it is on both sides; a read that
    declares neither is refused by anything that does not validate, which has no fields to say.
    """
    receipt = getattr(op, 'receipt_type', None)
    if not isinstance(receipt, type):
        return [], []
    members = _members(receipt)
    refusals: list[type[BaseModel]] = [m for m in members if issubclass(m, core.Rejection)]
    successes = [m for m in members if m not in refusals]
    can_refuse = bool(getattr(op, 'succeeds', None) or getattr(op, 'rejects', None))
    return successes, refusals or (successes if can_refuse else [])


def _choices(annotation: Any) -> list[Any] | None:
    """The values a field can only ever hold (a `Literal`, an `Enum`, a `bool`), for a `when:` to pick from."""
    while typing.get_origin(annotation) is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
    if annotation is bool:
        return [True, False]
    if typing.get_origin(annotation) is typing.Literal:
        return list(typing.get_args(annotation))
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return [m.value for m in annotation]
    if typing.get_origin(annotation) in (typing.Union, UnionType):
        found = [c for a in typing.get_args(annotation) if a is not type(None) for c in _choices(a) or []]
        return found or None
    return None


def field_values(members: Sequence[type[BaseModel]]) -> dict[str, list[Any]]:
    """Each field with a closed set of values over these receipt models, and those values."""
    values: dict[str, list[Any]] = {}
    for member in members:
        for name, info in member.model_fields.items():
            for value in _choices(info.annotation) or []:
                if value not in values.setdefault(name, []):
                    values[name].append(value)
    return {name: found for name, found in values.items() if found}


def _words(words: Any) -> Any:
    """A closing line as JSON: one line as text, lines by result as their cases."""
    return words.model_dump(mode='json', exclude_none=True) if isinstance(words, Closing) else words or ''


def operations(flow: core.StepFlow) -> list[dict[str, Any]]:
    found = []
    for op in flow.operations():
        succeeded, refused = outcomes(op)
        found.append(
            {
                'name': op.name,
                'say': _words(getattr(op, 'say', '')),
                'recap': _words(op.recap),
                'changes': op.changes,
                'approval': 'readback' if op.needs_approval else 'none',
                'fields': _names(succeeded),
                'refused_fields': _names(refused),
                'values': field_values(succeeded),
                'refused_values': field_values(refused),
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
