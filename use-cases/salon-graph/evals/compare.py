"""Every interpreter on the same reviewed cases, side by side.

    uv run python -m evals.compare                         # the rule floor only: offline, free
    uv run --env-file .env python -m evals.compare --live  # plus the classifier and the solver: billable

Each interpreter is scored against what it can say: the classifier against
each case's four-label answer, the solver and the floor against the full
meaning. False acceptances are counted the same way for all three, and are
the number that decides: a guard can refuse a stale or unheard proposal, but
not a wrong "yes" to a live one.

Requests are **projected** from the control flow in `salon/agents.py`, given
what each interpreter read: whether its reading lets an application action
answer the turn, or the step's agent has to run after it. They are not
measured on a call. A switch the others cannot read costs their step's agent
and then the destination's; a switch the graph refuses would cost the solver
one more.
`--out` saves the full reports as JSON, for the plan's record.
"""

import argparse
import asyncio
import json
import time

from pydantic_ai.usage import RunUsage

from evals.cases import CASES, Case, Meaning
from evals.consent import read_case
from evals.report import Report, Result
from evals.rules import decide
from evals.solver import solver_input, solver_meaning
from salon.classification import SOLVER_CLASSIFIER

RESOLVED_BY_ACTION = {'accept', 'end', 'move', 'add'}
"""Readings that an application action answers on its own, when the proposal was heard."""


def resolves(case: Case, meaning: Meaning, solver: bool) -> bool:
    """Whether this reading ends the turn without the step's agent running after it."""
    heard = case.delivered and not case.stale
    if meaning in RESOLVED_BY_ACTION:
        return heard and (meaning != 'accept' or case.about in ('phone', 'booking', 'complaint'))
    if not solver or case.stale:
        return False
    return meaning.startswith('switch_') or (meaning == 'correct_phone' and case.about == 'phone')


def projected_requests(case: Case, meaning: Meaning, interpreter: str) -> int:
    """Provider requests this turn would cost, following the control flow from this reading."""
    solver = interpreter == 'solver'
    if interpreter == 'rules':
        asks = 0
    elif interpreter == 'classifier':
        asks = 1 if case.delivered and not case.stale else 0
    else:
        asks = 0 if case.stale else 1
    if (asks or interpreter == 'rules') and resolves(case, meaning, solver):
        return asks
    handed_on = case.expect.startswith('switch_') and not solver
    return asks + 1 + handed_on


def rules_report() -> Report:
    results = []
    for case in CASES:
        started = time.perf_counter()
        actual = solver_meaning(case, decide(solver_input(case)))
        results.append(
            Result.of(
                case,
                case.expect,
                actual,
                time.perf_counter() - started,
                projected_requests(case, actual, 'rules'),
            )
        )
    return Report(interpreter='rules', results=results)


async def classifier_report() -> Report:
    usage, results = RunUsage(), []
    for case in CASES:
        started = time.perf_counter()
        actual = await read_case(case, usage)
        results.append(
            Result.of(
                case,
                case.legacy_meaning,
                actual,
                time.perf_counter() - started,
                projected_requests(case, actual, 'classifier'),
            )
        )
    return Report(interpreter='classifier', results=results)


async def solver_report() -> Report:
    usage, results = RunUsage(), []
    for case in CASES:
        started = time.perf_counter()
        actual = solver_meaning(case, await SOLVER_CLASSIFIER.classify(solver_input(case), usage=usage))
        results.append(
            Result.of(
                case,
                case.expect,
                actual,
                time.perf_counter() - started,
                projected_requests(case, actual, 'solver'),
            )
        )
    return Report(interpreter='solver', results=results)


def table(reports: list[Report]) -> str:
    """One line per interpreter, then accuracy per kind of reply."""
    lines = [
        f'{"interpreter":<12}{"accuracy":>9}{"false accepts":>15}{"projected requests":>20}{"p50 s":>8}{"p95 s":>8}'
    ]
    for r in reports:
        lines.append(
            f'{r.interpreter:<12}{r.accuracy:>9.3f}{r.false_accepts:>15}{r.requests:>20}'
            f'{r.seconds_p50:>8.3f}{r.seconds_p95:>8.3f}'
        )
    groups = sorted({g for r in reports for g in r.groups})
    lines.append('')
    lines.append(f'{"kind":<14}' + ''.join(f'{r.interpreter:>12}' for r in reports))
    for g in groups:
        cells = []
        for r in reports:
            seen = r.groups.get(g)
            cells.append(f'{seen.correct}/{seen.cases}' if seen else '-')
        lines.append(f'{g:<14}' + ''.join(f'{c:>12}' for c in cells))
    return '\n'.join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        '--live', action='store_true', help='also run the classifier and the solver (billable)'
    )
    parser.add_argument('--out', help='write the full reports to this JSON file')
    args = parser.parse_args()
    reports = [rules_report()]
    if args.live:
        reports += [await classifier_report(), await solver_report()]
    print(table(reports))
    if args.out:
        with open(args.out, 'w') as out:
            json.dump([r.model_dump(mode='json') for r in reports], out, indent=2, ensure_ascii=False)


if __name__ == '__main__':
    asyncio.run(main())
