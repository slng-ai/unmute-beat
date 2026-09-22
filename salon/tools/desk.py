"""What every step can do: end the call on the caller's own goodbye, and hand the caller on."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

from core.graph import Exit
from core.policies import claim_or_refuse
from salon.models.consent import ends
from salon.models.state import Handoff, SalonCall
from salon.tools.policy import ACTIONS


def _can_end(call: SalonCall) -> bool:
    """Whether the caller's own words say the call is over.

    A bare "no thanks" declines the last offer, not necessarily the whole
    call: it ends the call only when the last thing said to them was an offer
    of anything else.
    """
    if not ends(call.heard):
        return False
    if not re.fullmatch(r'no[ ,]*(?:thanks|thank you)[.! ]*', call.heard.strip(), re.IGNORECASE):
        return True
    previous = [r['text'] for r in call.transcript() if r['said'] != 'caller']
    if not previous and not call.all_spoken:
        previous = [
            p.content
            for m in call.messages
            if isinstance(m, ModelResponse)
            for p in m.parts
            if isinstance(p, TextPart)
        ]
    return bool(
        previous and re.search(r'anything else|anything more|something else', previous[-1], re.IGNORECASE)
    )


async def hang_up(ctx: RunContext[SalonCall], goodbye: str) -> str:
    """End the call, once the caller's own words say it is over: "no, that's all", "I'm good,
    thanks", "goodbye". A question is never one of those, however tired it sounds: answer it and wait.

    Args:
        goodbye: One short goodbye in your own words. It is the last thing they hear.
    """
    claim_or_refuse(ctx, ACTIONS)
    if not _can_end(ctx.deps):
        raise ModelRetry(
            f'The line is still open: the caller said "{ctx.deps.heard}", which is not them ending '
            'the call. Answer what they asked. Hang up only on their own goodbye.'
        )
    ctx.deps.end_requested('caller')
    await ctx.deps.speak_now(goodbye, origin='goodbye')
    await ctx.deps.end_call()
    return goodbye


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
    """
    names = tuple(dict.fromkeys(exit.to for exit in exits))

    async def hand_over(ctx, to, because) -> Handoff:
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
        return Handoff(to=to, because=because)

    hand_over.__annotations__ = {
        'ctx': RunContext[SalonCall],
        'to': Literal[names],  # ty: ignore[invalid-type-form]
        'because': str,
        'return': Handoff,
    }
    return hand_over
