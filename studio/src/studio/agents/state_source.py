"""An agent's facts as Python source: `state.py` read with `ast`, and the same text with one fact added, changed or removed.

The facts are the fields of the application's state model, a Pydantic class (`state: state.py:SalonState`).
The studio never imports it to edit it: it finds the class with `ast`, and each edit replaces the exact
text span of one field's statement (`name: kind = Field(...)`), adds one statement at the end of the class,
or adds a name to an import. Every other line, comment and docstring stays as it was.

A field the editor rewrites is written as `ruff format` would write it at 110 columns: on one line when it
fits, else its arguments on one indented line, else one argument a line with a trailing comma. Arguments it
does not change keep their own source text (a value written over several lines is written as `ast.unparse`
reads it: the same value).

What can be written, and from what:

    a description         the `description=` keyword of `Field(...)`; a plain `= None` becomes `Field(None, ...)`
    choices               an inline `Literal[...]`, never a name defined elsewhere
    limits                `min_length`/`max_length` on text, `ge`/`le` on a number
    may be empty          `X | None = None`, or `X = <a starting value>` (a call needs a default for every field)
    a new fact            one of `NewFact.KINDS`, empty when a call opens
    removing a fact       its statement, and the note (an attribute docstring) right after it

Anything else is refused (`SourceRefusedError`), with what to edit in code instead. `SourceProof` then shows
that the new text differs from the old in that one fact (and the imports it needs) and nothing else.
"""

import ast
import keyword
import re
import sys
from collections.abc import Sequence
from typing import Any, ClassVar

WIDTH = 110


class SourceRefusedError(ValueError):
    """An edit the studio could not make to the source and prove; it is made in code instead."""


class FieldSource:
    """One field of the state class as written: its statement, its kind, its value and the note after it."""

    def __init__(self, text: str, node: ast.AnnAssign, note: ast.Expr | None) -> None:
        self.text = text
        self.node = node
        self.note = note
        assert isinstance(node.target, ast.Name)
        self.name = node.target.id
        self.annotation = node.annotation
        self.value = node.value

    @property
    def start(self) -> int:
        """Its first line, from 1."""
        return self.node.lineno

    @property
    def end(self) -> int:
        """Its last line, the note's included."""
        return (self.note or self.node).end_lineno or self.node.lineno

    @property
    def note_text(self) -> str:
        """The attribute docstring after it: a note for developers, which the model never reads."""
        value = (
            self.note.value.value
            if self.note is not None and isinstance(self.note.value, ast.Constant)
            else ''
        )
        return value if isinstance(value, str) else ''

    def source(self, node: ast.AST) -> str:
        """A node's own text when it is on one line, else the same value as `ast.unparse` writes it."""
        found = ast.get_source_segment(self.text, node) or ast.unparse(node)
        return ast.unparse(node) if '\n' in found else found

    def form(self) -> str:
        """`none` for `= None`, `field` for `= Field(...)`, `other` for anything else (no value at all included)."""
        if isinstance(self.value, ast.Constant) and self.value.value is None:
            return 'none'
        if isinstance(self.value, ast.Call) and self._called(self.value) == 'Field':
            return 'field'
        return 'other'

    @staticmethod
    def _called(call: ast.Call) -> str:
        """The name a call is made by: `Field` for `Field(...)` and `pydantic.Field(...)`."""
        func = call.func
        return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ''

    def members(self) -> tuple[ast.expr, bool]:
        """The annotation without its `None`, and whether `None` was one of its members."""
        node = self.annotation
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            sides = [node.left, node.right]
            rest = [side for side in sides if not (isinstance(side, ast.Constant) and side.value is None)]
            if len(rest) == 1:
                return rest[0], True
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == 'Optional'
        ):
            return node.slice, True
        return node, False

    def literal(self) -> list[Any] | None:
        """The values of an inline `Literal[...]` (with or without `| None`); None for any other kind."""
        base, _ = self.members()
        if not (
            isinstance(base, ast.Subscript)
            and isinstance(base.value, ast.Name)
            and base.value.id == 'Literal'
        ):
            return None
        items = base.slice.elts if isinstance(base.slice, ast.Tuple) else [base.slice]
        if not all(isinstance(item, ast.Constant) for item in items):
            return None
        return [item.value for item in items if isinstance(item, ast.Constant)]

    def plain(self) -> str:
        """The builtin or `datetime` type a plain field holds (`str`, `int`, `bool`...), or '' for any other."""
        base, _ = self.members()
        return base.id if isinstance(base, ast.Name) and base.id in StateSource.PLAIN else ''

    def nullable(self) -> bool:
        """Whether `None` is written as one of its kinds."""
        return self.members()[1]

    def arguments(self) -> tuple[list[str], dict[str, str]]:
        """The value as `Field` arguments: its positional ones and its keywords, each as source text."""
        if self.form() == 'none':
            return ['None'], {}
        if self.form() != 'field':
            raise SourceRefusedError(
                f'{self.name} is not written as `= None` or `= Field(...)`: edit it in code'
            )
        assert isinstance(self.value, ast.Call)
        if any(kw.arg is None for kw in self.value.keywords) or any(
            isinstance(arg, ast.Starred) for arg in self.value.args
        ):
            raise SourceRefusedError(f'{self.name} unpacks its Field arguments: edit it in code')
        return (
            [self.source(arg) for arg in self.value.args],
            {kw.arg: self.source(kw.value) for kw in self.value.keywords if kw.arg is not None},
        )

    def keyword(self, name: str) -> Any:
        """A `Field` keyword's value when it is a plain constant (a description, a limit); None when it has none."""
        if self.form() != 'field':
            return None
        assert isinstance(self.value, ast.Call)
        found = next((kw.value for kw in self.value.keywords if kw.arg == name), None)
        try:
            return ast.literal_eval(found) if found is not None else None
        except ValueError:
            return None


class NewFact:
    """A fact the studio can write from scratch: its kind as Python, and the names that kind needs imported.

    `KINDS` maps each kind the add form offers to its annotation and the imports it needs, each import a
    `(module, name)`, `name` None for `import module`.
    """

    KINDS: ClassVar[dict[str, tuple[str, tuple[tuple[str, str | None], ...]]]] = {
        'text': ('str', ()),
        'whole_number': ('int', ()),
        'number': ('float', ()),
        'yes_no': ('bool', ()),
        'date': ('date', (('datetime', 'date'),)),
        'time': ('time', (('datetime', 'time'),)),
        'not_past': (
            'Annotated[date, core.NotPast]',
            (('typing', 'Annotated'), ('datetime', 'date'), ('core', None)),
        ),
        'phone': ('core.Phone', (('core', None),)),
        'quoted': ('core.Quoted', (('core', None),)),
        'choice': ('Literal[{choices}]', (('typing', 'Literal'),)),
    }

    def __init__(self, kind: str, choices: Sequence[str] = ()) -> None:
        if kind not in self.KINDS:
            raise SourceRefusedError(f'{kind!r} is not a kind the studio can add: {", ".join(self.KINDS)}')
        if kind == 'choice' and not choices:
            raise SourceRefusedError('one of a list needs at least one choice')
        self.kind = kind
        self.choices = list(choices)

    def annotation(self) -> str:
        """The kind as Python, `None` left out."""
        written, _ = self.KINDS[self.kind]
        return written.format(choices=StateSource.choices(self.choices))

    def imports(self) -> tuple[tuple[str, str | None], ...]:
        """What it needs imported, and `Field`, which every new fact is written with."""
        return (*self.KINDS[self.kind][1], ('pydantic', 'Field'))


class StateSource:
    """The text of the module that defines the state, its state class found, and the same text with one fact edited.

    `PLAIN` are the kinds a limit or a starting value can be written for. `LIMITS` are the `Field` keywords
    a limit is, by the plain kind that takes them.
    """

    PLAIN = frozenset({'str', 'int', 'float', 'bool', 'date', 'time'})
    LIMITS: ClassVar[dict[str, tuple[str, ...]]] = {
        'str': ('min_length', 'max_length'),
        'int': ('ge', 'le'),
        'float': ('ge', 'le'),
    }

    def __init__(self, text: str, class_name: str) -> None:
        self.text = text
        self.class_name = class_name
        try:
            self.tree = ast.parse(text)
        except SyntaxError as error:
            raise SourceRefusedError(
                f'the module does not read as Python: line {error.lineno}: {error.msg}'
            ) from None
        found = next(
            (n for n in self.tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None
        )
        if found is None:
            raise SourceRefusedError(f'the module defines no class {class_name} at its top level')
        self.cls = found
        self.fields = self._fields()

    def _fields(self) -> dict[str, FieldSource]:
        """Each field the class writes as `name: kind = value`, with the docstring right after it."""
        body = self.cls.body
        found: dict[str, FieldSource] = {}
        for n, node in enumerate(body):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                after = body[n + 1] if n + 1 < len(body) else None
                note = (
                    after
                    if isinstance(after, ast.Expr)
                    and isinstance(after.value, ast.Constant)
                    and isinstance(after.value.value, str)
                    else None
                )
                found[node.target.id] = FieldSource(self.text, node, note)
        return found

    def field(self, name: str) -> FieldSource:
        """A field by name, or why there is none to edit."""
        if name not in self.fields:
            raise SourceRefusedError(f'{self.class_name} has no fact {name!r} written in it')
        return self.fields[name]

    @staticmethod
    def choices(values: Sequence[Any]) -> str:
        """Choices as a `Literal`'s arguments: `'short', 'medium', 'long'`."""
        return ', '.join(repr(value) for value in values)

    def with_description(self, name: str, description: str) -> str:
        """The text with this fact's description set; '' removes it."""
        field = self.field(name)
        args, keywords = field.arguments()
        keywords = self._set(keywords, 'description', repr(description) if description else None)
        return self._replace(field, self._statement(field, self._annotation(field), args, keywords))

    def with_choices(self, name: str, choices: Sequence[str]) -> str:
        """The text with this fact's inline `Literal` holding exactly these choices."""
        field = self.field(name)
        if field.literal() is None:
            raise SourceRefusedError(
                f'{name} is not written as an inline Literal[...]: its choices are edited in code'
            )
        if not choices or len(set(choices)) != len(choices):
            raise SourceRefusedError('the choices must be at least one, each once')
        args, keywords = field.arguments()
        start = self._start(field, args)
        if start is not None and start not in choices:
            raise SourceRefusedError(f'{name} starts as {start!r}, which is not one of the new choices')
        kind = f'Literal[{self.choices(choices)}]'
        return self._replace(
            field, self._statement(field, self._typed(kind, field.nullable()), args, keywords)
        )

    def with_limits(self, name: str, limits: dict[str, int | float | None]) -> str:
        """The text with these limits set on a plain text or number fact; None removes one."""
        field = self.field(name)
        allowed = self.LIMITS.get(field.plain(), ())
        wrong = [key for key in limits if key not in allowed]
        if wrong:
            raise SourceRefusedError(
                f'{name} takes no {", ".join(wrong)} here: limits are min_length/max_length on plain text '
                'and ge/le on a plain number'
            )
        args, keywords = field.arguments()
        for key, value in limits.items():
            keywords = self._set(keywords, key, None if value is None else repr(value))
        return self._replace(field, self._statement(field, self._annotation(field), args, keywords))

    def with_empty(self, name: str, may_be_empty: bool, start: Any = None) -> str:
        """The text with this fact able to be empty (`| None`, starting empty) or not (starting as `start`)."""
        field = self.field(name)
        plain, literal = field.plain(), field.literal()
        if not plain and literal is None:
            raise SourceRefusedError(
                f'{name} is not a plain kind or an inline Literal: whether it may be empty is edited in code'
            )
        args, keywords = field.arguments()
        base = f'Literal[{self.choices(literal)}]' if literal is not None else plain
        if may_be_empty:
            return self._replace(
                field, self._statement(field, f'{base} | None', ['None', *args[1:]], keywords)
            )
        if start is None:
            raise SourceRefusedError(
                f'{name} needs a starting value when it may not be empty: a call starts with it'
            )
        if literal is not None and start not in literal:
            raise SourceRefusedError(f'{start!r} is not one of the choices of {name}')
        if plain in ('date', 'time'):
            raise SourceRefusedError(f'a {plain} has no starting value the studio can write: edit it in code')
        kinds = {'str': str, 'int': int, 'float': (int, float), 'bool': bool}
        if plain and (not isinstance(start, kinds[plain]) or (plain != 'bool' and isinstance(start, bool))):
            raise SourceRefusedError(f'{start!r} is not a {plain} value for {name}')
        return self._replace(field, self._statement(field, base, [repr(start), *args[1:]], keywords))

    def with_fact(self, name: str, fact: NewFact, description: str) -> str:
        """The text with a new fact at the end of the class, empty when a call opens, and the imports it needs."""
        if not name.isidentifier() or keyword.iskeyword(name) or not re.fullmatch(r'[a-z][a-z0-9_]*', name):
            raise SourceRefusedError(
                f'{name!r} is not a name for a fact: lower case letters, digits and underscores'
            )
        if name in self.fields or name.startswith('model_'):
            raise SourceRefusedError(f'{name} is taken')
        imports = StateImports(self)
        imports.require(fact.imports())
        indent = ' ' * (self.fields[next(iter(self.fields))].node.col_offset if self.fields else 4)
        keywords = {'description': repr(description)} if description else {}
        statement = self.layout(indent, f'{name}: {fact.annotation()} | None = ', 'Field', ['None'], keywords)
        lines = self.text.splitlines(keepends=True)
        last = max((node.end_lineno or node.lineno) for node in self.cls.body)
        added = ['\n', statement] if lines[last - 1].strip() else [statement]
        if not lines[last - 1].endswith('\n'):
            lines[last - 1] += '\n'
        lines[last:last] = added
        return imports.applied(''.join(lines))

    def without(self, name: str) -> str:
        """The text with this fact and its note gone, and a blank line it leaves doubled taken with it."""
        field = self.field(name)
        lines = self.text.splitlines(keepends=True)
        start, end = field.start - 1, field.end
        if start > 0 and not lines[start - 1].strip() and (end >= len(lines) or not lines[end].strip()):
            start -= 1
        del lines[start:end]
        return ''.join(lines)

    @staticmethod
    def _set(keywords: dict[str, str], key: str, value: str | None) -> dict[str, str]:
        """The keywords with one set in place (or added last), or removed when the value is None."""
        if value is None:
            return {k: v for k, v in keywords.items() if k != key}
        return keywords | {key: value}

    @staticmethod
    def _start(field: FieldSource, args: list[str]) -> Any:
        """The value it starts with, when it is a plain constant."""
        try:
            return ast.literal_eval(args[0]) if args else None
        except ValueError:
            return None

    @staticmethod
    def _typed(base: str, nullable: bool) -> str:
        """A kind written with `| None` when it may be empty."""
        return f'{base} | None' if nullable else base

    @staticmethod
    def _annotation(field: FieldSource) -> str:
        """The annotation's own text, unchanged."""
        return field.source(field.annotation)

    def _statement(
        self, field: FieldSource, annotation: str, args: list[str], keywords: dict[str, str]
    ) -> str:
        """The field's statement written again: `= None` when nothing but that is left, else `= Field(...)`."""
        indent = ' ' * field.node.col_offset
        head = f'{field.name}: {annotation} = '
        if args in (['None'], []) and not keywords:
            return f'{indent}{head}None\n'
        return self.layout(indent, head, 'Field', args or ['None'], keywords)

    @staticmethod
    def layout(indent: str, head: str, call: str, args: list[str], keywords: dict[str, str]) -> str:
        """A statement ending in a call, as `ruff format` lays it out in `WIDTH` columns."""
        parts = [*args, *(f'{key}={value}' for key, value in keywords.items())]
        flat = f'{indent}{head}{call}({", ".join(parts)})'
        if len(flat) <= WIDTH:
            return flat + '\n'
        inner = indent + '    '
        hugged = f'{inner}{", ".join(parts)}'
        if len(hugged) <= WIDTH:
            return f'{indent}{head}{call}(\n{hugged}\n{indent})\n'
        each = ''.join(f'{inner}{part},\n' for part in parts)
        return f'{indent}{head}{call}(\n{each}{indent})\n'

    def _replace(self, field: FieldSource, statement: str) -> str:
        """The text with the field's statement (its note kept) replaced."""
        lines = self.text.splitlines(keepends=True)
        start, end = field.node.lineno - 1, field.node.end_lineno or field.node.lineno
        lines[start:end] = [statement]
        return ''.join(lines)


class StateImports:
    """The names a new fact needs, found bound at the module's top level or added to its imports.

    A name the module binds to something else (`date` from another module) is refused, not shadowed.
    A name is added to an existing `from module import ...` in ruff's order, or a new statement is put
    among the standard library's imports in module order; `import core` goes after the last import.
    """

    def __init__(self, source: StateSource) -> None:
        self.source = source
        self.bound: dict[str, tuple[str, str | None]] = {}
        for node in source.tree.body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    self.bound[alias.asname or alias.name] = (node.module, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.bound[alias.asname or alias.name.split('.')[0]] = (alias.name, None)
            elif isinstance(
                node, ast.ClassDef | ast.FunctionDef | ast.Assign | ast.AnnAssign | ast.TypeAlias
            ):
                for name in self._targets(node):
                    self.bound[name] = ('', name)
        self.wanted: list[tuple[str, str | None]] = []

    @staticmethod
    def _targets(node: ast.stmt) -> list[str]:
        """The names a top-level statement binds."""
        if isinstance(node, ast.ClassDef | ast.FunctionDef):
            return [node.name]
        if isinstance(node, ast.Assign):
            return [t.id for t in node.targets if isinstance(t, ast.Name)]
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            return [node.target.id]
        if isinstance(node, ast.TypeAlias) and isinstance(node.name, ast.Name):
            return [node.name.id]
        return []

    def require(self, needs: Sequence[tuple[str, str | None]]) -> None:
        """Note what is missing; refuse a name bound to something else."""
        for module, name in needs:
            local = name or module
            held = self.bound.get(local)
            if held == (module, name):
                continue
            if held is not None:
                raise SourceRefusedError(
                    f'{local} in the state module is not {module}{"." + name if name else ""}: add this fact in code'
                )
            if (module, name) not in self.wanted:
                self.wanted.append((module, name))

    def applied(self, text: str) -> str:
        """The text with every missing name imported."""
        for module, name in self.wanted:
            text = self._one(text, module, name)
        return text

    @staticmethod
    def order(name: str) -> tuple[int, str]:
        """Ruff's order inside `from m import ...`: constants, then classes, then the rest."""
        kind = 0 if name.isupper() and len(name) > 1 else 1 if name[:1].isupper() else 2
        return kind, name

    def _one(self, text: str, module: str, name: str | None) -> str:
        """The text with one name imported."""
        tree = ast.parse(text)
        lines = text.splitlines(keepends=True)
        imports = [node for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)]
        same = next(
            (
                n
                for n in imports
                if isinstance(n, ast.ImportFrom) and n.module == module and not n.level and name
            ),
            None,
        )
        if same is not None and name is not None:
            names = [a.name if a.asname is None else f'{a.name} as {a.asname}' for a in same.names] + [name]
            names.sort(key=lambda each: self.order(each.split(' as ')[0]))
            flat = f'from {module} import {", ".join(names)}\n'
            statement = (
                flat
                if len(flat) <= WIDTH + 1
                else (f'from {module} import (\n' + ''.join(f'    {each},\n' for each in names) + ')\n')
            )
            lines[same.lineno - 1 : same.end_lineno] = [statement]
            return ''.join(lines)
        statement = f'from {module} import {name}\n' if name else f'import {module}\n'
        if module in sys.stdlib_module_names:
            stdlib = [n for n in imports if self._module(n) in sys.stdlib_module_names]
            later = next((n for n in stdlib if self._module(n) > module), None)
            at = (
                (later.lineno - 1)
                if later
                else (stdlib[-1].end_lineno or 0)
                if stdlib
                else (imports[0].lineno - 1 if imports else 0)
            )
        else:
            at = (imports[-1].end_lineno or 0) if imports else 0
        lines[at:at] = [statement]
        return ''.join(lines)

    @staticmethod
    def _module(node: ast.Import | ast.ImportFrom) -> str:
        """The top-level module a statement imports from."""
        name = node.module or '' if isinstance(node, ast.ImportFrom) else node.names[0].name
        return name.split('.')[0]


class SourceProof:
    """The old and new text of the state module compared as code: they may differ in one fact and in imports only.

    Every top-level statement but the imports and the state class must read the same; in the class, every
    statement but the fact's own (and its note) must read the same and stay in order. The imports may only
    gain names. A difference anywhere else is a reason to refuse the edit.
    """

    def __init__(self, old: str, new: str, class_name: str) -> None:
        self.old = StateSource(old, class_name)
        self.new = StateSource(new, class_name)

    def differences(self, name: str) -> list[str]:
        """What differs beyond the fact `name`, in words; empty when nothing else changed."""
        found: list[str] = []
        others = [self._others(source) for source in (self.old, self.new)]
        if others[0] != others[1]:
            found.append('a statement outside the facts changed')
        bodies = [self._body(source, name) for source in (self.old, self.new)]
        if bodies[0] != bodies[1]:
            found.append(f'a fact other than {name} changed, moved or appeared')
        before, after = (self._imports(source) for source in (self.old, self.new))
        if not before <= after:
            found.append('an import was taken away')
        return found

    def _others(self, source: StateSource) -> list[str]:
        """Every top-level statement but imports and the state class, as `ast.dump`."""
        return [
            ast.dump(node)
            for node in source.tree.body
            if not isinstance(node, ast.Import | ast.ImportFrom) and node is not source.cls
        ] + [ast.dump(node) for node in source.cls.decorator_list + source.cls.bases + source.cls.keywords]

    @staticmethod
    def _body(source: StateSource, name: str) -> list[str]:
        """The class's statements but this fact and its note, as `ast.dump`."""
        skip = (
            {id(source.fields[name].node), id(source.fields[name].note)} if name in source.fields else set()
        )
        return [ast.dump(node) for node in source.cls.body if id(node) not in skip]

    @staticmethod
    def _imports(source: StateSource) -> set[tuple[str, str, str]]:
        """Every name the module imports, with where from."""
        found: set[tuple[str, str, str]] = set()
        for node in source.tree.body:
            if isinstance(node, ast.ImportFrom):
                found |= {(node.module or '', a.name, a.asname or '') for a in node.names}
            elif isinstance(node, ast.Import):
                found |= {('', a.name, a.asname or '') for a in node.names}
        return found
