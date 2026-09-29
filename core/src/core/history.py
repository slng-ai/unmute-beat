"""Request-only projections of the call's protocol and delivered speech.

The protocol is retained for audit. Projection copies it: a reset or playback
correction must never rewrite another step's source history.

The copy is shallow: every projected message is a new object with a new
parts list (`dataclasses.replace`), and no part is ever changed in place, so
the source messages and their parts are never touched. A deep copy of the
whole history on every model request cost 0.12 s of CPU over ten N12
booking traces under the profiler, and bought nothing (session 4c).
"""

from collections.abc import Collection
from dataclasses import replace

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
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
    Speech left by cancellation without a protocol response goes where it was
    said: before the caller's words of the next turn, or before the latest
    request when it is this turn's. When that request holds tool results, the
    line joins the response that called the tools, so the pair stays whole.
    Put before the latest request whatever its turn, a line five turns old
    was sent as the agent's last words on every request after it (live call
    CAd33c3c, session 4d: "sorry no" read as the answer to "Anything else?").
    Speech filed under the request now going out (a
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
        at = next((i for i, m in enumerate(result) if _caller_turn(call, m) > segment.turn), None)
        if at is None:
            at = len(result) - 1 if result and isinstance(result[-1], ModelRequest) else len(result)
        before = result[at - 1] if at else None
        if isinstance(before, ModelResponse) and at < len(result) and _answers_tools(result[at]):
            said = ''.join(p.content for p in before.parts if isinstance(p, TextPart))
            parts = [TextPart(said + segment.text), *(p for p in before.parts if not isinstance(p, TextPart))]
            ids = [*(before.metadata or {}).get('speech_ids', []), segment.id]
            result[at - 1] = replace(
                before, parts=parts, metadata=(before.metadata or {}) | {'speech_ids': ids}
            )
        else:
            message = ModelResponse(parts=[TextPart(segment.text)], metadata={'speech_ids': [segment.id]})
            result.insert(at, message)
        present.add(segment.id)
    return result


def _caller_turn(call, message: ModelMessage) -> int:
    """The caller turn a request carries words from; 0 for anything else.

    A request of the run now going out has no `caller_turn` yet (a run's
    messages are tagged when it is kept), so its caller words are this turn's.
    """
    if not isinstance(message, ModelRequest):
        return 0
    turn = (message.metadata or {}).get('caller_turn')
    if turn is None and any(isinstance(p, UserPromptPart) for p in message.parts):
        return call.turn
    return turn or 0


def _answers_tools(message: ModelMessage) -> bool:
    """Whether a request holds tool results, which must follow the response that called them."""
    return any(isinstance(p, (ToolReturnPart, RetryPromptPart)) for p in message.parts)


def conversation(call, messages: list[ModelMessage], *, loaded: Collection[str] = ()) -> list[ModelMessage]:
    """`history: messages`: earlier caller turns as the conversation alone, what was said and heard.

    Before this caller turn, only the caller's words and the agent's speech
    are kept: `project` has already put the speech that was delivered, the
    read-backs, receipts and fixed lines, into the responses as text. Tool
    calls, tool results, retries and application results of earlier turns
    are left out. This turn's messages, and the run going out now, are kept
    whole, so the SDK can finish its own tool loop.

    One exception, so a loaded capability is not paid for twice: a
    `load_capability` call and its return are kept, as a pair, when the
    capability is one this step registers (`loaded`). Pydantic AI finds what
    is loaded, and the bundle's instructions, in exactly that pair. A step
    that does not register it never sees it.

    Adjacent requests, or adjacent responses, left by the filter are joined
    into one, so the roles still alternate.
    """
    start = next(
        (
            i
            for i, m in enumerate(messages)
            if isinstance(m, ModelRequest) and ((m.metadata or {}).get('caller_turn') or 0) >= call.turn
        ),
        None,
    )
    if start is None:
        start = next(
            (
                i
                for i in range(len(messages) - 1, -1, -1)
                if isinstance(messages[i], ModelRequest)
                and any(isinstance(p, UserPromptPart) for p in messages[i].parts)
            ),
            len(messages),
        )
    kept = {
        p.tool_call_id
        for m in messages[:start]
        for p in m.parts
        if getattr(p, 'tool_kind', None) == 'capability-load'
        and isinstance(p, ToolCallPart)
        and _capability(p) in loaded
    }
    result: list[ModelMessage] = []
    for message in messages[:start]:
        parts = [
            p
            for p in message.parts
            if not isinstance(p, (ToolCallPart, ToolReturnPart, RetryPromptPart, SystemPromptPart))
            or (isinstance(p, SystemPromptPart) and not (message.metadata or {}).get('application_action'))
            or getattr(p, 'tool_call_id', None) in kept
        ]
        if not parts:
            continue
        if result and type(result[-1]) is type(message):
            result[-1] = replace(result[-1], parts=[*result[-1].parts, *parts])
        else:
            result.append(replace(message, parts=parts))
    return [*result, *messages[start:]]


def _capability(part: ToolCallPart) -> str:
    try:
        return str(part.args_as_dict().get('id', ''))
    except ValueError, AssertionError:
        return ''
