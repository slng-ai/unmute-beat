"""Scripted interpretations for existing flow tests, not a language-understanding test.

These recorded labels replace the former word-list assumptions. Live semantic
accuracy is evaluated separately; this fake exercises the real request boundary.

Every narrow reading's classifier gets the same scripted model, which tells
the readings apart by the output schema it is asked for. `scripted` takes a
function from (reading name, the input it was sent) to a label; None is the
reading's default.
"""

import json
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from inspect import isawaitable
from typing import Any
from unittest.mock import patch

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from core.readings import Reading
from salon.classification import CONVERSATION, READINGS
from salon.diary import times_named

ALL: tuple[Reading, ...] = (*READINGS, CONVERSATION)
BY_OUTPUT = {reading.classifier.output_type.__name__: reading for reading in ALL}

type Script = Callable[[str, dict[str, Any]], Any]
"""(reading name, the input it was sent) -> a label, None for the default, or an awaitable of either."""


def reading_asked(messages, info) -> tuple[Reading, dict[str, Any]]:
    """Which reading a request is for, and the input it was sent."""
    reading = BY_OUTPUT[info.model_request_parameters.output_object.name]
    return reading, json.loads(messages[-1].parts[0].content)


def answered(reading: Reading, label: str | dict[str, str] | None) -> ModelResponse:
    """The model's answer for one reading: its label, or its default. A choice of 'unclear' picks nothing.

    A dict is the whole answer, field by field, for a reading with more than one field (`NamedChoice`).
    """
    if isinstance(label, dict):
        return ModelResponse(parts=[TextPart(json.dumps(label))])
    default = getattr(reading.fallback, reading.field)
    value = default if label is None else label
    if reading.field == 'choice' and value == 'unclear':
        value = ''
    return ModelResponse(parts=[TextPart(json.dumps({reading.field: value}))])


def script_model(script: Script) -> FunctionModel:
    async def respond(messages, info):
        reading, data = reading_asked(messages, info)
        label = script(reading.name, data)
        if isawaitable(label):
            label = await label
        return answered(reading, label)

    return FunctionModel(function=respond)


@contextmanager
def scripted(script: Script):
    """Every narrow reading answered by `script`."""
    model = script_model(script)
    with ExitStack() as stack:
        for reading in ALL:
            stack.enter_context(patch.object(reading.classifier, 'model', model))
        yield


@contextmanager
def classifier_with(model: FunctionModel):
    """Every narrow reading answered by one hand-built model (a broken one, a slow one...)."""
    with ExitStack() as stack:
        for reading in ALL:
            stack.enter_context(patch.object(reading.classifier, 'model', model))
        yield


LABELS = {
    'right': ('accept', 'unclear', 'continue'),
    'absolutely': ('accept', 'unclear', 'continue'),
    'actually, move the afternoon one to four o clock': ('unclear', 'move', 'continue'),
    'another one, on top of the nine o clock': ('unclear', 'add', 'continue'),
    'bye': ('unclear', 'unclear', 'end'),
    'can we move it to next monday?': ('unclear', 'move', 'continue'),
    'can you help me moving it to saturday, the same time?': ('unclear', 'move', 'continue'),
    'go ahead': ('accept', 'unclear', 'continue'),
    'go for it': ('accept', 'unclear', 'continue'),
    'goodbye': ('unclear', 'unclear', 'end'),
    "i'd like another one as well": ('unclear', 'add', 'continue'),
    "i'm good, thanks": ('unclear', 'unclear', 'end'),
    "let's move it to saturday, same time": ('unclear', 'move', 'continue'),
    'move it to saturday': ('unclear', 'move', 'continue'),
    'move my haircut to saturday': ('unclear', 'move', 'continue'),
    'no, thanks': ('unclear', 'unclear', 'end'),
    "no, that's all thanks": ('unclear', 'unclear', 'end'),
    'okay': ('accept', 'unclear', 'continue'),
    'perfect': ('accept', 'unclear', 'continue'),
    'sounds good': ('accept', 'unclear', 'continue'),
    'sure, please': ('accept', 'unclear', 'continue'),
    'take care, bye': ('unclear', 'unclear', 'end'),
    'that would be great': ('accept', 'unclear', 'continue'),
    "that's everything, thank you": ('unclear', 'unclear', 'end'),
    'three in the afternoon as well': ('unclear', 'add', 'continue'),
    'yes': ('accept', 'unclear', 'continue'),
    'yes, book it': ('accept', 'unclear', 'continue'),
    'yes, if it is free': ('conditional', 'unclear', 'continue'),
    'yes, that is my number': ('accept', 'unclear', 'continue'),
    "yes, that's right": ('accept', 'unclear', 'continue'),
    "yes, that's right, thank you": ('accept', 'unclear', 'continue'),
}

CHANGES = ('instead', 'as well', 'also')
"""What makes a scripted detour reply change the request it interrupted. The real classifier reads meaning."""


def _chosen(data: dict[str, Any]) -> str:
    """The slot on the table a scripted reply picks: the one whose time it names, if exactly one, and no condition.

    What the real classifier is asked to do, done by rule for scripted tests:
    a named time, or "the same time" as one the caller holds, read against the
    table. The application still checks the answer against the table.
    """
    if 'only if' in data['answer']:
        return ''
    named = set(times_named(data['answer']))
    if 'same time' in data['answer'].casefold():
        named |= {time for held in data['holding'] for time in times_named(held)}
    wanted = {time.replace(':', '') for time in named}
    matches = [o['id'] for o in data['options'] if o['id'].split('T')[1][:4] in wanted]
    return matches[0] if len(matches) == 1 else ''


def default_script(name: str, data: dict[str, Any]) -> str | None:
    """The labels a careful reader gives the utterances the flow tests use, by rule.

    A "no, thanks" ends the call only after a closing question, which the
    confirmations word several ways ("Anything else?", "What else can I do?").
    """
    key = data['answer'].casefold().strip().rstrip('.! ')
    confirmation, choice, conversation = LABELS.get(key, ('unclear', 'unclear', 'continue'))
    if key in ('no, thanks', 'no thanks', 'no thank you'):
        conversation = 'end' if ' else' in data['question'].casefold() else 'continue'
    match name:
        case 'confirmation':
            return confirmation
        case 'appointment_choice':
            return choice
        case 'conversation':
            return conversation
        case 'retained_approval':
            return 'keep'
        case 'chosen_offer':
            return _chosen(data)
        case 'request_change':
            return 'changed' if any(word in data['answer'].casefold() for word in CHANGES) else 'kept'
    return None


@contextmanager
def classifier_model():
    with scripted(default_script):
        yield
