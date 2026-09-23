"""Flow files: a versioned YAML (or JSON) description of a flow, checked before it can run.

A person edits the file; a developer registers what it may name. The file
lists steps by kind (`collect`, `confirm`, `action`, `conversation`, `end`)
and can only refer to registered field models, operations, knowledge and
receipt checks by name, so configuration never runs code. Simple fields can
be written inline (`string`, `integer`, `date`, `choice`) with plain limits.

`FlowSpec.compile` refuses a file with every problem it has, not just the
first: an unknown step kind or field, a repeated id, a destination that does
not exist, a name nobody registered, a field no earlier step collects, and
an action that needs approval but can be reached without its confirmation.
Each step kind is one spec class with one `to_step`, so a new kind is one new
pair of classes, not a new branch in a compiler.

Files are read with `yaml.safe_load`, which builds only plain data (never
Python objects), and JSON is valid YAML, so either format loads. Plain YAML
turns an unquoted `no`, `yes`, `on` or `off` into a boolean and an unquoted
`2026-10-01` into a date; a text field given one of those fails validation
with the step and field named, so quote such values.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, create_model
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from core.flow import Flow
from core.operations import Operation
from core.steps import (
    ActionStep,
    CollectStep,
    ConfirmStep,
    ConversationStep,
    End,
    Step,
    StepFlow,
    confirm_interpreter,
)

SPEC = ConfigDict(extra='forbid', frozen=True)


class FlowSpecError(ValueError):
    """The file cannot become a flow. `problems` lists every reason, for the person editing it."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__('; '.join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Registry:
    """Everything a flow file may name, registered in code by a developer.

    `operations` maps a name to a factory taking the flow's confirmation
    interpreter (an `Operation` subclass's constructor, typically), so the
    same operation instance serves the confirm step and the action step.
    """

    fields: Mapping[str, type[BaseModel]] = field(default_factory=dict)
    operations: Mapping[str, Callable[[Any], Operation]] = field(default_factory=dict)
    knowledge: Mapping[str, str] = field(default_factory=dict)
    checks: Mapping[str, Callable[[Mapping[str, Any]], bool]] = field(default_factory=dict)
    """Named receipt checks for an action's `accepts`: `eligible`, `matched`."""


class FieldSpec(BaseModel):
    """One inline field: a type and plain limits, nothing executable."""

    model_config = SPEC
    type: Literal['string', 'integer', 'date', 'choice'] = 'string'
    description: str = ''
    required: bool = True
    min_length: int | None = None
    max_length: int | None = None
    choices: list[str] = []

    def annotation(self) -> Any:
        if self.type == 'choice':
            return Literal[tuple(self.choices)]  # ty: ignore[invalid-type-form]
        return {'string': str, 'integer': int, 'date': date}[self.type]

    def field(self) -> Any:
        default = ... if self.required else None
        limits = Field(
            default, description=self.description, min_length=self.min_length, max_length=self.max_length
        )
        return (self.annotation(), limits)


@dataclass
class _Compiling:
    """What a step spec needs while it becomes a step."""

    registry: Registry
    operations: dict[str, Operation]


class StepSpec(BaseModel, ABC):
    """One step in a flow file."""

    model_config = SPEC
    id: str
    then: str = ''

    def destinations(self) -> list[str]:
        return [self.then] if self.then else []

    def names(self) -> list[tuple[str, str]]:
        """(what, name) for every registered name this step uses."""
        return []

    @abstractmethod
    def to_step(self, compiling: _Compiling) -> Step: ...


class CollectSpec(StepSpec):
    kind: Literal['collect']
    question: str
    fields: dict[str, FieldSpec] = {}
    model: str = ''
    """A registered field model, for fields with rules an inline field cannot state."""
    after: list[str] = []
    lookahead: bool = True

    def names(self) -> list[tuple[str, str]]:
        return [('field model', self.model)] if self.model else []

    def produces(self, registry: Registry) -> set[str]:
        model = registry.fields.get(self.model)
        return set(model.model_fields) if model is not None else set(self.fields)

    def to_step(self, compiling: _Compiling) -> Step:
        if self.model:
            model = compiling.registry.fields[self.model]
        else:
            model = create_model(
                f'{self.id.title()}Fields',
                __config__=ConfigDict(extra='forbid'),
                **{name: spec.field() for name, spec in self.fields.items()},
            )
        return CollectStep(
            id=self.id,
            then=self.then,
            fields=model,
            question=self.question,
            after=frozenset(self.after),
            lookahead=self.lookahead,
        )


class ConfirmSpec(StepSpec):
    kind: Literal['confirm']
    operation: str
    then: str
    on_reject: str
    on_revise: str

    def destinations(self) -> list[str]:
        return [self.then, self.on_reject, self.on_revise]

    def names(self) -> list[tuple[str, str]]:
        return [('operation', self.operation)]

    def to_step(self, compiling: _Compiling) -> Step:
        return ConfirmStep(
            id=self.id,
            then=self.then,
            operation=compiling.operations[self.operation],
            on_reject=self.on_reject,
            on_revise=self.on_revise,
        )


class ActionSpec(StepSpec):
    kind: Literal['action']
    operation: str
    accepts: str = ''
    after: list[str] = []
    on_error: str = ''
    on_unknown: str = ''

    def destinations(self) -> list[str]:
        return [*super().destinations(), *(to for to in (self.on_error, self.on_unknown) if to)]

    def names(self) -> list[tuple[str, str]]:
        return [('operation', self.operation), *([('check', self.accepts)] if self.accepts else [])]

    def to_step(self, compiling: _Compiling) -> Step:
        accepts = compiling.registry.checks[self.accepts] if self.accepts else (lambda _: True)
        return ActionStep(
            id=self.id,
            then=self.then,
            operation=compiling.operations[self.operation],
            accepts=accepts,
            after=frozenset(self.after),
            on_error=self.on_error,
            on_unknown=self.on_unknown,
        )


class ConversationSpec(StepSpec):
    kind: Literal['conversation']
    instructions: str
    knowledge: str = ''

    def names(self) -> list[tuple[str, str]]:
        return [('knowledge', self.knowledge)] if self.knowledge else []

    def to_step(self, compiling: _Compiling) -> Step:
        knowledge = compiling.registry.knowledge[self.knowledge] if self.knowledge else ''
        return ConversationStep(id=self.id, instructions=self.instructions, knowledge=knowledge)


class EndSpec(StepSpec):
    kind: Literal['end']
    outcome: Literal['completed', 'cancelled', 'failed', 'pending']
    say: str

    def to_step(self, compiling: _Compiling) -> Step:
        return End(id=self.id, outcome=self.outcome, say=self.say)


type AnyStep = Annotated[
    CollectSpec | ConfirmSpec | ActionSpec | ConversationSpec | EndSpec, Field(discriminator='kind')
]


class FlowSpec(BaseModel):
    """A whole flow file."""

    model_config = SPEC
    version: Literal[1]
    id: str
    steps: list[AnyStep]

    def problems(self, registry: Registry) -> list[str]:
        """Every reason this file cannot run with this registry. Empty means it can."""
        found: list[str] = []
        ids = [step.id for step in self.steps]
        found += [
            f'step id {i!r} is used more than once' for i in sorted({i for i in ids if ids.count(i) > 1})
        ]
        known = set(ids)
        sections = {
            'field model': registry.fields,
            'operation': registry.operations,
            'knowledge': registry.knowledge,
            'check': registry.checks,
        }
        for step in self.steps:
            found += [
                f'{step.id} sends the caller to {to!r}, which is not a step'
                for to in step.destinations()
                if to not in known
            ]
            found += [
                f'{step.id} names {what} {name!r}, which is not registered'
                for what, name in step.names()
                if name not in sections[what]
            ]
        collected: set[str] = set()
        for step in self.steps:
            if isinstance(step, CollectSpec | ActionSpec):
                found += [
                    f'{step.id} depends on {n!r}, which no earlier step collects'
                    for n in step.after
                    if n not in collected
                ]
            if isinstance(step, CollectSpec):
                collected |= step.produces(registry)
            if isinstance(step, ActionSpec):
                collected.add(step.id)
        found += self._unguarded_actions(registry)
        return found

    def _unguarded_actions(self, registry: Registry) -> list[str]:
        """An action that needs approval must be unreachable except through a confirm step for that operation."""
        found = []
        business = [s for s in self.steps if not isinstance(s, EndSpec)]
        edges: dict[str, set[str]] = {s.id: set(s.destinations()) for s in self.steps}
        for index, step in enumerate(business):
            if not step.then and not isinstance(step, EndSpec) and index + 1 < len(business):
                edges[step.id].add(business[index + 1].id)
        for action in [s for s in self.steps if isinstance(s, ActionSpec)]:
            factory = registry.operations.get(action.operation)
            operation_type = factory if isinstance(factory, type) else None
            if operation_type is None or not getattr(operation_type, 'needs_approval', True):
                continue
            gates = {
                s.id
                for s in self.steps
                if isinstance(s, ConfirmSpec) and s.operation == action.operation and s.then == action.id
            }
            if not gates:
                found.append(
                    f'{action.id} needs approval, and no confirm step for {action.operation!r} leads to it'
                )
                continue
            seen, queue = {business[0].id} if business else set(), deque([business[0].id] if business else [])
            while queue:
                here = queue.popleft()
                for there in edges.get(here, ()):
                    if there in gates or there in seen:
                        continue
                    seen.add(there)
                    queue.append(there)
            if action.id in seen:
                found.append(f'{action.id} can be reached without its confirmation')
        return found

    def compile(
        self, registry: Registry, *, model: Model | str, settings: ModelSettings | None = None
    ) -> StepFlow:
        """The runnable flow, or `FlowSpecError` with every problem. Compile once per published version."""
        if found := self.problems(registry):
            raise FlowSpecError(found)
        interpreter = confirm_interpreter(model, settings)
        operations = {name: make(interpreter) for name, make in registry.operations.items()}
        compiling = _Compiling(registry, operations)
        flow = StepFlow(
            [step.to_step(compiling) for step in self.steps],
            model=model,
            interpreter=interpreter,
            settings=settings,
        )
        Flow.of(flow.nodes)
        return flow


def load(text: str) -> FlowSpec:
    """Read a flow file's text, YAML or JSON. Unknown kinds and fields fail here, with Pydantic's account of where."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise FlowSpecError(['a flow file must be a mapping with version, id and steps'])
    return FlowSpec.model_validate(data)


def load_file(path: str | Path) -> FlowSpec:
    """Read a flow file from disk."""
    return load(Path(path).read_text())
