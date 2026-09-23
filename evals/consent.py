"""Explicit live semantic evaluation: uv run --env-file .env python -m evals.consent.

Makes billable model requests, one per case in `evals.cases`. Reports, per
kind of reply, how often the current classifier gave the reviewed answer, how
often it accepted something the caller did not agree to, and how long its
request took. Classifier latency, not voice latency.

The application only asks the classifier about a question the caller heard in
full, on the turn after it went out; for an interrupted or stale case it is
sent no pending question, exactly as `advance` would send it.
"""

import asyncio
import time

from pydantic_ai.usage import RunUsage

from evals.cases import CASES, Case, Meaning
from evals.report import Report, Result
from salon.classification import CLASSIFIER
from salon.models.consent import Asked, ConsentInput, ReplyClassification

OWNER = '+34600111222'


def classifier_meaning(case: Case, result: ReplyClassification) -> Meaning:
    """Read the four labels back as one meaning, the way `advance` acts on them."""
    if result.conversation == 'end':
        return 'end'
    if case.about == 'closing':
        return 'continue'
    if case.about == 'move_or_add':
        return result.appointment_choice if result.appointment_choice in ('move', 'add') else 'unclear'
    if result.confirmation in ('accept', 'reject', 'revise', 'conditional'):
        return result.confirmation
    return 'unclear'


def legacy_classification(meaning: Meaning, retained: bool = False) -> ReplyClassification:
    """The four labels a correct classifier gives for this meaning. The inverse of `classifier_meaning`."""
    fields: dict[str, str] = {'retained_approval': 'keep' if retained else 'unclear'}
    if meaning in ('accept', 'reject', 'revise', 'conditional', 'unclear'):
        fields |= {'confirmation': meaning, 'conversation': 'continue'}
    elif meaning in ('move', 'add'):
        fields |= {'appointment_choice': meaning, 'conversation': 'continue'}
    elif meaning == 'end':
        fields |= {'conversation': 'end'}
    elif meaning == 'continue':
        fields |= {'conversation': 'continue'}
    return ReplyClassification.model_validate(fields)


def consent_input(case: Case) -> ConsentInput:
    """What `classify_reply` would send for this case."""
    heard = case.delivered and not case.stale
    pending = Asked(about=case.about, spoken=case.spoken, owner=OWNER) if heard else None
    return ConsentInput(utterance=case.utterance, previous_speech=case.spoken, owner=OWNER, pending=pending)


async def main() -> None:
    usage = RunUsage()
    results = []
    for case in CASES:
        started = time.perf_counter()
        output = await CLASSIFIER.classify(consent_input(case), usage=usage)
        actual = classifier_meaning(case, output)
        results.append(Result.of(case, case.legacy_meaning, actual, time.perf_counter() - started))
    print(Report(interpreter='classifier', results=results).model_dump_json(indent=2))


if __name__ == '__main__':
    asyncio.run(main())
