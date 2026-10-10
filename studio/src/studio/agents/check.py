"""`python -m studio.agents.check path/to/agent.yaml`: the loader's verdict, the engine's moves and each operation, as JSON.

`FlowSpec.problems()` imports the application's state and operation modules to check them, so it
runs here, in a process of its own: an application's imports never land in the engine, and one
that hangs or crashes takes nothing else down. The manifest's package folder's parent goes on
the path first, as each use case's own folder is its import root (`use-cases/salon` holds `salon`).

A flow with no problems is then compiled on Pydantic AI's `TestModel`, which builds every agent
and calls no provider, so the moves come from the graph the engine walks (`Flow.moves`). Each
operation is reported with its closing lines and its receipt the way the state is described: the
receipt type's JSON schema, and for a success and for a refusal every field by its path
(`customer.name` inside a nested model) and the values a closed field can hold. The editor checks a
closing line's placeholders and each case's `when` against them; the web page draws a receipt from
the same schema it draws the state from.

    {"problems": [...], "moves": {"step": ["step", ...]}, "operations": [{"name": ..., "schema": {...}, ...}]}
"""

import json
import sys
import typing
from collections.abc import Iterator, Sequence
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import Any

from pydantic import BaseModel, RootModel
from pydantic_ai.models.test import TestModel

import core
from core.closing import Closing
from core.flow import Flow


class FieldType:
    """One field's annotation, unwrapped: the models inside it, and the values it can only ever hold."""

    def __init__(self, annotation: Any) -> None:
        while typing.get_origin(annotation) is typing.Annotated:
            annotation = typing.get_args(annotation)[0]
        self.annotation = annotation

    def options(self) -> list[FieldType]:
        """A union's members, `None` left out; any other type is its own one option."""
        if typing.get_origin(self.annotation) in (typing.Union, UnionType):
            return [
                FieldType(member) for member in typing.get_args(self.annotation) if member is not type(None)
            ]
        return [self]

    def models(self) -> list[type[BaseModel]]:
        """The models this field holds, alone or as the items of a list."""
        return [model for option in self.options() for model in option._own_models()]

    def closed(self) -> list[Any]:
        """The values the field can only ever hold (a `Literal`, an `Enum`, a `bool`); empty when it is open."""
        return list(dict.fromkeys(value for option in self.options() for value in option._own_values()))

    def _own_models(self) -> list[type[BaseModel]]:
        """The model this one option is, or the models its list items are."""
        if isinstance(self.annotation, type) and issubclass(self.annotation, BaseModel):
            return [self.annotation]
        if typing.get_origin(self.annotation) in (list, tuple):
            return [model for arg in typing.get_args(self.annotation) for model in FieldType(arg).models()]
        return []

    def _own_values(self) -> list[Any]:
        """The closed values of this one option."""
        if self.annotation is bool:
            return [True, False]
        if typing.get_origin(self.annotation) is typing.Literal:
            return list(typing.get_args(self.annotation))
        if isinstance(self.annotation, type) and issubclass(self.annotation, Enum):
            return [member.value for member in self.annotation]
        return []


class ModelFields:
    """Every field of some models by its path, nested models walked into, as a closing line names them."""

    DEPTH = 4

    def __init__(self, models: Sequence[type[BaseModel]]) -> None:
        self.models = list(models)

    def paths(self) -> list[str]:
        """Each field's path, a parent before its children: `customer`, `customer.name`, `items.spoken`."""
        return list(dict.fromkeys(path for path, _ in self._walk(self.models, '', 0)))

    def values(self) -> dict[str, list[Any]]:
        """Each path whose field has a closed set of values, and those values."""
        found: dict[str, list[Any]] = {}
        for path, kind in self._walk(self.models, '', 0):
            found.setdefault(path, [])
            found[path] += [value for value in kind.closed() if value not in found[path]]
        return {path: values for path, values in found.items() if values}

    def _walk(
        self, models: Sequence[type[BaseModel]], prefix: str, depth: int
    ) -> Iterator[tuple[str, FieldType]]:
        """Each field under these models with its type, then the fields of the models inside it."""
        for model in models:
            for name, info in model.model_fields.items():
                kind, path = FieldType(info.annotation), f'{prefix}{name}'
                yield path, kind
                if depth < self.DEPTH and (inner := kind.models()):
                    yield from self._walk(inner, f'{path}.', depth + 1)


class ReceiptShape:
    """What an operation's receipts can hold: the models a success and a refusal can be, and the receipt's schema.

    A union's `core.Rejection` members are its refusals and the rest its successes. One model with
    `succeeds` or `rejects` (`{status: booked}`) can be either, so it is on both sides; a read that
    declares neither is refused by anything that does not validate, which has no fields to say.
    """

    def __init__(self, operation: Any) -> None:
        receipt = getattr(operation, 'receipt_type', None)
        self.receipt = receipt if isinstance(receipt, type) and issubclass(receipt, BaseModel) else None
        members = self._members(self.receipt) if self.receipt is not None else []
        refusals = [member for member in members if issubclass(member, core.Rejection)]
        self.successes = [member for member in members if member not in refusals]
        can_refuse = bool(getattr(operation, 'succeeds', None) or getattr(operation, 'rejects', None))
        self.refusals = refusals or (self.successes if can_refuse else [])

    @staticmethod
    def _members(receipt: type[BaseModel]) -> list[type[BaseModel]]:
        """The receipt models a return can be: a union's members, or the one model."""
        if not issubclass(receipt, RootModel):
            return [receipt]
        return FieldType(receipt.model_fields['root'].annotation).models()

    def schema(self) -> dict[str, Any]:
        """The receipt type's JSON schema, as the state's is given: a union is its members, each with its fields."""
        return self.receipt.model_json_schema() if self.receipt is not None else {}

    def report(self) -> dict[str, Any]:
        """Fields and closed values, of a success and of a refusal, and the schema."""
        done, refused = ModelFields(self.successes), ModelFields(self.refusals)
        return {
            'fields': done.paths(),
            'refused_fields': refused.paths(),
            'values': done.values(),
            'refused_values': refused.values(),
            'schema': self.schema(),
        }


class OperationReport:
    """One operation as the editor shows it: its closing lines, what it does, and its receipt's shape."""

    def __init__(self, operation: Any) -> None:
        self.operation = operation

    @staticmethod
    def words(words: Any) -> Any:
        """A closing line as JSON: one line as text, lines by result as their cases."""
        return words.model_dump(mode='json', exclude_none=True) if isinstance(words, Closing) else words or ''

    def report(self) -> dict[str, Any]:
        """The operation, as JSON."""
        operation = self.operation
        return {
            'name': operation.name,
            'say': self.words(getattr(operation, 'say', '')),
            'recap': self.words(operation.recap),
            'changes': operation.changes,
            'approval': 'readback' if operation.needs_approval else 'none',
            'route': operation.route,
        } | ReceiptShape(operation).report()


class AgentCheck:
    """One manifest checked by the loader, and, when clean, compiled on a test model for its moves and operations."""

    def __init__(self, manifest: Path) -> None:
        self.manifest = manifest

    def run(self) -> dict[str, Any]:
        """The loader's problems; with none, the engine's moves and every operation's report."""
        sys.path.insert(0, str(self.manifest.resolve().parent.parent))
        spec = core.load(self.manifest)
        if problems := spec.problems():
            return {'problems': problems, 'moves': {}, 'operations': []}
        flow = spec.compile(model=TestModel())
        return {
            'problems': [],
            'moves': {step: sorted(to) for step, to in Flow.of(flow.nodes).moves.items()},
            'operations': [OperationReport(operation).report() for operation in flow.operations()],
        }


if __name__ == '__main__':
    print(json.dumps(AgentCheck(Path(sys.argv[1])).run(), default=str))
