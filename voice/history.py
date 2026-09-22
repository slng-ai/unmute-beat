"""Request-only projections of the call's protocol and delivered speech.

The protocol is retained for audit. Projection copies it: a reset or playback
correction must never rewrite another step's source history.
"""

from copy import deepcopy
from dataclasses import replace

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)


def paired(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Keep complete tool exchanges; never send an orphan left by cancellation."""
    calls = {p.tool_call_id for m in messages for p in m.parts if isinstance(p, ToolCallPart)}
    results = {
        p.tool_call_id
        for m in messages
        for p in m.parts
        if isinstance(p, (ToolReturnPart, RetryPromptPart)) and p.tool_call_id
    }
    complete = calls & results
    projected = []
    for message in deepcopy(messages):
        parts = [
            p
            for p in message.parts
            if not isinstance(p, (ToolCallPart, ToolReturnPart, RetryPromptPart))
            or not p.tool_call_id
            or p.tool_call_id in complete
        ]
        message = replace(message, parts=parts)
        if message.parts:
            projected.append(message)
    return projected


def project(
    call, messages: list[ModelMessage], *, excluded: frozenset[str] = frozenset()
) -> list[ModelMessage]:
    """Merge speech by response/segment IDs, with no mutation of source history.

    Fixed speech shares its producing response so the following tool result
    stays last. Google rejects requests ending with an assistant message.
    Speech left by cancellation without a protocol response is inserted before
    the next caller input. Caller turn IDs deduplicate reset/handoff inputs.
    """
    speech = [s for s in call.all_spoken if s.id not in excluded and s.id not in call._hidden_speech]
    by_id = {s.id: s for s in speech}
    result = []
    present = set()
    callers = set()
    for source in messages:
        message = deepcopy(source)
        meta = message.metadata or {}
        turn = meta.get('caller_turn')
        if turn is not None and isinstance(message, ModelRequest):
            if turn in callers:
                message = replace(
                    message, parts=[p for p in message.parts if not isinstance(p, UserPromptPart)]
                )
            callers.add(turn)
        ids = meta.get('speech_ids', [])
        response = meta.get('voice_response')
        if isinstance(message, ModelResponse) and (ids or response):
            ids = list(dict.fromkeys([*ids, *(s.id for s in speech if response and s.response == response)]))
            present.update(ids)
            text = ''.join(by_id[i].text for i in ids if i in by_id)
            message.metadata = meta | {'speech_ids': ids}
            parts = [p for p in message.parts if not isinstance(p, TextPart)]
            message = replace(message, parts=[TextPart(text), *parts] if text else parts)
        if message.parts:
            result.append(message)
    for segment in speech:
        if segment.id in present or not segment.text:
            continue
        message = ModelResponse(parts=[TextPart(segment.text)], metadata={'speech_ids': [segment.id]})
        at = len(result)
        if at and isinstance(result[-1], ModelRequest):
            at -= 1
        result.insert(at, message)
        present.add(segment.id)
    return result
