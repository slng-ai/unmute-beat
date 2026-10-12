"""Closing lines chosen by what the service answered: a line for each kind of result, and one for the rest.

An operation's `say` (and `recap`) is either one line, filled from the
receipt, or a list of cases tried in order:

    say:
      - when: {customer.status: existing}
        line: Welcome back, {customer.name}.
      - when: {customer.status: new}
        line: Thanks, I have set you up.
      - when: refused
        line: I could not find you with that number.
      - line: Thanks, I have you.

A line names receipt fields in braces, and a field inside a nested model by
its path: `{customer.name}`, `{customer[name]}`, `{items[0].spoken}`. A
field the receipt does not have is said as nothing, never as an error.
`when` names fields the same way, with the value each must hold (a list: any
of them; `null`: no value). A case with no `when` is the line for any other
success, and `when: refused` the line for any other refusal. A refusal is
said only when a case names it, by `refused` or by its own fields: otherwise
it goes back to the step's agent unsaid. Values compare as the receipt holds
them, or as their text, so `when: {verified: true}` reads as written in YAML.

`Words` is an operation's `say` or `recap` as declared: one line, or a `Closing`.
"""

import re
from collections.abc import Mapping, Sequence
from enum import Enum
from string import Formatter
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, RootModel, model_validator


class Blank(str):
    """A field the data does not have: said as nothing, whatever format it was asked in."""

    def __format__(self, spec: str) -> str:
        """Nothing, in any format: a missing number asked for as `{price:.2f}` is still said as nothing."""
        return ''


class FieldPath:
    """A field inside nested data, as a line names it: `customer.name`, `customer[name]`, `items[0].spoken`."""

    PART = re.compile(r'[^.\[\]]+')

    def __init__(self, text: str) -> None:
        self.text = text
        self.parts = tuple(self.PART.findall(text))

    @property
    def head(self) -> str:
        """The top-level field the path starts from."""
        return self.parts[0] if self.parts else ''

    def find(self, data: Any) -> Any:
        """The value at this path, or a `Blank` when any part of it is missing."""
        for part in self.parts:
            data = self._step(data, part)
            if isinstance(data, Blank):
                return data
        return data

    @staticmethod
    def _step(data: Any, part: str) -> Any:
        """One part of the path: a key of a mapping, an index of a list, or an attribute of a model."""
        if isinstance(data, Mapping):
            return data.get(part, Blank())
        if isinstance(data, Sequence) and not isinstance(data, str) and part.isdigit():
            return data[int(part)] if int(part) < len(data) else Blank()
        if isinstance(data, BaseModel):
            return getattr(data, part, Blank())
        return Blank()


class Template(Formatter):
    """One line with `{field}` placeholders, filled from a receipt or a state, nested fields included."""

    def __init__(self, line: str) -> None:
        super().__init__()
        self.line = line

    def fill(self, data: Mapping[str, Any]) -> str:
        """The line with every placeholder filled from the data; a field it does not have is said as nothing."""
        return self.vformat(self.line, (), data)

    def get_field(self, field_name: str, args: Sequence[Any], kwargs: Mapping[str, Any]) -> tuple[Any, str]:
        """A placeholder's value, read by its whole path rather than `str.format`'s attribute access."""
        return FieldPath(field_name).find(kwargs), field_name

    def fields(self) -> tuple[str, ...]:
        """Every field the line names, by its whole path, in order. Raises `ValueError` for an unclosed brace."""
        named = [name for _, name, _, _ in self.parse(self.line) if name]
        return tuple(dict.fromkeys(named))


class Case(BaseModel):
    """One closing line and the result it is for.

    `line` is what is said, filled from the receipt; empty says nothing. `when` maps field paths to
    the values meaning this result, or is `refused` for any refusal, or none for any success.
    """

    model_config = ConfigDict(extra='forbid', frozen=True)
    line: str
    when: dict[str, Any] | Literal['refused'] | None = None

    def fits(self, facts: Mapping[str, Any], refused: bool) -> bool:
        """Whether this case is the line for a receipt with these fields and this outcome."""
        if self.when is None:
            return not refused
        if self.when == 'refused':
            return refused
        return all(self._holds(FieldPath(name).find(facts), wanted) for name, wanted in self.when.items())

    @classmethod
    def _holds(cls, value: Any, wanted: Any) -> bool:
        """Whether a receipt value is the one a case names: equal, equal as text, or one of a list."""
        if isinstance(value, Blank):
            return False
        if isinstance(wanted, list):
            return any(cls._holds(value, each) for each in wanted)
        if isinstance(value, Enum):
            value = value.value
        if wanted is None or value is None:
            return value is wanted
        if isinstance(value, bool) or isinstance(wanted, bool):
            return value is wanted or str(value).lower() == str(wanted).lower()
        return value == wanted or str(value) == str(wanted)


type Words = str | Closing


class Closing(RootModel[tuple[Case, ...]]):
    """Closing lines by result, tried in order: the first case that fits the receipt is said."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode='after')
    def _reachable(self) -> Self:
        """Each case can be reached: one default line and one `refused` line at most, each after the cases it backs."""
        if not self.root:
            raise ValueError('give at least one case, or one line instead of a list')
        for kind, label in ((None, 'a case with no `when`'), ('refused', '`when: refused`')):
            at = [n for n, case in enumerate(self.root) if case.when == kind]
            if len(at) > 1:
                raise ValueError(f'{label} is given {len(at)} times: keep one')
            if at and any(isinstance(case.when, dict) for case in self.root[at[0] + 1 :]):
                raise ValueError(f'{label} comes before cases it would hide: put it last')
        return self

    @classmethod
    def declared(cls, value: object) -> Words:
        """A declared `say` or `recap`: one line kept as it is, a list of cases validated into a `Closing`."""
        return value if isinstance(value, str | Closing) else cls.model_validate(value)

    @classmethod
    def of(cls, words: object) -> Closing:
        """Any declared `say` or `recap` as cases: one line is the line for any success; nothing is no case."""
        if isinstance(words, Closing):
            return words
        return cls((Case(line=words),)) if isinstance(words, str) else cls((Case(line=''),))

    def __bool__(self) -> bool:
        """Whether any case says something."""
        return any(case.line for case in self.root)

    def said(self, facts: Mapping[str, Any]) -> str:
        """The success line for these receipt fields, filled from them; '' when no case is for it."""
        return self._filled(facts, refused=False)

    def refused(self, facts: Mapping[str, Any]) -> str:
        """The refusal line for these receipt fields, filled from them; '' when no case names it."""
        return self._filled(facts, refused=True)

    def lines(self) -> tuple[str, ...]:
        """Every line, in order."""
        return tuple(case.line for case in self.root)

    def fields(self) -> tuple[str, ...]:
        """Every field path any of its lines names, in order."""
        return tuple(dict.fromkeys(name for line in self.lines() for name in Template(line).fields()))

    def _filled(self, facts: Mapping[str, Any], refused: bool) -> str:
        """The first fitting case's line, filled and trimmed; '' when none fits."""
        line = next((case.line for case in self.root if case.fits(facts, refused)), '')
        return Template(line).fill(facts).strip() if line else ''
