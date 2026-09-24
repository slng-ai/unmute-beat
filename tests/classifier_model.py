"""Scripted interpretations for existing flow tests, not a language-understanding test.

These recorded labels replace the former word-list assumptions. Live semantic
accuracy is evaluated separately; this fake exercises the real request boundary.
"""

from contextlib import contextmanager
from unittest.mock import patch

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from salon.classification import CLASSIFIER
from salon.diary import times_named
from salon.models.consent import ConsentInput, ReplyClassification

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


def _change(data: ConsentInput) -> str:
    """Whether the caller kept the interrupted request, by rule for scripted tests."""
    if not data.detour_request:
        return 'unclear'
    said = ' '.join(data.detour_words).casefold()
    return 'changed' if any(word in said for word in CHANGES) else 'kept'


def _chosen(data: ConsentInput) -> str:
    """The slot on the table a scripted reply picks: the one whose time it names, if exactly one, and no condition.

    What the real classifier is asked to do, done by rule for scripted tests:
    a named time, or "the same time" as one the caller holds, read against the
    table. The application still checks the answer against the table.
    """
    if not data.offered or (data.pending is not None and data.pending.about not in ('offer', 'closing')):
        return ''
    if 'only if' in data.utterance:
        return ''
    named = set(times_named(data.utterance))
    if 'same time' in data.utterance.casefold():
        named |= {time for held in data.holding for time in times_named(held)}
    wanted = {time.replace(':', '') for time in named}
    matches = [o.slot_id for o in data.offered if o.slot_id.split('T')[1][:4] in wanted]
    return matches[0] if len(matches) == 1 else ''


def classify(messages, info):
    data = ConsentInput.model_validate_json(messages[-1].parts[0].content)
    key = data.utterance.casefold().strip().rstrip('.! ')
    confirmation, choice, conversation = LABELS.get(key, ('unclear', 'unclear', 'continue'))
    if data.pending is None or data.pending.about not in ('phone', 'booking', 'complaint'):
        confirmation = 'not_applicable'
    if key in ('no, thanks', 'no thanks', 'no thank you'):
        conversation = (
            'end'
            if (data.pending and data.pending.about == 'closing') or data.previous_speech == 'Anything else?'
            else 'continue'
        )
    result = ReplyClassification.model_validate(
        {
            'confirmation': confirmation,
            'appointment_choice': choice,
            'conversation': conversation,
            'retained_approval': 'keep' if data.retained_complaint else 'unclear',
            'chosen_offer': _chosen(data),
            'request_change': _change(data),
        }
    )
    return ModelResponse(parts=[TextPart(result.model_dump_json())])


@contextmanager
def classifier_model():
    with patch.object(CLASSIFIER, 'model', FunctionModel(function=classify)):
        yield
