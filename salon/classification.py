"""The salon's semantic decision boundary, independent of its conversational agents."""

import time
from typing import TYPE_CHECKING

from httpx import HTTPError
from loguru import logger
from pydantic import ValidationError
from pydantic_ai import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.models import infer_model
from pydantic_ai.models.google import GoogleModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits

from core.classifiers import Classifier
from core.state import Request
from salon.models.consent import ConsentInput, ReplyClassification

if TYPE_CHECKING:
    from salon.models.state import SalonCall


MODEL = infer_model('google-cloud:gemini-3.5-flash-lite')
"""One model for now. `build` takes it per agent, so giving one step a different model is a one-word edit."""
SETTINGS = GoogleModelSettings(
    service_tier='priority',
    google_thinking_config={'thinking_level': 'MINIMAL'},  # ty: ignore[invalid-argument-type]
)
"""Thinking pinned off rather than left out.

Left out, no `thinking_config` is sent and whatever Google currently defaults
this model to is what a call gets, a setting that can change under us on the
one dimension a voice turn cannot absorb.
"""


CLASSIFIER = Classifier(MODEL, ConsentInput, ReplyClassification, model_settings=SETTINGS)


async def classify_reply(
    call: SalonCall,
    *,
    usage: RunUsage | None = None,
    usage_limits: UsageLimits | None = None,
) -> ReplyClassification:
    """Classify at most once per accepted turn, never publishing a late or rebound answer.

    Consumers recheck the snapshot against the live call. Failed interpretations
    are cached too, so a tool retry cannot turn uncertainty into repeated guesses.
    """
    if call._classified_turn == call.turn or not call.heard.strip():
        return call.classification
    turn, generation = call.turn, call._generation
    if usage is not None and usage_limits is not None:
        usage_limits.check_before_request(usage)
    previous = [segment for segment in call.all_spoken if segment.turn < call.turn]
    last_turn = max((segment.turn for segment in previous), default=-1)
    previous_speech = ' '.join(segment.text for segment in previous if segment.turn == last_turn).strip()
    snapshot = ConsentInput(
        utterance=call.heard,
        previous_speech=previous_speech,
        owner=call.state.customer_phone,
        pending=call.pending_question(),
        retained_complaint=call._complaint,
    ).model_copy(deep=True)
    call._classified_turn = turn
    call._classification_input = snapshot
    if call.attempt == 0:
        call.retain_input()
    request = Request(started=time.perf_counter(), kind='classifier')
    call.requests.append(request)
    try:
        result = await CLASSIFIER.classify(snapshot, usage=usage, usage_limits=usage_limits)
    except (ValidationError, UnexpectedModelBehavior, ModelHTTPError, HTTPError) as error:
        logger.warning('consent classification failed: {}', type(error).__name__)
        result = ReplyClassification()
        request.fallback = True
    finally:
        request.duration = time.perf_counter() - request.started
    request.labels = result.model_dump_json()
    if call.turn != turn or not call.is_current(generation):
        return ReplyClassification()
    call._classification = result
    result = call.classification
    if call._complaint is not None and result.retained_approval != 'keep':
        call._complaint = None
    if (question := call.pending('complaint')) and result.confirmation == 'accept':
        call._complaint = question.model_copy(update={'owner': call.state.customer_phone})
    return result
