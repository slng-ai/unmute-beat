"""Explicit live semantic evaluation: uv run --env-file .env python -m evals.consent.

Makes billable model requests. Reports classifier latency, not voice latency.
"""

import asyncio
import json
import math
import time

from pydantic_ai.usage import RunUsage

from salon.classification import CLASSIFIER
from salon.models.consent import Asked, ConsentInput


async def main() -> None:
    booking = Asked(about='booking', spoken='Shall I book Friday at three?', action='book')
    closing = Asked(about='closing', spoken='Do you need anything else?')
    choice = Asked(about='move_or_add', spoken='Move your existing booking or add another?')
    cases = [
        (booking, 'Yes, please.', 'confirmation', 'accept'),
        (booking, 'Sí, resérvalo.', 'confirmation', 'accept'),
        (booking, 'Sí, book it please.', 'confirmation', 'accept'),
        (booking, 'Yes, if it costs less than twenty.', 'confirmation', 'conditional'),
        (booking, 'Sí, pero solo si cuesta menos de veinte.', 'confirmation', 'conditional'),
        (booking, 'Actually, make it Saturday.', 'confirmation', 'revise'),
        (booking, 'Mejor el sábado.', 'confirmation', 'revise'),
        (booking, 'Yes, how much will it cost?', 'confirmation', 'unclear'),
        (booking, 'No, thanks.', 'conversation', 'continue'),
        (closing, 'No, thanks.', 'conversation', 'end'),
        (closing, 'No, gracias. Eso es todo.', 'conversation', 'end'),
        (closing, 'Bye, pero antes, ¿cuánto cuesta?', 'conversation', 'continue'),
        (choice, 'Keep that one and add another.', 'appointment_choice', 'add'),
        (choice, 'Cambia la que ya tengo.', 'appointment_choice', 'move'),
        (choice, 'Yes.', 'appointment_choice', 'unclear'),
    ]
    usage = RunUsage()
    durations, results = [], []
    for question, utterance, field, expected in cases:
        started = time.perf_counter()
        output = await CLASSIFIER.classify(
            ConsentInput(utterance=utterance, previous_speech=question.spoken, owner='', pending=question),
            usage=usage,
        )
        durations.append(time.perf_counter() - started)
        actual = getattr(output, field)
        results.append({'utterance': utterance, 'field': field, 'expected': expected, 'actual': actual})
    durations.sort()
    print(
        json.dumps(
            {
                'results': results,
                'accuracy': sum(r['actual'] == r['expected'] for r in results) / len(results),
                'false_acceptances': sum(
                    r['field'] == 'confirmation' and r['expected'] != 'accept' and r['actual'] == 'accept'
                    for r in results
                ),
                'requests': usage.requests,
                'classifier_seconds_p50': durations[math.ceil(len(durations) * 0.5) - 1],
                'classifier_seconds_p95': durations[math.ceil(len(durations) * 0.95) - 1],
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == '__main__':
    asyncio.run(main())
