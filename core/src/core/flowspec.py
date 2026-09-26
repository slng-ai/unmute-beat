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

The application's state is one Pydantic model, named by `state:`
(`models.py:ClinicState`). Collect steps name its fields; operations take
their arguments from it by name; `identity:` names the field that says who
the caller is. The engine keeps it as `call.state.facts`, of exactly that
type, and no application subclasses the call.

`FlowSpec.compile` refuses a flow with every problem it has, not just the
first: an unknown kind or field, a repeated id, a destination that does not
exist, a missing file or name, a module without what its step needs, a prompt
naming a field the call does not have, a prompt shared by two steps, a field
the state does not have, a fact no earlier step collects, an operation
argument the state cannot supply, and an action that needs approval but can
be reached without its confirmation. Checking needs no model and no key:
`problems()` connects to nothing, and only `compile` builds agents.

Files are read with `yaml.safe_load`, which builds only plain data, and JSON
is valid YAML, so either format loads. Plain YAML turns an unquoted `no`,
`yes`, `on` or `off` into a boolean and an unquoted `2026-10-01` into a date;
a text field given one of those is refused with the step and field named.
"""

from __future__ import annotations

import importlib
import inspect
import re
import sys
import typing
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any, Literal, TypeAliasType, get_args, get_origin, get_type_hints

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator
from pydantic_ai import TemplateStr
from pydantic_ai.agent.spec import AgentSpec as NativeSpec
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from core.flow import Flow
from core.graph import Exit
from core.locale import ENGLISH, SPANISH, Locale
from core.operations import Operation
from core.state import CallContext, Intent
from core.steps import (
    ActionStep,
    AgentStep,
    CollectStep,
    ConfirmStep,
    ConversationStep,
    End,
    FlowCall,
    FunctionOperation,
    NoFacts,
    Step,
    StepFlow,
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

    `operations` maps a name to a factory taking no arguments (an `Operation`
    subclass, typically). An operation is authorised by the active step's
    agent's yes to its read-back, never by a reading of its own.
    """

    operations: Mapping[str, Callable[[], Operation]] = field(default_factory=dict)
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
            self.package = '.'.join(_on_path(parts, here))

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


def _on_path(parts: list[str], here: Path) -> list[str]:
    """The package's full dotted name, through any namespace packages (folders with no `__init__.py`) above it.

    salon-graph's `tests` is one: `tests/slice` is imported as `tests.slice`,
    not `slice`. Folders are added until the one above is on `sys.path`; with
    none found, the `__init__.py` chain alone is the name, as before.
    """
    entries = {Path(entry or '.').resolve() for entry in sys.path}
    named, above = list(parts), here
    while above not in entries and above.parent != above:
        named.insert(0, above.name)
        above = above.parent
    return named if above in entries else parts


class _Resolver:
    """Everything a flow names, looked up once, with every failure kept as a problem instead of raised."""

    def __init__(self, spec: FlowSpec, registry: Registry) -> None:
        self.spec = spec
        self.registry = registry
        self.folder = Folder(spec._root)
        self.problems: list[str] = []
        self._cache: dict[tuple[str, str], Any] = {}
        self._prompts: dict[str, str] = {}
        self.operations: dict[str, Operation] = {}
        self.explicit_model = False
        """`compile(model=...)` was given: every persona's own model is set aside for it."""
        self.assigned: dict[str, set[str]] = {}
        """Declared operation to the state fields its receipt sets, checked against what the model collects."""

    def fail(self, where: str, problem: str) -> None:
        """Keep a problem once: one missing file named by three references is one thing to fix."""
        said = f'{where}: {problem}' if where else problem
        if said not in self.problems:
            self.problems.append(said)

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

        if self.spec.call:
            return self._once('call', self.spec.call, 'call', find) or FlowCall[NoFacts]
        return FlowCall[self.state_type()]  # ty: ignore[invalid-type-form]

    def state_type(self) -> type[BaseModel]:
        """The application's state model, or `NoFacts` for a flow that keeps none."""

        def find() -> type[BaseModel]:
            found = self.folder.attr(self.spec.state)
            if not (isinstance(found, type) and issubclass(found, BaseModel)) or issubclass(
                found, CallContext
            ):
                raise _ProblemError(f'{self.spec.state!r} is not a Pydantic model of plain data')
            try:
                found()
            except Exception:
                raise _ProblemError(
                    f'{self.spec.state!r} needs a default for every field: a call starts empty'
                ) from None
            return found

        return (self._once('state', self.spec.state, 'state', find) if self.spec.state else None) or NoFacts

    def state_field(self, step: str, name: str) -> None:
        if name not in self.state_type().model_fields:
            self.fail(step, f'names {name!r}, which the state {self.state_type().__name__} does not have')

    def prompt(self, step: str, ref: str, agent: str = '') -> TemplateStr | None:
        """The step's prompt: its agent's shared instructions, its own file, the flow's includes and voice, compiled now.

        A persona's prompt may serve many steps; a step's own prompt belongs to that step alone.
        """
        if agent and agent not in self.spec.agents:
            return None
        if ref:
            owner = self._prompts.setdefault(ref, step)
            if owner != step:
                self.fail(step, f'prompt {ref!r} is already the prompt of {owner}; each step has its own')
                return None
        if not ref and not agent:
            self.fail(step, 'needs a prompt or an agent')
            return None

        def find() -> TemplateStr:
            parts = [self.folder.text(self.spec.agents[agent].prompt)] if agent else []
            text = '\n\n'.join([*parts, *([self.folder.text(ref)] if ref else [])])
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

        return self._once('prompt', f'{agent}|{ref}', step, find)

    def operation_type(self, step: str, ref: str) -> tuple[Callable[[], Operation], Any] | None:
        """An operation's factory and its module's `accepts`, if it has one. A declared name comes first."""

        def find() -> tuple[Callable[[], Operation], Any]:
            if ref in self.spec.operations:
                return self.declared(ref), None
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
            self.operations[ref] = found[0]()
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

    def function(self, where: str, ref: str, arity: int) -> Callable[..., Any] | None:
        """A plain function (or class) from the flow folder, callable with exactly `arity` positional arguments."""

        def find() -> Callable[..., Any]:
            found = self.folder.attr(ref)
            if not callable(found):
                raise _ProblemError(f'{ref!r} is not a function')
            try:
                inspect.signature(found).bind(*range(arity))
            except TypeError:
                raise _ProblemError(
                    f'{ref!r} must be callable with {arity} argument{"" if arity == 1 else "s"}'
                ) from None
            return found

        return self._once('function', ref, where, find)

    def domain_function(self, where: str, ref: str) -> Callable[..., Any] | None:
        """An operation's function or reconciliation: `(services, args)`, or `(services, args, intent: Intent)`.

        The third parameter, when there is one, must be annotated `Intent`:
        it is the only way core hands application code the identity of the
        action, and a name alone would let any third argument pass for it.
        """

        def find() -> Callable[..., Any]:
            found = self.folder.attr(ref)
            if not callable(found):
                raise _ProblemError(f'{ref!r} is not a function')
            takes = _positional(found)
            if takes not in (2, 3):
                raise _ProblemError(f'{ref!r} must take (services, args) or (services, args, intent)')
            if takes == 3 and not _takes_intent(found):
                raise _ProblemError(f'{ref!r} must annotate its third parameter with core.Intent')
            return found

        return self._once('function', ref, where, find)

    def folder_callable(self, where: str, ref: str) -> Callable[..., Any] | None:
        """A function from the flow folder whose number of arguments the caller checks itself."""

        def find() -> Callable[..., Any]:
            found = self.folder.attr(ref)
            if not callable(found):
                raise _ProblemError(f'{ref!r} is not a function')
            return found

        return self._once('function', ref, where, find)

    def declared(self, name: str) -> type[FunctionOperation]:
        """A declared operation, as one `FunctionOperation` subclass: every problem with it is kept, none raised.

        The function's signature is the schema: `(services, args: ArgsModel) -> ReceiptModel`.
        Every argument is bound to a state field, every assignment names a
        receipt field and a state field, and each pair must hold the same kind
        of value. Nothing here is typed again in the YAML.
        """
        if (cached := self._cache.get(('declared', name))) is not None:
            return cached
        decl, where = self.spec.operations[name], f'operation {name}'
        attrs: dict[str, Any] = {'name': name, 'binds': dict(decl.args), 'assign': dict(decl.assign)}
        attrs |= {
            'succeeds': dict(decl.succeeds),
            'say': decl.say,
            'needs_approval': decl.approval == 'readback',
            'matches': dict(decl.matches),
            'changes': decl.changes,
            'recap': decl.recap,
            'subject': decl.subject,
            'idempotent': decl.idempotent,
        }
        function = self.domain_function(where, decl.function)
        hints = _hints(function) if function is not None else {}
        params = list(inspect.signature(function).parameters) if function is not None else []
        args_type, receipt_type = (hints.get(params[1]) if params else None), hints.get('return')
        if function is not None and not _model(args_type):
            self.fail(where, f'{decl.function} must annotate its second parameter with a Pydantic model')
        if function is not None and not _model(receipt_type):
            self.fail(where, f'{decl.function} must annotate its return with the receipt, a Pydantic model')
        state = self.state_type().model_fields
        if _model(args_type):
            for arg in set(args_type.model_fields) - set(decl.args):
                self.fail(where, f'argument {arg!r} is not bound to a state field (args: {{{arg}: <field>}})')
            for arg, field_name in decl.args.items():
                if arg not in args_type.model_fields:
                    self.fail(where, f'{args_type.__name__} has no argument {arg!r}')
                elif field_name not in state:
                    self.fail(where, f'binds {arg!r} to {field_name!r}, which the state does not have')
                elif not _fits(state[field_name].annotation, args_type.model_fields[arg].annotation):
                    self.fail(
                        where, f'{field_name!r} cannot supply {arg!r}: they hold different kinds of value'
                    )
        if _model(receipt_type):
            for field_name, source in decl.assign.items():
                if source not in receipt_type.model_fields:
                    self.fail(where, f'assigns from {source!r}, which {receipt_type.__name__} does not have')
                elif field_name not in state:
                    self.fail(where, f'assigns {field_name!r}, which the state does not have')
                elif not _fits(receipt_type.model_fields[source].annotation, state[field_name].annotation):
                    self.fail(
                        where, f'{source!r} cannot set {field_name!r}: they hold different kinds of value'
                    )
            for field_name, arg in decl.matches.items():
                if field_name not in receipt_type.model_fields:
                    self.fail(where, f'matches {field_name!r}, which {receipt_type.__name__} does not have')
                elif _model(args_type) and arg not in args_type.model_fields:
                    self.fail(where, f'matches {field_name!r} to {arg!r}, which is not an argument')
            for field_name in decl.succeeds:
                if field_name not in receipt_type.model_fields:
                    self.fail(
                        where, f'succeeds on {field_name!r}, which {receipt_type.__name__} does not have'
                    )
        self.assigned[name] = set(decl.assign)
        if decl.approval == 'readback' and not decl.readback:
            self.fail(where, 'needs approval, so it needs a readback function (readback: file.py:words)')
        if decl.readback:
            found = self.folder_callable(where, decl.readback)
            takes = _positional(found) if found is not None else 1
            if found is not None and takes not in (1, 2):
                self.fail(where, f'{decl.readback!r} must take (args) or (services, args)')
            attrs['readback'] = staticmethod(found or (lambda args: ''))
            attrs['readback_uses_services'] = takes == 2
        if decl.reconcile:
            reconcile = self.domain_function(where, decl.reconcile)
            attrs['reconcile'] = staticmethod(reconcile or (lambda s, a: None))
            attrs['reconcile_takes_intent'] = reconcile is not None and _takes_intent(reconcile)
        if decl.recap and not decl.changes:
            self.fail(where, 'changes nothing, so a goodbye has nothing of it to recap (recap:)')
        if decl.subject and _model(receipt_type) and decl.subject not in receipt_type.model_fields:
            self.fail(where, f'names subject {decl.subject!r}, which {receipt_type.__name__} does not have')
        if decl.idempotent and function is not None and not _takes_intent(function):
            self.fail(
                where,
                f'is idempotent, so {decl.function} must take the Intent it dedups by as its third parameter',
            )
        attrs['function'] = staticmethod(function or (lambda s, a: None))
        attrs['function_takes_intent'] = function is not None and _takes_intent(function)
        attrs['args_type'] = args_type if _model(args_type) else NoFacts
        attrs['receipt_type'] = receipt_type if _model(receipt_type) else NoFacts
        found = type(f'{name.title().replace("_", "")}Operation', (FunctionOperation,), attrs)
        self._cache['declared', name] = found
        return found

    def choices(self, step: str, fields: list[str], refs: Mapping[str, str]) -> dict[str, Callable[..., Any]]:
        found = {}
        for name, ref in refs.items():
            if name not in fields:
                self.fail(step, f'offers choices for {name!r}, which the step does not ask for')
            if (choose := self.function(step, ref, 2)) is not None:
                found[name] = choose
        return found

    def persona(self, step: str, name: str) -> Any:
        """A persona's native spec, its model set aside when `compile` was given one explicitly."""
        if not name:
            return None
        if name not in self.spec.agents:
            self.fail(step, f'names the agent {name!r}, which `agents:` does not declare')
            return None
        native = self.spec.agents[name].spec
        return native.model_copy(update={'model': None}) if self.explicit_model else native


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
    prompt: str = ''
    agent: str = ''
    """A persona from `agents:`: its shared instructions come first, and its native spec builds this step's agent."""
    question: str
    fields: list[str]
    """Fields of the flow's state model this step asks the caller for. Their types and descriptions are the state's."""
    choices: dict[str, str] = {}
    """A field picked from options, and the function offering them: `{slot: clinic.py:free_times}`."""
    after: list[str] = []
    lookahead: bool = True

    def check(self, resolve: _Resolver) -> None:
        resolve.prompt(self.id, self.prompt, self.agent)
        resolve.persona(self.id, self.agent)
        if not self.fields:
            resolve.fail(self.id, 'a collect step needs fields')
        for name in [*self.fields, *self.after]:
            resolve.state_field(self.id, name)
        resolve.choices(self.id, self.fields, self.choices)

    def produces(self, resolve: _Resolver) -> set[str]:
        return set(self.fields)

    def to_step(self, resolve: _Resolver) -> Step:
        return CollectStep(
            id=self.id,
            then=self.then,
            fields=tuple(self.fields),
            question=self.question,
            after=frozenset(self.after),
            lookahead=self.lookahead,
            prompt=resolve.prompt(self.id, self.prompt, self.agent),
            choices=resolve.choices(self.id, self.fields, self.choices),
            spec=resolve.persona(self.id, self.agent),
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
        found = resolve.operation_type(self.id, self.uses)
        if found is not None and self.uses in resolve.spec.operations:
            return
        args = getattr(getattr(found[0], 'args_type', None), 'model_fields', {}) if found else {}
        for name in args:
            resolve.state_field(self.id, f'{name}')


class ConfirmSpec(_Operated):
    kind: Literal['confirm']
    then: str
    on_reject: str
    on_revise: str

    def destinations(self) -> list[str]:
        return [self.then, self.on_reject, self.on_revise]

    def check(self, resolve: _Resolver) -> None:
        super().check(resolve)
        declared = resolve.spec.operations.get(self.uses)
        if declared is not None and declared.approval != 'readback':
            resolve.fail(self.id, f'confirms {self.uses!r}, which is declared with no readback to confirm')

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
    returns: bool = False
    """A code node: on success, back to the step that handed the caller here (`ActionStep.returns`)."""

    def destinations(self) -> list[str]:
        return [*super().destinations(), *(to for to in (self.on_error, self.on_unknown) if to)]

    def check(self, resolve: _Resolver) -> None:
        super().check(resolve)
        if self.accepts:
            resolve.check(self.id, self.accepts)

    def to_step(self, resolve: _Resolver) -> Step:
        found = resolve.operation_type(self.id, self.uses)
        accepts = resolve.check(self.id, self.accepts) if self.accepts else (found[1] if found else None)
        operation = resolve.operation(self.uses)
        if accepts is None and isinstance(operation, FunctionOperation):
            accepts = operation.succeeded
        return ActionStep(
            id=self.id,
            then=self.then,
            operation=operation,
            accepts=accepts or (lambda _: True),
            after=frozenset(self.after),
            on_error=self.on_error,
            on_unknown=self.on_unknown,
            returns=self.returns,
        )


class HandoverSpec(BaseModel):
    """One step a conversation step may hand the caller to, and what for: the agent is shown the purpose."""

    model_config = SPEC
    to: str
    purpose: str = ''


class ConversationSpec(StepSpec):
    """An agent node: one agent in its own words, answering, and, when declared, recording, offering, acting and handing over.

    Its agent reads every reply itself; no reading runs before it. `requires`
    and `satisfied_by` interpose a prerequisite step and come back by code.
    """

    kind: Literal['conversation']
    prompt: str = ''
    agent: str = ''
    knowledge: str = ''
    """Added to the end of the prompt: a file in the flow folder, or a registry name."""
    fields: list[str] = []
    """State fields this step's agent may record from the caller (`collect`)."""
    choices: dict[str, str] = {}
    operations: list[str] = []
    """Declared operations this step's agent may carry out: each read back, and run only on its yes to a heard read-back."""
    exits: list[HandoverSpec] = []
    """Steps this one may hand the caller to (`hand_over`): `[booking]`, or `[{to: booking, purpose: ...}]`."""
    requires: list[str] = []
    """State facts this step needs before it runs; `satisfied_by` is interposed until they hold."""
    satisfied_by: str = ''
    guarded: bool = False
    """Whether model speech here is held a clause at a time and checked against what was done (`Node.guarded`)."""
    quoted: list[str] = []
    """Fields among `fields` that must be the caller's own words, found in what they said (a complaint)."""
    opens_with: str = ''
    """One of `operations`, read back by code on arrival when the state already gives its arguments."""
    asks: dict[str, str] = {}
    """Field -> words: on arrival by a hand-over, code asks for the first of these fields still empty, no model."""
    reads_back: dict[str, list[str]] = {}
    """Operation -> fields: a reply that only records one of them gets the operation read back by code, no request.

    Only while that operation is the one this step could run (`ConversationStep.reads_back`).
    """

    @field_validator('exits', mode='before')
    @classmethod
    def _plain_names(cls, exits: Any) -> Any:
        """`exits: [booking]` is short for `exits: [{to: booking}]`."""
        return [{'to': exit} if isinstance(exit, str) else exit for exit in exits or []]

    def destinations(self) -> list[str]:
        return [*(exit.to for exit in self.exits), *([self.satisfied_by] if self.satisfied_by else [])]

    def check(self, resolve: _Resolver) -> None:
        resolve.prompt(self.id, self.prompt, self.agent)
        resolve.persona(self.id, self.agent)
        for name in self.requires:
            resolve.state_field(self.id, name)
        if bool(self.requires) != bool(self.satisfied_by):
            resolve.fail(
                self.id,
                'requires and satisfied_by go together: what is needed, and the step that provides it',
            )
        for name in sorted(set(self.quoted) - set(self.fields)):
            resolve.fail(self.id, f'quotes {name!r}, which is not one of its fields')
        if self.opens_with and self.opens_with not in self.operations:
            resolve.fail(self.id, f'opens with {self.opens_with!r}, which is not one of its operations')
        for name in sorted(set(self.asks) - set(self.fields)):
            resolve.fail(self.id, f'asks for {name!r}, which is not one of its fields')
        for name, fields in self.reads_back.items():
            if name not in self.operations:
                resolve.fail(self.id, f'reads back {name!r}, which is not one of its operations')
            for field_name in sorted(set(fields) - set(self.fields)):
                resolve.fail(
                    self.id, f'reads back {name!r} on {field_name!r}, which is not one of its fields'
                )
        if self.knowledge:
            resolve.knowledge(self.id, self.knowledge)
        for name in self.fields:
            resolve.state_field(self.id, name)
        resolve.choices(self.id, self.fields, self.choices)
        for name in self.operations:
            if name not in resolve.spec.operations:
                resolve.fail(self.id, f'carries out {name!r}, which `operations:` does not declare')
            elif not resolve.spec.operations[name].say:
                resolve.fail(self.id, f'carries out {name!r}, which has no `say:` for its receipt')
            else:
                resolve.declared(name)

    def to_step(self, resolve: _Resolver) -> Step:
        prompt = resolve.prompt(self.id, self.prompt, self.agent)
        if self.knowledge and prompt is not None:
            knowledge = resolve.knowledge(self.id, self.knowledge) or ''
            prompt = TemplateStr(f'{prompt}\n\nKnowledge:\n{knowledge}', deps_type=resolve.call_type())
        return ConversationStep(
            id=self.id,
            instructions=prompt,
            fields=tuple(self.fields),
            choices=resolve.choices(self.id, self.fields, self.choices),
            operations=tuple(resolve.operation(name) for name in self.operations),
            exits=tuple(exit.to for exit in self.exits),
            purposes={exit.to: exit.purpose for exit in self.exits if exit.purpose},
            requires=tuple(self.requires),
            satisfied_by=self.satisfied_by,
            guarded=self.guarded,
            quoted=tuple(self.quoted),
            opens_with=resolve.operation(self.opens_with) if self.opens_with else None,
            asks=dict(self.asks),
            reads_back={name: tuple(fields) for name, fields in self.reads_back.items()},
            spec=resolve.persona(self.id, self.agent),
        )


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
    take_back: str = ''
    """Where a caller who takes the result back goes: a confirm step for the undo operation."""

    def destinations(self) -> list[str]:
        return [self.take_back] if self.take_back else []

    def to_step(self, resolve: _Resolver) -> Step:
        return End(id=self.id, outcome=self.outcome, say=self.say, take_back=self.take_back)


type AnyStep = Annotated[
    CollectSpec | ConfirmSpec | ActionSpec | ConversationSpec | AgentSpec | EndSpec,
    Field(discriminator='kind'),
]


class OperationSpec(BaseModel):
    """One ordinary business action: a typed function, where its arguments come from, and what its receipt proves.

    The function's signature is the schema (`def book(clinic: Backend, a: Appointment) -> Booked`);
    this only connects it to the state and the caller.
    """

    model_config = SPEC
    function: str
    """`file.py:name`, taking the call's services and the argument model, returning the receipt model."""
    args: dict[str, str] = {}
    """Each argument and the state field it is taken from. Every argument is bound; none is left to a model."""
    assign: dict[str, str] = {}
    """State fields set from the receipt, once it validates and succeeds: `{reference: reference}`."""
    succeeds: dict[str, str | int | bool] = {}
    """Receipt fields and the values meaning it worked: `{status: booked}`. Anything else is not success."""
    matches: dict[str, str] = {}
    """Receipt fields that must equal an argument: `{customer_phone: phone}`. A receipt about someone else counts for nothing."""
    approval: Literal['readback', 'none'] = 'readback'
    """`readback`: only after a heard read-back of these exact arguments and a yes. `none`: a read or a lookup."""
    readback: str = ''
    """`file.py:words`, taking the arguments, returning the sentence the caller agrees to."""
    reconcile: str = ''
    """`file.py:find`, `(services, args[, intent]) -> receipt | None`: what happened to an intent whose outcome is unknown."""
    say: str = ''
    """The receipt's speech, formatted from its fields: `Booked, reference {reference}.`"""
    recap: str = ''
    """What a goodbye may say about it later, formatted from the receipt: `You're booked for {spoken}.`

    Said only when the model points the goodbye at this receipt and the
    receipt still stands. Empty: a goodbye cannot recap it.
    """
    changes: bool = True
    """Whether it changes a business resource. `false` for a lookup: its receipt never backs a claim that something changed."""
    subject: str = ''
    """The receipt field naming the resource it acts on (`booking_id`): a later change to it supersedes this one."""
    idempotent: bool = False
    """The backend dedups by the intent id (the function's `Intent` parameter), so an unknown intent may be sent again."""


class PersonaSpec(BaseModel):
    """A reusable agent: shared instructions, and the SDK's own agent spec for its model and settings."""

    model_config = SPEC
    prompt: str
    spec: NativeSpec = NativeSpec()
    """Pydantic AI's native `AgentSpec` (model, model_settings, retries...), never retyped here. Its instructions are `prompt`."""


LOCALES: dict[str, Locale] = {'english': ENGLISH, 'spanish': SPANISH}


class LocaleSpec(BaseModel):
    """A language for the engine's fixed lines, with the application's own words where its domain decides them.

    `claims` are the verbs that claim a change in this application ("booked",
    "resolved"): patterns for the clause check on a `guarded` step. `goodbye`
    replaces the language's closing line. Each pattern is compiled here, so a
    broken one fails at load, never on a call.
    """

    model_config = SPEC
    base: Literal['english', 'spanish']
    claims: list[str] = []
    goodbye: str = ''

    @field_validator('claims')
    @classmethod
    def _compiles(cls, claims: list[str]) -> list[str]:
        for pattern in claims:
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(f'claim pattern {pattern!r} does not compile: {error}') from None
        return claims

    def build(self) -> Locale:
        base = LOCALES[self.base]
        return replace(base, claims=tuple(self.claims), goodbye=self.goodbye or base.goodbye)


class FlowSpec(BaseModel):
    """A whole flow file: the application's manifest, `agent.yaml`."""

    model_config = SPEC
    version: Literal[1]
    """The manifest format's version."""
    id: str
    app_version: str = ''
    """The application's own version, reported by the compiled flow."""
    entry: str = ''
    """The step a call starts on. Empty: the first step."""
    services: str = ''
    """`file.py:factory`, making one call's services when `open` is given none."""
    model: str = ''
    """The default model, like `google-gla:gemini-3.5-flash-lite`. A persona's own model wins; `compile(model=...)` wins over both."""
    locale: Literal['', 'english', 'spanish'] | LocaleSpec = ''
    """The engine's own fixed lines, in this language, or a language and the application's own words for it.

    Speech settings belong to the voice adapter.
    """
    agents: dict[str, PersonaSpec] = {}
    operations: dict[str, OperationSpec] = {}
    greeting: str = ''
    """The first thing a caller hears."""
    state: str = ''
    """The application's state model, like `models.py:ClinicState`: plain data, every field with a default."""
    identity: str = ''
    """The state field that says who the caller is. A question put to one identity is not answered by another."""
    call: str = ''
    """A call class of the application's own, like `models/state.py:SalonCall`, instead of `state`.

    ponytail: kept for salon-graph and Faro, which still subclass `CallContext`
    until their migration sessions (4 and 5); remove it then.
    """
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
        if self.call and self.state:
            resolve.fail('', 'name the state model or a call class, not both')
        resolve.call_type()
        if self.identity:
            resolve.state_field('identity', self.identity)
        business = [s for s in self.steps if isinstance(s, CollectSpec | ConfirmSpec | ActionSpec)]
        if business and not self.state:
            resolve.fail('', 'a flow with collect, confirm or action steps needs a state model (`state:`)')
        ids = [step.id for step in self.steps]
        for repeated in sorted({i for i in ids if ids.count(i) > 1}):
            resolve.fail('', f'step id {repeated!r} is used more than once')
        known = set(ids)
        if self.entry and self.entry not in known:
            resolve.fail('entry', f'{self.entry!r} is not a step')
        if self.services:
            resolve.function('services', self.services, 0)
        for name, persona in self.agents.items():
            if persona.spec.instructions is not None:
                resolve.fail(
                    f'agent {name}', 'its instructions are its prompt file; leave spec.instructions out'
                )
        for name in self.operations:
            resolve.declared(name)
        for step in self.steps:
            for to in step.destinations():
                if to not in known:
                    resolve.fail(step.id, f'sends the caller to {to!r}, which is not a step')
            step.check(resolve)
        spoken = {n for s in self.steps if isinstance(s, CollectSpec | ConversationSpec) for n in s.fields}
        for name, fields in resolve.assigned.items():
            for field_name in sorted(fields & spoken):
                resolve.fail(
                    f'operation {name}',
                    f'assigns {field_name!r}, which the caller gives; a receipt may not overwrite it',
                )
        collected: set[str] = set()
        for step in self.steps:
            if isinstance(step, CollectSpec | ActionSpec):
                for name in step.after:
                    if name not in collected:
                        resolve.fail(step.id, f'depends on {name!r}, which no earlier step collects')
            if isinstance(step, CollectSpec):
                collected |= step.produces(resolve)
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
        compiled = resolve.prompt(step_id, step.prompt, getattr(step, 'agent', ''))
        if compiled is None:
            raise FlowSpecError(resolve.problems)
        return compiled

    def compile(
        self,
        registry: Registry | None = None,
        *,
        model: Model | str | None = None,
        settings: ModelSettings | None = None,
    ) -> StepFlow:
        """The runnable flow, or `FlowSpecError` with every problem. Compile once per published version.

        `model` given here is used by every step, personas included, and by the
        readings: the explicit override a test or an evaluation passes. Without
        it, the manifest's `model:` is the default and a persona's own wins.
        """
        resolve = self._resolver(registry or Registry())
        if model is None and not self.model:
            resolve.fail('model', 'no model: set `model:` in the manifest or pass compile(model=...)')
        if resolve.problems:
            raise FlowSpecError(resolve.problems)
        resolve.explicit_model = model is not None
        model = model if model is not None else self.model
        state = resolve.state_type()
        flow = StepFlow(
            [step.to_step(resolve) for step in self.steps],
            model=model,
            settings=settings,
            state_type=state,
            identity=self.identity,
            call_type=resolve.call_type() if self.call else None,
            entry=self.entry,
            services=resolve.function('services', self.services, 0) if self.services else None,
            locale=self.locale.build() if isinstance(self.locale, LocaleSpec) else LOCALES.get(self.locale),
            version=self.app_version,
        )
        Flow.of(flow.nodes)
        return flow


def _positional(function: Callable[..., Any]) -> int:
    """How many positional arguments a function takes, or 0 when it takes any number."""
    parameters = inspect.signature(function).parameters.values()
    if any(p.kind is p.VAR_POSITIONAL for p in parameters):
        return 0
    return sum(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in parameters)


def _hints(function: Callable[..., Any]) -> dict[str, Any]:
    try:
        return get_type_hints(function)
    except Exception:
        return {}


def _model(kind: Any) -> typing.TypeIs[type[BaseModel]]:
    return isinstance(kind, type) and issubclass(kind, BaseModel)


def _takes_intent(function: Callable[..., Any]) -> bool:
    """Whether a domain function's third positional parameter is annotated `core.Intent`."""
    params = [
        p
        for p in inspect.signature(function).parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    return len(params) == 3 and _hints(function).get(params[2].name) is Intent


def _bare(kind: Any) -> Any:
    """What a type holds at bottom: aliases resolved, constraints and `None` left out."""
    while True:
        if isinstance(kind, TypeAliasType):
            kind = kind.__value__
        elif get_origin(kind) is Annotated:
            kind = get_args(kind)[0]
        elif get_origin(kind) in (typing.Union, type(int | None)):
            kept = [a for a in get_args(kind) if a is not type(None)]
            if len(kept) != 1:
                return frozenset(_bare(a) for a in kept)
            kind = kept[0]
        else:
            return kind


def _fits(source: Any, target: Any) -> bool:
    """Whether every value of one declared type is a value the other holds. The whole value is still validated at run time.

    Checked on what the types hold at bottom (`_bare`): the same type, or a
    set of literals inside another set, or inside the type they are values of.
    """
    if target is Any:
        return True
    given, held = _bare(source), _bare(target)
    if get_origin(given) is Literal:
        values = get_args(given)
        if get_origin(held) is Literal:
            return set(values) <= set(get_args(held))
        return isinstance(held, type) and all(isinstance(value, held) for value in values)
    return given == held


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
