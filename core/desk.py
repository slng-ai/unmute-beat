"""What every step can do, for any use case: hand the caller on, end the call on their goodbye, clarify.

A use case builds one `Desk` with its own call type, its own `Handoff`, the
names of its actions (at most one runs per model reply) and, optionally, a
typed request a hand-over can carry to one step (`Carry`). The tools it
hands back are the same for every use case; their docstrings are prompt text.

A goodbye is read by the call's own conversation (`CallContext.conversation`):
only its closing reading can say the caller ended the call.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import ModelResponse, ToolCallPart

from core.graph import Exit
from core.policies import claim_or_refuse
from core.state import CallContext, Handoff


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
