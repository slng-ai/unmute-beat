"""The agents: one per step, each with its own prompt file and its own tools.

Adding a step is a prompt in ``prompts/``, an entry in ``AGENTS``, and a name in
``AgentName``. There is no graph, no node config and no snapshot: an agent runs
until it either speaks (the turn is over) or returns a ``Handoff`` (someone else
takes it from here).
"""

from __future__ import annotations

from pathlib import Path

from pydantic_ai import Agent, ModelRetry, RunContext, TemplateStr

from voice.harness import Node, filler_hook
from voice.state import AgentName, CallState, Handoff, Phone

PROMPTS = Path(__file__).resolve().parent.parent / 'prompts' / 'voice'

MODEL = 'google-cloud:gemini-3.5-flash-lite'

GREETING = "Hi, you've reached the harness. Robin speaking, what can I do for you?"


def prompt(name: str) -> TemplateStr[CallState]:
    """Compile one prompt file against CallState.

    At import, so a `{{placeholder}}` naming no field raises TemplateSchemaError
    here and the process never starts, rather than rendering as an empty string
    into a live call.
    """
    return TemplateStr((PROMPTS / f'{name}.md').read_text(), deps_type=CallState)


# --- spoken filler ---------------------------------------------------------

# What to say while a tool runs. The model is not asked to narrate: it is told
# never to, because a generated "let me check" costs the round trip it is meant
# to cover. This line goes to TTS the moment the call is validated.
FILLER: dict[str, tuple[str, ...]] = {
    'confirm_phone': ("Let me get that written down.", "One second, saving that."),
}

# --- tools -------------------------------------------------------------------


async def confirm_phone(ctx: RunContext[CallState], phone: Phone) -> str:
    """Record the caller's number. Call this only after they agreed to the digits you read back.

    A caller who answers your readback with different digits is correcting you,
    not confirming; read the new ones back instead of calling this.
    """
    # Validated before it gets here: `Phone` is the tool's own argument type, so
    # a badly shaped number never reaches the state -- the model is handed the
    # error and corrects itself on the next turn.
    ctx.deps.caller_phone = phone
    return 'recorded'


# --- the table ---------------------------------------------------------------


async def hand_over(ctx: RunContext[CallState], to: AgentName, because: str) -> Handoff:
    """Give the caller to another agent, right now, for something that is not yours to do.

    This is the only way to move a caller. Writing a sentence about transferring
    them does nothing at all: the caller hears you say it and then sits there,
    still with you. Do this instead of saying it, and say nothing about it after.

    Args:
        to: The agent that takes it from here.
        because: One short sentence: what the caller wants. Never spoken.
    """
    if to == ctx.deps.agent:
        # A hop to yourself is a whole model request that says nothing, and the
        # processor's hop budget pays for it. Refused here instead, so the model
        # corrects itself inside this run and the caller hears an answer.
        raise ModelRetry(f'You are the {to} agent already. Answer the caller yourself.')
    return Handoff(to=to, because=because)


def build(name: AgentName, tools: list = []) -> Agent[CallState, str | Handoff]:
    return Agent[CallState, str | Handoff](
        MODEL,
        name=name,  # labels this agent's span in Langfuse
        deps_type=CallState,
        # `str` keeps speech streaming token by token to TTS; `Handoff` ends the
        # run with a typed value instead. A BaseModel-only output_type would
        # make every turn a tool call, so there would be nothing to stream and
        # time-to-first-word would become time-to-last-token.
        output_type=[str, hand_over],  # type: ignore[arg-type]
        instructions=prompt(name),
        tools=tools,
        capabilities=[filler_hook(FILLER)],
        model_settings={'service_tier': 'priority'},
    )


# If its tokens should be heard, it is a handoff and it belongs in here. If they
# should not -- a classifier, an extractor, a summariser -- it is delegation:
# `await worker.run(..., deps=ctx.deps, usage=ctx.usage)` from inside a tool,
# and it never goes in this table, because a delegated agent's text never
# reaches TTS and the caller would hear the gap.
AGENTS: dict[str, Node] = {
    'concierge': Node(build('concierge')),
    'intake': Node(
        build('intake', [confirm_phone]),
        enter=("Of course, let me take that down.", "Sure, go ahead."),
    ),
}
