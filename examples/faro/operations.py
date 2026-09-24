"""Every durable thing Faro does, one `Operation` each, and the executors the model's tools share with `advance`.

Three of them need the learner's yes to an exact question this code worded:
confirming the phone, creating a profile (the yes is to the sign-up offer),
and activating a plan (the yes is to one draft at one revision). The rest
are routine saves inside a phase the application has already reached, and
each checks that phase itself, whatever offered the tool.

Every write goes through the store and is committed before its receipt is
validated, so the learner is told something was saved only after it was.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import ModelRetry

from core.operations import Operation, operation_token
from core.proposals import BaseProposal
from core.steps import Prompted
from examples.faro import curriculum
from examples.faro.curriculum import Goal, Result
from examples.faro.models import (
    Attempt,
    Evidence,
    Language,
    Learner,
    MemoryFact,
    Phone,
    Plan,
    Progress,
    Session,
)
from examples.faro.state import FaroCall

ACTIVE: set[int] = set()
"""Learners on a call in this process. A second call for the same learner is refused.

ponytail: one process. Move it into the store when a second process serves calls.
"""


def agreed(call: FaroCall) -> bool:
    """Whether this turn's reply is a clear yes to the read-back waiting. Only core's `confirmation` reading can say."""
    return call.conversation.label(call, 'confirmation') == 'accept'


def _refuse_unless(condition: bool, why: str) -> None:
    if not condition:
        raise ModelRetry(f'Nothing was saved: {why}')


def _saved(call: FaroCall) -> int:
    return call.state.saves + 1


class PhoneArgs(BaseModel):
    phone: Phone


class Found(BaseModel):
    phone: str
    learner: Learner | None = None
    plan: Plan | None = None
    draft: Plan | None = None
    session: Session | None = None


class ConfirmPhone(Operation[FaroCall, PhoneArgs, Found, Prompted]):
    """Look up exactly the number the caller agreed to, and open their session. Nothing is read before the yes."""

    name = 'confirm_phone'
    args_type = PhoneArgs
    receipt_type = Found
    proposal_about = 'phone'

    def proposal(self, args: PhoneArgs) -> BaseProposal:
        return BaseProposal(about='phone', value=args.phone)

    async def _agreed(self, call: FaroCall, question: Any, *, usage: Any, usage_limits: Any) -> bool:
        return agreed(call)

    async def _execute(self, call: FaroCall, args: PhoneArgs, grant: Any) -> dict:
        found = await call.store.lookup(args.phone)
        if found is None:
            return {'phone': args.phone}
        learner, plan, last = found
        if learner.id in ACTIVE:
            raise ModelRetry(
                'This learner is already on another call. Tell the caller, and do not share any profile.'
            )
        draft_id = last.checkpoint.draft_id if last is not None else 0
        draft = await call.store.plan(learner.id, draft_id) if draft_id else None
        session = await call.store.open_session(learner.id)
        ACTIVE.add(learner.id)
        return {'phone': args.phone, 'learner': learner, 'plan': plan, 'draft': draft, 'session': session}

    async def _record(self, call: FaroCall, args: PhoneArgs, receipt: Found) -> Prompted:
        if receipt.learner is None or receipt.session is None:
            call.commit(phone=receipt.phone, phase='signup')
            return Prompted(
                about='signup', spoken=curriculum.signup_offer(call.state.explanation), value=receipt.phone
            )
        call.commit(phone=receipt.phone, phase=_resume_phase(receipt.learner, receipt.session, receipt.draft))
        draft = receipt.draft if receipt.draft is not None and receipt.draft.status == 'draft' else None
        call.load(receipt.learner, receipt.plan, receipt.session, draft, saved=1)
        return opening(call)


def _resume_phase(learner: Learner, session: Session, draft: Plan | None) -> str:
    """Where a returning learner stands: an agreed plan always opens on the choice; onboarding resumes its step."""
    if learner.status == 'active':
        return 'menu'
    phase = session.checkpoint.phase
    if phase == 'plan' and (draft is None or draft.status != 'draft'):
        return 'assessment'
    return phase if phase in ('onboarding', 'assessment', 'plan') else 'onboarding'


def opening(call: FaroCall) -> Prompted:
    """What a confirmed returning learner hears first, in their own explanation language."""
    state, plan, draft = call.state, call.plan, call.draft
    if state.phase == 'plan' and draft is not None:
        return propose(call, draft, greet=True)
    if plan is None:
        step = restate(call)
        hello = curriculum.resume_onboarding(state.name, state.explanation)
        return Prompted(about=step.about, spoken=f'{hello} {step.spoken}', value=step.value)
    if plan.finished:
        spoken = (
            f'Hola, {state.name}. Has completado todas las lecciones de tu plan. ¿Hacemos una evaluación nueva o repasamos?'
            if state.explanation == 'ES'
            else f'Hi, {state.name}. You have finished every lesson in your plan. Shall we reassess, or recap?'
        )
        return Prompted(about='menu', spoken=spoken)
    progress = plan.progress
    previous = plan.lessons[progress.lesson - 1] if progress.lesson else ''
    spoken = curriculum.status(
        state.name,
        state.explanation,
        plan.lessons[progress.lesson],
        progress.item,
        previous,
        bool(state.item),
    )
    return Prompted(about='menu', spoken=spoken)


def propose(call: FaroCall, draft: Plan, *, greet: bool = False) -> Prompted:
    """Put one draft at one revision to the learner, in words this code chose. Only a yes to these words activates it."""
    words = curriculum.plan_proposal(
        draft.target, draft.goal, draft.level, draft.minutes, call.state.explanation
    )
    if greet:
        words = f'{"Hola" if call.state.explanation == "ES" else "Hi"}, {call.state.name}. {words}'
    return Prompted(about='plan', spoken=words, value=f'{draft.id}:{draft.revision}')


async def read_back_phone(call: FaroCall, phone: str) -> Prompted:
    """Read a number back before anything is looked up. A new number clears whatever the old one had loaded."""
    if call.state.learner_id:
        ACTIVE.discard(call.state.learner_id)
    call._learner = call._plan = call._draft = call._session = None
    call.commit(
        phone='', learner_id=0, name='', brief='', signup_agreed=False, phase='identify', task='', item=''
    )
    return Prompted(about='phone', spoken=curriculum.read_back(phone, call.state.explanation), value=phone)


async def accept_signup(call: FaroCall, question: Any) -> Prompted:
    """A yes to the sign-up offer: permission to create a profile, not yet a profile. The name comes next."""
    call.answered()
    call.commit(signup_agreed=True, phase='name')
    return Prompted(about='name', spoken=curriculum.ask_name(call.state.explanation))


class NameArgs(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class Created(BaseModel):
    learner: Learner
    session: Session


class CreateLearner(Operation[FaroCall, NameArgs, Created, Prompted]):
    """Create the profile the caller agreed to, under the phone they confirmed. One phone, one profile."""

    name = 'save_name'
    args_type = NameArgs
    receipt_type = Created
    needs_approval = False

    async def _authorise(self, call: FaroCall, args: NameArgs, *, usage: Any, usage_limits: Any) -> None:
        state = call.state
        _refuse_unless(
            bool(state.phone) and not state.learner_id, 'the phone is not confirmed, or a profile is loaded.'
        )
        _refuse_unless(state.signup_agreed, 'the caller has not agreed to create a profile.')

    async def _execute(self, call: FaroCall, args: NameArgs, grant: Any) -> dict:
        learner, session = await call.store.create_learner(
            call.state.phone, args.name.strip(), call.state.explanation
        )
        ACTIVE.add(learner.id)
        return {'learner': learner, 'session': session}

    async def _record(self, call: FaroCall, args: NameArgs, receipt: Created) -> Prompted:
        call.commit(phase='onboarding')
        call.load(receipt.learner, None, receipt.session, saved=1)
        return Prompted(
            about='onboarding', spoken=curriculum.welcome(receipt.learner.name, call.state.explanation)
        )


class OnboardingArgs(BaseModel):
    target: Language | None = None
    goal: Goal | None = None
    minutes: int | None = Field(default=None, ge=5, le=60)


class Checkpointed(BaseModel):
    session: Session


class SaveOnboarding(Operation[FaroCall, OnboardingArgs, Checkpointed, Prompted]):
    """Save the onboarding answers given so far, then ask the next one; once all three are in, the placement starts."""

    name = 'save_onboarding'
    args_type = OnboardingArgs
    receipt_type = Checkpointed
    needs_approval = False

    async def _authorise(
        self, call: FaroCall, args: OnboardingArgs, *, usage: Any, usage_limits: Any
    ) -> None:
        _refuse_unless(call.state.phase == 'onboarding', 'onboarding is not the step under way.')
        _refuse_unless(bool(args.target or args.goal or args.minutes), 'no answer was given.')

    async def _execute(self, call: FaroCall, args: OnboardingArgs, grant: Any) -> dict:
        given = {k: v for k, v in args.model_dump().items() if v}
        checkpoint = call.session.checkpoint.model_copy(update=given)
        if checkpoint.target and checkpoint.goal and checkpoint.minutes:
            first = curriculum.next_task(checkpoint.target, [])
            assert first is not None
            checkpoint = checkpoint.model_copy(
                update={'phase': 'assessment', 'task': first.id, 'evidence': []}
            )
        return {'session': await call.store.save_checkpoint(call.session, checkpoint)}

    async def _record(self, call: FaroCall, args: OnboardingArgs, receipt: Checkpointed) -> Prompted:
        checkpoint = receipt.session.checkpoint
        call.commit(phase=checkpoint.phase)
        assert call.learner is not None
        call.load(call.learner, None, receipt.session, saved=1)
        if checkpoint.phase == 'assessment':
            spoken = curriculum.placement(call.state.explanation, checkpoint.task, first=True)
            return Prompted(about='task', spoken=spoken, value=checkpoint.task)
        missing = next(name for name in ('target', 'goal', 'minutes') if not getattr(checkpoint, name))
        return Prompted(about='onboarding', spoken=curriculum.ONBOARDING[missing][call.state.explanation])


class LanguageArgs(BaseModel):
    language: Language


class LanguageSet(BaseModel):
    language: str
    session: Session | None = None


class SetLanguage(Operation[FaroCall, LanguageArgs, LanguageSet, Prompted]):
    """Change the language Faro explains in, then ask the question waiting again, in that language.

    Asking to switch uses up the learner's turn, so the question they were
    answering would expire and their next "yes" would answer nothing. The
    question is put again instead, in code's words, and stays answerable.
    Stored on the profile once there is one.
    """

    name = 'set_language'
    args_type = LanguageArgs
    receipt_type = LanguageSet
    needs_approval = False

    async def _execute(self, call: FaroCall, args: LanguageArgs, grant: Any) -> dict:
        if not call.state.learner_id:
            return {'language': args.language}
        session = await call.store.save_checkpoint(
            call.session, call.session.checkpoint, explanation=args.language
        )
        return {'language': args.language, 'session': session}

    async def _record(self, call: FaroCall, args: LanguageArgs, receipt: LanguageSet) -> Prompted:
        call.commit(explanation=args.language)
        if receipt.session is not None and call.learner is not None:
            learner = call.learner.model_copy(update={'explanation': args.language})
            call.load(learner, call.plan, receipt.session, call.draft, saved=1)
        return restate(call)


def restate(call: FaroCall) -> Prompted:
    """The question the learner was answering, put again in the call's language, or where the call stands."""
    state, explanation = call.state, call.state.explanation
    last = call.last_proposal()
    about, value = (last[0].about, last[0].value) if last is not None else ('', '')
    if about == 'phone' and value:
        return Prompted(about='phone', spoken=curriculum.read_back(value, explanation), value=value)
    if about == 'signup':
        return Prompted(about='signup', spoken=curriculum.signup_offer(explanation), value=value)
    if state.phase == 'name':
        return Prompted(about='name', spoken=curriculum.ask_name(explanation))
    if state.phase == 'onboarding':
        missing = next((n for n in ('target', 'goal', 'minutes') if not getattr(state, n)), 'minutes')
        return Prompted(about='onboarding', spoken=curriculum.ONBOARDING[missing][explanation])
    if state.phase == 'plan' and call.draft is not None:
        return propose(call, call.draft)
    if state.phase == 'assessment' and state.task:
        return Prompted(about='task', spoken=curriculum.placement(explanation, state.task), value=state.task)
    if state.phase == 'lesson' and state.item:
        return exercise(call)
    if state.phase == 'menu' and call.plan is not None:
        return opening(call)
    if state.phase == 'identify':
        return Prompted(about='language', spoken=curriculum.ask_number(explanation))
    return Prompted(
        about='language', spoken='Claro, seguimos.' if explanation == 'ES' else 'Sure, let us carry on.'
    )


class AssessmentArgs(BaseModel):
    task_id: str
    answer: str = Field(max_length=600)
    assistance: str = Field(default='', max_length=200)
    result: Result
    rationale: str = Field(max_length=300)
    feedback: str = Field(default='', max_length=200)
    """Said only before a retry, such as "I didn't catch that." A placement does not correct."""


class Assessed(BaseModel):
    session: Session
    draft: Plan | None = None
    retained: bool = False


class RecordAssessment(Operation[FaroCall, AssessmentArgs, Assessed, Prompted]):
    """Save one placement answer. After the last one: a level from the rule and a plan proposal, or the plan kept."""

    name = 'record_assessment'
    args_type = AssessmentArgs
    receipt_type = Assessed
    needs_approval = False

    async def _authorise(
        self, call: FaroCall, args: AssessmentArgs, *, usage: Any, usage_limits: Any
    ) -> None:
        _refuse_unless(call.state.phase == 'assessment', 'no placement is under way.')
        _refuse_unless(
            args.task_id == call.state.task, f'the task waiting is {call.state.task}, not {args.task_id}.'
        )

    async def _execute(self, call: FaroCall, args: AssessmentArgs, grant: Any) -> dict:
        session = call.session
        checkpoint = session.checkpoint
        target = checkpoint.target or call.state.target
        assert target is not None
        evidence = [
            *checkpoint.evidence,
            Evidence(
                **args.model_dump(exclude={'feedback'}), bank=curriculum.BANK_VERSION, session_id=session.id
            ),
        ]
        results = [(e.task_id, e.result) for e in evidence]
        unclear_once = (
            args.result == 'insufficient_evidence' and [e.task_id for e in evidence].count(args.task_id) == 1
        )
        following = curriculum.task(args.task_id) if unclear_once else curriculum.next_task(target, results)
        if following is not None:
            checkpoint = checkpoint.model_copy(update={'evidence': evidence, 'task': following.id})
            return {'session': await call.store.save_checkpoint(session, checkpoint)}
        level, uncertainty = curriculum.level(results)
        current = call.plan
        if current is not None and current.level == level:
            checkpoint = checkpoint.model_copy(
                update={'evidence': evidence, 'task': '', 'phase': 'menu', 'reassessing': False}
            )
            return {'session': await call.store.save_checkpoint(session, checkpoint), 'retained': True}
        goal = current.goal if current else checkpoint.goal
        assert goal is not None
        draft = Plan(
            id=0,
            learner_id=session.learner_id,
            target=target,
            status='draft',
            revision=1,
            progress_revision=0,
            goal=goal,
            level=level,
            uncertainty=uncertainty,
            minutes=current.minutes if current else checkpoint.minutes,
            lessons=[lesson.id for lesson in curriculum.lessons(target, goal)],
            assessment=evidence,
            progress=current.progress if current else Progress(),
        )
        checkpoint = checkpoint.model_copy(update={'evidence': evidence, 'task': '', 'phase': 'plan'})
        plan, saved = await call.store.save_draft(session, draft, checkpoint)
        return {'session': saved, 'draft': plan}

    async def _record(self, call: FaroCall, args: AssessmentArgs, receipt: Assessed) -> Prompted:
        assert call.learner is not None
        call.commit(phase=receipt.session.checkpoint.phase)
        call.load(call.learner, call.plan, receipt.session, receipt.draft, saved=1)
        if receipt.draft is not None:
            return propose(call, receipt.draft)
        if receipt.retained:
            spoken = (
                f'Tu nivel sigue en {call.state.level}, así que mantenemos tu plan. ¿Seguimos con la lección?'
                if call.state.explanation == 'ES'
                else f'Your level is still {call.state.level}, so your plan stays as it is. Shall we carry on with the lesson?'
            )
            return Prompted(about='menu', spoken=spoken)
        explanation, waiting = call.state.explanation, call.state.task
        if waiting == args.task_id:
            spoken = f'{args.feedback} {curriculum.placement(explanation, waiting, again=True)}'.strip()
            return Prompted(about='task', spoken=spoken, value=waiting)
        return Prompted(about='task', spoken=curriculum.placement(explanation, waiting), value=waiting)


class PlanRef(BaseModel):
    plan_id: int
    revision: int

    @classmethod
    def of(cls, value: str) -> PlanRef:
        plan_id, _, revision = value.partition(':')
        return cls(plan_id=int(plan_id), revision=int(revision))


class Activated(BaseModel):
    plan: Plan
    session: Session


class ActivatePlan(Operation[FaroCall, PlanRef, Activated, Prompted]):
    """Activate exactly the proposal the learner heard in full and said yes to. The plan before is archived."""

    name = 'activate_plan'
    args_type = PlanRef
    receipt_type = Activated
    proposal_about = 'plan'

    def proposal(self, args: PlanRef) -> BaseProposal:
        return BaseProposal(about='plan', value=f'{args.plan_id}:{args.revision}')

    async def _agreed(self, call: FaroCall, question: Any, *, usage: Any, usage_limits: Any) -> bool:
        return agreed(call)

    async def _execute(self, call: FaroCall, args: PlanRef, grant: Any) -> dict:
        draft = call.draft
        _refuse_unless(
            draft is not None and draft.id == args.plan_id and draft.revision == args.revision,
            'that is not the proposal on the table.',
        )
        assert draft is not None
        checkpoint = call.session.checkpoint.model_copy(
            update={
                'phase': 'menu',
                'draft_id': 0,
                'task': '',
                'evidence': [],
                'reassessing': False,
                'item': '',
            }
        )
        plan, session = await call.store.activate(call.session, args.plan_id, args.revision, checkpoint)
        return {'plan': plan, 'session': session}

    async def _record(self, call: FaroCall, args: PlanRef, receipt: Activated) -> Prompted:
        assert call.learner is not None
        call.commit(phase='menu')
        call.load(
            call.learner.model_copy(update={'status': 'active'}), receipt.plan, receipt.session, saved=1
        )
        return Prompted(about='menu', spoken=curriculum.plan_active(call.state.explanation))


class ReviseArgs(BaseModel):
    minutes: int | None = Field(default=None, ge=5, le=60)
    goal: Goal | None = None


class Revised(BaseModel):
    draft: Plan


class RevisePlan(Operation[FaroCall, ReviseArgs, Revised, Prompted]):
    """Change the proposal's length or goal, then put the new revision to the learner. Nothing is activated."""

    name = 'revise_plan'
    args_type = ReviseArgs
    receipt_type = Revised
    needs_approval = False

    async def _authorise(self, call: FaroCall, args: ReviseArgs, *, usage: Any, usage_limits: Any) -> None:
        _refuse_unless(call.state.phase == 'plan' and call.draft is not None, 'no proposal is on the table.')
        _refuse_unless(bool(args.minutes or args.goal), 'no change was given.')

    async def _execute(self, call: FaroCall, args: ReviseArgs, grant: Any) -> dict:
        assert call.draft is not None
        return {
            'draft': await call.store.revise_draft(
                call.session, call.draft.id, args.minutes or 0, args.goal or ''
            )
        }

    async def _record(self, call: FaroCall, args: ReviseArgs, receipt: Revised) -> Prompted:
        assert call.learner is not None
        call.load(call.learner, call.plan, call.session, receipt.draft, saved=1)
        return propose(call, receipt.draft)


class Empty(BaseModel):
    pass


class Declined(BaseModel):
    session: Session


class DeclinePlan(Operation[FaroCall, Empty, Declined, str]):
    """A revised plan turned down: the evidence is kept, the agreed plan stays exactly as it was."""

    name = 'decline_plan'
    args_type = Empty
    receipt_type = Declined
    needs_approval = False

    async def _authorise(self, call: FaroCall, args: Empty, *, usage: Any, usage_limits: Any) -> None:
        _refuse_unless(call.state.phase == 'plan' and call.draft is not None, 'no proposal is on the table.')
        _refuse_unless(
            call.plan is not None, 'there is no agreed plan to keep; revise the proposal or stop here.'
        )

    async def _execute(self, call: FaroCall, args: Empty, grant: Any) -> dict:
        assert call.draft is not None
        checkpoint = call.session.checkpoint.model_copy(
            update={'phase': 'menu', 'draft_id': 0, 'reassessing': False}
        )
        return {'session': await call.store.decline(call.session, call.draft.id, checkpoint)}

    async def _record(self, call: FaroCall, args: Empty, receipt: Declined) -> str:
        assert call.learner is not None
        call.commit(phase='menu')
        call.load(call.learner, call.plan, receipt.session, saved=1)
        return (
            'Declined and noted. The agreed plan stays as it was. Offer to carry on with the lesson or recap.'
        )


class Opened(BaseModel):
    session: Session


class ContinueLesson(Operation[FaroCall, Empty, Opened, Prompted]):
    """Put the waiting exercise to the learner, from the plan: an unanswered one is asked again."""

    name = 'continue_lesson'
    args_type = Empty
    receipt_type = Opened
    needs_approval = False

    async def _authorise(self, call: FaroCall, args: Empty, *, usage: Any, usage_limits: Any) -> None:
        _refuse_unless(
            call.state.phase in ('menu', 'lesson') and call.plan is not None, 'there is no agreed plan.'
        )
        assert call.plan is not None
        _refuse_unless(not call.plan.finished, 'every lesson in the plan is complete; offer a reassessment.')

    async def _execute(self, call: FaroCall, args: Empty, grant: Any) -> dict:
        assert call.plan is not None
        checkpoint = call.session.checkpoint.model_copy(
            update={'phase': 'lesson', 'item': call.plan.waiting()}
        )
        return {'session': await call.store.save_checkpoint(call.session, checkpoint)}

    async def _record(self, call: FaroCall, args: Empty, receipt: Opened) -> Prompted:
        assert call.learner is not None
        call.commit(phase='lesson')
        call.load(call.learner, call.plan, receipt.session, saved=1)
        return exercise(call)


def exercise(call: FaroCall, feedback: str = '', *, again: bool = False) -> Prompted:
    """The waiting item, as the learner hears it, in their own language, after any feedback on the last try."""
    waiting = call.state.item
    spoken = f'{feedback} {curriculum.exercise(call.state.explanation, waiting, again=again)}'.strip()
    return Prompted(about='item', spoken=spoken, value=waiting)


class AnswerArgs(BaseModel):
    item_id: str
    answer: str = Field(max_length=600)
    result: Result
    feedback: str = Field(default='', max_length=200)
    """One short sentence said before the next line: praise, a correction or a hint, never the whole answer."""


class Answered(BaseModel):
    plan: Plan
    session: Session
    replayed: bool


class RecordAnswer(Operation[FaroCall, AnswerArgs, Answered, Prompted]):
    """Save one answer to the waiting item, once: evidence, progress, review and the next resume point together.

    The attempt id comes from the session, the item and the caller turn, so
    the same answer recorded twice finds the first record and counts once.
    """

    name = 'record_answer'
    args_type = AnswerArgs
    receipt_type = Answered
    needs_approval = False

    async def _authorise(self, call: FaroCall, args: AnswerArgs, *, usage: Any, usage_limits: Any) -> None:
        _refuse_unless(call.state.phase == 'lesson' and call.plan is not None, 'no exercise is under way.')
        _refuse_unless(
            args.item_id == call.state.item, f'the exercise waiting is {call.state.item}, not {args.item_id}.'
        )

    async def _execute(self, call: FaroCall, args: AnswerArgs, grant: Any) -> dict:
        assert call.plan is not None
        attempt = Attempt(
            id=operation_token(call.session.id, args.item_id, call.turn),
            item_id=args.item_id,
            answer=args.answer,
            result=args.result,
            session_id=call.session.id,
        )
        plan, session, replayed = await call.store.record_attempt(call.session, call.plan.id, attempt)
        return {'plan': plan, 'session': session, 'replayed': replayed}

    async def _record(self, call: FaroCall, args: AnswerArgs, receipt: Answered) -> Prompted:
        assert call.learner is not None and call.plan is not None
        before = len(call.plan.progress.completed)
        call.load(call.learner, receipt.plan, receipt.session, saved=0 if receipt.replayed else 1)
        plan = receipt.plan
        if len(plan.progress.completed) > before:
            finished = curriculum.lesson(plan.progress.completed[-1]).topic[call.state.explanation]
            following = (
                ''
                if plan.finished
                else curriculum.lesson(plan.lessons[plan.progress.lesson]).topic[call.state.explanation]
            )
            done = curriculum.lesson_done(call.state.explanation, finished, following)
            return Prompted(about='menu', spoken=f'{args.feedback} {done}'.strip())
        return exercise(call, args.feedback, again=plan.waiting() == args.item_id)


class Reassess(BaseModel):
    session: Session


class StartReassessment(Operation[FaroCall, Empty, Reassess, Prompted]):
    """Start a short reassessment. The agreed plan stands until a revision is accepted."""

    name = 'start_reassessment'
    args_type = Empty
    receipt_type = Reassess
    needs_approval = False

    async def _authorise(self, call: FaroCall, args: Empty, *, usage: Any, usage_limits: Any) -> None:
        _refuse_unless(
            call.state.phase in ('menu', 'lesson') and call.plan is not None, 'there is no agreed plan.'
        )

    async def _execute(self, call: FaroCall, args: Empty, grant: Any) -> dict:
        assert call.plan is not None
        first = curriculum.next_task(call.plan.target, [])
        assert first is not None
        checkpoint = call.session.checkpoint.model_copy(
            update={'phase': 'assessment', 'task': first.id, 'evidence': [], 'reassessing': True, 'item': ''}
        )
        return {'session': await call.store.save_checkpoint(call.session, checkpoint)}

    async def _record(self, call: FaroCall, args: Empty, receipt: Reassess) -> Prompted:
        assert call.learner is not None
        call.commit(phase='assessment')
        call.load(call.learner, call.plan, receipt.session, saved=1)
        spoken = curriculum.placement(call.state.explanation, call.state.task, first=True)
        return Prompted(about='task', spoken=spoken, value=call.state.task)


class FactArgs(BaseModel):
    key: str = Field(max_length=40)
    text: str = Field(max_length=200)


class Remembered(BaseModel):
    learner: Learner


class Remember(Operation[FaroCall, FactArgs, Remembered, str]):
    """Keep one thing the learner said about themselves. Same key, new fact: the correction replaces it."""

    name = 'remember'
    args_type = FactArgs
    receipt_type = Remembered
    needs_approval = False

    async def _authorise(self, call: FaroCall, args: FactArgs, *, usage: Any, usage_limits: Any) -> None:
        _refuse_unless(bool(call.state.learner_id), 'no learner is confirmed.')

    async def _execute(self, call: FaroCall, args: FactArgs, grant: Any) -> dict:
        fact = MemoryFact(key=args.key.strip().lower(), text=args.text.strip(), session_id=call.session.id)
        return {'learner': await call.store.remember(call.state.learner_id, fact)}

    async def _record(self, call: FaroCall, args: FactArgs, receipt: Remembered) -> str:
        call.load(receipt.learner, call.plan, call.session, call.draft, saved=1)
        return 'Remembered. Carry on with what you were doing.'


def recap(call: FaroCall) -> str:
    """What has been practised, what needs review and what comes next, read from the plan. Changes nothing."""
    plan = call.plan
    if plan is None:
        return 'There is no agreed plan yet, so there is nothing to recap.'
    done = [curriculum.lesson(i).topic['EN'] for i in plan.progress.completed]
    review = [curriculum.item(i)[1].instruction['EN'] for i in plan.progress.review]
    following = (
        'the plan is complete'
        if plan.finished
        else f'next: {curriculum.lesson(plan.lessons[plan.progress.lesson]).objective}'
    )
    answered = len([a for a in plan.progress.attempts if not a.id.startswith('seed-')])
    return (
        f'Plan: {plan.target} {plan.goal}, provisional {plan.level}. Lessons completed: {", ".join(done) or "none"}. '
        f'Needs review: {"; ".join(review) or "nothing"}. {following}. Answers recorded in sessions with Faro: {answered}. '
        'A recap is not an assessment: it changes no level and completes nothing.'
    )


async def finish(call: FaroCall, outcome: str) -> str:
    """Close the session once, whatever ended it: a goodbye, a dropped line. No model runs here.

    The summary is written by code from what the store recorded this
    session, so it cannot claim progress that did not happen.
    """
    if call._finished or call._session is None:
        call._finished = True
        return ''
    call._finished = True
    learned = call.session.checkpoint.learned
    if call.state.explanation == 'ES':
        summary = (
            f'Hoy has practicado {learned} ejercicio{"s" if learned != 1 else ""}.'
            if learned
            else 'Hoy hemos hablado sin hacer ejercicios nuevos.'
        )
    else:
        summary = (
            f'Today you practised {learned} exercise{"s" if learned != 1 else ""}.'
            if learned
            else 'Today we talked without new exercises.'
        )
    if call.plan is not None and not call.plan.finished:
        summary += f' {curriculum.next_step(call.state.explanation, call.plan.waiting())}'
    outcome = f'{outcome}: {learned} exercises' if learned else f'{outcome}: no learning'
    closed = await call.store.finalize(call.session.id, call.session.learner_id, outcome, summary)
    ACTIVE.discard(call.session.learner_id)
    if closed:
        call.commit(saves=_saved(call), phase='closed')
    return summary


CONFIRM_PHONE = ConfirmPhone()
CREATE_LEARNER = CreateLearner()
SAVE_ONBOARDING = SaveOnboarding()
SET_LANGUAGE = SetLanguage()
RECORD_ASSESSMENT = RecordAssessment()
ACTIVATE_PLAN = ActivatePlan()
REVISE_PLAN = RevisePlan()
DECLINE_PLAN = DeclinePlan()
CONTINUE_LESSON = ContinueLesson()
RECORD_ANSWER = RecordAnswer()
START_REASSESSMENT = StartReassessment()
REMEMBER = Remember()
