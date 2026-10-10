"""Flow files: a flow folder described by one YAML file, checked before it can run.

A flow is a folder. `agent.yaml` keys its steps by id, in order, and each
step carries its whole definition there: its kind, its own prompt file, the
Python module holding its deterministic code, and where it can go next.

    flows/access/
      agent.yaml
      voice.md              added to the top of every prompt
      prompts/team.md       one prompt file per step that runs a model
      steps/submit.py       one module per step that runs code

Three step kinds. A `conversation` step runs a model, so it names a
`prompt`; its agent answers with one typed decision per response
(`core.structured`), and code says every read-back and receipt. `action`
runs one operation with no model; `end` says the closing line. Neither has
a prompt field.

What a file may refer to:

* a path inside the flow's own folder (`prompts/team.md`, `steps/submit.py`,
  `steps/fields.py:Team`), and nothing outside it, so a file cannot reach
  into arbitrary code;
* a name in the `Registry`, for code shared by several flows.

The application's state is one Pydantic model, named by `state:`
(`models.py:ClinicState`). Conversation steps name its fields; operations take
their arguments from it by name; `identity:` names the field that says who
the caller is. The engine keeps it as `call.state.facts`, of exactly that
type, and no application subclasses the call.

`FlowSpec.compile` refuses a flow with every problem it has, not just the
first: an unknown kind or field, a repeated id, a destination that does not
exist, a missing file or name, a module without what its step needs, a prompt
naming a field the call does not have, a prompt shared by two steps, a field
the state does not have, an operation argument the state cannot supply,
and an action step whose operation needs a read-back. Checking needs no
model and no key:
`problems()` connects to nothing, and only `compile` builds agents.

Files are read with a `yaml.SafeLoader` that builds only plain data and
refuses a key given twice in one mapping (`_Manifest`), and JSON is valid
YAML, so either format loads. Plain YAML turns an unquoted `no`, `yes`, `on`
or `off` into a boolean and an unquoted `2026-10-01` into a date; a text
field given one of those is refused with the step and field named.

The manifest is `version: 2`, the only version. A step with no `kind` is a
`conversation`, under a flow context of no facts and the step's own history
(`FlowSpec._normalized`).
"""

from __future__ import annotations

import importlib
import inspect
import sys
import typing
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass, field, replace
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any, Literal, TypeAliasType, get_args, get_origin, get_type_hints
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    RootModel,
    ValidationError,
    create_model,
    field_validator,
    model_validator,
)
from pydantic_ai import TemplateStr
from pydantic_ai.agent.spec import AgentSpec as NativeSpec
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from core.actions import Action
from core.clock import CallClock
from core.closing import Closing, closing
from core.history import History, Scope
from core.locale import ENGLISH, SPANISH, Locale
from core.operations import Operation
from core.state import CallContext, Intent
from core.steps import (
    ActionStep,
    ConversationStep,
    End,
    FunctionOperation,
    NoFacts,
    Step,
    StepFlow,
)
from core.values import Receipt, Rejection, Subject, Version

SPEC = ConfigDict(extra='forbid', frozen=True)
QUOTE_CALLER = (
    '\n\n## Latest caller message\n\n'
    'Use this with the conversation above, including earlier speech fragments.\n\n> {{heard}}'
)
"""Appended to every prompt when a flow sets `quote_caller`: the caller's words, where the model reads them last."""


class ViewTemplate(TemplateStr):
    """A conversation step's prompt: compiled against the step's view of the call, and rendered from that view alone.

    The view is `{heard, state: {facts}}` with only the state fields the step
    sees, so `{{state.facts.x}}` for a hidden field, `{{services...}}` or
    anything else of the call fails when the manifest loads
    (`TemplateSchemaError`), and nothing outside the view is ever rendered.
    """

    __slots__ = ('_names', '_seen', '_state', '_view')

    def __init__(self, source: str, *, state: type[BaseModel], facts: tuple[str, ...] | None) -> None:
        self._names = tuple(state.model_fields) if facts is None else facts
        self._seen = create_model(  # ty: ignore[no-matching-overload]
            f'{state.__name__}Seen', **{n: (state.model_fields[n].annotation, None) for n in self._names}
        )
        self._state = create_model('SeenState', facts=(self._seen, None))
        self._view = create_model('SeenCall', heard=(str, ''), state=(self._state, None))
        super().__init__(source, deps_type=self._view)

    def __call__(self, ctx: Any) -> str:
        facts = ctx.deps.state.facts
        seen = self._seen.model_construct(**{name: getattr(facts, name) for name in self._names})
        return self.render(
            self._view.model_construct(heard=ctx.deps.heard, state=self._state.model_construct(facts=seen))
        )


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

    A use case's `tests` may be one: `tests/slice` is imported as `tests.slice`,
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
        self.choice_after: dict[str, frozenset[str] | None] = {}
        """Choice to the fields its offer rests on, as the first step that offers it declared (None: every field)."""
        self.routes: dict[str, frozenset[str]] = {}
        """Declared operation to the steps its receipt can move the caller to (`route`)."""

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

    def prompt(self, step: str, ref: str, agent: str, view: ContextSpec) -> TemplateStr | None:
        """The step's prompt: its agent's shared instructions, its own file, the flow's includes and voice, compiled now.

        A persona's prompt may serve many steps; a step's own prompt belongs to
        that step alone. It compiles against what that step sees (`view`,
        `ViewTemplate`), never the whole call.
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
                return ViewTemplate(text, state=self.state_type(), facts=view.shown())
            except Exception as error:
                raise _ProblemError(
                    f"prompt {ref or agent!r} reaches past this step's context (its facts and the caller's "
                    f'words): {error}'
                ) from None

        key = f'{agent}|{ref}|{view.shown()}'
        return self._once('prompt', key, step, find)

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
        """The one instance of an operation for this flow, shared by every step that names it."""
        if ref not in self.operations:
            found = self.operation_type('', ref)
            assert found is not None
            self.operations[ref] = found[0]()
        return self.operations[ref]

    def capability(self, step: str, ref: str) -> Any:
        """A native Pydantic AI capability from the flow folder: the object itself, or what a factory with no arguments makes."""

        def find() -> Any:
            found = self.folder.attr(ref)
            if isinstance(found, type) or (callable(found) and not isinstance(found, AbstractCapability)):
                found = found()
            if not isinstance(found, AbstractCapability):
                raise _ProblemError(f'{ref!r} is not a Pydantic AI capability')
            if found.defer_loading and not (found.id and found.description):
                raise _ProblemError(f'{ref!r} loads on demand, so it needs an id and a description')
            return found

        return self._once('capability', ref, step, find)

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

    def action(self, name: str) -> type[Action] | None:
        """The class a declared operation names (`name: file.py:Class`), or None for the long form or a broken ref."""
        ref = _class_ref(self.spec.operations.get(name))
        if ref is None:
            return None

        def find() -> type[Action]:
            found = self.folder.attr(ref)
            if not (isinstance(found, type) and issubclass(found, Action)):
                raise _ProblemError(f'{ref!r} is not a core.Lookup or core.Write')
            return found

        return self._once('action', ref, f'operation {name}', find)

    def decl(self, name: str) -> OperationSpec | None:
        """A declared operation in its long form, as the step checks read it; a class says the same keys itself."""
        declared = self.spec.operations.get(name)
        ref = _class_ref(declared)
        if ref is None:
            return declared if isinstance(declared, OperationSpec) else None
        action = self.action(name)
        spoken = action.spoken() if action is not None else {}
        return OperationSpec.model_validate({'function': ref, **spoken, **_closing(declared)})

    def declared(self, name: str) -> type[FunctionOperation]:
        """A declared operation, as one `FunctionOperation` subclass: every problem with it is kept, none raised.

        The function's signature is the schema: `(services, args: ArgsModel) -> ReceiptModel`.
        Every argument is bound to a state field, every assignment names a
        receipt field and a state field, and each pair must hold the same kind
        of value. Nothing here is typed again in the YAML.
        """
        if (cached := self._cache.get(('declared', name))) is not None:
            return cached
        if _class_ref(self.spec.operations[name]) is not None:
            found = self._declared_class(name)
            self._cache['declared', name] = found
            return found
        decl, where = self.spec.operations[name], f'operation {name}'
        assert isinstance(decl, OperationSpec)
        attrs: dict[str, Any] = {'name': name, 'binds': dict(decl.args), 'assign': dict(decl.assign)}
        attrs |= {
            'succeeds': dict(decl.succeeds),
            'say': decl.say,
            'needs_approval': decl.approval == 'readback',
            'matches': dict(decl.matches),
            'changes': decl.changes,
            'recap': decl.recap,
            'subject': decl.subject,
            'resource': decl.resource,
            'version': decl.version,
            'rejects': dict(decl.rejects),
            'idempotent': decl.idempotent,
            'confirms_by_restating': decl.restated == 'confirms',
            'route': decl.route,
        }
        if decl.restated == 'confirms' and (decl.changes or decl.approval != 'readback'):
            self.fail(
                where,
                'restated: confirms is only for a read-back of a lookup (changes: false, approval: readback): '
                'a value said again is never a yes to a change',
            )
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
            for key, declared in (('succeeds', decl.succeeds), ('rejects', decl.rejects)):
                for field_name in declared:
                    if field_name not in receipt_type.model_fields:
                        self.fail(
                            where, f'{key} on {field_name!r}, which {receipt_type.__name__} does not have'
                        )
            for field_name, value in decl.succeeds.items():
                refused = decl.rejects.get(field_name, [])
                if value == refused or (isinstance(refused, list) and value in refused):
                    self.fail(where, f'{field_name}: {value!r} cannot both succeed and reject')
            if decl.version and decl.version not in receipt_type.model_fields:
                self.fail(
                    where, f'names version {decl.version!r}, which {receipt_type.__name__} does not have'
                )
            if decl.route:
                self.routes[name] = self._route(where, receipt_type, decl.route)
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
        if decl.subject and decl.changes and not decl.version:
            self.fail(
                where, 'names a subject it changes, so it needs the receipt field of its version (version:)'
            )
        if (decl.resource or decl.version) and not decl.subject:
            self.fail(where, 'a resource kind or version needs the subject they are about (subject:)')
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

    def _declared_class(self, name: str) -> type[FunctionOperation]:
        """A class operation (`core.actions`), as the same `FunctionOperation` a long-form declaration builds.

        Every key the long form writes is read from the class and its
        receipt types, and checked the same way: bindings against the state,
        receipt fields against the receipt, the version beside the subject.
        """
        where = f'operation {name}'
        action = self.action(name)
        if action is None:
            return type(f'{name.title().replace("_", "")}Operation', (FunctionOperation,), {'name': name})
        state = self.state_type().model_fields
        binds, paths = self._action_binds(where, action, state)
        try:
            returned = get_type_hints(action.run, include_extras=True).get('return')
        except Exception as error:
            self.fail(where, f'{action.__name__}.run has a return annotation that does not resolve: {error}')
            returned = None
        members, tag = _members(returned)
        if not members:
            self.fail(
                where,
                f'{action.__name__}.run must annotate its return with a core.Receipt, or a union of them',
            )
        for member in members:
            if not issubclass(member, Receipt):
                self.fail(where, f'{member.__name__} is not a core.Receipt: every receipt subclasses it')
        if len(members) > 1 and not tag:
            self.fail(
                where, 'a receipt union needs a discriminator: Annotated[A | B, Field(discriminator=...)]'
            )
        won = [m for m in members if not issubclass(m, Rejection)]
        lost = [m for m in members if issubclass(m, Rejection)]
        if members and not won:
            self.fail(where, 'every receipt it can return is a core.Rejection: nothing could succeed')
        succeeds: dict[str, Any] = (
            {tag: [v for m in won for v in _tags(m, tag)]} if tag and len(members) > 1 else {}
        )
        rejects: dict[str, Any] = {tag: [v for m in lost for v in _tags(m, tag)]} if tag and lost else {}
        subject, kind, version = _marked(won)
        for field_name in action.matches:
            if field_name not in action.model_fields:
                self.fail(where, f'matches {field_name!r}, which is not one of its arguments')
            for member in members:
                if field_name not in member.model_fields:
                    self.fail(where, f'matches {field_name!r}, which {member.__name__} does not have')
        assign = (
            dict(action.assign) if action.assign is not None else self._inferred_assign(where, won, state)
        )
        for field_name, source in assign.items():
            if field_name not in state:
                self.fail(where, f'assigns {field_name!r}, which the state does not have')
            elif source == '*':
                if not any(_fits(m, state[field_name].annotation) for m in won):
                    self.fail(
                        where, f'{field_name!r} cannot hold the whole receipt: it holds another kind of value'
                    )
            elif any(source not in m.model_fields for m in won):
                self.fail(where, f'assigns from {source!r}, which a success receipt does not have')
        if action.changes and subject and not version:
            self.fail(where, 'names a subject it changes, so its receipt needs a core.Version field')
        try:
            own = {'say': closing(action.say), 'recap': closing(action.recap)}
        except ValidationError as error:
            self.fail(
                where, f'{action.__name__} has closing lines that do not read: {error.errors()[0]["msg"]}'
            )
            own = {'say': '', 'recap': ''}
        closing_lines = own | _closing(self.spec.operations.get(name))
        if closing_lines['recap'] and not action.changes:
            self.fail(where, 'changes nothing, so a goodbye has nothing of it to recap (recap)')
        if action.needs_approval() and not action.defines('readback'):
            self.fail(where, f'needs approval, so {action.__name__} needs a readback method')
        if action.restated_confirms and (action.changes or not action.needs_approval()):
            self.fail(
                where,
                'restated_confirms is only for a read-back of a lookup: a value said again is never a yes to a change',
            )
        if action.route and won:
            self.routes[name] = frozenset().union(*(self._route(where, m, action.route) for m in won))
        self.assigned[name] = set(assign)
        receipt_type: type[BaseModel] = (
            members[0] if len(members) == 1 else RootModel[returned] if members else NoFacts
        )
        attrs: dict[str, Any] = {
            'name': name,
            'binds': binds,
            'paths': paths,
            'assign': assign,
            'succeeds': succeeds,
            'rejects': rejects,
            'say': closing_lines['say'],
            'needs_approval': action.needs_approval(),
            'matches': {field_name: field_name for field_name in action.matches},
            'changes': action.changes,
            'recap': closing_lines['recap'],
            'subject': subject,
            'resource': kind,
            'version': version,
            'idempotent': action.idempotent,
            'confirms_by_restating': action.restated_confirms,
            'route': action.route,
            'function': staticmethod(_method(action, 'run')),
            'function_takes_intent': True,
            'args_type': action,
            'receipt_type': receipt_type,
        }
        if action.defines('readback'):
            attrs['readback'] = staticmethod(_method(action, 'readback'))
            attrs['readback_uses_services'] = True
        if action.defines('reconcile'):
            attrs['reconcile'] = staticmethod(_method(action, 'reconcile'))
            attrs['reconcile_takes_intent'] = True
        return type(f'{name.title().replace("_", "")}Operation', (FunctionOperation,), attrs)

    def _action_binds(
        self, where: str, action: type[Action], state: Mapping[str, Any]
    ) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
        """Each argument's state field, by name or by its alias, and the path inside it for a nested one."""
        binds: dict[str, str] = {}
        paths: dict[str, tuple[str, ...]] = {}
        for arg, info in action.model_fields.items():
            alias = info.validation_alias
            if alias is None:
                path: tuple[str, ...] = (arg,)
            elif isinstance(alias, str):
                path = (alias,)
            elif isinstance(alias, AliasPath) and all(isinstance(step, str) for step in alias.path):
                path = tuple(str(step) for step in alias.path)
            else:
                self.fail(
                    where,
                    f'{arg!r} names its source with an alias core cannot follow: a name or an AliasPath',
                )
                continue
            if path[0] not in state:
                self.fail(where, f'{arg!r} comes from {path[0]!r}, which the state does not have')
                continue
            kind: Any = state[path[0]].annotation
            for step in path[1:]:
                inner = _bare(kind)
                if not (_model(inner) and step in inner.model_fields):
                    self.fail(where, f'{arg!r} comes from {".".join(path)}, which the state does not have')
                    break
                kind = inner.model_fields[step].annotation
            else:
                if not _fits(kind, info.annotation):
                    self.fail(
                        where, f'{".".join(path)!r} cannot supply {arg!r}: they hold different kinds of value'
                    )
            binds[arg] = path[0]
            if len(path) > 1:
                paths[arg] = path
        return binds, paths

    def _inferred_assign(
        self, where: str, won: list[type[BaseModel]], state: Mapping[str, Any]
    ) -> dict[str, str]:
        """The one state field typed as the success receipt, given the whole receipt; none, or refused when two are."""
        holders = [name for name, info in state.items() if any(_bare(info.annotation) is m for m in won)]
        if len(holders) > 1:
            self.fail(
                where,
                f'{", ".join(holders)} could each hold its receipt: say which with assign = {{field: "*"}}',
            )
            return {}
        return {holders[0]: '*'} if holders else {}

    def _route(self, where: str, receipt_type: type[BaseModel], route: str) -> frozenset[str]:
        """The steps a receipt's `route` field can name: a `Literal` of step ids, checked against the manifest."""
        if route not in receipt_type.model_fields:
            self.fail(where, f'routes by {route!r}, which {receipt_type.__name__} does not have')
            return frozenset()
        kind = _bare(receipt_type.model_fields[route].annotation)
        values = get_args(kind) if get_origin(kind) is Literal else ()
        if not values or not all(isinstance(value, str) for value in values):
            self.fail(where, f'routes by {route!r}, which must be a Literal of step ids')
            return frozenset()
        known = {step.id for step in self.spec.steps}
        for value in sorted(set(values) - known - {''}):
            self.fail(where, f'routes to {value!r}, which is not a step')
        return frozenset(values) - {''}

    def choices(
        self, step: str, fields: list[str], refs: Mapping[str, str | ChoiceSpec]
    ) -> dict[str, Callable[..., Any]]:
        """Each choice's function, and what its offer rests on (`after:`), the same in every step that offers it.

        `after:` may name a state field another step changes: an offer is taken
        away call-wide (`FlowCall._choice_after`), so a slot resting on the
        booking being moved rests on it in a step that never picks one too.
        """
        found = {}
        for name, ref in refs.items():
            if name not in fields:
                self.fail(step, f'offers choices for {name!r}, which the step does not ask for')
            after = frozenset(ref.after) if isinstance(ref, ChoiceSpec) else None
            for field_name in sorted(after or ()):
                if field_name == name:
                    self.fail(step, f'offers {name!r} after {field_name!r}, which is the choice itself')
                elif field_name not in fields and field_name not in self.state_type().model_fields:
                    self.fail(
                        step,
                        f'offers {name!r} after {field_name!r}, which the state '
                        f'{self.state_type().__name__} does not have',
                    )
            if self.choice_after.setdefault(name, after) != after:
                self.fail(step, f'offers {name!r} after other fields than another step that offers it')
            if (
                choose := self.function(step, ref.function if isinstance(ref, ChoiceSpec) else ref, 2)
            ) is not None:
                found[name] = choose
        dependencies = {
            name: sorted((self.choice_after[name] or frozenset()) & found.keys()) for name in found
        }
        try:
            ordered = tuple(TopologicalSorter(dependencies).static_order())
        except CycleError:
            self.fail(step, 'has a cycle in its choice dependencies (after:)')
            return found
        return {name: found[name] for name in ordered}

    def after(self, refs: Mapping[str, str | ChoiceSpec]) -> dict[str, frozenset[str]]:
        """The declared `after:` of each choice that has one."""
        return {name: frozenset(ref.after) for name, ref in refs.items() if isinstance(ref, ChoiceSpec)}

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


class ActionSpec(_Operated):
    kind: Literal['action']
    accepts: str = ''
    """A registered receipt check. Without one, the module's own `accepts`, if it has one, decides."""
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
            on_error=self.on_error,
            on_unknown=self.on_unknown,
            returns=self.returns,
        )


class ChoiceSpec(BaseModel):
    """A choice whose offer rests on some fields only: `{function: functions.py:find_times, after: [day, time]}`.

    A plain reference (`slot: clinic.py:free_times`) rests on every other
    field: any change to the request takes its offer and its pick away. With
    `after:`, only a change to one of those fields does; `after: []` rests on
    none (the bookings a caller holds do not change with the day they ask
    for). A change of the caller's identity takes every choice away.
    """

    model_config = SPEC
    function: str
    after: list[str]


class HandoverSpec(BaseModel):
    """One step a conversation step may hand the caller to, and what for: the agent is shown the purpose."""

    model_config = SPEC
    to: str
    purpose: str = ''


class ContextSpec(BaseModel):
    """What a step's agent is sent from before this caller turn: `context: {history: {scope: node}, facts: [slot]}`.

    `history`: which earlier caller turns, as what was said and heard
    (`core.history.History`), or `none`. `facts`: `all`, `none`, or the
    state fields it may see. The current caller turn always reaches the
    step. Limiting `facts` hides nothing the permitted history already
    holds: a value the caller said is in their words. What the application
    enforces (approval, receipts, the ledgers) is the same whatever the step
    sees.
    """

    model_config = SPEC
    history: Literal['none'] | History = History()
    facts: Literal['all', 'none'] | list[str] = 'none'

    @field_validator('facts')
    @classmethod
    def _top_level(cls, facts: Any) -> Any:
        """A fact is a whole top-level state field: a path, an index or a wildcard is refused, not guessed at."""
        for name in facts if isinstance(facts, list) else ():
            if not name.isidentifier():
                raise ValueError(
                    f'{name!r} is not a state field: facts are whole top-level fields. Select the field that '
                    'holds it (a nested model is shown whole), or give the shareable value a field of its own'
                )
        return facts

    def shown(self) -> tuple[str, ...] | None:
        """The facts as a step holds them: None for all, () for none, else the listed fields."""
        return None if self.facts == 'all' else () if self.facts == 'none' else tuple(self.facts)

    def under(self, root: ContextSpec | None) -> ContextSpec:
        """This step's context: the flow's, with each member this step sets replacing it.

        `facts` replaces the flow's list, never adds to it; `history` replaces
        the flow's as a unit. What this step leaves out is the flow's.
        """
        base = root if root is not None else ContextSpec()
        return base.model_copy(update={name: getattr(self, name) for name in self.model_fields_set})


NO_CONTEXT = ContextSpec(history='none', facts='none')
"""`context: none`: neither earlier turns nor facts. This turn's words still reach the step."""


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
    """State fields this step's agent may record from the caller, in a decision's `update`."""
    choices: dict[str, str | ChoiceSpec] = {}
    """A field picked from options, and the function offering them, with the fields its offer rests on (`ChoiceSpec`)."""
    operations: list[str] = []
    """Declared operations this step's agent may carry out: each read back, and run only on its yes to a heard read-back."""
    exits: list[HandoverSpec] = []
    """Steps this one may hand the caller to (`hand_over`): `[booking]`, or `[{to: booking, purpose: ...}]`."""
    requires: list[str] = []
    """State facts this step needs before it runs; `satisfied_by` is interposed until they hold."""
    satisfied_by: str = ''
    quoted: list[str] = []
    """Fields among `fields` that must be the caller's own words, found in what they said (a complaint)."""
    opens_with: str = ''
    """One of `operations`, read back by code on arrival when the state already gives its arguments."""
    asks: dict[str, str] = {}
    """Field -> words: on arrival by a hand-over, code asks for the first of these fields still empty, no model."""
    offers: list[str] = []
    """Choices code offers on arrival (a hand-over or a completed prerequisite), each in its function's own words.

    The choice function owns the step's first line: it offers, or asks for
    what it still needs, in its `say`. Words and ids make an offer; words alone
    are a question; ids alone stand for the agent to word; neither leaves the
    arrival to the agent with the function's note (`ConversationStep.offers`).
    Tried after `opens_with` and `asks`.
    """
    reads_back: dict[str, list[str]] = {}
    """Operation -> fields: a reply that only records one of them gets the operation read back by code, no request.

    Only while that operation is the one this step could run (`ConversationStep.reads_back`).
    """
    carries_out: dict[str, list[str]] = {}
    """Operation -> fields: an operation with no read-back that code carries out when a reply only records one of these.

    The approval-free twin of `reads_back`. The model records what the
    caller said (an exercise answer and its grade) and is never offered the
    operation itself, so it cannot run it on values recorded on an earlier
    turn: whatever it runs on was recorded by this reply
    (`ConversationStep.carries`).
    """
    context: ContextSpec | Literal['none'] | None = None
    """What this step's agent is sent from before this turn (`ContextSpec`). None: the flow's `context:`."""
    consults: list[HandoverSpec] = []
    """Specialists this step may ask on the caller's behalf: `{to, purpose}`, like an exit that comes back.

    The specialist speaks for itself, asks for what it needs, and calls
    `done` once it has answered; the caller is back here with no second
    request, and a read-back that was waiting is put again, fresh. One
    consultation at a time: a specialist does not consult.
    """
    capabilities: list[str] = []
    """Native Pydantic AI capabilities for this step's agent, as `file.py:name` (an instance, or a factory taking none).

    An on-demand one (`defer_loading=True`, with an `id` and a `description`)
    is listed in a short catalog and loaded by the model when it needs it.
    """

    @field_validator('exits', mode='before')
    @classmethod
    def _plain_names(cls, exits: Any) -> Any:
        """`exits: [booking]` is short for `exits: [{to: booking}]`."""
        return [{'to': exit} if isinstance(exit, str) else exit for exit in exits or []]

    def destinations(self) -> list[str]:
        return [
            *(exit.to for exit in [*self.exits, *self.consults]),
            *([self.satisfied_by] if self.satisfied_by else []),
        ]

    def check(self, resolve: _Resolver) -> None:
        resolve.prompt(self.id, self.prompt, self.agent, self.seen(resolve))
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
        for name in self.offers:
            if name not in self.choices:
                resolve.fail(self.id, f'offers {name!r} on arrival, which is not one of its choices')
        for name, fields in self.carries_out.items():
            declared = resolve.decl(name)
            if declared is None:
                resolve.fail(self.id, f'carries out {name!r}, which `operations:` does not declare')
                continue
            if declared.approval != 'none':
                resolve.fail(
                    self.id, f'carries out {name!r} by code, which needs a read-back and a yes first'
                )
            if name in self.operations:
                resolve.fail(
                    self.id, f'carries out {name!r} by code and offers it to the model too; pick one'
                )
            if not declared.say:
                resolve.fail(self.id, f'carries out {name!r}, which has no `say:` for its receipt')
            for field_name in sorted(set(fields) - set(self.fields)):
                resolve.fail(
                    self.id, f'carries out {name!r} on {field_name!r}, which is not one of its fields'
                )
            resolve.declared(name)
        for name, fields in self.reads_back.items():
            if name not in self.operations:
                resolve.fail(self.id, f'reads back {name!r}, which is not one of its operations')
            for field_name in sorted(set(fields) - set(self.fields)):
                resolve.fail(
                    self.id, f'reads back {name!r} on {field_name!r}, which is not one of its fields'
                )
        if self.knowledge:
            resolve.knowledge(self.id, self.knowledge)
        shown = self.seen(resolve).shown()
        for name in shown or ():
            resolve.state_field(self.id, name)
        if shown is not None:
            for name in sorted(set(self.fields) - set(shown)):
                resolve.fail(self.id, f'may change {name!r} but cannot see it: add it to context.facts')
        for ref in self.capabilities:
            resolve.capability(self.id, ref)
        for consult in self.consults:
            target = next((step for step in resolve.spec.steps if step.id == consult.to), None)
            if target is not None and not isinstance(target, ConversationSpec):
                resolve.fail(self.id, f'consults {consult.to!r}, which is not a conversation step')
            elif target is not None and target.consults:
                resolve.fail(
                    self.id, f'consults {consult.to!r}, which consults others: one consultation at a time'
                )
            if consult.to in {exit.to for exit in self.exits}:
                resolve.fail(self.id, f'hands over to {consult.to!r} and consults it too; pick one')
        for name in self.fields:
            resolve.state_field(self.id, name)
        resolve.choices(self.id, self.fields, self.choices)
        for name in self.operations:
            if name not in resolve.spec.operations:
                resolve.fail(self.id, f'carries out {name!r}, which `operations:` does not declare')
            elif not (found := resolve.decl(name)) or not (found.say or self._reads(found)):
                resolve.fail(self.id, f'carries out {name!r}, which has no `say:` for its receipt')
            else:
                resolve.declared(name)

    def _reads(self, declared: Any) -> bool:
        """A pure read: no read-back, no change, no route. Its answer goes back to the agent."""
        return declared.approval == 'none' and not declared.changes and not declared.route

    def seen(self, resolve: _Resolver) -> ContextSpec:
        """This step's context: the flow's, with this step's members replacing it one by one (`ContextSpec.under`).

        `context: none` is no facts and no history. It is what the step's
        prompt compiles against (`ViewTemplate`).
        """
        root = resolve.spec.context
        root = NO_CONTEXT if root == 'none' else root
        own = NO_CONTEXT if self.context == 'none' else self.context
        return own.under(root) if own is not None else root

    def scope(self, resolve: _Resolver) -> Scope:
        """What this step's automatic context may show (`core.history.Scope`)."""
        view = self.seen(resolve)
        owned = [
            found
            for name in dict.fromkeys([*self.operations, *self.carries_out])
            if (found := resolve.operation(name)) is not None
        ]
        return Scope(
            facts=view.shown(),
            fields=frozenset(self.fields),
            operations=frozenset(op.name for op in owned),
            binds={op.name: dict(getattr(op, 'binds', {}) or {}) for op in owned},
            assign={op.name: dict(getattr(op, 'assign', {}) or {}) for op in owned},
        )

    def to_step(self, resolve: _Resolver) -> Step:
        view = self.seen(resolve)
        prompt = resolve.prompt(self.id, self.prompt, self.agent, view)
        if self.knowledge and prompt is not None:
            knowledge = resolve.knowledge(self.id, self.knowledge) or ''
            prompt = ViewTemplate(
                f'{prompt}\n\nKnowledge:\n{knowledge}', state=resolve.state_type(), facts=view.shown()
            )
        return ConversationStep(
            id=self.id,
            instructions=prompt,
            fields=tuple(self.fields),
            choices=resolve.choices(self.id, self.fields, self.choices),
            choice_after=resolve.after(self.choices),
            operations=tuple(resolve.operation(name) for name in self.operations),
            exits=tuple(exit.to for exit in [*self.exits, *self.consults]),
            purposes={exit.to: exit.purpose for exit in [*self.exits, *self.consults] if exit.purpose},
            consults=frozenset(consult.to for consult in self.consults),
            consulted=any(
                consult.to == self.id
                for step in resolve.spec.steps
                if isinstance(step, ConversationSpec)
                for consult in step.consults
            ),
            requires=tuple(self.requires),
            satisfied_by=self.satisfied_by,
            quoted=tuple(self.quoted),
            opens_with=resolve.operation(self.opens_with) if self.opens_with else None,
            asks=dict(self.asks),
            offers=tuple(self.offers),
            reads_back={name: tuple(fields) for name, fields in self.reads_back.items()},
            carries=tuple(
                (resolve.operation(name), tuple(fields)) for name, fields in self.carries_out.items()
            ),
            routes=tuple(
                sorted(
                    frozenset().union(
                        *(
                            resolve.routes.get(name, frozenset())
                            for name in [*self.operations, *self.carries_out]
                        )
                    )
                    - {self.id}
                )
            ),
            spec=resolve.persona(self.id, self.agent),
            history=view.history,
            facts=view.shown(),
            scope=self.scope(resolve),
            capabilities=tuple(
                found for ref in self.capabilities if (found := resolve.capability(self.id, ref)) is not None
            ),
        )


class EndSpec(StepSpec):
    kind: Literal['end']
    outcome: Literal['completed', 'cancelled', 'failed', 'pending']
    say: str
    take_back: str = ''
    """Where a caller who takes the result back goes: the step whose operation undoes it, with its own read-back."""

    def destinations(self) -> list[str]:
        return [self.take_back] if self.take_back else []

    def to_step(self, resolve: _Resolver) -> Step:
        return End(id=self.id, outcome=self.outcome, say=self.say, take_back=self.take_back)


type AnyStep = Annotated[
    ActionSpec | ConversationSpec | EndSpec,
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
    rejects: dict[str, str | int | bool | list[str | int | bool]] = {}
    """Receipt fields and the values meaning a terminal no: `{status: [full, stale]}` (a list: any of them).

    A change whose receipt says neither `succeeds` nor `rejects` (a `pending`,
    a status nobody declared) settles nothing: its intent stays unknown until
    reconciliation says which. A read (`changes: false`) cannot have changed
    anything, so for it anything but `succeeds` is simply a no.
    """
    matches: dict[str, str] = {}
    """Receipt fields that must equal an argument: `{customer_phone: phone}`. A receipt about someone else counts for nothing."""
    approval: Literal['readback', 'none'] = 'readback'
    """`readback`: only after a heard read-back of these exact arguments and a yes. `none`: a read or a lookup."""
    restated: Literal['confirms'] | None = None
    """`confirms`: a caller who answers the heard read-back by saying its value again has confirmed it.

    The application's policy, for a lookup only (`changes: false`): the
    salon's rule that the same phone number said back is a yes. Without it,
    recording the unchanged value is only a record, and the step's own agent
    reads the reply. The loader refuses it on anything that changes a
    resource: a value said again ("No, do not book Friday at two, I'm only
    checking") is never consent to a booking, move, cancel or complaint.
    """
    readback: str = ''
    """`file.py:words`, taking the arguments, returning the sentence the caller agrees to."""
    reconcile: str = ''
    """`file.py:find`, `(services, args[, intent]) -> receipt | None`: what happened to an intent whose outcome is unknown."""
    say: str | Closing = ''
    """The receipt's speech, formatted from its fields: `Booked, reference {reference}.`

    Or lines by result (`core.closing`): a list of `{when: {field: value}, line: ...}` cases, tried in order,
    with `when: refused` for a refusal said by code and a case with no `when` for any other success.
    """
    recap: str | Closing = ''
    """What a goodbye may say about it later, formatted from the receipt: `You're booked for {spoken}.`

    Said only when the model points the goodbye at this receipt and the
    receipt still stands. Empty: a goodbye cannot recap it.
    """
    changes: bool = True
    """Whether it changes a business resource. `false` for a lookup: its receipt never backs a claim that something changed."""
    subject: str = ''
    """The receipt field naming the resource it acts on (`booking_id`): a later change to it supersedes this one."""
    resource: str = ''
    """The kind of resource `subject` names (`ticket`, `address`). Ids are unique only within a kind, so ticket 1
    and address 1 are two resources, and every operation on one kind (resolve and reopen) shares it. Default: the
    `subject` field's name."""
    version: str = ''
    """The receipt field holding the resource's version after the change (`now_revision`). Required with `subject`
    on a change: it, not the order answers arrive in, says which of two changes stands."""
    idempotent: bool = False
    """The backend dedups by the intent id (the function's `Intent` parameter), so an unknown intent may be sent again."""
    route: str = ''
    """The receipt field naming the step the caller moves to once this succeeds: code's move, not a model's.

    Its type must be a `Literal` of step ids, each one the running step
    itself (stay) or a step the manifest declares, so the graph knows every
    move before a call runs. The application's receipt says which, from its
    own records (where a returning learner left off); no model chooses it.
    """


class ClassOperationSpec(BaseModel):
    """A class operation (`core.Lookup`, `core.Write`) with its closing lines set here rather than on the class.

        book_appointment: {class: operations.py:BookAppointment, say: "You're all set for {spoken}."}

    `say` and `recap` replace the class's own when given, so a studio can change what is said after an
    operation works without touching its code; everything else is still read from the class.
    """

    model_config = SPEC
    class_: str = Field(alias='class')
    """`file.py:Class`, as the one-line form names it."""
    say: str | Closing | None = None
    recap: str | Closing | None = None


def _class_ref(entry: object) -> str | None:
    """The class a declared operation names, in either of its forms; None for a long-form function."""
    if isinstance(entry, str):
        return entry
    return entry.class_ if isinstance(entry, ClassOperationSpec) else None


def _closing(entry: object) -> dict[str, str | Closing]:
    """The closing lines a class operation's manifest entry sets, over the class's own."""
    if not isinstance(entry, ClassOperationSpec):
        return {}
    return {key: value for key, value in (('say', entry.say), ('recap', entry.recap)) if value is not None}


class PersonaSpec(BaseModel):
    """A reusable agent: shared instructions, and the SDK's own agent spec for its model and settings."""

    model_config = SPEC
    prompt: str
    spec: NativeSpec = NativeSpec()
    """Pydantic AI's native `AgentSpec` (model, model_settings, retries...), never retyped here. Its instructions are `prompt`."""


LOCALES: dict[str, Locale] = {'english': ENGLISH, 'spanish': SPANISH}


class LocaleSpec(BaseModel):
    """A language for the engine's fixed lines, with the application's own words where its domain decides them.

    `goodbye` replaces the language's closing line; `refusals` word a
    refused value for the caller, by its validator's error type.
    """

    model_config = SPEC
    base: Literal['english', 'spanish']
    goodbye: str = ''
    refusals: dict[str, str] = {}
    """The caller's words for a refused value, by its validator's error type, over the language's own (`Locale.refusals`)."""

    def build(self) -> Locale:
        base = LOCALES[self.base]
        return replace(
            base, goodbye=self.goodbye or base.goodbye, refusals={**base.refusals, **self.refusals}
        )


class CurrentDatetimeSpec(BaseModel):
    """The call's clock: `current_datetime: {enabled: true, timezone: Europe/Madrid, calendar_days: 14}`.

    Where the business counts its day, read once when a call opens. With
    `enabled`, every step's model is told today's date, weekday and time,
    and the next `calendar_days` days, each labelled ("tomorrow", "Saturday
    next week"), so it works a date out from a list instead of counting.
    `enabled: false` keeps the date out of the prompts; validators still
    read it. The locale is a separate thing: a Spanish desk in London is
    Europe/London.
    """

    model_config = SPEC
    enabled: bool = True
    timezone: str = 'UTC'
    calendar_days: int = Field(0, ge=0, le=366)

    @field_validator('timezone')
    @classmethod
    def _known(cls, name: str) -> str:
        try:
            ZoneInfo(name)
        except ZoneInfoNotFoundError, ValueError:
            raise ValueError(f'{name!r} is not a timezone (an IANA name, like Europe/Madrid)') from None
        return name

    def build(self) -> CallClock:
        return CallClock(zone=ZoneInfo(self.timezone), calendar_days=self.calendar_days, shown=self.enabled)


class FlowSpec(BaseModel):
    """A whole flow file: the application's manifest, `agent.yaml`."""

    model_config = SPEC
    version: Literal[2]
    """The manifest format's version: 2, steps keyed by id (`_normalized`)."""
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
    language: str = ''
    """The state attribute (a field or a property) holding the language a call is in, for `locales`."""
    locales: dict[str, Literal['english', 'spanish'] | LocaleSpec] = {}
    """The fixed lines for each value of `language`, chosen per call as that value changes. `locale` is the fallback."""
    agents: dict[str, PersonaSpec] = {}
    operations: dict[str, OperationSpec | ClassOperationSpec | str] = {}
    """Each business action: the long form, or `file.py:Class`, a `core.Lookup` or `core.Write` that says it all itself."""
    greeting: str = ''
    """The first thing a caller hears."""
    state: str = ''
    """The application's state model, like `models.py:ClinicState`: plain data, every field with a default."""
    identity: str = ''
    """The state field that says who the caller is. A question put to one identity is not answered by another."""
    voice: str = ''
    """A file added to the top of every step's prompt: the shared tone and persona."""
    includes: dict[str, str] = {}
    """Files substituted for `<!--name-->` markers in any prompt: shared rules, documents."""
    quote_caller: bool = False
    """End every prompt with the caller's latest words."""
    context: ContextSpec | Literal['none'] = ContextSpec()
    """What every step's agent is sent from before this caller turn, unless a step says otherwise: no facts, its own turns."""
    current_datetime: CurrentDatetimeSpec | None = None
    """The call's timezone, and whether and how far ahead each step's model is shown the date. None: UTC, today only."""
    steps: list[AnyStep]
    _root: Path | None = PrivateAttr(default=None)

    @model_validator(mode='before')
    @classmethod
    def _normalized(cls, data: Any) -> Any:
        """The steps keyed by id as one ordered steps table.

        A step's key is its id; an `id:` inside it that says otherwise is
        refused, never picked between. A step with no `kind` is a
        conversation. Steps keep the order they are written in, so `entry`
        and `then` default to the next one.
        """
        if not isinstance(data, dict) or data.get('version') != 2:
            return data
        steps = data.get('steps')
        if not isinstance(steps, dict):
            raise ValueError('steps are keyed by id (`steps: {desk: {...}}`), not listed')
        listed = []
        for key, body in steps.items():
            if not isinstance(body, dict):
                raise ValueError(f'step {key!r}: a step is a mapping of its keys')
            if body.get('id', key) != key:
                raise ValueError(f'step {key!r}: its id {body["id"]!r} differs from its key; keep one')
            listed.append({'id': key, 'kind': 'conversation', **body})
        return {**data, 'steps': listed}

    def _resolver(self, registry: Registry) -> _Resolver:
        resolve = _Resolver(self, registry)
        if self.identity:
            resolve.state_field('identity', self.identity)
        if bool(self.language) != bool(self.locales):
            resolve.fail(
                'language', 'language and locales go together: the state attribute, and a locale per value'
            )
        elif self.language and not _attribute(resolve.state_type(), self.language):
            resolve.fail('language', f'names {self.language!r}, which the state has no field or property for')
        if any(isinstance(s, ActionSpec) for s in self.steps) and not self.state:
            resolve.fail('', 'a flow with action steps needs a state model (`state:`)')
        ids = [step.id for step in self.steps]
        for repeated in sorted(i for i, n in Counter(ids).items() if n > 1):
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
        for step in self.steps:
            name = getattr(step, 'agent', '')
            persona = self.agents.get(name)
            if persona is not None and persona.spec.output_schema is not None:
                resolve.fail(
                    step.id,
                    f'its agent {name!r} sets spec.output_schema, which core would replace with this '
                    "step's own outputs; leave it out",
                )
        for name in self.operations:
            resolve.declared(name)
        for step in self.steps:
            for to in step.destinations():
                if to not in known:
                    resolve.fail(step.id, f'sends the caller to {to!r}, which is not a step')
            step.check(resolve)
        spoken = {n for s in self.steps if isinstance(s, ConversationSpec) for n in s.fields}
        for name, fields in resolve.assigned.items():
            for field_name in sorted(fields & spoken):
                resolve.fail(
                    f'operation {name}',
                    f'assigns {field_name!r}, which the caller gives; a receipt may not overwrite it',
                )
        self._unguarded_actions(resolve)
        return resolve

    def problems(self, registry: Registry | None = None) -> list[str]:
        """Every reason this flow cannot run. Empty means it can."""
        return self._resolver(registry or Registry()).problems

    def _unguarded_actions(self, resolve: _Resolver) -> None:
        """An action step runs its operation with no model, so it may not run one that needs a read-back and a yes.

        That operation belongs on a conversation step, which reads it back and
        carries it out only on a heard yes.
        """
        for action in [s for s in self.steps if isinstance(s, ActionSpec)]:
            found = resolve._cache.get(('operation', action.uses))
            operation_type = found[0] if found else None
            if isinstance(operation_type, type) and getattr(operation_type, 'needs_approval', True):
                resolve.fail(
                    action.id,
                    f'runs {action.uses!r}, which needs a read-back and a yes: carry it out on a conversation step',
                )

    def prompt(self, step_id: str, registry: Registry | None = None) -> TemplateStr:
        """One step's compiled prompt, exactly as its agent receives it."""
        step = next((s for s in self.steps if s.id == step_id and isinstance(s, ConversationSpec)), None)
        if step is None:
            raise FlowSpecError([f'{step_id!r} is not a step with a prompt'])
        resolve = _Resolver(self, registry or Registry())
        compiled = resolve.prompt(step_id, step.prompt, step.agent, step.seen(resolve))
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
        return StepFlow(
            [step.to_step(resolve) for step in self.steps],
            model=model,
            settings=settings,
            state_type=state,
            identity=self.identity,
            entry=self.entry,
            services=resolve.function('services', self.services, 0) if self.services else None,
            locale=_built(self.locale) if self.locale else None,
            locales={value: _built(spec) for value, spec in self.locales.items()},
            language=self.language,
            version=self.app_version,
            clock=self.current_datetime.build() if self.current_datetime is not None else None,
        )


def _attribute(state: type[BaseModel], name: str) -> bool:
    """Whether the state model has this field, or a property of that name."""
    return name in state.model_fields or isinstance(getattr(state, name, None), property)


def _built(spec: str | LocaleSpec) -> Locale:
    """A manifest locale: a language's own lines, or those lines with the application's words for its domain."""
    return spec.build() if isinstance(spec, LocaleSpec) else LOCALES[spec]


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


def _members(returned: Any) -> tuple[list[type[BaseModel]], str]:
    """The receipt models a class operation can return, and the union's discriminator: `([Saved, Declined], 'status')`."""
    tag = ''
    kind = returned
    if get_origin(kind) is Annotated:
        kind, *meta = get_args(kind)
        tag = next(
            (str(m.discriminator) for m in meta if isinstance(getattr(m, 'discriminator', None), str)), ''
        )
    options = get_args(kind) if get_origin(kind) in (typing.Union, type(int | None)) else (kind,)
    members = [m for m in options if m is not type(None)]
    return ([m for m in members if _model(m)] if all(_model(m) for m in members) else []), tag


def _tags(member: type[BaseModel], tag: str) -> list[Any]:
    """The discriminator values one union member answers with: its `Literal`'s values."""
    info = member.model_fields.get(tag)
    kind = _bare(info.annotation) if info is not None else None
    return list(get_args(kind)) if get_origin(kind) is Literal else []


def _marked(won: list[type[BaseModel]]) -> tuple[str, str, str]:
    """The success receipts' `Subject` field, its resource kind, and their `Version` field: '' where unmarked."""
    marks = [
        (name, mark) for member in won for name, info in member.model_fields.items() for mark in info.metadata
    ]
    subject, kind = next(
        ((name, mark.kind or name) for name, mark in marks if isinstance(mark, Subject)), ('', '')
    )
    version = next((name for name, mark in marks if isinstance(mark, Version)), '')
    return subject, kind, version


def _method(action: type[Action], method: str) -> Callable[..., Any]:
    """`(services, args, intent)` calling `args.<method>(services[, intent])`: a coroutine when the method is one."""
    bound = getattr(action, method)
    takes_intent = method != 'readback'
    if inspect.iscoroutinefunction(bound):

        async def run_async(services: Any, args: Any, intent: Intent | None = None) -> Any:
            return await (
                getattr(args, method)(services, intent) if takes_intent else getattr(args, method)(services)
            )

        return run_async

    def run(services: Any, args: Any, intent: Intent | None = None) -> Any:
        return getattr(args, method)(services, intent) if takes_intent else getattr(args, method)(services)

    return run


class _Manifest(yaml.SafeLoader):
    """`yaml.safe_load`'s loader, refusing a key given twice in one mapping: plain YAML keeps the last, silently."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            if key_node.tag == 'tag:yaml.org,2002:merge':
                continue
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, Hashable):
                continue
            if key in seen:
                raise FlowSpecError(
                    [f'line {key_node.start_mark.line + 1}: {key!r} is given twice in one mapping']
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _by_step(error: ValidationError, steps: Any) -> list[str]:
    """A manifest's validation errors, each named by its step's key rather than a list index."""
    names = list(steps) if isinstance(steps, dict) else []
    found = []
    for each in error.errors():
        loc, said = list(each['loc']), each['msg'].removeprefix('Value error, ')
        if loc[:1] == ['steps'] and len(loc) > 1 and isinstance(loc[1], int) and loc[1] < len(names):
            name = names[loc[1]]
            rest = loc[3:] if len(loc) > 2 and loc[2] == steps[name].get('kind', 'conversation') else loc[2:]
            where = '.'.join(str(part) for part in rest)
            found.append(f'step {name!r}: {where}: {said}' if where else f'step {name!r}: {said}')
        else:
            where = '.'.join(str(part) for part in loc)
            found.append(f'{where}: {said}' if where else said)
    return found


def load(text: str, root: str | Path | None = None) -> FlowSpec:
    """Read a flow file's text, YAML or JSON; `root` is its folder. Unknown kinds and fields fail here.

    A repeated key fails. Every problem is in one `FlowSpecError`, each named
    by its step.
    """
    data = yaml.load(text, Loader=_Manifest)
    if not isinstance(data, dict):
        raise FlowSpecError(['a flow file must be a mapping with version, id and steps'])
    try:
        spec = FlowSpec.model_validate(data)
    except ValidationError as error:
        raise FlowSpecError(_by_step(error, data.get('steps'))) from None
    spec._root = Path(root) if root is not None else None
    return spec


def load_file(path: str | Path) -> FlowSpec:
    """Read a flow file from disk; the files it names are looked up in the same folder."""
    path = Path(path)
    return load(path.read_text(), root=path.parent)
