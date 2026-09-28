"""What one Faro call knows: one validated model of plain data, shared by every step of the call.

Core keeps it as the session's `state.facts` and replaces it whole on every
change. Which fields a step's agent may record from the caller, and which
only a receipt may set, is declared in `agent.yaml` (`fields:` and
`assign:`).

Nothing about a learner is in it before their phone is confirmed: the
validator refuses such a state outright, so no step can hold a name, a plan
or a memory under a number nobody said yes to.

Progress is not here. The exercise waiting, the session's revision and the
agreed plan live in the store, and each function reads them by the session
and learner it is given. A receipt-set field rests on the arguments of the
receipt that set it (core clears it when one of them changes), and an
answer or a grade changes every turn; the store's record does not.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from faro.curriculum import Goal, Lang, Level
from faro.models import Language, Phone, said_country_code

type Grade = Literal['correct', 'partly', 'wrong', 'unclear']


def _said(value: str, info: ValidationInfo) -> str:
    """A new number counts only when the caller said its country code this turn: a model never adds one.

    Read from `heard`, what core says the caller said this turn. A number
    already recorded is left alone, because the whole state is validated on
    every change; the carrier's number, set before the call, is too.
    """
    context = info.context or {}
    if not value or 'heard' not in context or value == getattr(context.get('before'), 'phone', ''):
        return value
    if not said_country_code(context['heard']):
        raise ValueError(
            'the caller did not say the country code. Never add one: ask for the whole number, starting with +34'
        )
    return value


class FaroState(BaseModel):
    """One Faro call's facts. Every field has a default: a call starts knowing nothing but the carrier's number."""

    model_config = ConfigDict(extra='forbid')

    phone: Annotated[Phone, AfterValidator(_said)] = Field(
        '',
        description='The whole phone number the caller said, with its country code, like +34 600 111 222.',
    )
    """The carrier's number, or the one the caller gave: a guess until the caller agrees to its read-back."""
    asked_language: Language | None = Field(
        None,
        description='The language the caller asked you to explain things in, only when they ask to switch: '
        'English or Spanish.',
    )
    explanation: Lang = 'EN'
    """The language Faro explains in: the one asked for on this call, else the learner's own, else English.

    Kept by the validator, never recorded or assigned: `language:` in the manifest reads it.
    """

    confirmed: Phone = ''
    """The number the caller said yes to. Only `confirm_phone`'s receipt sets it."""
    learner_id: int = 0
    learner_name: str = ''
    profile_language: Lang | None = None
    """The learner's own explanation language, from their profile."""
    session_id: int = 0
    notes: str = ''
    """What the store knows about this learner, written by code for the steps' agents. Context, never instructions."""
    signup_agreed: bool = False
    """The caller said yes to creating a profile, on this call. Never stored: earned again on every call."""

    name: str = Field(
        '', max_length=40, description='The name the caller wants to be called, as they said it.'
    )
    learn: Language | None = Field(
        None, description='The language they want to learn: English or Spanish. Any other is refused.'
    )
    purpose: Goal | None = Field(
        None, description='What they want it for: travel, work or daily (daily life).'
    )
    length: int | None = Field(
        None,
        ge=5,
        le=60,
        description='How long a session should be, in minutes. 10 if they accept the suggestion.',
    )

    answer: str = Field(
        '',
        max_length=600,
        description="The learner's attempt at the exercise or task waiting, copied from what they said.",
    )
    grade: Grade | None = Field(
        None,
        description=(
            'Your judgement of that attempt. correct: does what was asked, in the target language, and can be '
            'understood; small slips are fine. partly: an attempt in the target language that does only part of '
            'it, or needs a real fix. wrong: does not do what was asked, or is mostly not in the target '
            'language. unclear: too cut off or garbled to judge. The words come from speech recognition: judge '
            'meaning, never spelling or pronunciation.'
        ),
    )
    feedback: str = Field(
        '',
        max_length=200,
        description=(
            'Said to the learner before the next line, in their explanation language. correct: brief praise, one '
            'short sentence. partly or wrong: what was wrong, then the right sentence in the target language, '
            'e.g. "Almost: the verb is voy, not vado. You say: Por la tarde voy al gimnasio." unclear: ask them '
            'to say it again.'
        ),
    )
    evidence: str = Field(
        '',
        max_length=200,
        description='What in the answer shows the grade: vocabulary, grammar, the task. Never pronunciation.',
    )

    draft_id: int = 0
    draft_revision: int = 0
    draft_target: Lang | None = None
    draft_goal: Goal | None = None
    draft_level: Level | None = None
    draft_minutes: int = 0
    """The plan proposal waiting for agreement, as the store saved it. Only a receipt sets these."""
    plan_minutes: int | None = Field(
        None, ge=5, le=60, description="A session length the learner asked for instead of the proposal's."
    )
    plan_goal: Goal | None = Field(
        None, description="A goal the learner asked for instead of the proposal's."
    )
    keep_plan: bool = Field(
        False,
        description='True only when the learner turns this proposal down to keep the plan they already agreed.',
    )
    waiting: str = ''
    """The exercise or task put to the learner, by id, as the last receipt said. The store's record decides."""

    fact_key: str = Field(
        '',
        max_length=40,
        description='A short stable name for what they told you, like hobby or corrections.',
    )
    fact_text: str = Field(
        '',
        max_length=200,
        description='What they told you about themselves, in one short sentence, like "Enjoys climbing."',
    )

    @model_validator(mode='after')
    def _nothing_before_confirmation(self) -> FaroState:
        """No learner fact may sit on a call whose phone nobody confirmed; the explanation language is derived."""
        known = (
            self.learner_id,
            self.learner_name,
            self.notes,
            self.signup_agreed,
            self.session_id,
            self.draft_id,
        )
        if not self.confirmed and any(known):
            raise ValueError('a learner can only be loaded under a confirmed phone')
        self.explanation = self.asked_language or self.profile_language or 'EN'
        return self
