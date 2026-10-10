"""Closing lines chosen by what the service answered: a line for each kind of result, and one for the rest.

An operation's `say` (and `recap`) is either one line, formatted from the
receipt's fields, or a list of cases tried in order:

    say:
      - when: {customer_status: existing}
        line: Welcome back, {name}.
      - when: {customer_status: new}
        line: Thanks, I have set you up.
      - when: refused
        line: I could not find you with that number.
      - line: Thanks, I have you.

`when` names receipt fields and the value each must hold (a list: any of
them; `null`: no value). A case with no `when` is the line for any other
success, and `when: refused` the line for any other refusal. A refusal is
said only when a case names it, by `refused` or by its own fields (`when:
{reason: no_match}`): otherwise it goes back to the step's agent unsaid, as
before. Values compare as the receipt holds them, or as their text, so
`when: {verified: true}` and `when: {count: 2}` read as written in YAML.
"""

from collections.abc import Mapping
from enum import Enum
from string import Formatter
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, RootModel, model_validator


class Case(BaseModel):
    """One closing line and the result it is for."""

    model_config = ConfigDict(extra='forbid', frozen=True)
    line: str
    """What is said, formatted from the receipt's fields: `Welcome back, {name}.` May be empty: say nothing."""
    when: dict[str, Any] | Literal['refused'] | None = None
    """Receipt fields and the values meaning this result; `refused` for any refusal; none for any success."""

    def fits(self, facts: Mapping[str, Any], refused: bool) -> bool:
        """Whether this case is the line for a receipt with these fields and this outcome."""
        if self.when is None:
            return not refused
        if self.when == 'refused':
            return refused
        return all(name in facts and _holds(facts[name], wanted) for name, wanted in self.when.items())


def _holds(value: Any, wanted: Any) -> bool:
    """Whether a receipt value is the one a case names: equal, equal as text, or one of a list."""
    if isinstance(wanted, list):
        return any(_holds(value, each) for each in wanted)
    if isinstance(value, Enum):
        value = value.value
    if wanted is None or value is None:
        return value is wanted
    if isinstance(value, bool) or isinstance(wanted, bool):
        return value is wanted or str(value).lower() == str(wanted).lower()
    return value == wanted or str(value) == str(wanted)


class Closing(RootModel[tuple[Case, ...]]):
    """Closing lines by result, tried in order; formats like a string, so every reader of `say` takes either."""

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
            later = [case for case in self.root[at[0] + 1 :] if isinstance(case.when, dict)] if at else []
            if later:
                raise ValueError(f'{label} comes before cases it would hide: put it last')
        return self

    def __bool__(self) -> bool:
        return any(case.line for case in self.root)

    def line_for(self, facts: Mapping[str, Any], refused: bool = False) -> str | None:
        """The template for this result; None when no case is for it."""
        return next((case.line for case in self.root if case.fits(facts, refused)), None)

    def format_map(self, facts: Mapping[str, Any]) -> str:
        """The success line for these receipt fields, formatted from them; '' when no case is for it."""
        template = self.line_for(facts)
        return template.format_map(facts) if template else ''

    def refused(self, facts: Mapping[str, Any]) -> str:
        """The refusal line for these receipt fields, formatted from them; '' when no case names it."""
        template = self.line_for(facts, refused=True)
        return template.format_map(facts) if template else ''

    def lines(self) -> tuple[str, ...]:
        """Every line, in order: what a check reads for placeholders."""
        return tuple(case.line for case in self.root)


type Words = str | Closing
"""An operation's `say` or `recap`: one line, or lines by result."""


def closing(value: object) -> Words:
    """One line kept as it is; a list of cases validated into a `Closing`."""
    if isinstance(value, str | Closing):
        return value
    return Closing.model_validate(value)


def refused_words(words: object, facts: Mapping[str, Any]) -> str:
    """What is said for a refusal with these fields: only a `Closing` case naming it says anything."""
    return words.refused(facts).strip() if isinstance(words, Closing) else ''


def placeholders(words: object) -> set[str]:
    """Every field a closing line formats, in each of its lines."""
    lines = words.lines() if isinstance(words, Closing) else (words,) if isinstance(words, str) else ()
    return {
        name.split('[')[0].split('.')[0]
        for line in lines
        for _, name, _, _ in Formatter().parse(line)
        if name
    }
