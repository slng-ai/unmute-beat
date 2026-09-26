"""Request-only projections of the call's protocol and delivered speech.

The protocol is retained for audit. Projection copies it: a reset or playback
correction must never rewrite another step's source history.

The copy is shallow: every projected message is a new object with a new
parts list (`dataclasses.replace`), and no part is ever changed in place, so
the source messages and their parts are never touched. A deep copy of the
whole history on every model request cost 0.12 s of CPU over ten N12
booking traces under the profiler, and bought nothing (session 4c).
"""

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
    for message in messages:
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
    the next caller input. Speech filed under the request now going out (a
    holding line said as it starts) is left out of it: it joins that
    request's response when there is one. Inserted as a stray response, it
    went in front of the caller's words.

    A holding line (`tool_filler`) is never sent: it answers nothing, and a
    model that saw "One moment." as its own words wrote it into its replies
    (32 replies in the 20-call baseline of session 4, then said twice).

    Caller turn IDs deduplicate reset/handoff inputs: within one turn, caller
    words already sent are not sent again, whether repeated as they were or
    as the whole turn joined up. A turn that arrived in pieces keeps every
    piece, because each one holds words the others do not.
    """
    speech = [
        s
        for s in call.all_spoken
        if s.id not in excluded and s.id not in call._hidden_speech and s.origin != 'tool_filler'
    ]
    by_id = {s.id: s for s in speech}
    result = []
    present = set()
    callers: dict[int, list[str]] = {}
    for source in messages:
        message = replace(source, parts=list(source.parts))
        meta = message.metadata or {}
        turn = meta.get('caller_turn')
        if turn is not None and isinstance(message, ModelRequest):
            sent = callers.setdefault(turn, [])
            kept = []
            for part in message.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    if part.content in sent or (sent and part.content == ' '.join(sent)):
                        continue
                    sent.append(part.content)
                kept.append(part)
            message = replace(message, parts=kept)
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
        if (
            segment.id in present
            or not segment.text
            or (segment.response and segment.response == call._response)
        ):
            continue
        message = ModelResponse(parts=[TextPart(segment.text)], metadata={'speech_ids': [segment.id]})
        at = len(result)
        if at and isinstance(result[-1], ModelRequest):
            at -= 1
        result.insert(at, message)
        present.add(segment.id)
    return result
