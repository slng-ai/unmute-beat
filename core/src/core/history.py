"""Request-only projections of the call's protocol and delivered speech.

The protocol is retained for audit. Projection copies it: a reset or playback
correction must never rewrite another step's source history.

The copy is shallow: every projected message is a new object with a new
parts list (`dataclasses.replace`), and no part is ever changed in place, so
the source messages and their parts are never touched. A deep copy of the
whole history on every model request cost 0.12 s of CPU over ten N12
booking traces under the profiler, and bought nothing (session 4c).
"""

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from typing import Annotated, Any, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolAvailabilityDeltaPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.tools import ToolDefinition

_TOOLS = TypeAdapter(list[ToolDefinition])

_VOLATILE = frozenset({'timestamp', 'tool_call_id', 'run_id', 'conversation_id', 'metadata'})
"""Keys that differ between identical requests, or that no provider adapter sends: left out of a `Payload`."""


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _stable(v) for k, v in value.items() if k not in _VOLATILE and not k.startswith('provider_')
        }
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


@dataclass(frozen=True)
class Payload:
    """One model request as the model boundary received it, as text: what a test or report measures and searches.

    Captured from a `FunctionModel`'s `(messages, info)`, so it is the
    Pydantic AI model boundary, not the provider's wire format: the adapter's
    own serialization is not in it. Each component is canonical JSON with the
    volatile keys left out (`_VOLATILE`, `provider_*`), so the same request
    gives the same bytes on every run and before and after a change.
    """

    instructions: str
    history: str
    tools: str
    outputs: str

    @classmethod
    def of(cls, messages: list[ModelMessage], info: AgentInfo) -> Payload:
        def canonical(value: Any) -> str:
            return json.dumps(_stable(value), sort_keys=True, ensure_ascii=False)

        return cls(
            instructions=info.instructions or '',
            history=canonical(ModelMessagesTypeAdapter.dump_python(list(messages), mode='json')),
            tools=canonical(_TOOLS.dump_python(info.function_tools, mode='json')),
            outputs=canonical(
                {
                    'text': info.allow_text_output,
                    'tools': _TOOLS.dump_python(info.output_tools, mode='json'),
                }
            ),
        )

    def sizes(self) -> dict[str, int]:
        """Bytes (UTF-8) of each component and of the whole request. Bytes, not tokens."""
        found = {
            name: len(getattr(self, name).encode())
            for name in ('instructions', 'history', 'tools', 'outputs')
        }
        return found | {'total': sum(found.values())}

    def results(self) -> int:
        """Bytes (UTF-8) of the tool results inside `history`: a part of it, left out of `sizes` so pins keep."""
        return sum(
            len(json.dumps(part['content'], sort_keys=True, ensure_ascii=False).encode())
            for message in json.loads(self.history)
            for part in message['parts']
            if part.get('part_kind') == 'tool-return'
        )

    def pinned(self) -> dict[str, Any]:
        """The request as readable data, for a golden file whose diff is the review of any change."""
        return {
            'instructions': self.instructions.splitlines(),
            'history': json.loads(self.history),
            'tools': json.loads(self.tools),
            'outputs': json.loads(self.outputs),
            'bytes': self.sizes(),
        }

    def __contains__(self, text: str) -> bool:
        return any(text in part for part in (self.instructions, self.history, self.tools, self.outputs))


def paired(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Keep complete tool exchanges; never send an orphan left by cancellation.

    Two requests in a row become one, their metadata kept, the later's
    winning. A structured run ends on its output's return request, and the
    next turn's caller words follow it: Pydantic AI merges the two before a
    request and drops the metadata, so every earlier turn lost its
    `caller_turn` and counted as the current one (a `turns:` window sent
    everything).
    """
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
        if not message.parts:
            continue
        last = projected[-1] if projected else None
        if isinstance(message, ModelRequest) and isinstance(last, ModelRequest):
            message = replace(
                message,
                parts=[*last.parts, *message.parts],
                instructions=message.instructions or last.instructions,
                metadata=(last.metadata or {}) | (message.metadata or {}) or None,
            )
            projected[-1] = message
        else:
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
            before = list(sent)
            kept = []
            for part in message.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    if part.content in before or (before and part.content == ' '.join(before)):
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
        if parts:
            _join(result, message, parts)
    return [*result, *messages[start:]]


def _capability(part: ToolCallPart) -> str:
    try:
        return str(part.args_as_dict().get('id', ''))
    except ValueError, AssertionError:
        return ''


def _join(result: list[ModelMessage], message: ModelMessage, parts: list[Any]) -> None:
    """Add a filtered message, joined to the one before it when both are requests or both responses."""
    if result and type(result[-1]) is type(message):
        result[-1] = replace(result[-1], parts=[*result[-1].parts, *parts])
    else:
        result.append(replace(message, parts=parts))


def _is_tool(part: Any) -> bool:
    return isinstance(part, (ToolCallPart, ToolReturnPart, RetryPromptPart))


def _control(part: Any, loads: Collection[str]) -> bool:
    """A capability's control state: its `load_capability` call or return, or the tools it revealed."""
    return isinstance(part, ToolAvailabilityDeltaPart) or getattr(part, 'tool_call_id', None) in loads


def _turns(call, messages: list[ModelMessage]) -> list[int]:
    """The caller turn each message belongs to: its request's `caller_turn`, carried on to what follows it.

    A request with the caller's words and no tag yet is the run going out now,
    so it is this turn's (`_caller_turn`); a request of tool results and a
    response belong to the turn before them.
    """
    found, turn = [], 0
    for message in messages:
        if isinstance(message, ModelRequest):
            tagged = (message.metadata or {}).get('caller_turn')
            if tagged is not None:
                turn = tagged
            elif any(isinstance(p, UserPromptPart) for p in message.parts):
                turn = call.turn
        found.append(turn)
    return found


class History(BaseModel):
    """`context: {history: {scope: node, turns: 8}}`: which earlier caller turns a step is sent, as whole turns.

    `scope: node` sends the caller turns this step took part in, on any visit:
    a turn where one of its runs answered, or where code spoke as this step.
    The step's id decides, never its persona: two steps sharing a prompt keep
    apart. `scope: conversation` sends every earlier turn. `turns` keeps only
    the most recent of those; the current turn is always sent on top. It
    counts caller turns, not messages or tokens.

    Like `history: messages`, earlier turns are sent as what was said and
    heard: the caller's words and the speech they were sent, with tool
    exchanges and application results left out. In the current turn, only
    this run's own tool exchange is sent; another step's from the same caller
    turn is not (a parent's `hand_over` before a consultation).

    Some turns are kept whatever the window, because the step cannot do its
    task right without them (`window`): the turn the caller asked for this
    step, even when a prerequisite ran first; the turn its own pending
    question was put, and every turn since (the caller's corrections). The
    `load_capability` records of the capabilities it registers are kept from
    any turn, so a loaded capability is not loaded again. Anchors can exceed
    `turns`; the turns kept past it are logged.

    This minimises context. It is not a confidentiality boundary: words the
    caller says, and speech sent to them, may name anything, and are sent as
    said. Nothing is removed from the call's retained history.

    A history object opts the step into the scoped view (`Scope`).
    """

    model_config = ConfigDict(extra='forbid', frozen=True)
    scope: Literal['node', 'conversation'] = 'node'
    turns: Annotated[int, Field(gt=0, strict=True)] | None = None
    """Recent eligible caller turns sent before the current one. None: all of them."""

    def window(
        self, call, messages: list[ModelMessage], step: str, owned: Collection[str] = ()
    ) -> tuple[frozenset[int], frozenset[int]]:
        """The earlier caller turns sent to `step`, and those of them kept only as anchors, past the window.

        A turn a consultation handled for `step` (`metadata['for']`) is one
        it took part in: what the caller said to the specialist, a change to
        their request among it, is theirs to `step`, whatever the specialist
        made of it.
        """
        turns = _turns(call, messages)
        earlier = sorted({t for t in turns if t < call.turn})
        took_part = {
            t
            for m, t in zip(messages, turns, strict=True)
            if step in ((m.metadata or {}).get('step'), (m.metadata or {}).get('for'))
        }
        took_part |= {s.turn for s in call.all_spoken if s.step == step}
        eligible = [t for t in earlier if self.scope == 'conversation' or t in took_part]
        recent = set(eligible if self.turns is None else eligible[-self.turns :])
        anchors = set()
        arrived = call._arrived.get(step)
        if arrived is not None and arrived < call.turn:
            anchors.add(arrived)
        asked = call._proposals.asked
        if asked is not None and getattr(asked, 'about', '') in owned:
            anchors |= {t for t in earlier if t >= asked.turn}
        return frozenset(recent | anchors), frozenset(anchors - recent)

    def select(
        self,
        call,
        messages: list[ModelMessage],
        *,
        step: str,
        loaded: Collection[str] = (),
        owned: Collection[str] = (),
    ) -> list[ModelMessage]:
        """What `step` is sent of `messages` (already `project`ed): see the class.

        A message a run of this call already kept carries its step
        (`metadata['step']`); one that does not is the run going out now, and
        only its tool exchange is sent whole. No message keeps its
        `instructions`: the request going out gets its own from the request
        parameters, and an earlier request's are another step's application
        state.

        A step that takes over within the turn (a prerequisite returning, a
        hand-over) continues from the other run's tool results, which are not
        its to see. When leaving them out ends the history on speech, the
        caller's words of this turn are sent again last, as the turn engine
        does for a run with no prompt (`core.turn`): a request cannot end on
        the model's own reply.
        """
        kept, beyond = self.window(call, messages, step, owned)
        if beyond:
            logger.info(
                'history for {} keeps turns {} beyond its window of {}', step, sorted(beyond), self.turns
            )
        loads = {
            p.tool_call_id
            for m in messages
            for p in m.parts
            if getattr(p, 'tool_kind', None) == 'capability-load'
            and isinstance(p, ToolCallPart)
            and _capability(p) in loaded
        }
        result: list[ModelMessage] = []
        for message, turn in zip(messages, _turns(call, messages), strict=True):
            meta = message.metadata or {}
            own = 'step' not in meta
            if turn != call.turn and turn not in kept:
                parts = [p for p in message.parts if _control(p, loads)]
            else:
                parts = [
                    p
                    for p in message.parts
                    if _control(p, loads)
                    or (own and _is_tool(p))
                    or (
                        not _is_tool(p)
                        and not isinstance(p, ToolAvailabilityDeltaPart)
                        and not (isinstance(p, SystemPromptPart) and meta.get('application_action'))
                    )
                ]
            if parts:
                if isinstance(message, ModelRequest):
                    message = replace(message, instructions=None)
                _join(result, message, parts)
        if result and isinstance(result[-1], ModelResponse) and call.heard:
            result.append(ModelRequest(parts=[UserPromptPart(call.heard)]))
        return result


@dataclass(frozen=True)
class Scope:
    """What a scoped step's automatic context may show of the call: what `core.steps.context` asks.

    Built at compile from the step's manifest entry. The call keeps
    everything; this only decides what the step is told. `facts` are the
    state fields it sees (None: all), `fields` those it may write,
    `operations` those it owns (offers them, carries them out, opens with
    them). `binds` and `assign` are each owned operation's argument sources
    and receipt assignments, by top-level state field: an argument is shown
    only when its source field is, and a receipt only through the visible
    fields it set, never whole.

    Seeing a field does not let the step write it, and writing one does not
    let it see it: `fields` and `facts` are declared apart.
    """

    facts: tuple[str, ...] | None
    fields: frozenset[str] = frozenset()
    operations: frozenset[str] = frozenset()
    binds: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    assign: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def shows(self, name: str) -> bool:
        return self.facts is None or name in self.facts

    def owns(self, operation: str) -> bool:
        return operation in self.operations

    def offers(self, offered: Mapping[str, Any]) -> dict[str, Any]:
        """The offers for choices this step records and sees; another step's offer stays with that step."""
        return {name: ids for name, ids in offered.items() if name in self.fields and self.shows(name)}

    def public_args(self, operation: str, args: Mapping[str, Any] | str) -> dict[str, Any]:
        """An owned operation's arguments, only those bound from a field this step sees (a hidden identity is not)."""
        found = json.loads(args) if isinstance(args, str) else dict(args)
        binds = self.binds.get(operation, {})
        return {name: value for name, value in found.items() if self.shows(binds.get(name, name))}

    def public_receipt(self, operation: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
        """What an owned operation's receipt set in the fields this step sees."""
        return {
            name: receipt if source == '*' else receipt.get(source)
            for name, source in self.assign.get(operation, {}).items()
            if self.shows(name)
        }
