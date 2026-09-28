"""Faro's domain functions: the typed functions `agent.yaml` connects to, and the call's services.

Each function takes the call's services (`Faro`) and a validated argument
model that core builds from the state (`args:` in the manifest), and returns
a receipt model; core validates it before a field is read. Most receipts
carry `said`, the application's words for what happened in the learner's
explanation language, and `next`, the step the learner goes to now
(`route: next`): the store's record of where they are decides it, never a
model.

Three actions wait on the learner's yes to an exact question this code
words: confirming the phone, agreeing to a profile (the yes is to the
sign-up offer), and activating a plan (the yes is to one proposal). The rest
are routine saves inside a step that has already been reached, and each
checks what the store says before it writes.

Grading is the step's agent's: it records the attempt and its grade
(`collect`), and code carries out `record_attempt` straight after
(`carries_out:`). What an attempt counts as is decided here: help before
the answer, or a miss before it in this session, makes a right answer
assisted, never independent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from core import Intent, Refused
from faro import curriculum
from faro.curriculum import Goal, Lang, Level, Result
from faro.models import Attempt, Checkpoint, Evidence, Learner, MemoryFact, Phone, Plan, Progress, Session
from faro.state import FaroState, Grade
from faro.store import OwnershipError, StaleCheckpointError, Store

ACTIVE: set[int] = set()
"""Learners on a call in this process. A second call for the same learner is refused.

ponytail: one process. Move it into the store when a second process serves calls.
"""

type Known = Annotated[int, Field(ge=1)]
"""A learner or session id: present only once the phone is confirmed and the store answered."""


class Faro:
    """One call's services: the learning store, and the call's own facts for the words code says.

    `facts` is set by `open_call` right after the call opens. The words of a
    read-back or a receipt follow the language the call is in now; the
    language is never an argument, because a receipt-set fact rests on its
    receipt's arguments, and a learner switching language must not unload
    the learner.
    """

    def __init__(self, store: Store) -> None:
        self.store = store
        self.facts: Callable[[], FaroState] = FaroState

    @property
    def language(self) -> Lang:
        return self.facts().explanation


def _refuse_unless(condition: bool, why: str) -> None:
    if not condition:
        raise Refused(f'Nothing was saved: {why}')


async def _session(faro: Faro, learner_id: int, session_id: int) -> Session:
    """The session as the store has it now. A session the store closed or that is not this learner's is refused."""
    try:
        session = await faro.store.session(session_id, learner_id)
    except OwnershipError as error:
        raise Refused(f'Nothing was saved: {error}') from None
    _refuse_unless(session.status == 'active', 'this session is closed.')
    return session


async def _write(faro: Faro, session: Session, checkpoint: Checkpoint) -> Session:
    """Save a checkpoint over the session it was built from, in the call's language. Stale is an authoritative no."""
    try:
        return await faro.store.save_checkpoint(session, checkpoint, explanation=faro.language)
    except StaleCheckpointError as error:
        raise Refused(f'Nothing was saved: {error}') from None


def brief(learner: Learner, plan: Plan | None) -> str:
    """The learner as the store knows them, for the steps' agents: sourced facts only, never instructions."""
    lines = [f'Learner: {learner.name}. Explains in {learner.explanation}. Profile status: {learner.status}.']
    lines += [f'Remembered ({f.key}, from session {f.session_id}): {f.text}' for f in learner.memory]
    if plan is not None:
        done = [curriculum.lesson(i).topic['EN'] for i in plan.progress.completed]
        lines.append(
            f'Agreed plan: learning {plan.target} {plan.goal}, provisional {plan.level}, {plan.minutes}-minute sessions.'
        )
        lines.append(f'Lessons completed: {", ".join(done) or "none yet"}.')
        review = [curriculum.item(i)[1].instruction['EN'] for i in plan.progress.review]
        lines.append(f'Review items (practised with help): {"; ".join(review) or "none"}.')
        if plan.summary:
            lines.append(f'Last session: {plan.summary}')
    return '\n'.join(lines)


class PhoneArgs(BaseModel):
    phone: Annotated[Phone, Field(min_length=1)]


class Found(BaseModel):
    """What the store has under the number the caller agreed to, and where they carry on."""

    phone: str
    learner_id: int = 0
    learner_name: str = ''
    profile_language: Lang | None = None
    session_id: int = 0
    notes: str = ''
    draft_id: int = 0
    draft_revision: int = 0
    draft_target: Lang | None = None
    draft_goal: Goal | None = None
    draft_level: Level | None = None
    draft_minutes: int = 0
    waiting: str = ''
    next: Literal['signup', 'onboarding', 'assessment', 'plan', 'menu']
    said: str = ''


def phone_words(faro: Faro, args: PhoneArgs) -> str:
    return curriculum.read_back(args.phone, faro.language)


async def confirm_phone(faro: Faro, args: PhoneArgs) -> Found:
    """Look up exactly the number the caller agreed to and open their session. Nothing is read before the yes."""
    found = await faro.store.lookup(args.phone)
    if found is None:
        return Found(phone=args.phone, next='signup')
    learner, plan, last = found
    if learner.id in ACTIVE:
        raise Refused(
            'This learner is already on another call. Tell the caller, and do not share any profile.'
        )
    draft = None
    if last is not None and last.checkpoint.draft_id:
        draft = await faro.store.plan(learner.id, last.checkpoint.draft_id)
    draft = draft if draft is not None and draft.status == 'draft' else None
    session = await faro.store.open_session(learner.id)
    ACTIVE.add(learner.id)
    language = faro.facts().asked_language or learner.explanation
    step, said = _resume(learner, plan, session, draft, language)
    return Found(
        phone=args.phone,
        learner_id=learner.id,
        learner_name=learner.name,
        profile_language=learner.explanation,
        session_id=session.id,
        notes=brief(learner, plan),
        draft_id=draft.id if draft else 0,
        draft_revision=draft.revision if draft else 0,
        draft_target=draft.target if draft else None,
        draft_goal=draft.goal if draft else None,
        draft_level=draft.level if draft else None,
        draft_minutes=draft.minutes if draft else 0,
        waiting=session.checkpoint.task if step == 'assessment' else session.checkpoint.item,
        next=step,
        said=said,
    )


def _resume(
    learner: Learner, plan: Plan | None, session: Session, draft: Plan | None, language: Lang
) -> tuple[Literal['onboarding', 'assessment', 'plan', 'menu'], str]:
    """Where a returning learner stands, and what they hear first: an agreed plan always opens on the choice."""
    checkpoint = session.checkpoint
    if learner.status == 'active' and plan is not None:
        if plan.finished:
            return 'menu', curriculum.finished_plan(learner.name, language)
        progress = plan.progress
        previous = plan.lessons[progress.lesson - 1] if progress.lesson else ''
        said = curriculum.status(
            learner.name,
            language,
            plan.lessons[progress.lesson],
            progress.item,
            previous,
            bool(checkpoint.item),
        )
        return 'menu', said
    if checkpoint.phase == 'plan' and draft is not None:
        return 'plan', curriculum.hello(learner.name, language)
    hello = curriculum.resume_onboarding(learner.name, language)
    if checkpoint.phase == 'assessment' and checkpoint.task:
        return 'assessment', f'{hello} {curriculum.placement(language, checkpoint.task)}'
    return 'onboarding', f'{hello} {_next_question(checkpoint, language)}'


def _next_question(checkpoint: Checkpoint, language: Lang) -> str:
    missing = next(name for name in ('target', 'goal', 'minutes') if not getattr(checkpoint, name))
    return curriculum.ONBOARDING[missing][language]


class SignupArgs(BaseModel):
    phone: Annotated[Phone, Field(min_length=1)]


class Consent(BaseModel):
    phone: str
    agreed: bool
    said: str


def signup_words(faro: Faro, args: SignupArgs) -> str:
    return curriculum.signup_offer(faro.language)


def agree_signup(faro: Faro, args: SignupArgs) -> Consent:
    """A yes to the sign-up offer: permission to create a profile, not a profile yet. Nothing is stored."""
    return Consent(phone=args.phone, agreed=True, said=curriculum.ask_name(faro.language))


class NewLearner(BaseModel):
    phone: Annotated[Phone, Field(min_length=1)]
    name: Annotated[str, Field(min_length=1, max_length=40)]
    agreed: bool
    """The yes to the sign-up offer, on this call: without it there is nothing to create."""


class Created(BaseModel):
    phone: str
    learner_id: int
    learner_name: str
    profile_language: Lang
    session_id: int
    notes: str
    next: Literal['onboarding']
    said: str


async def create_learner(faro: Faro, args: NewLearner) -> Created:
    """Create the profile the caller agreed to, under the phone they confirmed. One phone, one profile."""
    _refuse_unless(
        args.agreed, 'the caller has not agreed to create a profile; wait for their yes to the offer.'
    )
    try:
        learner, session = await faro.store.create_learner(args.phone, args.name.strip(), faro.language)
    except OwnershipError as error:
        raise Refused(f'Nothing was saved: {error}') from None
    ACTIVE.add(learner.id)
    return Created(
        phone=args.phone,
        learner_id=learner.id,
        learner_name=learner.name,
        profile_language=learner.explanation,
        session_id=session.id,
        notes=brief(learner, None),
        next='onboarding',
        said=curriculum.welcome(learner.name, faro.language),
    )


class Onboarding(BaseModel):
    learner_id: Known
    session_id: Known
    learn: Lang | None = None
    purpose: Goal | None = None
    length: int | None = Field(default=None, ge=5, le=60)


class Onboarded(BaseModel):
    learner_id: int
    waiting: str = ''
    next: Literal['onboarding', 'assessment']
    said: str


async def save_onboarding(faro: Faro, args: Onboarding) -> Onboarded:
    """Save the onboarding answers given so far, then ask the next; once all three are in, the placement starts."""
    session = await _session(faro, args.learner_id, args.session_id)
    _refuse_unless(session.checkpoint.phase == 'onboarding', 'onboarding is not the step under way.')
    given = {
        name: value
        for name, value in (('target', args.learn), ('goal', args.purpose), ('minutes', args.length))
        if value
    }
    _refuse_unless(bool(given), 'no answer was given.')
    checkpoint = session.checkpoint.model_copy(update=given)
    if checkpoint.target and checkpoint.goal and checkpoint.minutes:
        first = curriculum.next_task(checkpoint.target, [])
        assert first is not None
        checkpoint = checkpoint.model_copy(update={'phase': 'assessment', 'task': first.id, 'evidence': []})
        await _write(faro, session, checkpoint)
        said = curriculum.placement(faro.language, first.id, first=True)
        return Onboarded(learner_id=args.learner_id, waiting=first.id, next='assessment', said=said)
    await _write(faro, session, checkpoint)
    return Onboarded(
        learner_id=args.learner_id, next='onboarding', said=_next_question(checkpoint, faro.language)
    )


class Learning(BaseModel):
    learner_id: Known
    session_id: Known


class Said(BaseModel):
    learner_id: int
    said: str


async def repeat_waiting(faro: Faro, args: Learning) -> Said:
    """Say the exercise or task waiting again, in the call's language now: after a switch of language. Changes nothing."""
    session = await _session(faro, args.learner_id, args.session_id)
    checkpoint = session.checkpoint
    if checkpoint.phase == 'lesson' and checkpoint.item:
        return Said(learner_id=args.learner_id, said=curriculum.exercise(faro.language, checkpoint.item))
    _refuse_unless(
        checkpoint.phase == 'assessment' and bool(checkpoint.task), 'nothing is waiting for an answer.'
    )
    return Said(learner_id=args.learner_id, said=curriculum.placement(faro.language, checkpoint.task))


class HelpArgs(BaseModel):
    learner_id: Known
    session_id: Known
    waiting: str = ''
    """What the call last put to the learner: it keeps one exercise's help apart from the next one's."""


class Helped(BaseModel):
    learner_id: int
    helped: str
    said: str = ''


async def note_help(faro: Faro, args: HelpArgs) -> Helped:
    """The learner is going to the coach: the exercise or task waiting is marked helped for this session, first.

    So "help before the answer" is the store's record, not a model's memory:
    an answer to it, in the coach or back in the lesson later, is assisted.
    """
    session = await _session(faro, args.learner_id, args.session_id)
    checkpoint = session.checkpoint
    waiting = checkpoint.item if checkpoint.phase == 'lesson' else checkpoint.task
    if waiting and waiting != checkpoint.helped:
        await _write(faro, session, checkpoint.model_copy(update={'helped': waiting}))
    return Helped(learner_id=args.learner_id, helped=waiting)


class AttemptArgs(BaseModel):
    learner_id: Known
    session_id: Known
    answer: Annotated[str, Field(min_length=1, max_length=600)]
    grade: Grade
    feedback: str = Field(default='', max_length=200)
    evidence: str = Field(default='', max_length=200)


class Recorded(BaseModel):
    """One attempt recorded, and where the learner goes: the same exercise again, the next one, or on."""

    learner_id: int
    result: Result
    replayed: bool = False
    draft_id: int = 0
    draft_revision: int = 0
    draft_target: Lang | None = None
    draft_goal: Goal | None = None
    draft_level: Level | None = None
    draft_minutes: int = 0
    waiting: str = ''
    next: Literal['assessment', 'lesson', 'coach', 'plan', 'menu']
    said: str


LESSON_RESULTS: dict[Grade, Result] = {
    'correct': 'independent',
    'partly': 'not_yet',
    'wrong': 'not_yet',
    'unclear': 'insufficient_evidence',
}
"""In a lesson only a correct sentence moves on: a partly right one is a miss, corrected and asked again."""
PLACEMENT_RESULTS: dict[Grade, Result] = {
    'correct': 'independent',
    'partly': 'assisted',
    'wrong': 'not_yet',
    'unclear': 'insufficient_evidence',
}
"""In a placement, partly right counts as done with help: a placement measures, and does not teach."""


async def record_attempt(faro: Faro, args: AttemptArgs, intent: Intent) -> Recorded:
    """Record one graded attempt at what is waiting, right or wrong, once: a lesson exercise or a placement task.

    The attempt is keyed by its intent, so the same attempt sent again after
    a lost reply finds its first record and counts once (`idempotent`).
    """
    session = await _session(faro, args.learner_id, args.session_id)
    if session.checkpoint.phase == 'lesson':
        return await _lesson_attempt(faro, args, intent, session)
    _refuse_unless(
        session.checkpoint.phase == 'assessment' and bool(session.checkpoint.task),
        'nothing is waiting for an answer.',
    )
    return await _placement_attempt(faro, args, intent, session)


async def find_attempt(faro: Faro, args: AttemptArgs, intent: Intent) -> Recorded | None:
    """What an attempt whose reply was lost did, by its intent: None while the store has no record of it.

    Found, the learner hears the line that is waiting now; the attempt counted
    once. Not found, core sends the same intent again (`idempotent`), and the
    store still counts it once.
    """
    session = await faro.store.session(args.session_id, args.learner_id)
    language = faro.language
    plan = await faro.store.active_plan(args.learner_id)
    attempt = next((a for a in plan.progress.attempts if a.id == intent.id), None) if plan else None
    if plan is not None and attempt is not None:
        waiting = plan.waiting()
        said = curriculum.exercise(language, waiting, again=waiting == attempt.item_id) if waiting else ''
        return Recorded(
            learner_id=args.learner_id,
            result=attempt.result,
            replayed=True,
            waiting=waiting,
            next='lesson',
            said=said,
        )
    evidence = next((e for e in session.checkpoint.evidence if e.id == intent.id), None)
    if evidence is None:
        return None
    task = session.checkpoint.task
    said = curriculum.placement(language, task) if task else ''
    return Recorded(
        learner_id=args.learner_id,
        result=evidence.result,
        replayed=True,
        waiting=task,
        next='assessment',
        said=said,
    )


def _missed_before(plan: Plan, session: Session, item_id: str) -> bool:
    """Whether this item was already answered wrongly in this session: a right answer now came after a correction."""
    return any(
        a.item_id == item_id and a.session_id == session.id and a.result == 'not_yet'
        for a in plan.progress.attempts
    )


async def _lesson_attempt(faro: Faro, args: AttemptArgs, intent: Intent, session: Session) -> Recorded:
    plan = await faro.store.active_plan(args.learner_id)
    _refuse_unless(plan is not None and not plan.finished, 'there is no exercise waiting.')
    assert plan is not None
    waiting = plan.waiting()
    helped = session.checkpoint.helped == waiting
    result = LESSON_RESULTS[args.grade]
    if result == 'independent' and (helped or _missed_before(plan, session, waiting)):
        result = 'assisted'
    attempt = Attempt(id=intent.id, item_id=waiting, answer=args.answer, result=result, session_id=session.id)
    try:
        after, _, replayed = await faro.store.record_attempt(session, plan.id, attempt)
    except (OwnershipError, StaleCheckpointError, ValueError) as error:
        raise Refused(f'Nothing was saved: {error}') from None
    language = faro.language
    if len(after.progress.completed) > len(plan.progress.completed):
        finished = curriculum.lesson(after.progress.completed[-1]).topic[language]
        following = (
            '' if after.finished else curriculum.lesson(after.lessons[after.progress.lesson]).topic[language]
        )
        said = f'{args.feedback} {curriculum.lesson_done(language, finished, following)}'.strip()
        return Recorded(learner_id=args.learner_id, result=result, replayed=replayed, next='menu', said=said)
    again = after.waiting() == waiting
    said = f'{args.feedback} {curriculum.exercise(language, after.waiting(), again=again)}'.strip()
    step = 'coach' if again and helped else 'lesson'
    return Recorded(
        learner_id=args.learner_id,
        result=result,
        replayed=replayed,
        waiting=after.waiting(),
        next=step,
        said=said,
    )


async def _placement_attempt(faro: Faro, args: AttemptArgs, intent: Intent, session: Session) -> Recorded:
    checkpoint = session.checkpoint
    waiting = checkpoint.task
    helped = checkpoint.helped == waiting
    result = PLACEMENT_RESULTS[args.grade]
    if result == 'independent' and helped:
        result = 'assisted'
    replayed = any(e.id == intent.id for e in checkpoint.evidence)
    evidence = list(checkpoint.evidence)
    if not replayed:
        evidence.append(
            Evidence(
                task_id=waiting,
                answer=args.answer,
                assistance='the companion helped first' if helped else '',
                result=result,
                rationale=args.evidence or args.grade,
                bank=curriculum.BANK_VERSION,
                session_id=session.id,
                id=intent.id,
            )
        )
    target = checkpoint.target or curriculum.target_of(waiting)
    results = [(e.task_id, e.result) for e in evidence]
    unclear_once = result == 'insufficient_evidence' and [e.task_id for e in evidence].count(waiting) == 1
    following = curriculum.task(waiting) if unclear_once else curriculum.next_task(target, results)
    language = faro.language
    if following is not None:
        if not replayed:
            await _write(
                faro, session, checkpoint.model_copy(update={'evidence': evidence, 'task': following.id})
            )
        again = following.id == waiting
        said = (
            f'{args.feedback} {curriculum.placement(language, waiting, again=True)}'.strip()
            if again
            else curriculum.placement(language, following.id)
        )
        step = 'coach' if again and helped else 'assessment'
        return Recorded(
            learner_id=args.learner_id,
            result=result,
            replayed=replayed,
            waiting=following.id,
            next=step,
            said=said,
        )
    level, uncertainty = curriculum.level(results)
    current = await faro.store.active_plan(args.learner_id)
    if current is not None and current.level == level:
        if not replayed:
            done = checkpoint.model_copy(
                update={'evidence': evidence, 'task': '', 'phase': 'menu', 'reassessing': False}
            )
            await _write(faro, session, done)
        said = curriculum.retained(level, language)
        return Recorded(learner_id=args.learner_id, result=result, replayed=replayed, next='menu', said=said)
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
    proposed = checkpoint.model_copy(update={'evidence': evidence, 'task': '', 'phase': 'plan'})
    try:
        saved, _ = await faro.store.save_draft(session, draft, proposed)
    except StaleCheckpointError as error:
        raise Refused(f'Nothing was saved: {error}') from None
    return Recorded(
        learner_id=args.learner_id,
        result=result,
        replayed=replayed,
        draft_id=saved.id,
        draft_revision=saved.revision,
        draft_target=saved.target,
        draft_goal=saved.goal,
        draft_level=saved.level,
        draft_minutes=saved.minutes,
        next='plan',
        said='',
    )


class ProposalArgs(BaseModel):
    """One plan proposal at one revision, with any change the learner asked for: exactly what a yes agrees to."""

    learner_id: Known
    session_id: Known
    plan_id: Known
    revision: Known
    target: Lang
    level: Level
    goal: Goal
    minutes: Known
    new_goal: Goal | None = None
    new_minutes: int | None = Field(default=None, ge=5, le=60)


def proposal_words(faro: Faro, args: ProposalArgs) -> str:
    return curriculum.plan_proposal(
        args.target, args.new_goal or args.goal, args.level, args.new_minutes or args.minutes, faro.language
    )


class Moved(BaseModel):
    """A save that moves the learner on; each kind names the one step it moves them to (`route: next`)."""

    learner_id: int
    waiting: str = ''
    said: str


class ToMenu(Moved):
    next: Literal['menu']


class ToLesson(Moved):
    next: Literal['lesson']


class ToPlacement(Moved):
    next: Literal['assessment']


async def activate_plan(faro: Faro, args: ProposalArgs) -> ToMenu:
    """Activate exactly the proposal the learner heard in full and said yes to, with the change they asked for.

    A change becomes a new revision of the draft first, then that revision is
    activated: the plan agreed is the one read back. The plan before is
    archived in the same store transaction as the activation.
    """
    session = await _session(faro, args.learner_id, args.session_id)
    revision = args.revision
    try:
        if (args.new_goal and args.new_goal != args.goal) or (
            args.new_minutes and args.new_minutes != args.minutes
        ):
            revised = await faro.store.revise_draft(
                session, args.plan_id, args.new_minutes or 0, args.new_goal or ''
            )
            revision = revised.revision
        checkpoint = session.checkpoint.model_copy(
            update={
                'phase': 'menu',
                'draft_id': 0,
                'task': '',
                'evidence': [],
                'reassessing': False,
                'item': '',
            }
        )
        await faro.store.activate(session, args.plan_id, revision, checkpoint)
    except (OwnershipError, StaleCheckpointError) as error:
        raise Refused(f'Nothing was saved: {error}') from None
    return ToMenu(learner_id=args.learner_id, next='menu', said=curriculum.plan_active(faro.language))


class DraftArgs(BaseModel):
    learner_id: Known
    session_id: Known
    plan_id: Known
    keep: bool
    """The learner said they keep the plan they agreed: recorded by the step's agent, carried out by code."""


async def decline_plan(faro: Faro, args: DraftArgs) -> ToMenu:
    """A revised plan turned down: the evidence is kept, the agreed plan stays exactly as it was."""
    _refuse_unless(args.keep, 'the learner has not said they keep their plan.')
    session = await _session(faro, args.learner_id, args.session_id)
    _refuse_unless(
        await faro.store.active_plan(args.learner_id) is not None,
        'there is no agreed plan to keep; the learner can accept or change the proposal, or stop here.',
    )
    checkpoint = session.checkpoint.model_copy(update={'phase': 'menu', 'draft_id': 0, 'reassessing': False})
    try:
        await faro.store.decline(session, args.plan_id, checkpoint)
    except StaleCheckpointError as error:
        raise Refused(f'Nothing was saved: {error}') from None
    return ToMenu(learner_id=args.learner_id, next='menu', said=curriculum.kept_plan(faro.language))


async def continue_lesson(faro: Faro, args: Learning) -> ToLesson:
    """Put the exercise waiting in the learner's plan to them. One they have not answered yet is asked again."""
    session = await _session(faro, args.learner_id, args.session_id)
    plan = await faro.store.active_plan(args.learner_id)
    _refuse_unless(plan is not None, 'there is no agreed plan.')
    assert plan is not None
    _refuse_unless(not plan.finished, 'every lesson in the plan is complete; offer a reassessment.')
    waiting = plan.waiting()
    await _write(faro, session, session.checkpoint.model_copy(update={'phase': 'lesson', 'item': waiting}))
    said = curriculum.exercise(faro.language, waiting)
    return ToLesson(learner_id=args.learner_id, waiting=waiting, next='lesson', said=said)


async def start_reassessment(faro: Faro, args: Learning) -> ToPlacement:
    """Start a short reassessment. The agreed plan stands until the learner accepts a new one."""
    session = await _session(faro, args.learner_id, args.session_id)
    plan = await faro.store.active_plan(args.learner_id)
    _refuse_unless(plan is not None, 'there is no agreed plan.')
    assert plan is not None
    first = curriculum.next_task(plan.target, [])
    assert first is not None
    checkpoint = session.checkpoint.model_copy(
        update={'phase': 'assessment', 'task': first.id, 'evidence': [], 'reassessing': True, 'item': ''}
    )
    await _write(faro, session, checkpoint)
    said = curriculum.placement(faro.language, first.id, first=True)
    return ToPlacement(learner_id=args.learner_id, waiting=first.id, next='assessment', said=said)


async def read_progress(faro: Faro, args: Learning) -> Said:
    """A recap of the plan as the store has it, in code's words. A recap completes nothing and changes no level."""
    plan = await faro.store.active_plan(args.learner_id)
    language = faro.language
    if plan is None:
        return Said(learner_id=args.learner_id, said=curriculum.recap(language, [], [], ''))
    done = [curriculum.lesson(i).topic[language] for i in plan.progress.completed]
    review = [curriculum.item(i)[1].instruction[language] for i in plan.progress.review]
    following = '' if plan.finished else curriculum.lesson(plan.lessons[plan.progress.lesson]).topic[language]
    return Said(learner_id=args.learner_id, said=curriculum.recap(language, done, review, following))


class FactArgs(BaseModel):
    learner_id: Known
    session_id: Known
    key: Annotated[str, Field(min_length=1, max_length=40)]
    text: Annotated[str, Field(min_length=1, max_length=200)]


async def remember(faro: Faro, args: FactArgs) -> Said:
    """Keep one thing the learner said about themselves. Same key, new fact: the correction replaces it."""
    fact = MemoryFact(key=args.key.strip().lower(), text=args.text.strip(), session_id=args.session_id)
    await faro.store.remember(args.learner_id, fact)
    return Said(learner_id=args.learner_id, said=curriculum.noted(faro.language))


async def finish(faro: Faro, state: FaroState, outcome: str) -> bool:
    """Close the session once, whatever ended it: a goodbye or a dropped line. No model runs here.

    The summary is written by code from what the store recorded this session,
    so it cannot claim progress that did not happen. False when there was no
    session, or it was already closed.
    """
    if not state.session_id:
        return False
    session = await faro.store.session(state.session_id, state.learner_id)
    plan = await faro.store.active_plan(state.learner_id)
    learned = session.checkpoint.learned
    waiting = plan.waiting() if plan is not None and not plan.finished else ''
    summary = curriculum.summary(state.explanation, learned, waiting)
    told = f'{outcome}: {learned} exercises' if learned else f'{outcome}: no learning'
    closed = await faro.store.finalize(session.id, session.learner_id, told, summary)
    ACTIVE.discard(state.learner_id)
    return closed
