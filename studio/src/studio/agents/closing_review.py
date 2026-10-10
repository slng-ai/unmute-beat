"""Each closing line checked against the receipt it will be filled from, before it can be saved.

Core does not check a closing line's placeholders when it loads a manifest, and says a field the
receipt does not have as nothing; so the studio checks, on the operation's receipt shape
(`studio.agents.check`): a line names only fields its own result has, a success's for a success line
and a refusal's for a refusal line, a nested one by its path (`{customer.name}`); a case's `when`
names fields of one receipt, with values its fields can hold; a refusal line is said only by an
operation that can be refused; a goodbye never recaps a refusal; and every brace is closed.
"""

from typing import Any

from core.closing import FieldPath, Template
from studio.agents.models import Case, Operation, Problem


class Side:
    """One result an operation can have, a success or a refusal: its label, its field paths and their closed values."""

    def __init__(self, label: str, fields: list[str], values: dict[str, list[Any]]) -> None:
        self.label = label
        self.fields = fields
        self.values = values

    @staticmethod
    def path(name: str) -> str:
        """A field as the receipt shape names it: `items[0].spoken` and `customer[name]` read `items.spoken`, `customer.name`."""
        return '.'.join(part for part in FieldPath(name).parts if not part.isdigit())

    def has(self, name: str) -> bool:
        """Whether a receipt of this result has the field."""
        return self.path(name) in self.fields

    def holds(self, name: str, wanted: Any) -> bool:
        """Whether the field exists here and can hold the value a case names (any value when it is open)."""
        closed = self.values.get(self.path(name))
        return self.has(name) and (
            not closed or any(ClosingReview.same(v, c) for v in ClosingReview.options(wanted) for c in closed)
        )


class ClosingReview:
    """Every closing line of one operation, checked against its receipt's two sides."""

    def __init__(self, operation: Operation) -> None:
        self.operation = operation
        self.done = Side('a success', operation.fields, operation.values)
        self.refused = Side('a refusal', operation.refused_fields, operation.refused_values)
        self.known = bool(operation.fields or operation.refused_fields)

    def problems(self) -> list[Problem]:
        """What is wrong with each of its lines, case by case."""
        return [
            problem
            for key in ('say', 'recap')
            for case, where in self._cases(key)
            for problem in self._case(key, case, where)
        ]

    def _cases(self, key: str) -> list[tuple[Case, str]]:
        """Each line of `say` or `recap` as a case, with where it is: one line is the case for any success."""
        words = getattr(self.operation, key)
        where = f'operation {self.operation.name} · {key}'
        if isinstance(words, str):
            return [(Case(line=words), where)] if words else []
        return [(case, f'{where} · case {n}') for n, case in enumerate(words, 1)]

    def _case(self, key: str, case: Case, where: str) -> list[Problem]:
        """One case: the result it is for, then the fields its line names."""
        sides, found = self._sides(key, case, where)
        if found and found[-1].level == 'error' and not sides:
            return found
        return found + self._fields(case, sides, where)

    def _sides(self, key: str, case: Case, where: str) -> tuple[list[Side], list[Problem]]:
        """The results a case can be the line for, and what is wrong with its `when`."""
        if case.when is None:
            return [self.done], []
        if case.when == 'refused':
            if key == 'recap':
                return [], [
                    Problem(
                        where=where, what='a goodbye recaps only what stands: a refusal is never recapped'
                    )
                ]
            if not self.operation.refused_fields:
                warning = 'this operation is never refused with a receipt, so this line is never said'
                return [self.refused], [Problem(where=where, what=warning, level='warning')]
            return [self.refused], []
        when = case.when
        sides = [
            side
            for side in (self.done, self.refused)
            if side.fields and all(side.holds(n, w) for n, w in when.items())
        ]
        if sides or not self.known:
            return sides, []
        return [], [Problem(where=where, what=f'{self._never(when)}: this line is never said')]

    def _never(self, when: dict[str, Any]) -> str:
        """Why no receipt fits a `when`: a field neither side has, a value no field holds, or the two sides mixed."""
        unknown = sorted(name for name in when if not (self.done.has(name) or self.refused.has(name)))
        if unknown:
            return f'`when` names {", ".join(unknown)}, which its receipt does not have'
        closed = {**self.refused.values, **self.done.values}
        odd = [
            f'{name} is never {", ".join(map(str, self.options(wanted)))} (it is one of {", ".join(map(str, closed[path]))})'
            for name, wanted in when.items()
            if (path := Side.path(name)) in closed
            and not any(self.same(v, c) for v in self.options(wanted) for c in closed[path])
        ]
        return (
            '; '.join(odd) or '`when` mixes values of a success and of a refusal: no one receipt has them all'
        )

    def _fields(self, case: Case, sides: list[Side], where: str) -> list[Problem]:
        """The fields a line names that the result it is for does not have, and an unclosed brace."""
        try:
            named = Template(case.line).fields()
        except ValueError as error:
            return [Problem(where=where, what=f'{error}: the line could not be said')]
        missing = [name for name in named if self.known and not any(side.has(name) for side in sides)]
        if not missing:
            return []
        labels = {side.label for side in sides}
        result = labels.pop() if len(labels) == 1 else 'its receipt'
        allowed = list(dict.fromkeys(field for side in sides for field in side.fields))
        what = (
            f'names {", ".join("{" + m + "}" for m in missing)}, which {result} does not have '
            f'({", ".join(allowed) or "no fields"}): it would be said as a gap'
        )
        return [Problem(where=where, what=what)]

    @staticmethod
    def options(wanted: Any) -> list[Any]:
        """A `when` value as the values it accepts: a list is any of them."""
        return wanted if isinstance(wanted, list) else [wanted]

    @staticmethod
    def same(value: Any, closed: Any) -> bool:
        """Whether a `when` value is a closed value, compared as core compares them (`core.closing`)."""
        return value == closed or str(value).lower() == str(closed).lower()
