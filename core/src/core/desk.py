"""What every step can do, for any use case: hand the caller on, end the call on their goodbye, clarify.

Legacy: Faro until session 5. The public path (`core.steps`, `core.flowspec`)
never imports this module; a manifest's conversation steps hand over and say
goodbye through their own outputs. `AgentBuilder`, the hand-built step
assembly the salon used until session 4, lives here with the `Desk` it needs.

A use case builds one `Desk` with its own call type, its own `Handoff`, the
names of its actions (at most one runs per model reply) and, optionally, a
typed request a hand-over can carry to one step (`Carry`). The tools it
hands back are the same for every use case; their docstrings are prompt text.

A goodbye is read by the call's own conversation (`CallContext.conversation`):
only its closing reading can say the caller ended the call.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Literal

from pydantic_ai import Agent, ModelRetry, RunContext, Tool, ToolOutput
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage, UsageLimits

from core.graph import Exit, LocalAction, Node, Opening, lent
from core.policies import claim_or_refuse, one_action_per_reply, request_clock, speak_by
from core.state import CallContext, Handoff, VoiceState
from core.steps import AgentStep


@dataclass(frozen=True)
class Carry:
    """A typed request that a hand-over to one step carries, so that step can act on it with no model request.

    Typed while the caller's words are in front of the model. The salon's
    front desk carries the booking asked for to `booking`, which offers times
    for it on its return from verification.
    """

    to: str
    """The step it is carried to. Only a `hand_over` that can reach it gets the argument."""
    name: str
    """The argument's name in `hand_over`."""
    type: Any
    """The argument's type. Its schema is what the model is sent."""
    doc: str
    """The argument's entry in `hand_over`'s Args, appended as is. Prompt text."""
    field: str
    """The state field the carried value is committed to, in the same action as the hand-over."""


def _nothing_blocks(call: Any) -> str:
    return ''


class Desk[C: CallContext]:
    """The actions every step shares, built once per use case from its information."""

    def __init__(
        self,
        *,
        call_type: type[C],
        handoff: type[Handoff],
        actions: frozenset[str],
        carry: Carry | None = None,
        blocked: Callable[[C], str] = _nothing_blocks,
    ) -> None:
        self.call_type = call_type
        self.handoff = handoff
        self.actions = actions
        """The actions at most one of which runs per model reply (`claim_or_refuse`)."""
        self.carry = carry
        self.blocked = blocked
        """Why the caller cannot switch task right now, or ''. The salon's: a write whose outcome is unknown."""
        self._hand_overs: dict[tuple[tuple[Exit, ...], tuple[str, ...]], Callable[..., Any]] = {}

    def can_end(self, call: C) -> bool:
        """Only a bound semantic interpretation can establish an end-of-call request."""
        conversation = call.conversation
        return conversation.label(call, conversation.closing.name) == 'end'

    async def end_conversation(self, call: C, goodbye: str) -> str:
        """Shared executor for a classified closing reply and the model-facing tool."""
        if not self.can_end(call):
            raise ModelRetry('The caller has not clearly ended the call. Answer their request or clarify.')
        call.answered()
        call.end_requested('caller')
        await call.speak_now(goodbye, origin='goodbye')
        await call.end_call()
        return goodbye

    async def switch_task(
        self, call: C, exits: tuple[Exit, ...], target: str, details: str, reply: str
    ) -> Handoff:
        """Abandon the unfinished proposal and move the caller to another task: one application action.

        The executor behind a solver's `SwitchTask`. The edge has to be open from
        this step right now, exactly as `hand_over` checks it, so a switch the
        graph does not allow is refused and the step's own agent answers instead.
        Nothing that already happened is undone: a booking made earlier stands.
        Whatever `blocked` names blocks the switch, such as a write whose outcome
        is unknown, because moving on would let the caller believe nothing was
        done when nobody knows that.

        The details travel as an application result, so the next step reads what
        the caller already said instead of asking again. The move ends the turn:
        the destination's opening speaks if it has one, else `reply` does.
        """
        if why := self.blocked(call):
            raise ModelRetry(why)
        if target == call.state.agent or not any(
            exit.to == target and exit.guard(call.state) for exit in exits
        ):
            raise ModelRetry(
                f'Nothing was switched: {target} is not somewhere this caller can go from here right now.'
            )
        call.answered()
        call.retain_action('hand_over', {'to': target, 'details': details})
        return self.handoff(
            to=target,
            because=details or f'the caller switched to {target}',
            ends_turn=True,
            reply=reply,
        )

    async def clarify(self, call: C, question: str) -> str:
        """Ask the one short question that settles an unclear reply.

        The proposal is consumed, not kept: whatever the caller says next is not an
        answer to a readback they have not heard again, so a yes after this never
        authorises anything until the proposal is put to them afresh.
        """
        if not question.strip() or call.claims_completion(question):
            raise ModelRetry('Nothing was asked: that question was empty or claimed a change nothing made.')
        call.answered()
        await call.speak_now(question, origin='clarify')
        return question

    async def hang_up(self, ctx: RunContext[Any], goodbye: str) -> str:
        """End the call, once the caller's own words say it is over: "no, that's all", "I'm good,
        thanks", "goodbye". A question is never one of those, however tired it sounds: answer it and wait.

        Args:
            goodbye: One short goodbye in your own words. It is the last thing they hear.
        """
        claim_or_refuse(ctx, self.actions)
        call = ctx.deps
        await call.conversation.read(call, usage=ctx.usage, usage_limits=ctx.usage_limits, closing=True)
        return await self.end_conversation(call, goodbye)

    def hand_over_for(self, exits: tuple[Exit, ...], questions: tuple[str, ...] = ()) -> Callable[..., Any]:
        """One step's `hand_over`, carrying only the steps that step may actually reach. Built once per step.

        A shared `hand_over` over every step offers the model routes that are wrong
        on this call, and the self-hop it then has to be told not to take.
        Narrowing the `Literal` per step deletes both: an illegal transition is not
        refused at runtime, it is unrepresentable in the schema the model is sent,
        and Pydantic AI hands back its own retry if a provider ignores the enum.

        The destination is checked again at execution, against the state as it is
        now: the schema that offered it was built before the tools in the same
        reply ran, and one of them can have closed the edge. A handoff paired with
        a readback in the same reply is refused, so the SDK tries the question
        instead of silently skipping it.

        The annotations are built from values, so they cannot be written as
        annotations: under deferred annotation evaluation `Literal[names]` would be
        resolved in a scope where `names` is gone.

        A step that can reach the step a `Carry` goes to also gets its argument,
        typed while the caller's words are in front of the model. Every other
        step's `hand_over` has no such parameter: its signature is trimmed, so the
        schema the model is sent is exactly what it would be without it.
        """
        key = (exits, questions)
        if (built := self._hand_overs.get(key)) is None:
            built = self._hand_overs[key] = self._hand_over(exits, questions)
        return built

    def _hand_over(self, exits: tuple[Exit, ...], questions: tuple[str, ...]) -> Callable[..., Any]:
        names = tuple(dict.fromkeys(exit.to for exit in exits))
        carry = self.carry if self.carry is not None and self.carry.to in names else None
        actions = self.actions
        handoff = self.handoff

        async def hand_over(ctx, to, because, **carried) -> Handoff:
            """Give the caller to another agent, right now, for something that is not yours to do.

            This is the only way to move a caller. Writing a sentence about transferring
            them does nothing at all: the caller hears you say it and then sits there,
            still with you, while nobody does the thing. So do this instead of saying
            it, and say nothing about it afterwards -- they hear one short line and
            carry straight on in the same conversation.

            Args:
                to: The agent that takes it from here.
                because: One short sentence: what the caller wants. Never spoken.
            """
            if (
                ctx.messages
                and isinstance(reply := ctx.messages[-1], ModelResponse)
                and any(
                    isinstance(part, ToolCallPart) and part.tool_name in questions for part in reply.parts
                )
            ):
                raise ModelRetry(
                    'Ask the readback here and wait for the caller. Do not hand over in that reply.'
                )
            claim_or_refuse(ctx, actions)
            if not any(exit.to == to and exit.guard(ctx.deps.state) for exit in exits):
                raise ModelRetry(
                    f'{to} is not somewhere this caller can go from here right now. '
                    'Nothing was done. Answer them yourself, or pick a destination that is open.'
                )
            if carry is not None and to == carry.to and (value := carried.get(carry.name)) is not None:
                ctx.deps.commit(**{carry.field: value})
            return handoff(to=to, because=because)

        hand_over.__annotations__ = {
            'ctx': RunContext[self.call_type],  # ty: ignore[invalid-type-form]
            'to': Literal[names],  # ty: ignore[invalid-type-form]
            'because': str,
            **({carry.name: carry.type | None} if carry is not None else {}),
            'return': handoff,
        }
        signature = inspect.signature(hand_over)
        parameters = [p for p in signature.parameters.values() if p.kind is not inspect.Parameter.VAR_KEYWORD]
        if carry is not None:
            parameters.append(
                inspect.Parameter(
                    carry.name, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=carry.type | None
                )
            )
            hand_over.__doc__ = f'{inspect.cleandoc(hand_over.__doc__ or "")}\n{carry.doc}'
        hand_over.__signature__ = signature.replace(parameters=parameters)  # ty: ignore[unresolved-attribute]
        return hand_over


def _moved_this_run(messages: Sequence[ModelMessage], borrowed: Mapping[str, Any]) -> bool:
    """Whether a borrowed tool has returned since the caller's words: the move it stands for is made."""
    for message in reversed(messages):
        if not isinstance(message, ModelRequest):
            continue
        if any(isinstance(p, ToolReturnPart) and p.tool_name in borrowed for p in message.parts):
            return True
        if any(isinstance(p, UserPromptPart) for p in message.parts):
            return False
    return False


class AgentBuilder[C: CallContext]:
    """Builds a hand-built step's agent the same way for every use case.

    A use case gives it its call type, model and settings, the names of its
    actions (at most one runs per model reply), its `Desk`, its `advance` (what
    decides a turn before any model runs) and the request by which a run must
    answer with what it holds. Each step then passes only what its own code
    can say (`build`).
    """

    def __init__(
        self,
        *,
        call_type: type[C],
        model: Model | str,
        settings: ModelSettings | None,
        actions: frozenset[str],
        desk: Desk[C],
        advance: Callable[[Any, RunUsage, UsageLimits], Awaitable[LocalAction | None]] | None,
        speak_by: int,
    ) -> None:
        self.call_type = call_type
        self.model = model
        self.settings = settings
        self.actions = actions
        self.desk = desk
        self.advance = advance
        self.speak_by = speak_by

    def build(
        self,
        step: AgentStep,
        tools: Sequence[Any] = (),
        finishers: Sequence[Any] = (),
        asks: Sequence[Any] = (),
        openings: tuple[Opening[Any], ...] = (),
        structured_speech: bool = False,
        offered_when: Mapping[str, Callable[[Any], bool]] | None = None,
        stand_ins: Mapping[Callable[..., Any], Callable[..., Any]] | None = None,
        model: Model | str | None = None,
        shares_detour: bool = False,
    ) -> Node[Any]:
        """One step: its agent, its legal exits, and what has to hold before it runs.

        The step's name, prompt, exits and requirements come from its entry in
        the flow file; the arguments here are what only its code can say.
        Everything that differs between steps is an argument, so this names no
        step and no tool: `structured_speech` for a step that speaks only
        through its outputs, `offered_when` for an output offered only while a
        fact holds, and `stand_ins` for an output whose name the model knows but
        whose implementation is this step's own.

        Every output tool is named explicitly. Pydantic AI calls a lone output
        function `final_result` and renames a bare `hand_over` to
        `final_result_hand_over` as soon as there is more than one output, which
        the per-request narrowing then fails to find by name. Everything that
        writes is `sequential`, so two of them can never overlap; the conflict
        policy refusing the second is the brace to that belt.

        A question ends the run the same way finishing the step does, and for the
        same reason: the words are already decided, so a further model request
        would only be a chance to word them differently. Questions are not
        actions, because asking changes nothing and writes nothing.

        `str` in the output list is what keeps speech streaming to TTS. A step
        that can write or verify speaks only through its output functions, so it
        does not get it and provider text never reaches TTS from it.

        The whole output set is always declared; what varies per request is which
        of it is *offered*, decided by the two prepare hooks below.
        """
        exits, actions = step.exits, self.actions
        borrowed = lent(exits)
        named = [
            ToolOutput((stand_ins or {}).get(fn, fn), name=fn.__name__, sequential=fn.__name__ in actions)
            for fn in finishers
        ]
        questions = [ToolOutput(fn, name=fn.__name__) for fn in asks]
        handoff = (
            [
                ToolOutput(
                    self.desk.hand_over_for(exits, tuple(fn.__name__ for fn in asks)),
                    name='hand_over',
                    sequential=True,
                )
            ]
            if exits
            else []
        )
        offered = offered_when or {}
        declared = [*([str] if not structured_speech else []), *handoff, *named, *questions]

        def outputs(state: VoiceState) -> list:
            """What this step may return: the superset `narrow` picks from at each request."""
            return declared

        async def only_reachable(ctx: RunContext[Any], defs: list[ToolDefinition]) -> list[ToolDefinition]:
            """Take a borrowed tool back out while the edge it stands for is shut.

            A caller the step cannot yet serve is not offered the tool the edge
            lends, so the model cannot call it and be refused, which costs the
            round trip the lending was there to save. Re-run before every model
            request, so a tool that ran a moment ago and changed the state is
            reflected in the very next schema.
            """
            return [d for d in defs if d.name not in borrowed or borrowed[d.name].guard(ctx.deps.state)]

        async def narrow(ctx: RunContext[Any], defs: list[ToolDefinition]) -> list[ToolDefinition]:
            """Offer only the destinations that are open at this request.

            The output-tool half of `only_reachable`, and a separate hook because
            Pydantic AI prepares ordinary and output tools through different ones.
            An exit whose guard has stopped holding leaves the enum the model is
            sent, so it cannot be chosen rather than being refused after it was;
            with none left, `hand_over` goes entirely. An output with an
            `offered_when` fact is offered only while it holds.

            Once a borrowed tool has returned in this run, `hand_over` goes too:
            the tool's success already moves the caller (`moved_by`), so a
            hand-over after it is a second request for a move already made.
            The user's call of 24 September read the diary from the front desk,
            then handed over to booking as well.
            """
            legal = list(dict.fromkeys(e.to for e in exits if e.guard(ctx.deps.state)))
            if _moved_this_run(ctx.messages, borrowed):
                legal = []
            kept = []
            for tool_def in defs:
                if (when := offered.get(tool_def.name)) is not None and not when(ctx.deps):
                    continue
                if tool_def.name != 'hand_over':
                    kept.append(tool_def)
                    continue
                if not legal:
                    continue
                schema = deepcopy(tool_def.parameters_json_schema)
                if (to := schema.get('properties', {}).get('to')) is not None:
                    to.pop('const', None)
                    to['enum'] = legal
                kept.append(replace(tool_def, parameters_json_schema=schema))
            return kept

        return Node(
            Agent[Any, object](
                model if model is not None else self.model,
                name=step.id,
                deps_type=self.call_type,
                output_type=declared,
                instructions=step.instructions,
                tools=[
                    *(
                        fn if isinstance(fn, Tool) else Tool(fn, sequential=fn.__name__ in actions)
                        for fn in tools
                    ),
                    *(Tool(fn, sequential=fn.__name__ in actions) for exit in exits for fn in exit.via),
                ],
                capabilities=[
                    one_action_per_reply(actions),
                    Hooks(prepare_tools=only_reachable, prepare_output_tools=narrow),
                    *([speak_by(self.speak_by)] if not structured_speech else []),
                    request_clock(),
                ],
                model_settings=self.settings,
            ),
            exits=exits,
            requires=step.requires,
            satisfied_by=step.satisfied_by,
            outputs=outputs,
            guarded=step.guarded,
            shares_detour=shares_detour,
            model_speech=not structured_speech,
            advance=self.advance,
            openings=openings,
        )
