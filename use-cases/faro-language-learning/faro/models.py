"""Faro's records as the store keeps them, and the shapes every tool argument is checked against.

Everything read back from SQLite is validated into one of these before a
field is used: a JSON column is a string until it has been through its model.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

import phonenumbers
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, TypeAdapter, ValidationError
from pydantic_extra_types.phone_numbers import PhoneNumberValidator

from faro import curriculum
from faro.curriculum import Goal, Lang, Level, Result

_RECORD = ConfigDict(extra='forbid')

_NAMES: dict[str, Lang] = {
    'en': 'EN',
    'english': 'EN',
    'inglés': 'EN',
    'ingles': 'EN',
    'es': 'ES',
    'spanish': 'ES',
    'español': 'ES',
    'espanol': 'ES',
    'castellano': 'ES',
}


def _language(value: object) -> object:
    """A spoken language name as EN or ES. Anything else is refused, naming the two on offer."""
    if not isinstance(value, str):
        return value
    found = _NAMES.get(value.strip().lower())
    if found is None:
        raise ValueError(f'{value!r} is not offered. Faro teaches only English (EN) or Spanish (ES).')
    return found


type Language = Annotated[Lang, BeforeValidator(_language)]
"""EN or ES, from either code or a spoken name. French, or anything else, fails at the boundary."""

_SPANISH = TypeAdapter(
    Annotated[
        str | phonenumbers.PhoneNumber, PhoneNumberValidator(number_format='E164', supported_regions=['ES'])
    ]
)


def _phone(value: object) -> object:
    if value == '':
        return value
    return _SPANISH.validate_python(value)


type Phone = Annotated[str, BeforeValidator(_phone)]
"""A Spanish number in E.164 (`+34600111222`), or '' for none. A number with no +34 is refused, never guessed."""

_DIGITS = re.compile(r'(?:\+|00)[\d\s().-]{8,}\d')


def phone_in(words: str) -> str:
    """The Spanish number said in these words, in E.164, or '' when there is none or it is not Spanish.

    Only a number with its country code counts: a Spanish-looking nine
    digits with no +34 is left for the model to ask about again.
    """
    for found in reversed(_DIGITS.findall(words)):
        digits = re.sub(r'[^\d+]', '', found)
        if digits.startswith('00'):
            digits = f'+{digits[2:]}'
        try:
            return str(_SPANISH.validate_python(digits))
        except ValidationError:
            continue
    return ''


def why_not(number: str) -> str:
    """Why a number cannot be used, in words for the model, or '' when it can."""
    digits = re.sub(r'[^\d+]', '', number)
    if not digits.startswith('+'):
        return 'It has no country code. Ask for the full number starting with +34; do not guess one.'
    try:
        _SPANISH.validate_python(digits)
    except ValidationError as error:
        if 'supported region' in str(error):
            return 'It is not a Spanish number. Faro only takes Spanish numbers (+34). Ask again.'
        return 'It is not a valid number. Ask them to say it again, digit by digit.'
    return ''


class MemoryFact(BaseModel):
    """Something the learner said about themselves, kept as context, never as an instruction."""

    model_config = _RECORD
    key: str = Field(max_length=40, description='A short stable name, e.g. "hobby" or "corrections".')
    text: str = Field(max_length=200)
    session_id: int
    """The session it was said in: every fact has a source."""


class Learner(BaseModel):
    model_config = _RECORD
    id: int
    phone: Phone
    name: str
    explanation: Lang
    status: Literal['onboarding', 'active']
    memory: list[MemoryFact] = Field(default_factory=list, max_length=20)


class Evidence(BaseModel):
    """One placement answer: what was asked, what was said, and what it shows."""

    model_config = _RECORD
    task_id: str
    answer: str
    assistance: str = ''
    result: Result
    rationale: str
    bank: str
    session_id: int


class Attempt(BaseModel):
    """One accepted answer to a lesson item. Its id is derived, so a replay finds it instead of adding one."""

    model_config = _RECORD
    id: str
    item_id: str
    answer: str
    result: Result
    session_id: int


class Progress(BaseModel):
    """Where the learner is in their plan, and everything that got them there."""

    model_config = _RECORD
    lesson: int = 0
    """Index into the plan's lessons of the lesson under way."""
    item: int = 0
    """Index into that lesson's items of the item waiting."""
    completed: list[str] = Field(default_factory=list)
    """Lesson ids whose independent check passed."""
    review: list[str] = Field(default_factory=list)
    """Item ids answered with help or not yet: practised, not mastered."""
    attempts: list[Attempt] = Field(default_factory=list)


class Plan(BaseModel):
    model_config = _RECORD
    id: int
    learner_id: int
    target: Lang
    status: Literal['draft', 'active', 'archived', 'declined']
    revision: int
    """Changes when the content changes: goal, level, length, lessons. A yes is to one revision."""
    progress_revision: int
    """Changes on every routine save, without touching the agreement."""
    goal: Goal
    level: Level
    uncertainty: str
    minutes: int
    lessons: list[str]
    assessment: list[Evidence] = Field(default_factory=list)
    progress: Progress = Field(default_factory=Progress)
    summary: str = ''

    @property
    def finished(self) -> bool:
        return self.progress.lesson >= len(self.lessons)

    def waiting(self) -> str:
        """The id of the item the learner does next, or '' when the plan is finished."""
        if self.finished:
            return ''
        return curriculum.lesson(self.lessons[self.progress.lesson]).items[self.progress.item].id

    def after(self, attempt: Attempt) -> Plan:
        """This plan with one accepted answer applied: the completion rules, in one place.

        Evidence is kept whatever the result. Nothing moves on until the
        learner says it correctly: a miss (`not_yet`) keeps the item waiting,
        and it is corrected and asked again. A correct answer moves on, and
        after the lesson's check the lesson completes. A correct answer that
        needed help or a correction first is recorded as `assisted` and put
        on the review list, so the record stays honest about how it went. An
        answer that gave no evidence, such as a misheard one, moves nothing
        and counts as no failure.
        """
        if attempt.item_id != self.waiting():
            raise ValueError(
                f'{attempt.item_id} is not the item waiting ({self.waiting() or "plan finished"})'
            )
        progress = self.progress.model_copy(deep=True)
        progress.attempts.append(attempt)
        lesson = curriculum.lesson(self.lessons[progress.lesson])
        item = lesson.items[progress.item]
        if attempt.result in ('assisted', 'not_yet') and item.id not in progress.review:
            progress.review.append(item.id)
        if attempt.result in ('insufficient_evidence', 'not_yet'):
            return self.model_copy(update={'progress': progress})
        if item.check:
            progress.completed.append(lesson.id)
            progress.lesson, progress.item = progress.lesson + 1, 0
        else:
            progress.item += 1
        return self.model_copy(update={'progress': progress})


type Phase = Literal[
    'identify', 'signup', 'name', 'onboarding', 'assessment', 'plan', 'menu', 'lesson', 'closed'
]


class Checkpoint(BaseModel):
    """What a session saves so the next one can carry on: the phase, and the work not yet in a plan."""

    model_config = _RECORD
    phase: Phase = 'identify'
    target: Lang | None = None
    goal: Goal | None = None
    minutes: int = 0
    evidence: list[Evidence] = Field(default_factory=list)
    """Placement answers of an assessment still under way."""
    task: str = ''
    """The placement task waiting for an answer."""
    draft_id: int = 0
    """The plan proposal waiting for agreement."""
    item: str = ''
    """The lesson item put to the learner and not yet answered."""
    reassessing: bool = False
    learned: int = 0
    """Attempts accepted in this session. Zero means the session practised nothing."""


class Session(BaseModel):
    model_config = _RECORD
    id: int
    learner_id: int
    plan_id: int | None
    predecessor: int | None
    status: Literal['active', 'completed', 'interrupted']
    revision: int
    checkpoint: Checkpoint
    outcome: str = ''
