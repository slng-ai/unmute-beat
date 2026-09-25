"""Explicit live semantic evaluation: uv run --env-file .env python -m evals.consent.

Makes billable model requests, one per case in `evals.cases`, each to the one
narrow reading that case's question calls for: a read-back to `CONFIRMATION`,
move-or-add to `APPOINTMENT_CHOICE`, "anything else?" to `CONVERSATION`.
Reports, per kind of reply, how often it gave the reviewed answer, how often
it accepted something the caller did not agree to, and how long its request
took. Classifier latency, not voice latency.

The application only reads an answer to a question the caller heard in full,
on the turn after it went out; for an interrupted or stale case it sends no
request at all, exactly as `advance` would, and the case reads as unclear.
"""

import asyncio
import time

from pydantic_ai.usage import RunUsage

from core.classifiers import Exchange
from core.readings import ChoiceInput, ReadBackInput, Reading
from evals.cases import CASES, Case, Meaning
from evals.report import Report, Result
from salon.classification import APPOINTMENT_CHOICE, CONFIRMATION, CONVERSATION, MOVE_OR_ADD, READ_BACKS


def reading_for(case: Case) -> Reading:
    """The one reading the application runs on a reply to this case's question."""
    if case.about in READ_BACKS:
        return CONFIRMATION
    return APPOINTMENT_CHOICE if case.about == 'move_or_add' else CONVERSATION


def reading_input(case: Case) -> Exchange | None:
    """What that reading is sent for this case, or None when the application would send nothing."""
    if not case.delivered or case.stale:
        return None
    if case.about in READ_BACKS:
        return ReadBackInput(question=case.spoken, answer=case.utterance, proposal=case.spoken)
    if case.about == 'move_or_add':
        return ChoiceInput(question=case.spoken, answer=case.utterance, options=MOVE_OR_ADD)
    return Exchange(question=case.spoken, answer=case.utterance)


def classifier_meaning(case: Case, label: str) -> Meaning:
    """Read one reading's label back as one meaning, the way `advance` acts on it."""
    if case.about == 'closing':
        return 'end' if label == 'end' else 'continue'
    if case.about == 'move_or_add':
        return label if label in ('move', 'add') else 'unclear'
    return label if label in ('accept', 'reject', 'revise', 'conditional') else 'unclear'


def label_of(meaning: Meaning, reading: str) -> str | None:
    """The label a correct reading gives for this meaning. The inverse of `classifier_meaning`; None is the default."""
    match reading:
        case 'confirmation':
            return (
                meaning if meaning in ('accept', 'reject', 'revise', 'conditional', 'unclear') else 'unclear'
            )
        case 'appointment_choice':
            return meaning if meaning in ('move', 'add') else ''
        case 'conversation':
            return 'end' if meaning == 'end' else 'continue'
        case 'retained_approval':
            return 'keep'
    return None


async def read_case(case: Case, usage: RunUsage) -> Meaning:
    """What the live reading makes of this case, as one meaning. No request when the application would send none."""
    reading, value = reading_for(case), reading_input(case)
    if value is None:
        return classifier_meaning(case, getattr(reading.fallback, reading.field))
    output = await reading.classifier.classify(value, usage=usage)
    return classifier_meaning(case, getattr(output, reading.field))


async def main() -> None:
    usage = RunUsage()
    results = []
    for case in CASES:
        started = time.perf_counter()
        actual = await read_case(case, usage)
        results.append(Result.of(case, case.legacy_meaning, actual, time.perf_counter() - started))
    print(Report(interpreter='classifier', results=results).model_dump_json(indent=2))


if __name__ == '__main__':
    asyncio.run(main())
