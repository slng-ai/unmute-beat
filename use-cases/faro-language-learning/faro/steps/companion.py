"""Step `companion`: Faro's one agent, with each tool offered only in the phases it belongs to.

The phase gate is in two places on purpose. A tool's `prepare` (or an
output's `offered_when`) keeps it out of the schema the model is sent, so it
cannot be chosen at the wrong moment; each operation checks the phase again
when it runs, because a schema is not a guard.

The docstrings below are prompt text: the model reads them.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from pydantic import TypeAdapter, ValidationError
from pydantic_ai import ModelRetry, RunContext, Tool
from pydantic_ai.tools import ToolDefinition

from core.graph import Node
from core.policies import claim_or_refuse
from core.steps import AgentStep, Prompted
from faro import curriculum
from faro.conversation import ACTIONS, BUILDER, DESK, IDENTIFYING
from faro.curriculum import Goal
from faro.models import Phone, why_not
from faro.operations import (
    CONTINUE_LESSON,
    CREATE_LEARNER,
    DECLINE_PLAN,
    REMEMBER,
    REVISE_PLAN,
    SAVE_ONBOARDING,
    SET_LANGUAGE,
    START_REASSESSMENT,
    Empty,
    FactArgs,
    LanguageArgs,
    NameArgs,
    OnboardingArgs,
    ReviseArgs,
    finish,
    propose,
    read_back_phone,
    recap,
)
from faro.state import FaroCall

_PHONE = TypeAdapter(Phone)
LEARNING = ('menu', 'lesson')


def _usage(ctx: RunContext[FaroCall]) -> dict[str, Any]:
    return {'usage': ctx.usage, 'usage_limits': ctx.usage_limits}


_CODE = re.compile(
    r'(^|[^\d])(\+\s*34|00\s*34)|(más|mas|plus)\s+(treinta\s+y\s+cuatro|thirty[- ]four|34)', re.I
)
"""A Spanish country code said by the caller, in digits or in words, at the start of a number.

ponytail: an English and Spanish pattern. "treinta y cuatro" inside a number
without "más" before it does not count, which is the point.
"""


def said_spanish_code(call: FaroCall) -> bool:
    """Whether the caller said +34 in this turn or the one before, rather than the model adding it (L02, 24 Sep)."""
    return any(_CODE.search(words) for words in (call.heard, call._inputs.get(call.turn - 1, '')))


async def read_back_number(ctx: RunContext[FaroCall], number: str) -> Prompted:
    """Read the caller's phone number back and wait for their yes. Nothing about anyone is looked up before it.
    Use it for a number they just said, or a corrected one.

    Args:
        number: The whole number as they said it, country code included, e.g. +34 600 111 222. Never add a country code they did not say.
    """
    if why := why_not(number):
        raise ModelRetry(why)
    if not said_spanish_code(ctx.deps):
        raise ModelRetry(
            'Nothing was read back: the caller did not say +34 themselves. Never add a country code. '
            'Ask for the whole number starting with +34.'
        )
    try:
        phone = _PHONE.validate_python(number)
    except ValidationError:
        raise ModelRetry('It is not a valid Spanish number. Ask again, digit by digit.') from None
    return await read_back_phone(ctx.deps, phone)


async def save_name(ctx: RunContext[FaroCall], name: str) -> Prompted:
    """Create the learner's profile with the name they want to be called. The next question is then asked for you.

    Args:
        name: The name they asked to be called, as they said it.
    """
    claim_or_refuse(ctx, ACTIONS)
    return await CREATE_LEARNER.run(ctx.deps, NameArgs(name=name), **_usage(ctx))


async def save_onboarding(
    ctx: RunContext[FaroCall], target: str | None = None, goal: Goal | None = None, minutes: int | None = None
) -> Prompted:
    """Save the onboarding answers the learner just gave. The next question, or the first placement task, is
    then asked for you, so say nothing yourself in this reply. Pass only what they actually said.

    Args:
        target: The language they want to learn: English or Spanish. Pass what they said, e.g. "Spanish". Any other language is refused; then tell them Faro teaches only English and Spanish.
        goal: What they want it for: travel, work, or daily life.
        minutes: How long a session should be, in minutes. 10 if they accept the suggestion.
    """
    claim_or_refuse(ctx, ACTIONS)
    try:
        args = OnboardingArgs.model_validate({'target': target, 'goal': goal, 'minutes': minutes})
    except ValidationError as error:
        raise ModelRetry(f'Nothing was saved: {error.errors()[0]["msg"]}') from None
    return await SAVE_ONBOARDING.run(ctx.deps, args, **_usage(ctx))


async def set_language(ctx: RunContext[FaroCall], language: str) -> Prompted:
    """Switch the language you explain things in, when the learner asks: Spanish or English. This is not the
    language they are learning. The question waiting is then asked again for you, in that language.

    Args:
        language: English or Spanish.
    """
    claim_or_refuse(ctx, ACTIONS)
    try:
        args = LanguageArgs.model_validate({'language': language})
    except ValidationError as error:
        raise ModelRetry(f'Nothing was changed: {error.errors()[0]["msg"]}') from None
    return await SET_LANGUAGE.run(ctx.deps, args, **_usage(ctx))


async def present_plan(ctx: RunContext[FaroCall]) -> Prompted:
    """Say the plan proposal on the table again, word for word, and wait for their answer.
    Use it when they ask to hear it again or did not hear it all.
    """
    call = ctx.deps
    if call.draft is None:
        raise ModelRetry('There is no plan proposal on the table.')
    return propose(call, call.draft)


async def revise_plan(
    ctx: RunContext[FaroCall], minutes: int | None = None, goal: Goal | None = None
) -> Prompted:
    """Change the plan proposal the way the learner asked, then say the new proposal to them. Nothing is activated.
    Use it for "yes, but only five minutes" or "I'd rather focus on work".

    Args:
        minutes: The session length they asked for.
        goal: travel, work, or daily life, if they asked to change it.
    """
    claim_or_refuse(ctx, ACTIONS)
    return await REVISE_PLAN.run(ctx.deps, ReviseArgs(minutes=minutes, goal=goal), **_usage(ctx))


async def decline_plan(ctx: RunContext[FaroCall]) -> str:
    """The learner turned down a revised plan and wants to keep the plan they already have."""
    claim_or_refuse(ctx, ACTIONS)
    return await DECLINE_PLAN.run(ctx.deps, Empty(), **_usage(ctx))


async def continue_lesson(ctx: RunContext[FaroCall]) -> Prompted:
    """Put the next exercise of their plan to them. An exercise they have not answered yet is asked again."""
    claim_or_refuse(ctx, ACTIONS)
    return await CONTINUE_LESSON.run(ctx.deps, Empty(), **_usage(ctx))


async def start_reassessment(ctx: RunContext[FaroCall]) -> Prompted:
    """Start a short reassessment of their level, when they ask to check or change it. Their plan stays until
    they accept a new one.
    """
    claim_or_refuse(ctx, ACTIONS)
    return await START_REASSESSMENT.run(ctx.deps, Empty(), **_usage(ctx))


async def remember(ctx: RunContext[FaroCall], key: str, text: str) -> str:
    """Keep something the learner told you about themselves: an interest, or how they like to practise.
    A correction uses the same key as the fact it replaces. Only what they said; never guess.

    Args:
        key: A short stable name, e.g. "hobby", "corrections", "scenarios".
        text: The fact in one short sentence, e.g. "Enjoys climbing."
    """
    claim_or_refuse(ctx, ACTIONS)
    return await REMEMBER.run(ctx.deps, FactArgs(key=key, text=text), **_usage(ctx))


async def read_progress(ctx: RunContext[FaroCall]) -> str:
    """Read their plan's progress: lessons done, items to review, what comes next. For a recap. Changes nothing."""
    return recap(ctx.deps)


async def hang_up(ctx: RunContext[FaroCall], goodbye: str) -> str:
    """End the call, once the learner's own words say it is over: "that's all for today", "adiós", "hasta luego".
    A question is never one of those: answer it and wait.

    Args:
        goodbye: One or two short sentences in your explanation language: one thing they did well today, one thing to practise. Do not say anything was saved: the application says that after saving.
    """
    claim_or_refuse(ctx, ACTIONS)
    call = ctx.deps
    await call.conversation.read(call, usage=ctx.usage, usage_limits=ctx.usage_limits, closing=True)
    if not DESK.can_end(call):
        raise ModelRetry(
            'The learner has not clearly ended the call. Answer them, or ask what they would like to do.'
        )
    summary = await finish(call, 'goodbye')
    if not summary:
        return await DESK.end_conversation(call, goodbye)
    saved = curriculum.saved(call.state.explanation, call.session.checkpoint.learned)
    return await DESK.end_conversation(call, f'{goodbye} {saved}')


def _in(*phases: str) -> Callable[[RunContext[FaroCall], ToolDefinition], Any]:
    async def prepare(ctx: RunContext[FaroCall], tool_def: ToolDefinition) -> ToolDefinition | None:
        return tool_def if ctx.deps.state.phase in phases else None

    return prepare


def _when(*phases: str) -> Callable[[FaroCall], bool]:
    return lambda call: call.state.phase in phases


def node(step: AgentStep) -> Node[Any]:
    """The one step. Every phase is this agent; what it may do in each phase is which tools it is offered."""
    return BUILDER.build(
        step,
        tools=[
            Tool(decline_plan, prepare=_in('plan'), sequential=True),
            Tool(remember, prepare=_in('onboarding', 'assessment', 'plan', *LEARNING), sequential=True),
            Tool(read_progress, prepare=_in(*LEARNING)),
        ],
        finishers=[
            set_language,
            save_name,
            save_onboarding,
            revise_plan,
            continue_lesson,
            start_reassessment,
            hang_up,
        ],
        asks=[read_back_number, present_plan],
        offered_when={
            'read_back_number': _when(*IDENTIFYING),
            'save_name': _when('name'),
            'save_onboarding': _when('onboarding'),
            'present_plan': _when('plan'),
            'revise_plan': _when('plan'),
            'continue_lesson': _when(*LEARNING),
            'start_reassessment': _when(*LEARNING),
        },
    )
