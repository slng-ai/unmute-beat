"""Faro's conversation, given to core's acts, and the one builder its step is made with.

Three questions wait on a yes: the phone read-back, the sign-up offer and
the plan proposal. Each is a `ReadBack`, so one `confirmation` reading with
core's `CONFIRM` classes reads the reply, and a clear yes runs its action
with no agent request. "Maybe", "yes, but only five minutes" or a reply to
a proposal the learner talked over authorises nothing: the companion agent
answers instead. A number said out loud before anyone is identified is read
back by code (`HeardValue`).

While an exercise or placement task waits, every reply is also read by the
grading reading (`GRADE`). An attempt is recorded by code, right or wrong,
and the learner hears the reading's short feedback and then the next line,
both said by the application. Only a reply that is not an attempt (a
question, "slower, please", a hesitation) reaches the companion agent, which
coaches and never grades. So an answer can no longer be praised and left
unsaved, which is what a live call showed (trace 34e4fe68, 24 September).

Everything else is the companion agent's.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.models import infer_model
from pydantic_ai.models.google import GoogleModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits

from core.classifiers import Classifier, Exchange
from core.conversation import Closing, Conversation, FixedChoice, HeardValue, ReadBack
from core.desk import AgentBuilder, Desk
from core.graph import LocalAction
from core.readings import Option, Reading
from core.state import Handoff
from core.steps import Prompted
from faro import curriculum
from faro.curriculum import Result
from faro.models import phone_in
from faro.operations import (
    ACTIVATE_PLAN,
    CONFIRM_PHONE,
    CONTINUE_LESSON,
    RECORD_ANSWER,
    RECORD_ASSESSMENT,
    SAVE_ONBOARDING,
    START_REASSESSMENT,
    AnswerArgs,
    AssessmentArgs,
    Empty,
    OnboardingArgs,
    PhoneArgs,
    PlanRef,
    accept_signup,
    finish,
    read_back_phone,
)
from faro.state import FaroCall

MODEL = infer_model('google-cloud:gemini-3.5-flash-lite')
SETTINGS = GoogleModelSettings(
    service_tier='priority',
    google_thinking_config={'thinking_level': 'MINIMAL'},  # ty: ignore[invalid-argument-type]
)
"""The salon's model and settings, so the two use cases are measured on the same footing."""

ACTIONS = frozenset(
    {
        'save_name',
        'save_onboarding',
        'set_language',
        'revise_plan',
        'decline_plan',
        'continue_lesson',
        'start_reassessment',
        'remember',
        'hang_up',
    }
)
"""Everything that writes to the store or ends the call. At most one runs per model reply."""

SPEAK_BY = 3
"""The request by which a run must answer with what it holds: a save, then its correction, then speech."""

IDENTIFYING = ('identify', 'signup', 'name')
"""The phases before a profile is loaded, when a number said out loud is a (new) phone to confirm."""

NEXT_STEP = (
    Option(id='continue', said='carry on with the lesson or exercise'),
    Option(id='recap', said='hear a recap of their progress first'),
    Option(id='reassess', said='check or change their level'),
)
"""What "carry on, or recap first?" can be answered with. A recap is the agent's to say, from `read_progress`."""


async def _goodbye(call: FaroCall) -> str:
    summary = await finish(call, 'goodbye')
    if summary:
        learned = call.session.checkpoint.learned
        return await DESK.end_conversation(call, curriculum.goodbye(call.state.explanation, summary, learned))
    return await DESK.end_conversation(
        call, '¡Hasta pronto!' if call.state.explanation == 'ES' else 'Goodbye!'
    )


def conversation(model=MODEL) -> Conversation[FaroCall]:
    """Faro, as information given to core's acts. Tests build it on a scripted model."""
    return Conversation[FaroCall](
        context=(
            'A learner is on the phone with Faro, a bilingual English and Spanish language-learning companion. '
            'They may reply in Spanish or in English.'
        ),
        model=model,
        settings=SETTINGS,
        read_back_context='Faro read something back word for word and is waiting for a yes.',
        read_backs=(
            ReadBack[FaroCall](
                about='phone',
                action='confirm_phone',
                run=lambda call, q: CONFIRM_PHONE.run(call, PhoneArgs(phone=q.value)),
            ),
            ReadBack[FaroCall](about='signup', action='accept_signup', run=accept_signup),
            ReadBack[FaroCall](
                about='plan',
                action='activate_plan',
                run=lambda call, q: ACTIVATE_PLAN.run(call, PlanRef.of(q.value)),
            ),
        ),
        choices=(
            FixedChoice[FaroCall](
                name='next_step',
                about='menu',
                options=NEXT_STEP,
                context=(
                    'Faro asked whether to carry on with the lesson or do something else first. '
                    'Read which one the learner chose; a goodbye or a question picks none.'
                ),
                actions={
                    'continue': lambda call, q: LocalAction(
                        'continue_lesson', lambda: CONTINUE_LESSON.run(call, Empty())
                    ),
                    'reassess': lambda call, q: LocalAction(
                        'start_reassessment', lambda: START_REASSESSMENT.run(call, Empty())
                    ),
                },
                when=lambda call: call.plan is not None,
            ),
        ),
        values=(
            HeardValue[FaroCall](
                about='phone',
                action='read_back_number',
                parse=phone_in,
                read_back=read_back_phone,
                when=lambda call: call.state.phase in IDENTIFYING,
            ),
        ),
        closing=Closing[FaroCall](goodbye=_goodbye),
    )


FARO = conversation()

DESK = Desk[FaroCall](call_type=FaroCall, handoff=Handoff, actions=ACTIONS)


class GradeInput(Exchange):
    """An answer to one exercise or placement task, with what the grader needs to judge it."""

    last_said: str = Field(description='What Faro said just before the answer. It may be a hint.')
    target: str = Field(description='The language the learner must answer in.')
    explanation: str = Field(description="The learner's own language. Feedback is written in it.")
    objective: str = Field(description='What the exercise practises.')


class Grade(BaseModel):
    """One answer, judged. The application turns `result` into what is recorded; the model only reads."""

    model_config = ConfigDict(extra='forbid')
    result: Literal['correct', 'partly', 'wrong', 'unclear', 'no_attempt'] = Field(
        default='no_attempt',
        description=(
            'correct: does what was asked, in the target language, and can be understood; small slips are fine. '
            'partly: an attempt in the target language that does only part of it, or needs a real fix. '
            'wrong: an attempt that does not do what was asked, or is mostly not in the target language. '
            'unclear: an attempt too cut off or garbled to judge. '
            'no_attempt: not an answer to the exercise: a question, asking to explain or slow down, hesitation, '
            'small talk, or a goodbye.'
        ),
    )
    feedback: str = Field(
        default='',
        max_length=200,
        description=(
            'Written in the explanation language, praise included; said before the next line. Correct: brief '
            'praise, one short sentence. Partly or wrong: the correction, in at most two short sentences: what was '
            'wrong, then the right sentence in the target language, e.g. "Almost: the verb is voy, not vado. '
            'You say: Por la tarde voy al gimnasio." They are then asked to say it again. Unclear: ask them to '
            'say it again. Empty for no_attempt.'
        ),
    )
    rationale: str = Field(
        default='',
        max_length=200,
        description='What in the answer shows the result: vocabulary, grammar, task. Never pronunciation.',
    )


RESULTS: dict[str, Result] = {
    'correct': 'independent',
    'partly': 'assisted',
    'wrong': 'not_yet',
    'unclear': 'insufficient_evidence',
}
"""What each grade records. A correct answer the companion helped with is recorded as assisted (`graded`)."""


def _grading(call: FaroCall) -> GradeInput | None:
    """The answer to judge: only while an exercise or task waits, and no other question is being answered."""
    state = call.state
    asked = call.pending_question()
    if asked is not None and asked.about not in ('item', 'task'):
        return None
    if state.phase == 'lesson' and state.item:
        lesson, _ = curriculum.item(state.item)
        said, waiting, objective = (
            curriculum.exercise(state.explanation, state.item),
            state.item,
            lesson.objective,
        )
    elif state.phase == 'assessment' and state.task:
        said, waiting = curriculum.placement(state.explanation, state.task), state.task
        objective = f'placement: {curriculum.task(state.task).evidence}'
    else:
        return None
    return GradeInput(
        question=said,
        answer=call.heard,
        last_said=call.said_last_turn(),
        target=curriculum.LANGUAGE_NAMES['EN'][curriculum.target_of(waiting)],
        explanation=curriculum.LANGUAGE_NAMES['EN'][state.explanation],
        objective=objective,
    )


GRADE = Reading[FaroCall, GradeInput](
    'grade',
    Classifier(
        MODEL,
        GradeInput,
        Grade,
        context=(
            'A learner on a language-learning call was given an exercise in their own language and answers it in '
            'the language they are learning. Judge the answer as a patient teacher: whether it is an attempt, and '
            'how well it does what was asked. The words come from speech recognition, so judge meaning, not '
            'spelling or punctuation.'
        ),
        model_settings=SETTINGS,
    ),
    _grading,
    field='result',
)
"""Faro's own reading, built from core's `Reading` and `Classifier`: its dataset is `harness-reading/grade`."""


def _missed_before(call: FaroCall, item_id: str) -> bool:
    """Whether this item was already answered wrongly in this session: a right answer now came after a correction."""
    plan = call.plan
    return plan is not None and any(
        a.item_id == item_id and a.session_id == call.session.id and a.result == 'not_yet'
        for a in plan.progress.attempts
    )


def graded(call: FaroCall) -> LocalAction | None:
    """Record this turn's attempt, when the grading reading found one. None leaves the turn to the agent.

    In a lesson only a correct sentence moves on: a partly right one is a
    miss, corrected and asked again (the learner's rule, 24 September). In a
    placement, partly right counts as done with help, because a placement
    measures and does not teach.
    """
    if not GRADE.ran(call) or (grade := cast(Grade, GRADE.current(call))).result == 'no_attempt':
        return None
    state = call.state
    result = RESULTS[grade.result]
    waiting = state.item if state.phase == 'lesson' else state.task
    helped = call._coached == waiting
    answer = call.heard[:600]
    if state.phase == 'lesson':
        if grade.result == 'partly':
            result = 'not_yet'
        if result == 'independent' and (helped or _missed_before(call, waiting)):
            result = 'assisted'
        attempt = AnswerArgs(item_id=waiting, answer=answer, result=result, feedback=grade.feedback)
        return LocalAction('record_answer', lambda: RECORD_ANSWER.run(call, attempt))
    if result == 'independent' and helped:
        result = 'assisted'
    evidence = AssessmentArgs(
        task_id=waiting,
        answer=answer,
        result=result,
        rationale=grade.rationale or grade.result,
        assistance='the companion helped first' if helped else '',
        feedback=grade.feedback,
    )
    return LocalAction('record_assessment', lambda: RECORD_ASSESSMENT.run(call, evidence))


class OnboardingAnswer(BaseModel):
    """An onboarding reply, read into the three things onboarding asks. The application saves and asks on."""

    model_config = ConfigDict(extra='forbid')
    language: Literal['english', 'spanish', 'other', 'none'] = Field(
        default='none',
        description=(
            'The language they want to learn. english or spanish; other for any other language '
            '(French, German...); none when they did not name one.'
        ),
    )
    goal: Literal['travel', 'work', 'daily', 'none'] = Field(
        default='none',
        description='What they want it for: travel, work, daily (daily life); none when they did not say.',
    )
    minutes: int = Field(
        default=0,
        ge=0,
        le=60,
        description=(
            'Session length in minutes. The suggested length when they accept the suggestion in the question '
            '("fine", "yes"); 0 when they did not say.'
        ),
    )


def _onboarding(call: FaroCall) -> Exchange | None:
    """The reply to an onboarding question: only while onboarding, and only to one of its questions."""
    last = call.last_proposal()
    if call.state.phase != 'onboarding' or last is None or last[0].about != 'onboarding':
        return None
    return Exchange(question=last[0].spoken, answer=call.heard)


ONBOARD = Reading[FaroCall, Exchange](
    'onboarding',
    Classifier(
        MODEL,
        Exchange,
        OnboardingAnswer,
        context=(
            'A new learner is setting up language practice with Faro, which teaches only English and Spanish. '
            'Read which of the setup answers this reply gives. The reply may be in English or Spanish.'
        ),
        model_settings=SETTINGS,
    ),
    _onboarding,
    field='language',
)
"""Faro's second own reading: onboarding answers are saved by code, so no model words a second question."""

_LANGUAGES = {'english': 'EN', 'spanish': 'ES'}


def onboarded(call: FaroCall) -> LocalAction | None:
    """Save what this onboarding reply gave, or refuse a language Faro does not teach. None leaves it to the agent."""
    if not ONBOARD.ran(call):
        return None
    heard = cast(OnboardingAnswer, ONBOARD.current(call))
    given = {
        'target': _LANGUAGES.get(heard.language),
        'goal': None if heard.goal == 'none' else heard.goal,
        'minutes': heard.minutes if 5 <= heard.minutes <= 60 else None,
    }
    if any(given.values()):
        args = OnboardingArgs.model_validate(given)
        return LocalAction('save_onboarding', lambda: SAVE_ONBOARDING.run(call, args))
    if heard.language == 'other':
        return LocalAction('refuse_language', lambda: _only_two(call))
    return None


async def _only_two(call: FaroCall) -> Prompted:
    return Prompted(about='onboarding', spoken=curriculum.only_two(call.state.explanation))


def unheard(call: FaroCall) -> LocalAction | None:
    """A question waiting on a yes that the learner talked over is put again in full, before anything else.

    Its answer cannot count (core's ledger refuses it), and a model left to
    repeat it in its own words puts no question a later yes could answer:
    the loop a live run showed (L11, 24 September).
    """
    last = call.last_proposal()
    if last is None or last[1] or last[0].about not in FARO.read_back_abouts:
        return None
    return LocalAction('say_again', lambda: _again(call, last[0]))


async def _again(call: FaroCall, question: Any) -> Prompted:
    prefix = 'Te lo repito entero.' if call.state.explanation == 'ES' else 'Let me say that again in full.'
    return Prompted(about=question.about, spoken=f'{prefix} {question.spoken}', value=question.value)


async def advance(call: FaroCall, usage: RunUsage, limits: UsageLimits) -> LocalAction | None:
    """Read the reply before any agent runs: an exercise answer is recorded, a setup answer saved, a yes acted on.

    Faro's readings run side by side with the conversation's, so they add no
    wait of their own. An attempt wins over a goodbye in the same breath: the
    answer is saved first.
    """
    if (again := unheard(call)) is not None:
        return again
    grading = GRADE.applies(call)
    own = [r.interpret(call, usage=usage, usage_limits=limits) for r in (GRADE, ONBOARD) if r.applies(call)]
    await asyncio.gather(
        FARO.read(call, usage=usage, usage_limits=limits, closing=grading, before_model=True), *own
    )
    if grading and leaving(call):
        return LocalAction('hang_up', lambda: _goodbye(call))
    return graded(call) or onboarded(call) or FARO.decide(call)


def leaving(call: FaroCall) -> bool:
    """A goodbye while an exercise waits, and no correct answer in it: the call ends, nothing is graded.

    The grading reading can read "that's all, bye" as a wrong answer, and
    the learner was then corrected and had to say goodbye twice (L10, 24
    September). The closing reading runs alongside it and says which it is.
    """
    return (
        FARO.label(call, FARO.closing.name) == 'end' and cast(Grade, GRADE.current(call)).result != 'correct'
    )


BUILDER = AgentBuilder[FaroCall](
    call_type=FaroCall,
    model=MODEL,
    settings=SETTINGS,
    actions=ACTIONS,
    desk=DESK,
    advance=advance,
    speak_by=SPEAK_BY,
)
