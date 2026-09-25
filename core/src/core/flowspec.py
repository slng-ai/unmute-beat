"""Flow files: a flow folder described by one YAML file, checked before it can run.

A flow is a folder. `flow.yaml` lists the steps in order, and each step
carries its whole definition there: its kind, its own prompt file, the Python
module holding its deterministic code, and where it can go next.

    flows/access/
      flow.yaml
      voice.md              added to the top of every prompt
      prompts/team.md       one prompt file per step that runs a model
      steps/submit.py       one module per step that runs code

Six step kinds. `collect`, `conversation` and `agent` run a model, so each
names a `prompt`. `confirm`, `action` and `end` are code only: they have no
prompt field at all, because the words the caller agrees to are written by
code, never by a model. An `agent` step is a hand-built step (the salon's):
its module's `node` function builds its agent, tools and openings.

What a file may refer to:

* a path inside the flow's own folder (`prompts/team.md`, `steps/submit.py`,
  `steps/fields.py:Team`), and nothing outside it, so a file cannot reach
  into arbitrary code;
* a name in the `Registry`, for code shared by several flows.

`FlowSpec.compile` refuses a flow with every problem it has, not just the
first: an unknown kind or field, a repeated id, a destination that does not
exist, a missing file or name, a module without what its step needs, a prompt
naming a field the call does not have, a prompt shared by two steps, a fact no
earlier step collects, and an action that needs approval but can be reached
without its confirmation.

Files are read with `yaml.safe_load`, which builds only plain data, and JSON
is valid YAML, so either format loads. Plain YAML turns an unquoted `no`,
`yes`, `on` or `off` into a boolean and an unquoted `2026-10-01` into a date;
a text field given one of those is refused with the step and field named.
"""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, create_model, field_validator
from pydantic_ai import TemplateStr
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from core.flow import Flow
from core.graph import Exit
from core.operations import Operation
from core.state import CallContext
from core.steps import (
    ActionStep,
    AgentStep,
    CollectStep,
    ConfirmStep,
    ConversationStep,
    End,
    FlowCall,
    Step,
    StepFlow,
    confirm_interpreter,
)

SPEC = ConfigDict(extra='forbid', frozen=True)
QUOTE_CALLER = (
    '\n\n## Latest caller message\n\n'
    'Use this with the conversation above, including earlier speech fragments.\n\n> {{heard}}'
)
"""Appended to every prompt when a flow sets `quote_caller`: the caller's words, where the model reads them last."""


class FlowSpecError(ValueError):
    """The flow cannot run. `problems` lists every reason, for the person editing it."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__('; '.join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Registry:
    """Code shared by several flows, named by a plain word instead of a path.

    `operations` maps a name to a factory taking the flow's confirmation
    interpreter (an `Operation` subclass, typically).
    """

    fields: Mapping[str, type[BaseModel]] = field(default_factory=dict)
    operations: Mapping[str, Callable[[Any], Operation]] = field(default_factory=dict)
    knowledge: Mapping[str, str] = field(default_factory=dict)
    checks: Mapping[str, Callable[[Mapping[str, Any]], bool]] = field(default_factory=dict)
    """Named receipt checks for an action's `accepts`: `eligible`, `matched`."""


def is_path(ref: str) -> bool:
    """A file reference, as opposed to a registry name: registry names are plain words."""
    return '/' in ref or '.' in ref


class _ProblemError(Exception):
    """One thing wrong with the flow, said for the person editing it."""


class Folder:
    """A flow's folder: where its prompts and step modules live, and the only place they may come from.

    Step modules are imported by their package name, so a flow folder must be
    a Python package (an `__init__.py`), and a module the rest of the code
    also imports is the same module, not a second copy.
    """

    def __init__(self, root: Path | None) -> None:
        self.root = root.resolve() if root is not None else None
        self.package = ''
        if self.root is not None and (self.root / '__init__.py').exists():
            parts, here = [], self.root
            while (here / '__init__.py').exists():
                parts.insert(0, here.name)
                here = here.parent
            self.package = '.'.join(parts)

    def path(self, ref: str) -> Path:
        if self.root is None:
            raise _ProblemError(f'{ref!r} is a file, and a flow read from text has no folder; use load_file')
        path = (self.root / ref).resolve()
        if not path.is_relative_to(self.root):
            raise _ProblemError(f'{ref!r} is outside the flow folder')
        if not path.is_file():
            raise _ProblemError(f'{ref!r} does not exist')
        return path

    def text(self, ref: str) -> str:
        return self.path(ref).read_text()

    def module(self, ref: str) -> ModuleType:
        if not ref.endswith('.py'):
            raise _ProblemError(f'{ref!r} is not a Python file')
        path = self.path(ref)
        if not self.package:
            raise _ProblemError(f'{ref!r} cannot be imported: the flow folder is not a Python package')
        assert self.root is not None
        dotted = '.'.join([self.package, *path.relative_to(self.root).with_suffix('').parts])
        try:
            return importlib.import_module(dotted)
        except Exception as error:
            raise _ProblemError(f'{ref!r} failed to import: {type(error).__name__}: {error}') from None

    def attr(self, ref: str) -> Any:
        file, _, name = ref.partition(':')
        if not name:
            raise _ProblemError(f'{ref!r} must name what to take from the file, like steps/fields.py:Team')
        module = self.module(file)
        if not hasattr(module, name):
            raise _ProblemError(f'{file!r} has no {name!r}')
        return getattr(module, name)


class _Resolver:
    """Everything a flow names, looked up once, with every failure kept as a problem instead of raised."""

    def __init__(self, spec: FlowSpec, registry: Registry) -> None:
        self.spec = spec
        self.registry = registry
        self.folder = Folder(spec._root)
        self.problems: list[str] = []
        self._cache: dict[tuple[str, str], Any] = {}
        self._prompts: dict[str, str] = {}
        self.interpreter: Any = None
        self.operations: dict[str, Operation] = {}

    def fail(self, where: str, problem: str) -> None:
        self.problems.append(f'{where}: {problem}' if where else problem)

    def _once(self, kind: str, key: str, where: str, find: Callable[[], Any]) -> Any:
        if (kind, key) not in self._cache:
            try:
                self._cache[kind, key] = find()
            except _ProblemError as problem:
                self.fail(where, str(problem))
                self._cache[kind, key] = None
        return self._cache[kind, key]

    def call_type(self) -> type[CallContext]:
        def find() -> type[CallContext]:
            found = self.folder.attr(self.spec.call)
            if not (isinstance(found, type) and issubclass(found, CallContext)):
                raise _ProblemError(f'{self.spec.call!r} is not a CallContext class')
            return found

        return (self._once('call', self.spec.call, 'call', find) if self.spec.call else None) or FlowCall

    def prompt(self, step: str, ref: str) -> TemplateStr | None:
        """The step's prompt: its file, the flow's includes and voice, compiled against the call now."""
        owner = self._prompts.setdefault(ref, step)
        if owner != step:
            self.fail(step, f'prompt {ref!r} is already the prompt of {owner}; each step has its own')
            return None

        def find() -> TemplateStr:
            text = self.folder.text(ref)
            for name, include in self.spec.includes.items():
                text = text.replace(f'<!--{name}-->', self.folder.text(include))
            if self.spec.voice:
                text = f'{self.folder.text(self.spec.voice)}\n\n{text}'
            if self.spec.quote_caller:
                text += QUOTE_CALLER
            try:
                return TemplateStr(text, deps_type=self.call_type())
            except Exception as error:
                raise _ProblemError(f'prompt {ref!r} does not fit the call: {error}') from None

        return self._once('prompt', ref, step, find)

    def fields(self, step: str, ref: str) -> type[BaseModel] | None:
        def find() -> type[BaseModel]:
            found = self.folder.attr(ref) if is_path(ref) else self.registry.fields.get(ref)
            if found is None:
                raise _ProblemError(f'field model {ref!r} is not registered')
            if not (isinstance(found, type) and issubclass(found, BaseModel)):
                raise _ProblemError(f'{ref!r} is not a Pydantic model')
            return found

        return self._once('fields', ref, step, find)

    def operation_type(self, step: str, ref: str) -> tuple[Callable[[Any], Operation], Any] | None:
        """An operation's factory and its module's `accepts`, if it has one."""

        def find() -> tuple[Callable[[Any], Operation], Any]:
            if is_path(ref):
                module = self.folder.module(ref)
                if not callable(getattr(module, 'OPERATION', None)):
                    raise _ProblemError(f'{ref!r} has no OPERATION')
                return module.OPERATION, getattr(module, 'accepts', None)
            if ref not in self.registry.operations:
                raise _ProblemError(f'operation {ref!r} is not registered')
            return self.registry.operations[ref], None

        return self._once('operation', ref, step, find)

    def operation(self, ref: str) -> Operation:
        """The one instance of an operation for this flow, shared by its confirm step and its action step."""
        if ref not in self.operations:
            found = self.operation_type('', ref)
            assert found is not None
            self.operations[ref] = found[0](self.interpreter)
        return self.operations[ref]

    def check(self, step: str, name: str) -> Callable[[Mapping[str, Any]], bool] | None:
        if name not in self.registry.checks:
            self.fail(step, f'check {name!r} is not registered')
            return None
        return self.registry.checks[name]

    def knowledge(self, step: str, ref: str) -> str | None:
        def find() -> str:
            if is_path(ref):
                return self.folder.text(ref)
            if ref not in self.registry.knowledge:
                raise _ProblemError(f'knowledge {ref!r} is not registered')
            return self.registry.knowledge[ref]

        return self._once('knowledge', ref, step, find)

    def module(self, step: str, ref: str) -> ModuleType | None:
        return self._once('module', ref, step, lambda: self.folder.module(ref))


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


class StepSpec(BaseModel, ABC):
    """One step in a flow file."""

    model_config = SPEC
    id: str
    then: str = ''

    def destinations(self) -> list[str]:
        return [self.then] if self.then else []

    def check(self, resolve: _Resolver) -> None:
        """Look up everything this step names, so every problem is found before anything is built."""

    @abstractmethod
    def to_step(self, resolve: _Resolver) -> Step: ...


class CollectSpec(StepSpec):
    kind: Literal['collect']
    prompt: str
    question: str
    fields: dict[str, FieldSpec] = {}
    model: str = ''
    """A field model with rules an inline field cannot state: `steps/fields.py:Team`, or a registry name."""
    after: list[str] = []
    lookahead: bool = True

    def check(self, resolve: _Resolver) -> None:
        resolve.prompt(self.id, self.prompt)
        if self.model:
            resolve.fields(self.id, self.model)
        elif not self.fields:
            resolve.fail(self.id, 'a collect step needs fields or a model')

    def produces(self, resolve: _Resolver) -> set[str]:
        model = resolve.fields(self.id, self.model) if self.model else None
        return set(model.model_fields) if model is not None else set(self.fields)

    def to_step(self, resolve: _Resolver) -> Step:
        model = resolve.fields(self.id, self.model) if self.model else None
        if model is None:
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
            prompt=resolve.prompt(self.id, self.prompt),
        )


class _Operated(StepSpec):
    """A step that runs an operation: named by its module (`code`) or by a registry name (`operation`)."""

    code: str = ''
    """The step's module: `steps/submit.py`, which must provide `OPERATION`."""
    operation: str = ''

    @property
    def uses(self) -> str:
        return self.code or self.operation

    def check(self, resolve: _Resolver) -> None:
        if bool(self.code) == bool(self.operation):
            resolve.fail(self.id, 'name exactly one of code or operation')
            return
        if self.code and not self.code.endswith('.py'):
            resolve.fail(self.id, f'code {self.code!r} must be a Python file in the flow folder')
            return
        resolve.operation_type(self.id, self.uses)


class ConfirmSpec(_Operated):
    kind: Literal['confirm']
    then: str
    on_reject: str
    on_revise: str

    def destinations(self) -> list[str]:
        return [self.then, self.on_reject, self.on_revise]

    def to_step(self, resolve: _Resolver) -> Step:
        return ConfirmStep(
            id=self.id,
            then=self.then,
            operation=resolve.operation(self.uses),
            on_reject=self.on_reject,
            on_revise=self.on_revise,
        )


class ActionSpec(_Operated):
    kind: Literal['action']
    accepts: str = ''
    """A registered receipt check. Without one, the module's own `accepts`, if it has one, decides."""
    after: list[str] = []
    on_error: str = ''
    on_unknown: str = ''

    def destinations(self) -> list[str]:
        return [*super().destinations(), *(to for to in (self.on_error, self.on_unknown) if to)]

    def check(self, resolve: _Resolver) -> None:
        super().check(resolve)
        if self.accepts:
            resolve.check(self.id, self.accepts)

    def to_step(self, resolve: _Resolver) -> Step:
        found = resolve.operation_type(self.id, self.uses)
        accepts = resolve.check(self.id, self.accepts) if self.accepts else (found[1] if found else None)
        return ActionStep(
            id=self.id,
            then=self.then,
            operation=resolve.operation(self.uses),
            accepts=accepts or (lambda _: True),
            after=frozenset(self.after),
            on_error=self.on_error,
            on_unknown=self.on_unknown,
        )


class ConversationSpec(StepSpec):
    kind: Literal['conversation']
    prompt: str
    knowledge: str = ''
    """Added to the end of the prompt: a file in the flow folder, or a registry name."""

    def check(self, resolve: _Resolver) -> None:
        resolve.prompt(self.id, self.prompt)
        if self.knowledge:
            resolve.knowledge(self.id, self.knowledge)

    def to_step(self, resolve: _Resolver) -> Step:
        prompt = resolve.prompt(self.id, self.prompt)
        if self.knowledge and prompt is not None:
            knowledge = resolve.knowledge(self.id, self.knowledge) or ''
            prompt = TemplateStr(f'{prompt}\n\nKnowledge:\n{knowledge}', deps_type=resolve.call_type())
        return ConversationStep(id=self.id, instructions=prompt)


class ExitSpec(BaseModel):
    """One legal move out of a hand-built step: where, when (a guard in the step's module), and which borrowed tools make it."""

    model_config = SPEC
    to: str
    when: str = ''
    via: list[str] = []


class AgentSpec(StepSpec):
    kind: Literal['agent']
    prompt: str
    code: str
    """The step's module: `steps/verify.py`, which must provide `node(step) -> Node`."""
    exits: list[ExitSpec] = []
    requires: list[str] = []
    satisfied_by: str = ''
    guarded: bool = False

    @field_validator('exits', mode='before')
    @classmethod
    def _plain_names(cls, exits: Any) -> Any:
        """`exits: [concierge]` is short for `exits: [{to: concierge}]`."""
        return [{'to': exit} if isinstance(exit, str) else exit for exit in exits or []]

    def destinations(self) -> list[str]:
        return [*(exit.to for exit in self.exits), *([self.satisfied_by] if self.satisfied_by else [])]

    def check(self, resolve: _Resolver) -> None:
        resolve.prompt(self.id, self.prompt)
        module = resolve.module(self.id, self.code)
        if module is None:
            return
        if not callable(getattr(module, 'node', None)):
            resolve.fail(self.id, f'{self.code!r} has no node(step) function')
        for exit in self.exits:
            for name in [*([exit.when] if exit.when else []), *exit.via]:
                if not hasattr(module, name):
                    resolve.fail(
                        self.id, f'exit to {exit.to} names {name!r}, which {self.code!r} does not have'
                    )

    def to_step(self, resolve: _Resolver) -> Step:
        module = resolve.module(self.id, self.code)
        assert module is not None
        exits = tuple(
            Exit(exit.to, getattr(module, exit.when), tuple(getattr(module, name) for name in exit.via))
            if exit.when
            else Exit(exit.to, via=tuple(getattr(module, name) for name in exit.via))
            for exit in self.exits
        )
        return AgentStep(
            id=self.id,
            then=self.then,
            instructions=resolve.prompt(self.id, self.prompt),
            build=module.node,
            exits=exits,
            requires=tuple(self.requires),
            satisfied_by=self.satisfied_by,
            guarded=self.guarded,
        )


class EndSpec(StepSpec):
    kind: Literal['end']
    outcome: Literal['completed', 'cancelled', 'failed', 'pending']
    say: str

    def to_step(self, resolve: _Resolver) -> Step:
        return End(id=self.id, outcome=self.outcome, say=self.say)


type AnyStep = Annotated[
    CollectSpec | ConfirmSpec | ActionSpec | ConversationSpec | AgentSpec | EndSpec,
    Field(discriminator='kind'),
]


class FlowSpec(BaseModel):
    """A whole flow file."""

    model_config = SPEC
    version: Literal[1]
    id: str
    greeting: str = ''
    """The first thing a caller hears."""
    call: str = ''
    """The call class the prompts are checked against, like `models/state.py:SalonCall`. Empty means `FlowCall`."""
    voice: str = ''
    """A file added to the top of every step's prompt: the shared tone and persona."""
    includes: dict[str, str] = {}
    """Files substituted for `<!--name-->` markers in any prompt: shared rules, documents."""
    quote_caller: bool = False
    """End every prompt with the caller's latest words."""
    steps: list[AnyStep]
    _root: Path | None = PrivateAttr(default=None)

    def _resolver(self, registry: Registry) -> _Resolver:
        resolve = _Resolver(self, registry)
        resolve.call_type()
        ids = [step.id for step in self.steps]
        for repeated in sorted({i for i in ids if ids.count(i) > 1}):
            resolve.fail('', f'step id {repeated!r} is used more than once')
        known = set(ids)
        for step in self.steps:
            for to in step.destinations():
                if to not in known:
                    resolve.fail(step.id, f'sends the caller to {to!r}, which is not a step')
            step.check(resolve)
        collected: set[str] = set()
        for step in self.steps:
            if isinstance(step, CollectSpec | ActionSpec):
                for name in step.after:
                    if name not in collected:
                        resolve.fail(step.id, f'depends on {name!r}, which no earlier step collects')
            if isinstance(step, CollectSpec):
                collected |= step.produces(resolve)
            if isinstance(step, ActionSpec):
                collected.add(step.id)
        self._unguarded_actions(resolve)
        return resolve

    def problems(self, registry: Registry | None = None) -> list[str]:
        """Every reason this flow cannot run. Empty means it can."""
        return self._resolver(registry or Registry()).problems

    def _unguarded_actions(self, resolve: _Resolver) -> None:
        """An action that needs approval must be unreachable except through a confirm step for that operation."""
        business = [s for s in self.steps if not isinstance(s, EndSpec | AgentSpec)]
        edges: dict[str, set[str]] = {s.id: set(s.destinations()) for s in self.steps}
        for index, step in enumerate(business):
            if not step.then and index + 1 < len(business):
                edges[step.id].add(business[index + 1].id)
        for action in [s for s in self.steps if isinstance(s, ActionSpec)]:
            found = resolve._cache.get(('operation', action.uses))
            operation_type = found[0] if found else None
            if not isinstance(operation_type, type) or not getattr(operation_type, 'needs_approval', True):
                continue
            gates = {
                s.id
                for s in self.steps
                if isinstance(s, ConfirmSpec) and s.uses == action.uses and s.then == action.id
            }
            if not gates:
                resolve.fail(
                    action.id, f'needs approval, and no confirm step for {action.uses!r} leads to it'
                )
                continue
            start = business[0].id if business else ''
            seen, queue = {start}, deque([start])
            while queue:
                for there in edges.get(queue.popleft(), ()):
                    if there not in gates and there not in seen:
                        seen.add(there)
                        queue.append(there)
            if action.id in seen:
                resolve.fail(action.id, 'can be reached without its confirmation')

    def prompt(self, step_id: str, registry: Registry | None = None) -> TemplateStr:
        """One step's compiled prompt, exactly as its agent receives it."""
        step = next(
            (
                s
                for s in self.steps
                if s.id == step_id and isinstance(s, CollectSpec | ConversationSpec | AgentSpec)
            ),
            None,
        )
        if step is None:
            raise FlowSpecError([f'{step_id!r} is not a step with a prompt'])
        resolve = _Resolver(self, registry or Registry())
        compiled = resolve.prompt(step_id, step.prompt)
        if compiled is None:
            raise FlowSpecError(resolve.problems)
        return compiled

    def compile(
        self, registry: Registry | None = None, *, model: Model | str, settings: ModelSettings | None = None
    ) -> StepFlow:
        """The runnable flow, or `FlowSpecError` with every problem. Compile once per published version."""
        resolve = self._resolver(registry or Registry())
        if resolve.problems:
            raise FlowSpecError(resolve.problems)
        resolve.interpreter = confirm_interpreter(model, settings)
        flow = StepFlow(
            [step.to_step(resolve) for step in self.steps],
            model=model,
            interpreter=resolve.interpreter,
            settings=settings,
            call_type=resolve.call_type(),
        )
        Flow.of(flow.nodes)
        return flow


def load(text: str, root: str | Path | None = None) -> FlowSpec:
    """Read a flow file's text, YAML or JSON; `root` is its folder. Unknown kinds and fields fail here."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise FlowSpecError(['a flow file must be a mapping with version, id and steps'])
    spec = FlowSpec.model_validate(data)
    spec._root = Path(root) if root is not None else None
    return spec


def load_file(path: str | Path) -> FlowSpec:
    """Read a flow file from disk; the files it names are looked up in the same folder."""
    path = Path(path)
    return load(path.read_text(), root=path.parent)
