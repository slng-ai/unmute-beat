"""One Faro call: the typed state its prompt is compiled against, and the call that carries the store's records.

The state holds what the prompt may show and what the guards read: the
phase, the confirmed phone, the learner's languages and the item waiting.
Nothing about a learner is in it before their phone is confirmed; the
validator refuses such a state outright, so no code path can load a name or
a plan under an unconfirmed number.

The records themselves (learner, plan, draft, session) are private: they are
the store's, read back and validated, and only `load` and the operations
replace them.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

from pydantic import Field, PrivateAttr, model_validator

from core.locale import ENGLISH, SPANISH, Locale
from core.state import CallContext, VoiceState
from examples.faro import curriculum
from examples.faro.curriculum import Goal, Lang
from examples.faro.models import Learner, Phase, Phone, Plan, Session
from examples.faro.store import Store, store

if TYPE_CHECKING:
    from core.conversation import Conversation

_CLAIM = re.compile(
    r'\b(saved|recorded|activated|updated your|marked|guardad[oa]s?|registrad[oa]s?|activad[oa]|actualizad[oa]|marcad[oa]s?)\b',
    re.IGNORECASE,
)
"""Words that tell the learner the store changed. Said with no save this turn, they are held back.

ponytail: an English and Spanish word list, like the salon's. Replace it with
a reading if live traces show it holding back honest sentences.
"""


class FaroState(VoiceState):
    agent: Literal['companion'] = 'companion'
    phase: Phase = 'identify'
    caller_id: Phone = ''
    """The carrier's number: a guess until the caller agrees to its read-back."""
    phone: Phone = ''
    """The confirmed number. Empty until the caller said yes to its read-back."""
    learner_id: int = 0
    name: str = ''
    explanation: Lang = 'EN'
    """The learner's own language: Faro explains and gives instructions in it. It starts in English; the learner can switch."""
    target: Lang | None = None
    """The language being learnt. Only EN or ES, and never inferred from the phone."""
    goal: Goal | None = None
    minutes: int = 0
    level: str = ''
    task: str = ''
    """The placement task waiting for an answer."""
    item: str = ''
    """The lesson item put to the learner and not yet answered."""
    signup_agreed: bool = False
    """The caller said yes to creating a profile, on this connection. Never stored, earned again each call."""
    reassessing: bool = False
    brief: str = ''
    """What the application knows about this learner, written by code for the prompt. Empty before confirmation."""
    saves: int = 0
    """How many writes the store has acknowledged on this call. A claim of saving needs one this turn."""

    @model_validator(mode='after')
    def _nothing_before_confirmation(self) -> FaroState:
        """No learner fact may sit on a call whose phone nobody confirmed."""
        if not self.phone and (self.learner_id or self.name or self.brief or self.signup_agreed):
            raise ValueError('a learner can only be loaded under a confirmed phone')
        return self


class FaroCall(CallContext):
    """One connection. Phone confirmation and consent live here and die with it; learning lives in the store."""

    state: FaroState = Field(default_factory=FaroState)
    _store: Store | None = PrivateAttr(default=None)
    _learner: Learner | None = PrivateAttr(default=None)
    _plan: Plan | None = PrivateAttr(default=None)
    _draft: Plan | None = PrivateAttr(default=None)
    _session: Session | None = PrivateAttr(default=None)
    _saves_before: int = PrivateAttr(default=0)
    _finished: bool = PrivateAttr(default=False)
    _coached: str = PrivateAttr(default='')
    """The exercise or task the companion spoke about while it waited: a right answer to it came with help."""

    @property
    def locale(self) -> Locale:
        """The engine's own lines follow the learner's explanation language, turn by turn."""
        return SPANISH if self.state.explanation == 'ES' else ENGLISH

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = store()
        return self._store

    @property
    def session(self) -> Session:
        assert self._session is not None, 'no session before a learner is confirmed'
        return self._session

    @property
    def learner(self) -> Learner | None:
        return self._learner

    @property
    def plan(self) -> Plan | None:
        return self._plan

    @property
    def draft(self) -> Plan | None:
        return self._draft

    @property
    def conversation(self) -> Conversation[FaroCall]:
        from examples.faro.conversation import FARO

        return FARO

    def identity(self) -> str:
        return self.state.phone

    def load(
        self,
        learner: Learner,
        plan: Plan | None,
        session: Session,
        draft: Plan | None = None,
        *,
        saved: int = 0,
    ) -> None:
        """Replace the records with what the store just returned, and the state with what they say."""
        self._learner, self._plan, self._session, self._draft = learner, plan, session, draft
        checkpoint = session.checkpoint
        source = plan if plan is not None else draft
        self.commit(
            learner_id=learner.id,
            name=learner.name,
            explanation=learner.explanation,
            target=source.target if source else checkpoint.target,
            goal=source.goal if source else checkpoint.goal,
            minutes=source.minutes if source else checkpoint.minutes,
            level=source.level if source else '',
            task=checkpoint.task,
            item=checkpoint.item,
            reassessing=checkpoint.reassessing,
            saves=self.state.saves + saved,
        )
        self.commit(brief=self.write_brief())

    def write_brief(self) -> str:
        """The learner as the application knows them, for the prompt: sourced facts only, never instructions."""
        learner, plan, draft, state = self._learner, self._plan, self._draft, self.state
        if learner is None:
            return ''
        lines = [
            f'Learner: {learner.name}. Explains in {learner.explanation}. Profile status: {learner.status}.'
        ]
        lines += [f'Remembered ({f.key}, from session {f.session_id}): {f.text}' for f in learner.memory]
        if plan is not None:
            done = [curriculum.lesson(i).topic['EN'] for i in plan.progress.completed]
            lines.append(
                f'Agreed plan: learning {plan.target} {plan.goal}, provisional {plan.level}, {plan.minutes}-minute sessions.'
            )
            lines.append(f'Lessons completed: {", ".join(done) or "none yet"}.')
            if plan.finished:
                lines.append('Every lesson in the plan is complete.')
            else:
                lesson = curriculum.lesson(plan.lessons[plan.progress.lesson])
                lines.append(f'Current lesson: {lesson.topic["EN"]}. Objective: {lesson.objective}')
            review = [curriculum.item(i)[1].instruction['EN'] for i in plan.progress.review]
            lines.append(f'Review items (practised with help): {"; ".join(review) or "none"}.')
            if plan.summary:
                lines.append(f'Last session: {plan.summary}')
        answer_in = (
            curriculum.LANGUAGE_NAMES['EN'][state.target] if state.target else 'the language they learn'
        )
        if state.item:
            lines.append(
                f'Exercise waiting ({state.item}), said to them as: "{curriculum.exercise(state.explanation, state.item)}". '
                f'They answer in {answer_in}.'
            )
        if state.task:
            lines.append(
                f'Placement task waiting ({state.task}), said to them as: '
                f'"{curriculum.placement(state.explanation, state.task)}". They answer in {answer_in}.'
            )
        if draft is not None and state.phase == 'plan':
            lines.append(
                'Proposal waiting for agreement: '
                + curriculum.plan_proposal(
                    draft.target, draft.goal, draft.level, draft.minutes, state.explanation
                )
            )
        if state.phase == 'onboarding':
            missing = [
                name
                for name, value in (
                    ('target language', state.target),
                    ('goal', state.goal),
                    ('session length', state.minutes),
                )
                if not value
            ]
            lines.append(f'Onboarding still needs: {", ".join(missing) or "nothing"}.')
        return '\n'.join(lines)

    def turn_started(self, heard: str = '') -> None:
        super().turn_started(heard)
        self._saves_before = self.state.saves

    def note_model_text(self, text: str) -> None:
        """The companion is speaking: if an exercise or task is waiting, the answer that follows had help."""
        super().note_model_text(text)
        if waiting := (self.state.item if self.state.phase == 'lesson' else self.state.task):
            self._coached = waiting

    def wrote_this_turn(self) -> bool:
        """Whether the store acknowledged a write during this turn."""
        return self.state.saves != self._saves_before

    def claims_completion(self, clause: str) -> bool:
        """A clause saying something was saved or activated, on a turn where nothing was."""
        return not self.wrote_this_turn() and bool(_CLAIM.search(clause))

    def unbacked_claim(self) -> str:
        if self.wrote_this_turn():
            return ''
        for segment in self.spoken:
            if segment.kind == 'model' and (hit := _CLAIM.search(segment.text)):
                return hit.group(0)
        return ''


def open_call(caller_id: str = '', learning: Store | None = None) -> FaroCall:
    """A new connection. A caller ID that is not a valid Spanish number is dropped, never guessed at."""
    from examples.faro.models import phone_in

    call = FaroCall(state=FaroState(caller_id=phone_in(caller_id)))
    call._store = learning
    return call
