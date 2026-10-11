"""Every fact an agent keeps track of, read in the check's own process: its kind in words, its rules, and where it is used.

The facts are the fields of the application's state model. This module imports the model (it runs inside
`studio.agents.check`, in a process of its own) and reads each field's real annotation, so a kind is named
from the type itself, never guessed from its name: `core.Phone` is a phone number, `core.Quoted` the
caller's own words, `Annotated[date, core.NotPast]` a date not in the past, a nested model a group of
details, a union "a time, or one of: morning, afternoon, evening".

Where a fact is used comes from the manifest and the compiled flow, as the engine reads them:

    fills         a conversation step's `fields:`               (the model may record it there)
    sees          a step's facts, its own `context.facts` or the flow's (`listed` when a list names it, which
                  the manifest then holds: a use that stops it being removed)
    asks          a step's `asks:`, the words code asks for it with
    needed        a step's `requires:`: the step waits for it, filled in by `satisfied_by`
    identity      the manifest's `identity:`: who the caller is
    offered       a step's `choices:`: a choice function offers its values
    rests_on      a choice's `after:` names it: a change to it takes the offer away
    read_back     a step's `reads_back:`, an operation read back by code when it is recorded
    carried_out   a step's `carries_out:`, an operation code runs when it is recorded
    input         an operation takes it as an argument (`binds`, or a path inside it)
    set_by        an operation's receipt sets it (`assign`)
    prompt        a config text file names `state.facts.<it>`
    code          another module of the package names it (an attribute, a keyword or a string): the
                  studio cannot prove such code does not read it, so the fact cannot be removed here

A fact is grouped by who fills it in: the caller (some step records it), tools (only a receipt sets it),
or the application when the call opens (nothing in the manifest does).
"""

import ast
import datetime as dt
import inspect
import re
import typing
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import Any

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

import core
from core import values
from core.flowspec import ContextSpec, ConversationSpec, LocaleSpec
from studio.agents.state_source import FieldSource, SourceRefusedError, StateImports, StateSource


class Words:
    """Field names in plain words: `booking_day` is "Booking day"."""

    @staticmethod
    def label(name: str) -> str:
        """A field name as a person reads it."""
        spaced = name.replace('_', ' ').strip()
        return spaced[:1].upper() + spaced[1:]


class Kind:
    """One annotation, read into what a person needs to know: what kind of value, its choices, its rules.

    `code` names the kind (`text`, `whole_number`, `number`, `yes_no`, `date`, `not_past`, `time`,
    `date_time`, `choice`, `phone`, `quoted`, `group`, `list`, `union`, `other`); `words` says it.
    """

    WORDS: typing.ClassVar[dict[str, tuple[str, str]]] = {
        'text': ('Text', 'some text'),
        'whole_number': ('Whole number', 'a whole number'),
        'number': ('Number', 'a number'),
        'yes_no': ('Yes/No', 'yes or no'),
        'date': ('Date', 'a date'),
        'not_past': ('A date not in the past', 'a date not in the past'),
        'time': ('Time', 'a time'),
        'date_time': ('Date and time', 'a date and time'),
        'choice': ('One of a list', 'one of a list'),
        'phone': ('Phone number', 'a phone number'),
        'quoted': ("The caller's own words", "the caller's own words"),
        'group': ('A group of details', 'a group of details'),
        'other': ('Something else', 'something else'),
    }
    DEPTH = 3

    def __init__(self, annotation: Any, folder: Path, depth: int = 0) -> None:
        self.folder = folder
        self.depth = depth
        self.rules: list[dict[str, Any]] = []
        self.limits: dict[str, Any] = {}
        self.not_past = False
        self.quoted = False
        self.nullable = False
        self.annotation = self._unwrap(annotation)

    def _unwrap(self, annotation: Any) -> Any:
        """The annotation with `Annotated` taken off, its validators and limits kept."""
        while typing.get_origin(annotation) is typing.Annotated:
            annotation, *metadata = typing.get_args(annotation)
            for item in metadata:
                self._metadata(item)
        return annotation

    def _metadata(self, item: Any) -> None:
        """One `Annotated` item: a core value type's validator, a limit, or a rule the application wrote."""
        func = getattr(item, 'func', None)
        if func is getattr(values, '_not_past', None):
            self.not_past = True
        elif func is getattr(values, '_quoted', None):
            self.quoted = True
        elif func is not None:
            self.rules.append(Rule(func, self.folder).report())
        elif found := Limits.of(item):
            self.limits |= found
        elif isinstance(item, FieldInfo):
            for each in item.metadata:
                self._metadata(each)

    def members(self) -> list[Kind]:
        """A union's kinds, `None` left out (and noted); any other kind is its own one member."""
        if typing.get_origin(self.annotation) in (typing.Union, UnionType):
            args = typing.get_args(self.annotation)
            self.nullable = self.nullable or type(None) in args
            found = [Kind(arg, self.folder, self.depth) for arg in args if arg is not type(None)]
            for member in found:
                self.rules += member.rules
                self.limits |= member.limits
            return found
        return [self]

    def report(self) -> dict[str, Any]:
        """The kind, as the Facts page shows it."""
        members = self.members()
        if len(members) > 1:
            choices = [value for member in members if member.code() == 'choice' for value in member.choices()]
            if all(member.code() == 'choice' for member in members):
                return self._shape('choice', choices=choices)
            parts = [member.report() for member in members]
            spoken = [
                part['article']
                if part['code'] != 'choice'
                else 'one of: ' + ', '.join(map(str, part['choices']))
                for part in parts
            ]
            words = ', or '.join(spoken)
            return {
                'code': 'union',
                'words': words[:1].upper() + words[1:],
                'article': words,
                'choices': choices,
                'nullable': self.nullable,
                'members': parts,
                'fields': [],
                'item': None,
                'defined_in': '',
            }
        only = members[0]
        report = only._single()
        report['nullable'] = self.nullable
        return report

    def code(self) -> str:
        """The kind's code, for one member."""
        return self._single()['code']

    def choices(self) -> list[Any]:
        """The values a closed kind can hold."""
        return self._single().get('choices', [])

    def _single(self) -> dict[str, Any]:
        """One kind that is not a union."""
        kind = self.annotation
        origin = typing.get_origin(kind)
        if self.quoted:
            return self._shape('quoted')
        if origin is typing.Literal:
            return self._shape('choice', choices=list(typing.get_args(kind)))
        if isinstance(kind, type) and issubclass(kind, Enum):
            return self._shape(
                'choice', choices=[member.value for member in kind], defined_in=self._where(kind)
            )
        if kind is bool:
            return self._shape('yes_no')
        if isinstance(kind, type) and issubclass(kind, core.Phone):
            return self._shape('phone')
        if kind is int:
            return self._shape('whole_number')
        if kind in (float, Decimal):
            return self._shape('number')
        if kind is str:
            return self._shape('text')
        if kind is dt.datetime:
            return self._shape('date_time')
        if kind is dt.date:
            return self._shape('not_past' if self.not_past else 'date')
        if kind is dt.time:
            return self._shape('time')
        if isinstance(kind, type) and issubclass(kind, BaseModel):
            inner = (
                [
                    Fact.nested(name, info, self.folder, self.depth + 1)
                    for name, info in kind.model_fields.items()
                ]
                if self.depth < self.DEPTH
                else []
            )
            return self._shape('group', fields=inner, defined_in=self._where(kind), model=kind.__name__)
        if origin in (list, tuple, set, frozenset):
            args = [arg for arg in typing.get_args(kind) if arg is not Ellipsis]
            item = Kind(args[0], self.folder, self.depth + 1).report() if args else None
            what = item['article'] if item else 'anything'
            return self._shape(
                'list', item=item, words=f'A list, each one {what}', article=f'a list, each one {what}'
            )
        return self._shape('other', words=f'Something else ({getattr(kind, "__name__", kind)!s})')

    def _shape(self, code: str, **more: Any) -> dict[str, Any]:
        """A kind report with its words filled in."""
        title, article = self.WORDS.get(code, ('', ''))
        return {
            'code': code,
            'words': title,
            'article': article,
            'choices': [],
            'nullable': False,
            'members': [],
            'fields': [],
            'item': None,
            'defined_in': '',
        } | more

    def _where(self, kind: type) -> str:
        """The file a type is defined in, relative to the agent's package folder when it is inside it."""
        try:
            path = Path(inspect.getfile(kind)).resolve()
        except TypeError:
            return ''
        return (
            str(path.relative_to(self.folder.parent))
            if path.is_relative_to(self.folder.parent)
            else path.name
        )


class Limits:
    """Limits written on a field (`annotated_types`' metadata, as Pydantic keeps it), by the `Field` keyword that writes each."""

    NAMES: typing.ClassVar[dict[str, str]] = {
        'MinLen': 'min_length',
        'MaxLen': 'max_length',
        'Ge': 'ge',
        'Le': 'le',
        'Gt': 'gt',
        'Lt': 'lt',
        'MultipleOf': 'multiple_of',
    }

    @classmethod
    def of(cls, item: Any) -> dict[str, Any]:
        """One limit as `{keyword: value}`; nothing for metadata that is no limit."""
        key = cls.NAMES.get(type(item).__name__)
        return {key: getattr(item, key)} if key and hasattr(item, key) else {}


class Rule:
    """A validator the application wrote: its name, where it is, and what its errors say."""

    def __init__(self, func: Any, folder: Path) -> None:
        self.func = getattr(func, '__func__', func)
        self.folder = folder

    def report(self) -> dict[str, Any]:
        """The rule as the Facts page shows it, read-only."""
        name = getattr(self.func, '__name__', str(self.func))
        try:
            file = Path(inspect.getsourcefile(self.func) or '').resolve()
            line = inspect.getsourcelines(self.func)[1]
            source = inspect.getsource(self.func)
        except OSError, TypeError:
            return {'name': name, 'where': '', 'says': [], 'types': []}
        shown = (
            str(file.relative_to(self.folder.parent))
            if file.is_relative_to(self.folder.parent)
            else file.name
        )
        says, types = self.errors(source)
        return {'name': name, 'where': f'{shown} line {line}', 'says': says, 'types': types}

    @staticmethod
    def errors(source: str) -> tuple[list[str], list[str]]:
        """The messages its raised errors carry, and the error types a `PydanticCustomError` names."""
        try:
            tree = ast.parse(inspect.cleandoc(source) if source.startswith((' ', '\t')) else source)
        except SyntaxError:
            return [], []
        says: list[str] = []
        types: list[str] = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)):
                continue
            called = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            texts = [Rule.text(arg) for arg in node.args]
            if called == 'PydanticCustomError' and len(texts) >= 2:
                types += [texts[0]] if texts[0] else []
                says += [texts[1]] if texts[1] else []
            elif called.endswith(('Error', 'Exception')) and texts and texts[0]:
                says.append(texts[0])
        return says, types

    @staticmethod
    def text(node: ast.expr) -> str:
        """A message argument as text: a string as it reads, an f-string with its holes in braces."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return ''.join(
                str(part.value) if isinstance(part, ast.Constant) else '{' + ast.unparse(part.value) + '}'
                for part in node.values
                if isinstance(part, ast.Constant | ast.FormattedValue)
            )
        return ''


class Fact:
    """One field of the state, top-level or inside a group, as the Facts page shows it."""

    @staticmethod
    def nested(name: str, info: FieldInfo, folder: Path, depth: int) -> dict[str, Any]:
        """A field inside a group of details: shown, never edited here."""
        kind = Kind(info.annotation, folder, depth)
        for item in info.metadata:
            kind._metadata(item)
        return {
            'name': name,
            'label': Words.label(name),
            'description': info.description or '',
            'kind': kind.report(),
        }

    @staticmethod
    def start(info: FieldInfo) -> tuple[bool, Any]:
        """Whether it starts empty, and the value it starts with otherwise."""
        if info.default_factory is not None:
            return False, '(set by code when the call opens)'
        if info.default is PydanticUndefined:
            return False, '(none: a call cannot start without it)'
        if info.default is None:
            return True, None
        return False, info.default.isoformat() if hasattr(info.default, 'isoformat') else info.default


class Usage:
    """Where every fact is used, from the manifest and the compiled flow, by fact name."""

    PLACEHOLDER = re.compile(r'state\.facts\.([A-Za-z_][A-Za-z0-9_]*)')
    TEXT = frozenset({'.yaml', '.yml', '.md', '.json', '.txt', '.hbs'})

    def __init__(self, spec: core.FlowSpec, flow: Any, manifest: Path, state_file: Path | None) -> None:
        self.spec = spec
        self.flow = flow
        self.manifest = manifest
        self.state_file = state_file
        self.names = list(flow.state_type.model_fields)
        self.found: dict[str, list[dict[str, Any]]] = {name: [] for name in self.names}

    def add(self, name: str, kind: str, **where: Any) -> None:
        """One use of one fact; a name that is not a fact is left out (a path, a typo the loader reports)."""
        if name in self.found:
            use = {'kind': kind} | where
            if use not in self.found[name]:
                self.found[name].append(use)

    def read(self) -> dict[str, list[dict[str, Any]]]:
        """Every use, by fact."""
        flow_context = self.spec.context if isinstance(self.spec.context, ContextSpec) else None
        for step in self.spec.steps:
            if isinstance(step, ConversationSpec):
                self._step(step, flow_context)
        if self.spec.identity:
            self.add(self.spec.identity, 'identity')
        for operation in self.flow.operations():
            for argument, name in (getattr(operation, 'binds', None) or {}).items():
                self.add(name, 'input', operation=operation.name, argument=argument)
            for argument, path in (getattr(operation, 'paths', None) or {}).items():
                if path:
                    self.add(
                        path[0], 'input', operation=operation.name, argument=argument, path='.'.join(path)
                    )
            for name, source in (getattr(operation, 'assign', None) or {}).items():
                self.add(
                    name,
                    'set_by',
                    operation=operation.name,
                    detail='its whole result' if source == '*' else source,
                )
        self._prompts()
        self._code()
        return self.found

    def _step(self, step: ConversationSpec, flow_context: ContextSpec | None) -> None:
        """One conversation step's uses of facts."""
        for name in step.fields:
            self.add(name, 'fills', step=step.id)
        context = (
            ContextSpec(history='none', facts='none')
            if step.context == 'none'
            else step.context.under(flow_context)
            if isinstance(step.context, ContextSpec)
            else flow_context or ContextSpec()
        )
        shown = context.shown()
        for name in self.names if shown is None else shown:
            self.add(name, 'sees', step=step.id, detail='' if shown is None else 'listed')
        for name, words in step.asks.items():
            self.add(name, 'asks', step=step.id, detail=words)
        for name in step.requires:
            self.add(name, 'needed', step=step.id, detail=step.satisfied_by)
        for name, choice in step.choices.items():
            function = choice if isinstance(choice, str) else choice.function
            self.add(name, 'offered', step=step.id, function=function)
            after = (
                [other for other in self.names if other != name] if isinstance(choice, str) else choice.after
            )
            for rests in after:
                self.add(rests, 'rests_on', step=step.id, function=function, choice=name)
        for operation, names in step.reads_back.items():
            for name in names:
                self.add(name, 'read_back', step=step.id, operation=operation)
        for operation, names in step.carries_out.items():
            for name in names:
                self.add(name, 'carried_out', step=step.id, operation=operation)

    def _prompts(self) -> None:
        """Every config text file that names `state.facts.<fact>`."""
        folder = self.manifest.parent
        for path in sorted(folder.rglob('*')):
            if path.is_file() and path.suffix in self.TEXT and '__pycache__' not in path.parts:
                for line, text in enumerate(path.read_text(errors='replace').splitlines(), 1):
                    for name in self.PLACEHOLDER.findall(text):
                        self.add(name, 'prompt', file=str(path.relative_to(folder)), line=line)

    def _code(self) -> None:
        """Every other module of the package that names a fact: an attribute, a keyword, or a string."""
        folder = self.manifest.parent
        for path in sorted(folder.rglob('*.py')):
            if '__pycache__' in path.parts or (
                self.state_file is not None and path.resolve() == self.state_file.resolve()
            ):
                continue
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError, UnicodeDecodeError:
                continue
            for node in ast.walk(tree):
                named = (
                    node.attr
                    if isinstance(node, ast.Attribute)
                    else node.arg
                    if isinstance(node, ast.keyword)
                    else node.value
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                    else None
                )
                if isinstance(named, str) and named in self.found:
                    self.add(
                        named, 'code', file=str(path.relative_to(folder)), line=getattr(node, 'lineno', 0)
                    )


class FactsReport:
    """Every fact of a compiled flow's state, with its kind, rules, uses and what the studio may edit of it."""

    def __init__(self, spec: core.FlowSpec, flow: Any, manifest: Path) -> None:
        self.spec = spec
        self.flow = flow
        self.manifest = manifest
        self.state = flow.state_type
        path, _, self.class_name = (spec.state or '').partition(':')
        self.state_file = (manifest.parent / path) if path else None
        self.source: StateSource | None = None
        if self.state_file is not None and self.state_file.exists():
            try:
                self.source = StateSource(self.state_file.read_text(), self.class_name)
            except SourceRefusedError:
                self.source = None

    def report(self) -> dict[str, Any]:
        """The facts, the state's own rules, where it is defined, and its JSON schema."""
        uses = Usage(self.spec, self.flow, self.manifest, self.state_file).read()
        refusals = self._refusals()
        validators = self._validators()
        facts = [
            self._fact(name, info, uses.get(name, []), validators.get(name, []), refusals)
            for name, info in self.state.model_fields.items()
        ]
        return {
            'file': str(self.state_file.relative_to(self.manifest.parent.parent)) if self.state_file else '',
            'class_name': self.class_name or self.state.__name__,
            'rules': validators.get('', []),
            'facts': facts,
            'schema': self.state.model_json_schema(),
            'steps': [step.id for step in self.spec.steps if isinstance(step, ConversationSpec)],
        }

    def _refusals(self) -> dict[str, str]:
        """The words the caller hears for a refused value, by its error type (`locale.refusals`)."""
        locales = [self.spec.locale, *self.spec.locales.values()]
        return {
            key: words
            for locale in locales
            if isinstance(locale, LocaleSpec)
            for key, words in locale.refusals.items()
        }

    def _validators(self) -> dict[str, list[dict[str, Any]]]:
        """The state class's own `field_validator`s by field, and its `model_validator`s under ''."""
        folder = self.manifest.parent
        decorators = self.state.__pydantic_decorators__
        found: dict[str, list[dict[str, Any]]] = {}
        for each in decorators.field_validators.values():
            for name in each.info.fields:
                found.setdefault(name, []).append(Rule(each.func, folder).report())
        for each in decorators.model_validators.values():
            found.setdefault('', []).append(Rule(each.func, folder).report())
        return found

    def _fact(
        self,
        name: str,
        info: FieldInfo,
        uses: list[dict[str, Any]],
        validators: list[dict[str, Any]],
        refusals: dict[str, str],
    ) -> dict[str, Any]:
        """One top-level fact, whole."""
        folder = self.manifest.parent
        kind = Kind(info.annotation, folder)
        for item in info.metadata:
            kind._metadata(item)
        report = kind.report()
        rules = [
            rule | {'heard': [refusals[t] for t in rule['types'] if t in refusals]}
            for rule in kind.rules + validators
        ]
        empty, start = Fact.start(info)
        field = self.source.fields.get(name) if self.source is not None else None
        kinds = {use['kind'] for use in uses}
        group = 'caller' if 'fills' in kinds else 'tools' if 'set_by' in kinds else 'open'
        editable, why = self._editable(name, field, report, kind, rules, uses)
        return {
            'name': name,
            'label': Words.label(name),
            'group': group,
            'description': info.description or '',
            'note': field.note_text if field is not None else '',
            'line': field.start if field is not None else 0,
            'kind': report,
            'starts_empty': empty,
            'start': start,
            'limits': kind.limits,
            'rules': rules,
            'uses': uses,
            'editable': editable,
            'why_not': why,
            'spec': self._spec(name, info, field, report, kind, editable),
        }

    def _editable(
        self,
        name: str,
        field: FieldSource | None,
        kind: dict[str, Any],
        read: Kind,
        rules: list[dict[str, Any]],
        uses: list[dict[str, Any]],
    ) -> tuple[dict[str, bool], dict[str, str]]:
        """What of this fact the studio can change and prove, and for the rest, why not."""
        where = str(self.state_file.name) if self.state_file else 'the state module'
        why: dict[str, str] = {}
        if field is None:
            text = f'{name} is not written as `{name}: kind = value` in {where}: edit it in code'
            return dict.fromkeys(
                ('description', 'choices', 'limits', 'empty', 'remove'), False
            ), dict.fromkeys(('description', 'choices', 'limits', 'empty', 'remove'), text)
        form_ok = field.form() in ('none', 'field')
        if not form_ok:
            why['description'] = (
                f'its value is not written as `= None` or `= Field(...)`: edit it in code ({where} line {field.start})'
            )
        plain = field.plain()
        literal = field.literal()
        if kind['code'] in ('choice', 'union') and kind['choices'] and literal is None:
            alias = ast.unparse(field.members()[0])
            defined = kind.get('defined_in') or self._defined(alias)
            why['choices'] = (
                f'these choices are {alias}, defined in {defined or "code"}: edit them in code'
                if kind['code'] == 'choice'
                else f'{alias} is a kind made of two kinds, defined in code: edit it in code'
            )
        elif kind['code'] != 'choice':
            why['choices'] = 'it is not one of a list'
        if not (plain in StateSource.LIMITS and form_ok):
            why['limits'] = 'limits are set here only on plain text and plain numbers'
        if rules:
            why.setdefault('limits', 'it has a rule in code: change its limits there')
        if not ((plain in ('str', 'int', 'float', 'bool') or literal is not None) and form_ok):
            why['empty'] = (
                'whether it may be empty is set here only on plain text, numbers, yes/no and inline lists'
            )
        code = [use for use in uses if use['kind'] == 'code']
        others = [use for use in uses if use['kind'] != 'sees' or use.get('detail') == 'listed']
        if others or code:
            why['remove'] = 'it is used: take it out of each use listed first'
        editable = {key: key not in why for key in ('description', 'choices', 'limits', 'empty', 'remove')}
        return editable, why

    def _defined(self, alias: str) -> str:
        """The file a name used in the state module comes from: the module itself, or the one it imports it from."""
        if self.source is None or self.state_file is None:
            return ''
        module, _ = StateImports(self.source).bound.get(alias.split('.')[0], ('?', None))
        if module == '':
            return self.state_file.name
        found = self.manifest.parent.parent / (module.replace('.', '/') + '.py')
        return str(found.relative_to(self.manifest.parent.parent)) if found.exists() else module

    @staticmethod
    def _spec(
        name: str,
        info: FieldInfo,
        field: FieldSource | None,
        kind: dict[str, Any],
        read: Kind,
        editable: dict[str, bool],
    ) -> dict[str, Any] | None:
        """The fact as the studio could write it from scratch, when it can: what the proof rebuilds its schema from."""
        if field is None or not (editable['empty'] or editable['choices']) or read.rules:
            return None
        if kind['code'] not in ('text', 'whole_number', 'number', 'yes_no', 'choice'):
            return None
        empty, start = Fact.start(info)
        return {
            'kind': kind['code'],
            'choices': kind['choices'],
            'may_be_empty': kind['nullable'],
            'start': None if empty else start,
            'description': info.description or '',
            'limits': {
                key: value
                for key, value in read.limits.items()
                if key in ('min_length', 'max_length', 'ge', 'le')
            },
        }
