"""Explicit live evaluation of the state solver: uv run --env-file .env python -m evals.solver.

Makes billable model requests, one per case in `evals.cases`, and scores the
solver against each case's full meaning (`expect`), which includes the
corrections and switches the classifier cannot say. Solver latency, not voice
latency.
"""

import asyncio
import time

from pydantic_ai.usage import RunUsage

from core.decisions import Accept, Conditional, EndCall, NoDecision, Reject, Revise
from evals.cases import CASES, PHONE, Case, Meaning
from evals.report import Report, Result
from salon.classification import DESTINATIONS, SOLVER_CLASSIFIER
from salon.models.consent import (
    Asked,
    ChooseAppointment,
    CorrectPhone,
    SalonSolution,
    SalonSwitch,
    SolverInput,
)

STEP = {'phone': 'verify', 'booking': 'booking', 'move_or_add': 'booking', 'complaint': 'complaints'}
"""The step each kind of question is asked from. A closing question is asked at the front desk."""
REPLIES = {
    'complaints': "Okay, let's sort that out. What happened?",
    'booking': 'Sure, let me help you book.',
}


def solver_meaning(case: Case, solution: SalonSolution) -> Meaning:
    """Read a decision back in the cases' vocabulary."""
    match solution.decision:
        case Accept():
            return 'accept'
        case Reject():
            return 'reject'
        case Revise():
            return 'revise'
        case Conditional():
            return 'conditional'
        case CorrectPhone():
            return 'correct_phone'
        case ChooseAppointment(choice=choice):
            return choice
        case SalonSwitch(target='complaints'):
            return 'switch_complaints'
        case SalonSwitch(target='booking'):
            return 'switch_booking'
        case EndCall():
            return 'end'
        case _:
            return 'continue' if case.about == 'closing' else 'unclear'


def solution_for(case: Case) -> SalonSolution:
    """The decision a correct solver returns for this case. The inverse of `solver_meaning`."""
    decision = {
        'accept': Accept(),
        'reject': Reject(),
        'revise': Revise(),
        'conditional': Conditional(),
        'end': EndCall(),
        'correct_phone': CorrectPhone(digits=case.digits),
        'move': ChooseAppointment(choice='move'),
        'add': ChooseAppointment(choice='add'),
        'switch_complaints': SalonSwitch(target='complaints', details=case.details),
        'switch_booking': SalonSwitch(target='booking', details=case.details),
    }.get(case.expect, NoDecision())
    reply = REPLIES.get(decision.target, '') if isinstance(decision, SalonSwitch) else ''
    return SalonSolution(decision=decision, reply=reply)


def solver_input(case: Case) -> SolverInput:
    """What `SalonSolver._input` would send for this case."""
    step = STEP.get(case.about, 'concierge')
    proposal = None if case.stale else Asked(about=case.about, spoken=case.spoken, owner=PHONE, value=PHONE)
    return SolverInput(
        utterance=case.utterance,
        previous_speech=case.spoken,
        step=step,  # ty: ignore[invalid-argument-type]
        owner=PHONE,
        proposal=proposal,
        delivered=case.delivered and not case.stale,
        destinations=tuple(d for d in DESTINATIONS if d.name != step),
    )


async def main() -> None:
    usage = RunUsage()
    results = []
    for case in CASES:
        started = time.perf_counter()
        output = await SOLVER_CLASSIFIER.classify(solver_input(case), usage=usage)
        actual = solver_meaning(case, output)
        results.append(Result.of(case, case.expect, actual, time.perf_counter() - started))
    print(Report(interpreter='solver', results=results).model_dump_json(indent=2))


if __name__ == '__main__':
    asyncio.run(main())
