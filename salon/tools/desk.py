"""What every step can do: end the call on the caller's own goodbye, and hand the caller on."""

from __future__ import annotations

import inspect
from functools import lru_cache
from typing import Literal

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import ModelResponse, ToolCallPart

from core.graph import Exit
from core.policies import claim_or_refuse
from salon.classification import classify_reply
from salon.models.state import BookingRequest, Handoff, SalonCall
from salon.tools.policy import ACTIONS


def _can_end(call: SalonCall) -> bool:
    """Only a bound semantic interpretation can establish an end-of-call request."""
    return call.classification.conversation == 'end'


async def end_conversation(call: SalonCall, goodbye: str) -> str:
    """Shared executor for a classified closing reply and the model-facing tool."""
    if not _can_end(call):
        raise ModelRetry('The caller has not clearly ended the call. Answer their request or clarify.')
    call.answered()
    call.end_requested('caller')
    await call.speak_now(goodbye, origin='goodbye')
    await call.end_call()
    return goodbye


async def switch_task(
    call: SalonCall, exits: tuple[Exit, ...], target: str, details: str, reply: str
) -> Handoff:
    """Abandon the unfinished proposal and move the caller to another task: one application action.

    The executor behind the solver's `SwitchTask`. The edge has to be open from
    this step right now, exactly as `hand_over` checks it, so a switch the
    graph does not allow is refused and the step's own agent answers instead.
    Nothing that already happened is undone: a booking made earlier stands.
    A write whose outcome is unknown blocks the switch, because moving on would
    let the caller believe nothing was booked when nobody knows that.

    The details travel as an application result, so the next step reads what
    the caller already said instead of asking again. The move ends the turn:
    the destination's opening speaks if it has one, else `reply` does.
    """
    if call.state.unresolved is not None:
        raise ModelRetry(
            'Nothing was switched: a booking change is still waiting on the diary. Resolve that first.'
        )
    if target == call.state.agent or not any(exit.to == target and exit.guard(call.state) for exit in exits):
        raise ModelRetry(
            f'Nothing was switched: {target} is not somewhere this caller can go from here right now.'
        )
    call.answered()
    call.retain_action('hand_over', {'to': target, 'details': details})
    return Handoff(
        to=target,  # ty: ignore[invalid-argument-type]
        because=details or f'the caller switched to {target}',
        ends_turn=True,
        reply=reply,
    )


async def clarify(call: SalonCall, question: str) -> str:
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


async def hang_up(ctx: RunContext[SalonCall], goodbye: str) -> str:
    """End the call, once the caller's own words say it is over: "no, that's all", "I'm good,
    thanks", "goodbye". A question is never one of those, however tired it sounds: answer it and wait.

    Args:
        goodbye: One short goodbye in your own words. It is the last thing they hear.
    """
    claim_or_refuse(ctx, ACTIONS)
    await classify_reply(ctx.deps, usage=ctx.usage, usage_limits=ctx.usage_limits)
    return await end_conversation(ctx.deps, goodbye)


@lru_cache
def hand_over_for(exits: tuple[Exit, ...], questions: tuple[str, ...] = ()):
    """One step's `hand_over`, carrying only the steps that step may actually reach.

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

    A step that can send the caller to `booking` also gets `booking`, the
    request typed while the caller's words are in front of the model. When
    `booking` interposes verification, that is what lets the booking step
    offer times on its return without a request of its own. Every other step's
    `hand_over` has no such parameter: its signature is trimmed, so the schema
    the model is sent is exactly what it was.
    """
    names = tuple(dict.fromkeys(exit.to for exit in exits))
    carries = 'booking' in names

    async def hand_over(ctx, to, because, booking=None) -> Handoff:
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
            and any(isinstance(part, ToolCallPart) and part.tool_name in questions for part in reply.parts)
        ):
            raise ModelRetry('Ask the readback here and wait for the caller. Do not hand over in that reply.')
        claim_or_refuse(ctx, ACTIONS)
        if not any(exit.to == to and exit.guard(ctx.deps.state) for exit in exits):
            raise ModelRetry(
                f'{to} is not somewhere this caller can go from here right now. '
                'Nothing was done. Answer them yourself, or pick a destination that is open.'
            )
        if to == 'booking' and booking is not None:
            ctx.deps.commit(wanted=booking)
        return Handoff(to=to, because=because)

    hand_over.__annotations__ = {
        'ctx': RunContext[SalonCall],
        'to': Literal[names],  # ty: ignore[invalid-type-form]
        'because': str,
        **({'booking': BookingRequest | None} if carries else {}),
        'return': Handoff,
    }
    if carries:
        hand_over.__doc__ = f'{inspect.cleandoc(hand_over.__doc__ or "")}\n{_BOOKING_ARG}'
    else:
        signature = inspect.signature(hand_over)
        hand_over.__signature__ = signature.replace(  # ty: ignore[unresolved-attribute]
            parameters=[p for p in signature.parameters.values() if p.name != 'booking']
        )
    return hand_over


_BOOKING_ARG = (
    '    booking: Only with to="booking", for a new appointment the caller\n'
    '        has asked for: the service, their own day words, and their\n'
    '        time words if they gave any. Leave it out if they named no\n'
    '        service or no day.'
)
"""The `booking` argument's description, appended to `hand_over`'s Args only where the argument exists."""
