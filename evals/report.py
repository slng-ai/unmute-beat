"""One result shape and one report for every interpreter, so they compare line by line."""

import math

from pydantic import BaseModel, ConfigDict, computed_field

from evals.cases import Case, Meaning


class Result(BaseModel):
    """One case, one interpreter: what it should have meant, what it was read as, and how long that took."""

    model_config = ConfigDict(frozen=True)

    id: str
    group: str
    expected: Meaning
    actual: Meaning
    seconds: float
    requests: int = 1

    @classmethod
    def of(cls, case: Case, expected: Meaning, actual: Meaning, seconds: float, requests: int = 1) -> Result:
        return cls(
            id=case.id,
            group=case.group,
            expected=expected,
            actual=actual,
            seconds=round(seconds, 3),
            requests=requests,
        )

    @computed_field
    @property
    def correct(self) -> bool:
        return self.actual == self.expected

    @computed_field
    @property
    def false_accept(self) -> bool:
        """Read as agreement when the caller did not agree: the one error a guard cannot catch."""
        return self.actual == 'accept' and self.expected != 'accept'


class Group(BaseModel):
    """The results for one kind of reply."""

    cases: int
    correct: int
    false_accepts: int


class Report(BaseModel):
    """Every result for one interpreter, with the numbers worth comparing kept apart."""

    interpreter: str
    results: list[Result]

    @computed_field
    @property
    def accuracy(self) -> float:
        return round(sum(r.correct for r in self.results) / max(len(self.results), 1), 3)

    @computed_field
    @property
    def false_accepts(self) -> int:
        return sum(r.false_accept for r in self.results)

    @computed_field
    @property
    def requests(self) -> int:
        return sum(r.requests for r in self.results)

    @computed_field
    @property
    def seconds_p50(self) -> float:
        return percentile([r.seconds for r in self.results], 0.5)

    @computed_field
    @property
    def seconds_p95(self) -> float:
        return percentile([r.seconds for r in self.results], 0.95)

    @computed_field
    @property
    def groups(self) -> dict[str, Group]:
        groups: dict[str, Group] = {}
        for r in self.results:
            seen = groups.get(r.group, Group(cases=0, correct=0, false_accepts=0))
            groups[r.group] = Group(
                cases=seen.cases + 1,
                correct=seen.correct + r.correct,
                false_accepts=seen.false_accepts + r.false_accept,
            )
        return groups


def percentile(values: list[float], share: float) -> float:
    """The nearest-rank percentile, or 0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * share) - 1]
