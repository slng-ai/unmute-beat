"""Faro, offline: the acceptance cases through core's public path, a real SQLite file and scripted step agents.

Each step's agent is a script: every request takes the next move, words or
tool calls, as a live model would send them. So these check the plumbing:
identity, consent, grading as recorded, persistence and resume, isolation.
Language understanding and grading accuracy are the live runs' job
(`faro-tools/bench.py`). Each test has its own database file; a "restart"
opens the same file again, as a new process would, and one test really does
kill a process after a commit.

L01-L16 are the cases the legacy build was checked on; F1-F9 are what the
public path adds to them: no reader or classifier imported, hostile model
output refused where it runs, delivery evidence bound to the exact
question, a lost reply reconciled once, and calls kept apart. F10 and F11
check what manifest version 2 adds: identify, plan, menu and coach are
`speech: structured`, so their moves are `reply`, `record` or an operation
with its `update`; signup and onboarding are prose and assessment and lesson
rendered, so theirs are `collect`, words and `wait`.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

import core
import faro
from core.history import Payload
from core.locale import ENGLISH, SPANISH
from faro import close_call, curriculum, greeting, open_call
from faro.functions import ACTIVE
from faro.models import Attempt
from faro.store import StaleCheckpointError, Store, fixture

LUCIA, DIEGO, EMMA, NEW = '+34600111222', '+34600333444', '+34600555666', '+34600777888'


class Script:
    """The step agents: each request takes the next move and records the step, its tools and what it was told.

    A move is `('say', words)`, `('call', tool, args)` or `('calls', [(tool, args), ...])` for one response
    with several tool calls. With `key`, only a request whose instructions name the key takes a move from
    that key's queue: two calls on one flow, side by side, each get their own script.
    """

    def __init__(self) -> None:
        self.moves: dict[str, list[tuple[Any, ...]]] = {'': []}
        self.steps: list[str] = []
        self.offered: list[set[str]] = []
        self.told: list[str] = []
        self.payloads: list[Payload] = []
        """This turn's requests whole: instructions, history, tool and output schemas (`core.history.Payload`)."""
        self.given: list[tuple[list[Any], AgentInfo]] = []
        """This turn's requests as the model boundary got them, for the adapter's own mapping (`adapter_bytes`)."""
        self.call: core.FlowCall | None = None
        self.model = FunctionModel(self.reading, stream_function=self.agent)

    def reading(self, messages: Any, info: AgentInfo) -> ModelResponse:
        raise AssertionError('a separate reading was requested; only step agents may run')

    def queue(self, info: AgentInfo) -> list[tuple[Any, ...]]:
        told = getattr(info, 'instructions', '') or ''
        return next((q for key, q in self.moves.items() if key and key in told), self.moves[''])

    async def agent(self, messages: Any, info: AgentInfo):
        told = getattr(info, 'instructions', '') or ''
        self.steps.append(
            next((line.split('`')[1] for line in told.splitlines() if line.startswith('You are the `')), '?')
        )
        self.offered.append({t.name for t in (*info.function_tools, *info.output_tools)})
        self.told.append(told)
        self.payloads.append(Payload.of(messages, info))
        self.given.append((list(messages), info))
        moves = self.queue(info)
        if not moves:
            yield 'Vale.'
            return
        kind, *rest = moves.pop(0)
        if kind == 'say':
            yield rest[0]
        elif kind == 'call':
            yield {0: DeltaToolCall(name=rest[0], json_args=json.dumps(rest[1]))}
        else:
            yield {
                i: DeltaToolCall(name=name, json_args=json.dumps(args))
                for i, (name, args) in enumerate(rest[0])
            }


SCRIPT = Script()
FLOW = faro.SPEC.compile(model=SCRIPT.model)


def collect(**values: Any) -> tuple[str, str, dict[str, Any]]:
    return ('call', 'collect', {'values': values})


def call(tool: str, **args: Any) -> tuple[str, str, dict[str, Any]]:
    return ('call', tool, args)


def say(words: str) -> tuple[str, str]:
    return ('say', words)


def reply(words: str, **update: Any) -> tuple[str, str, dict[str, Any]]:
    """A structured step's answer, with the caller's update when there is one."""
    return ('call', 'reply', {'say': words, **({'update': update} if update else {})})


def record(**update: Any) -> tuple[str, str, dict[str, Any]]:
    """A structured step's update where code carries out or reads back what follows."""
    return ('call', 'record', {'update': update})


def grade(answer: str, result: str, feedback: str = '') -> tuple[str, str, dict[str, Any]]:
    """An attempt graded on the rendered assessment or lesson."""
    return collect(answer=answer, grade=result, feedback=feedback)


class Line:
    """One connection to Faro, its greeting said, with every turn's words recorded as the caller got them.

    `greeted=False` leaves the greeting unacknowledged: nothing says it reached the caller.
    """

    def __init__(
        self,
        path: str,
        caller_id: str = '',
        flow: core.StepFlow = FLOW,
        script: Script = SCRIPT,
        greeted: bool = True,
        clock: Any = None,
    ) -> None:
        self.store = Store(path)
        self.store.open()
        self.flow, self.script = flow, script
        self.call = open_call(flow, caller_id, learning=self.store, clock=clock)
        script.call = self.call
        self.heard = [core.greet(self.call, greeting(flow, self.call))]
        if greeted:
            self.call.acknowledge(self.call.spoken[-1].id)

    async def turn(self, words: str, *moves: tuple[Any, ...], key: str = '', heard: bool = True) -> str:
        """One caller turn: every segment of it, joined. `heard=False` leaves it unacknowledged: nothing reached them."""
        self.script.moves[key] = list(moves)
        self.script.steps.clear()
        self.script.offered.clear()
        self.script.payloads.clear()
        self.script.given.clear()
        reply = await core.text_turn(self.flow, self.call, words)
        if heard:
            self.call.acknowledge(*(s.id for s in reply.segments))
        assert reply.finished.status == 'answered', (words, reply.finished)
        return ' '.join(s.text for s in reply.segments if s.text)

    @property
    def requests(self) -> int:
        return len(self.script.steps)

    async def session(self):
        facts = self.call.state.facts
        return await self.store.session(facts.session_id, facts.learner_id)


def database() -> str:
    return str(Path(tempfile.mkdtemp()) / 'faro.sqlite3')


def restart() -> None:
    """What a process restart clears: the in-process record of who is on a call."""
    ACTIVE.clear()


YES = call('confirm_phone')


async def seeds_are_identified_only_after_the_yes():
    """L01: each seed hears their own checkpoint, in their own language, only after confirming, in one request."""
    for phone, name, where, locale in (
        (LUCIA, 'Lucía', 'segundo ejercicio', SPANISH),
        (DIEGO, 'Diego', 'describir un proyecto', SPANISH),
        (EMMA, 'Emma', 'first exercise', ENGLISH),
    ):
        restart()
        line = Line(database(), phone)
        assert curriculum.spaced(phone) in line.heard[0] and name not in line.heard[0], line.heard
        facts = line.call.state.facts
        assert not facts.notes and not facts.learner_id and not facts.confirmed
        said = await line.turn('Sí, es ese.', YES)
        assert name in said and where in said, said
        assert line.requests == 1 and line.call.state.agent == 'menu', (line.requests, line.call.state.agent)
        others = {'Lucía', 'Diego', 'Emma'} - {name}
        assert not any(o in said or o in line.call.state.facts.notes for o in others), said
        assert line.call.locale.stumble == locale.stumble, 'the engine speaks the learner language'
        assert name not in ''.join(line.script.told[0].split('Facts:')[1:]), (
            'nothing about them was shown before'
        )
    assert 'Shall we carry on' in said and line.call.state.facts.explanation == 'EN', (
        'Emma explains in English'
    )


async def a_wrong_or_foreign_number_discloses_nothing():
    """L02: no caller ID, a foreign number, a number without its code, a correction: nothing loaded before a yes."""
    restart()
    line = Line(database())
    assert 'plus thirty-four' in line.heard[0] and line.heard[0].startswith("Hi, I'm Faro"), line.heard
    said = await line.turn(
        'Es el +33 6 12 34 56 78.',
        call('confirm_phone', update={'phone': '+33 6 12 34 56 78'}),
        reply('Solo puedo usar números españoles, con el +34. ¿Me lo dices otra vez?'),
    )
    assert 'españoles' in said and not line.call.state.facts.phone and line.call.open_question() is None, said
    said = await line.turn(
        'Seis cero cero uno uno uno dos dos dos.',
        call('confirm_phone', update={'phone': '+34 600 111 222'}),
        reply('¿Con el +34?'),
    )
    assert not line.call.state.facts.phone and line.call.open_question() is None, (
        'a +34 the caller never said is not added by the model'
    )
    said = await line.turn(
        'Más treinta y cuatro, seis cero cero, uno uno uno, dos dos dos.',
        call('confirm_phone', update={'phone': '+34 600 111 222'}),
    )
    assert said == f'Let me check: is your number {curriculum.spaced(LUCIA)}?', said
    assert line.requests == 1, 'a number recorded is read back by code, with no second request'

    restart()
    line = Line(database(), LUCIA)
    said = await line.turn(
        'No, es el +34 600 333 444.', call('confirm_phone', update={'phone': '+34 600 333 444'})
    )
    assert curriculum.spaced(DIEGO) in said and line.requests == 1, said
    assert not line.call.state.facts.learner_id and 'Lucía' not in said and 'Diego' not in said
    said = await line.turn('Sí.', YES)
    assert 'Diego' in said and line.call.state.facts.confirmed == DIEGO, said


async def to_assessment(line: Line) -> None:
    """A new English speaker learning Spanish, from the greeting to the first placement task."""
    await line.turn('Yes.', YES)
    await line.turn('Yes.', call('agree_signup'))
    await line.turn('Ana.', collect(name='Ana'))
    await line.turn('Spanish, for travel, ten minutes.', collect(learn='ES', purpose='travel', length=10))
    assert line.call.state.agent == 'assessment', line.call.state.agent


async def an_unknown_caller_signs_up_and_agrees_a_plan():
    """L03 and L04: consent, onboarding said by code, French refused, placement recorded, one plan.

    Every onboarding answer and placement attempt is one request: the step's
    agent records it, and code saves it and asks the next question.
    """
    restart()
    line = Line(database(), NEW)
    said = await line.turn('Yes.', YES)
    assert "I can't find a profile" in said and line.call.state.agent == 'signup', said
    assert (await line.store.counts())['learners'] == 3, 'confirming a number creates nothing'
    said = await line.turn('Yes, please.', call('agree_signup'))
    assert 'What would you like me to call you?' in said and line.requests == 1, said
    said = await line.turn('Call me Nico.', collect(name='Nico'))
    assert 'Nice to meet you, Nico. Which language would you like to learn' in said, said
    assert (await line.store.counts())['learners'] == 4 and line.call.state.agent == 'onboarding'
    said = await line.turn(
        'French.', collect(learn='French'), say('I only teach English or Spanish. Which would you like?')
    )
    assert line.call.state.facts.learn is None and 'English or Spanish' in said, said
    said = await line.turn('Spanish.', collect(learn='Spanish'))
    assert said == curriculum.ONBOARDING['goal']['EN'] and line.requests == 1, said
    await line.turn('For travel.', collect(purpose='travel'))
    said = await line.turn('Ten minutes.', collect(length=10))
    assert line.call.state.agent == 'assessment' and line.call.state.facts.waiting == 'es-intro', said
    assert 'Answer in Spanish. Tell me about yourself and why you want to learn Spanish.' in said, said
    for answer, result in (
        ('Me llamo Nico y quiero viajar.', 'correct'),
        ('Un café, por favor. ¿Cuánto?', 'partly'),
        ('Yo ir a París.', 'wrong'),
    ):
        said = await line.turn(answer, grade(answer, result))
        assert line.requests == 1, 'each placement answer is one request: recorded by code straight after'
    assert 'We will start with Spanish A2 for travel' in said and line.call.state.agent == 'plan', said
    said = await line.turn('Yes, sounds good.', call('activate_plan'))
    assert 'your plan is active' in said and line.call.state.agent == 'menu', said
    found = await line.store.lookup(NEW)
    assert (
        found is not None
        and found[1] is not None
        and found[1].level == 'A2'
        and len(found[1].assessment) == 3
    )
    assert found[0].status == 'active' and (await line.store.counts())['plans'] == 4


async def at_proposal(path: str | None = None, heard: bool = True) -> Line:
    """A new learner of Spanish with a plan proposal just read to them; `heard=False` leaves it unacknowledged."""
    restart()
    line = Line(path or database(), NEW)
    await to_assessment(line)
    for words in ('Hola.', 'Un café.'):
        await line.turn(words, grade(words, 'partly'))
    await line.turn('Fui a Roma.', grade('Fui a Roma.', 'partly'), heard=heard)
    assert line.call.state.agent == 'plan', line.call.state.agent
    return line


async def declines_conditions_and_revisions_activate_nothing():
    """L05: a refused sign-up leaves no row; "yes, but five minutes" activates nothing; only its new read-back counts."""
    restart()
    line = Line(database(), NEW)
    await line.turn('Yes.', YES)
    said = await line.turn('No, thanks.', call('decline'), say('No problem, I have not created anything.'))
    assert (await line.store.counts())['learners'] == 3 and not line.call.state.facts.signup_agreed, said

    line = await at_proposal()
    first = line.call.open_question() or line.call.pending_question()
    said = await line.turn('Yes, but only five minutes.', record(plan_minutes=5))
    assert 'about 5 minutes' in said and line.requests == 1, said
    assert await line.store.active_plan(line.call.state.facts.learner_id) is None, (
        'nothing is activated by a condition'
    )
    second = line.call.open_question()
    assert second.value != first.value, (first.value, second.value)
    said = await line.turn('Yes.', call('activate_plan'))
    plan = await line.store.active_plan(line.call.state.facts.learner_id)
    assert plan is not None and plan.revision == 2 and plan.minutes == 5, plan


async def onboarding_resumes_after_a_restart():
    """L06: a language switch, a restart after the language, mid-placement and awaiting agreement: resumed, no duplicates."""
    restart()
    path = database()
    line = Line(path, NEW)
    await line.turn('Sí.', YES)
    said = await line.turn('¿Podemos hablar en español?', collect(asked_language='español'))
    assert said == curriculum.signup_offer('ES') and line.call.locale.goodbye == '¡Hasta la próxima!', said
    await line.turn('Sí.', call('agree_signup'))
    await line.turn('Ana.', collect(name='Ana'))
    said = await line.turn('Inglés.', collect(learn='inglés'))
    assert said == curriculum.ONBOARDING['goal']['ES'], 'a Spanish speaker hears the questions in Spanish'

    restart()
    line = Line(path, NEW)
    assert line.store.interrupted == 1, 'the session a crash left open is marked interrupted'
    said = await line.turn('Sí.', YES)
    assert 'Habíamos empezado' in said and line.call.state.agent == 'onboarding', said
    assert (await line.session()).checkpoint.target == 'EN'
    said = await line.turn('Para el trabajo, diez minutos.', collect(purpose='work', length=10))
    assert 'Responde en inglés. Háblame de ti y de por qué quieres aprender inglés.' in said, said
    await line.turn('I am Ana.', grade('I am Ana.', 'partly'))

    restart()
    line = Line(path, NEW)
    await line.turn('Sí.', YES)
    assert line.call.state.agent == 'assessment' and line.call.state.facts.waiting == 'en-order'
    assert len((await line.session()).checkpoint.evidence) == 1, 'the first answer survived the restart'
    for words in ('A coffee.', 'I went to Rome.'):
        await line.turn(words, grade(words, 'partly'))
    assert line.call.state.agent == 'plan'

    restart()
    line = Line(path, NEW)
    said = await line.turn('Sí.', YES)
    assert said.startswith('Hola, Ana. Empezaremos con inglés'), said
    await line.turn('Sí.', call('activate_plan'))
    assert await line.store.active_plan(line.call.state.facts.learner_id) is not None
    assert (await line.store.counts())['learners'] == 4, 'one profile, however many calls it took'


async def lucia_carries_on_from_exercise_two():
    """L07: exercise 1 kept, exercise 2 put again, its answer saved once, and a replay counted once."""
    restart()
    line = Line(database(), LUCIA)
    await line.turn('Sí.', YES)
    said = await line.turn('Seguimos.', call('continue_lesson'))
    assert 'segundo ejercicio. Responde en inglés: Ahora di cuántos quieres' in said, said
    assert line.call.state.facts.waiting == 'en-travel-2-2' and line.call.state.agent == 'lesson'
    said = await line.turn('Two coffees, please.', grade('Two coffees, please.', 'correct', '¡Muy bien!'))
    assert said.startswith('¡Muy bien!') and 'Pide dos bebidas' in said and line.requests == 1, said
    plan = await line.store.active_plan(line.call.state.facts.learner_id)
    assert plan is not None and [a.item_id for a in plan.progress.attempts][-2:] == [
        'en-travel-2-1',
        'en-travel-2-2',
    ]
    revision = plan.progress_revision
    replayed = await line.store.record_attempt(await line.session(), plan.id, plan.progress.attempts[-1])
    assert replayed[2] and replayed[0].progress_revision == revision, 'the replay was not counted'


async def a_recap_changes_nothing():
    """L08: Diego's recap is code's words from the plan, and leaves its level, lessons and progress untouched."""
    restart()
    line = Line(database(), DIEGO)
    await line.turn('Sí.', YES)
    before = await line.store.lookup(DIEGO)
    said = await line.turn('Solo un repaso.', call('read_progress'))
    after = await line.store.lookup(DIEGO)
    assert before is not None and after is not None and before[1] == after[1], 'the plan is exactly as it was'
    assert 'describir un proyecto' in said and 'Has completado' in said, said


async def emma_reassesses_and_the_old_plan_stands_until_a_yes():
    """L09: evidence is saved either way; a declined revision leaves the agreed plan, an accepted one archives it."""
    for accept in (False, True):
        restart()
        line = Line(database(), EMMA)
        await line.turn('Yes.', YES)
        said = await line.turn('Can you check my level?', call('start_reassessment'))
        assert curriculum.PLACEMENT['ES'][0].said('EN', 'ES') in said, 'Emma hears the task in English'
        for words in ('Hola.', 'Un café.', 'Fui a Roma.'):
            said = await line.turn(words, grade(words, 'partly'))
        assert 'Spanish A2' in said and line.call.state.agent == 'plan', said
        learner = line.call.state.facts.learner_id
        old = await line.store.active_plan(learner)
        if accept:
            await line.turn('Yes.', call('activate_plan'))
            plan = await line.store.active_plan(learner)
            assert plan is not None and plan.level == 'A2' and old is not None and plan.id != old.id
            assert plan.progress.completed == ['es-daily-1'], 'progress carries over to the revision'
        else:
            said = await line.turn('No, keep my plan.', record(keep_plan=True))
            plan = await line.store.active_plan(learner)
            assert plan is not None and old is not None and plan.id == old.id and plan.level == 'A1', said
            draft = await line.store.plan(learner, line.call.state.facts.draft_id)
            assert draft.status == 'declined' and len(draft.assessment) == 3, 'the evidence is kept'
            assert line.call.state.agent == 'menu'


async def honest_evidence_moves_only_what_it_shows():
    """L10: a misheard answer moves nothing and is no failure; a missed item stays waiting and goes on review."""
    restart()
    line = Line(database(), LUCIA)
    await line.turn('Sí.', YES)
    await line.turn('Seguimos.', call('continue_lesson'))
    learner = line.call.state.facts.learner_id
    said = await line.turn(
        'Tw... cof...', grade('Tw... cof...', 'unclear', 'No te he oído bien, ¿me lo repites?')
    )
    plan = await line.store.active_plan(learner)
    assert (
        plan is not None and plan.waiting() == 'en-travel-2-2' and 'en-travel-2-2' not in plan.progress.review
    )
    assert (
        said
        == 'No te he oído bien, ¿me lo repites? Otra vez, en inglés: Ahora di cuántos quieres: pide dos de algo.'
    ), 'the retry is short: feedback, then the instruction, no introduction again'
    said = await line.turn(
        'I want coffee two.', grade('I want coffee two.', 'wrong', 'Casi: el número va delante.')
    )
    plan = await line.store.active_plan(learner)
    assert (
        plan is not None and plan.waiting() == 'en-travel-2-2' and plan.progress.review == ['en-travel-2-2']
    )
    assert said.startswith('Casi: el número va delante. Otra vez'), said
    said = await line.turn(
        'Two coffee.', grade('Two coffee.', 'partly', 'Casi. Se dice: Two coffees, please.')
    )
    plan = await line.store.active_plan(learner)
    assert plan is not None and plan.waiting() == 'en-travel-2-2', 'partly right is not right: no moving on'
    assert (
        said.startswith('Casi. Se dice: Two coffees, please. Otra vez, en inglés:')
        and 'ejercicio' not in said
    )
    await line.turn('Two coffees, please.', grade('Two coffees, please.', 'correct'))
    plan = await line.store.active_plan(learner)
    assert plan is not None and plan.waiting() == 'en-travel-2-3', 'said correctly, it moves on'
    assert plan.progress.attempts[-1].result == 'assisted', 'right after a correction is recorded as assisted'
    await line.turn('Coffee.', grade('Coffee.', 'wrong'))
    plan = await line.store.active_plan(learner)
    assert plan is not None and plan.progress.review == ['en-travel-2-2', 'en-travel-2-3']
    assert 'en-travel-2' not in plan.progress.completed
    said = await line.turn(
        'Two teas, please. How much is it?', grade('Two teas, please. How much is it?', 'correct')
    )
    plan = await line.store.active_plan(learner)
    assert plan is not None and plan.progress.completed[-1] == 'en-travel-2', (
        'the check said right completes it'
    )
    assert line.call.state.agent == 'menu' and 'Lección completada' in said, said


async def a_goodbye_during_an_exercise_is_not_graded():
    """A goodbye while an exercise waits ends the call: nothing is graded, and the goodbye is Faro's own line."""
    restart()
    line = Line(database(), LUCIA)
    await line.turn('Sí.', YES)
    await line.turn('Seguimos.', call('continue_lesson'))
    plan = await line.store.active_plan(line.call.state.facts.learner_id)
    attempts = len(plan.progress.attempts) if plan else 0
    said = await line.turn('Nada más por hoy, gracias. Adiós.', call('goodbye'))
    assert line.call.ended == 'caller' and said.endswith('¡Hasta la próxima!') and 'Otra vez' not in said, (
        said
    )
    plan = await line.store.active_plan(line.call.state.facts.learner_id)
    assert plan is not None and len(plan.progress.attempts) == attempts, 'nothing graded'


async def a_proposal_talked_over_activates_nothing():
    """L11: the learner cut in during the plan; their yes is to words they did not hear, so nothing activates."""
    line = await at_proposal(heard=False)
    proposal = line.call.open_question()
    segment = line.call.spoken[-1]
    assert segment.text == proposal.spoken
    assert line.call.acknowledge(segment.id, heard=proposal.spoken[:20])
    said = await line.turn('Yes!', call('activate_plan'), heard=False)
    learner = line.call.state.facts.learner_id
    assert await line.store.active_plan(learner) is None, (
        'a yes to a proposal heard in part activates nothing'
    )
    assert said == proposal.spoken, 'code puts the proposal again, word for word'
    line.call.acknowledge(*(s.id for s in line.call.spoken))
    await line.turn('Yes, sounds good.', call('activate_plan'))
    assert await line.store.active_plan(learner) is not None, (
        'the yes to the proposal heard in full activates it'
    )


async def every_ending_saves_the_session():
    """L12: a goodbye and a dropped line both close the session once, with a summary code wrote."""
    restart()
    line = Line(database(), DIEGO)
    await line.turn('Sí.', YES)
    said = await line.turn('No, hoy no. Adiós.', call('goodbye'))
    assert line.call.ended == 'caller' and said.endswith('¡Hasta la próxima!'), said
    assert 'guardado' not in said, 'the goodbye claims no save: the session is closed after it'
    assert await close_call(line.call)
    found = await line.store.lookup(DIEGO)
    assert found is not None and found[2] is not None and found[2].status == 'completed'
    assert found[2].outcome == 'goodbye: no learning' and found[1] is not None and found[1].summary

    restart()
    line = Line(database(), LUCIA)
    await line.turn('Sí.', YES)
    assert await close_call(line.call) and not await close_call(line.call), 'closed once'
    found = await line.store.lookup(LUCIA)
    assert found is not None and found[2] is not None and found[2].outcome == 'disconnected: no learning'


async def replays_failures_and_stale_writes_change_nothing():
    """L13: a stale checkpoint is refused, and a write that fails half-way leaves the plan and session as they were."""
    restart()
    line = Line(database(), LUCIA)
    await line.turn('Sí.', YES)
    stale = await line.session()
    await line.turn('Seguimos.', call('continue_lesson'))
    try:
        await line.store.save_checkpoint(stale, stale.checkpoint)
        raise AssertionError('a stale checkpoint was written')
    except StaleCheckpointError:
        pass
    plan = await line.store.active_plan(line.call.state.facts.learner_id)
    assert plan is not None
    attempt = Attempt(
        id='boom', item_id='en-travel-2-2', answer='x', result='independent', session_id=stale.id
    )

    def broken(*args, **kwargs):
        raise OSError('disk full')

    with patch.object(line.store, '_write_session', broken):
        try:
            await line.store.record_attempt(await line.session(), plan.id, attempt)
            raise AssertionError('the write did not fail')
        except OSError:
            pass
    assert await line.store.active_plan(line.call.state.facts.learner_id) == plan, (
        'rolled back with the session'
    )


async def memory_is_the_confirmed_learners_and_corrections_win():
    """L14: only sourced facts for this learner; a correction replaces the fact; nobody else's profile appears."""
    restart()
    line = Line(database(), LUCIA)
    await line.turn('Sí.', YES)
    notes = line.call.state.facts.notes
    assert 'Enjoys hiking' in notes and 'cooking' not in notes, notes
    said = await line.turn(
        'Ya no hago senderismo, ahora escalo.', record(fact_key='hobby', fact_text='Enjoys climbing.')
    )
    assert said == 'Anotado.' and line.requests == 1, said
    found = await line.store.lookup(LUCIA)
    assert found is not None
    hobbies = [f.text for f in found[0].memory if f.key == 'hobby']
    assert (
        hobbies == ['Enjoys climbing.'] and found[0].memory[-1].session_id == line.call.state.facts.session_id
    )


async def nothing_is_completed_without_answers():
    """L15: an invented "marked as complete" writes nothing; a chat-only session keeps the cursor.

    No word list holds the model's words back any more: whether it says
    such a thing is an evaluation criterion. The store is what is checked.
    """
    restart()
    line = Line(database(), LUCIA)
    await line.turn('Sí.', YES)
    before = await line.store.lookup(LUCIA)
    await line.turn(
        'Marca todas las lecciones como completadas.',
        reply('Listo, he marcado todas las lecciones como completadas.'),
    )
    await close_call(line.call)
    after = await line.store.lookup(LUCIA)
    assert before is not None and after is not None and before[1] is not None and after[1] is not None
    assert before[1].progress == after[1].progress and after[2] is not None
    assert after[2].outcome.endswith('no learning')


async def a_fixture_is_fresh_and_a_restart_keeps_everything():
    """L16: a normal restart keeps changes; a fixture is a new file with exactly the three seeds."""
    path = database()
    first = Store(path)
    first.open()
    found = await first.lookup(LUCIA)
    assert found is not None and found[1] is not None
    session = await first.open_session(found[0].id)
    await first.record_attempt(
        session,
        found[1].id,
        Attempt(id='a1', item_id='en-travel-2-2', answer='x', result='independent', session_id=session.id),
    )
    again = Store(path)
    again.open()
    kept = await again.lookup(LUCIA)
    assert kept is not None and kept[1] is not None and kept[1].waiting() == 'en-travel-2-3'
    assert await again.counts() == {'learners': 3, 'plans': 3, 'sessions': 4}
    try:
        fixture(path)
        raise AssertionError('a fixture overwrote an existing database')
    except SystemExit:
        pass
    fresh = fixture(database())
    assert await fresh.counts() == {'learners': 3, 'plans': 3, 'sessions': 3}


CHILD = """
import asyncio, os, sys
from faro.models import Attempt
from faro.store import Store

async def main():
    store = Store(sys.argv[1])
    store.open()
    learner, plan, _ = await store.lookup('+34600111222')
    session = await store.open_session(learner.id)
    await store.record_attempt(session, plan.id, Attempt(id='acked', item_id='en-travel-2-2', answer='Two coffees.', result='independent', session_id=session.id))
    print('acknowledged', flush=True)
    os._exit(9)

asyncio.run(main())
"""


async def an_acknowledged_answer_survives_a_killed_process():
    """L16b: a process killed straight after an acknowledged save: the next one resumes after it, a replay counts once."""
    path = database()
    result = subprocess.run(
        [sys.executable, '-c', CHILD, path],
        env={**os.environ, 'GOOGLE_API_KEY': 'x'},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 9 and 'acknowledged' in result.stdout, result.stderr[-2000:]
    restarted = Store(path)
    restarted.open()
    assert restarted.interrupted == 1, 'the killed session is marked interrupted'
    found = await restarted.lookup(LUCIA)
    assert found is not None and found[1] is not None and found[1].waiting() == 'en-travel-2-3'
    session = await restarted.open_session(found[0].id)
    assert session.checkpoint.item == 'en-travel-2-3', 'the resume point moved with the answer'
    replay = Attempt(
        id='acked',
        item_id='en-travel-2-2',
        answer='Two coffees.',
        result='independent',
        session_id=session.id,
    )
    plan, _, replayed = await restarted.record_attempt(session, found[1].id, replay)
    assert replayed and plan.progress_revision == found[1].progress_revision


async def each_step_offers_only_its_own_tools():
    """F1: before a phone is confirmed no learning tool exists; a lesson offers no tool that grades, and none that writes.

    A structured step's prompt names no tool it lacks (`collect`, `wait`, `decline` as a tool).
    """
    restart()
    line = Line(database())
    await line.turn('Hola.', reply('¿Me dices tu número?'))
    offered = line.script.offered[0]
    assert offered == {'reply', 'record', 'confirm_phone', 'say_again', 'goodbye'}, offered
    line = Line(database(), LUCIA)
    await line.turn('Sí.', YES)
    await line.turn('Seguimos.', call('continue_lesson'))
    await line.turn('Hmm.', call('wait'))
    offered = line.script.offered[0]
    assert offered == {'collect', 'goodbye', 'wait', 'hand_over'}, offered
    assert line.script.steps == ['lesson']
    everywhere = {name for node in FLOW.nodes.values() for name in _tools(node)}
    assert not everywhere & {
        'record_attempt',
        'create_learner',
        'save_onboarding',
        'note_help',
        'remember',
    }, 'what code carries out is never offered to a model'
    for step in faro.SPEC.steps:
        if getattr(step, 'speech', '') == 'structured':
            prompt = str(faro.SPEC.prompt(step.id))
            assert not V1_TOOLS.findall(prompt), (step.id, V1_TOOLS.findall(prompt))


V1_TOOLS = re.compile(r'`(?:collect|wait)`|call `decline`')
"""Tools a structured step does not have, named the way the prompts name tools. `decline` is `reply`'s field."""


def _tools(node) -> set[str]:
    return {t.name for toolset in node.agent.toolsets for t in getattr(toolset, 'tools', {}).values()} | {
        getattr(o, 'name', '') or ''
        for o in (node.agent.output_type if isinstance(node.agent.output_type, list) else [])
    }


async def a_coached_answer_is_assisted_and_the_check_completes_the_lesson():
    """F2: help marks the exercise in the store first, so the answer after it is assisted, even back in the lesson."""
    restart()
    line = Line(database(), EMMA)
    await line.turn('Yes.', YES)
    said = await line.turn("Let's carry on.", call('continue_lesson'))
    assert 'Shopping, first exercise. Answer in Spanish: Ask for a kilo of apples at the market.' in said, (
        said
    )
    said = await line.turn(
        'How do I say apples?',
        call('hand_over', to='help', because='a question about the exercise'),
        reply('Apples is "manzanas". Try it!'),
    )
    assert said == 'Apples is "manzanas". Try it!' and line.call.state.agent == 'coach', said
    assert (await line.session()).checkpoint.helped == 'es-daily-2-1', (
        'help is the store record, before the coach speaks'
    )
    assert line.script.steps == ['lesson', 'coach']
    said = await line.turn(
        'Un kilo de manzanas, por favor.',
        record(answer='Un kilo de manzanas, por favor.', grade='correct', feedback='Great!'),
    )
    plan = await line.store.active_plan(line.call.state.facts.learner_id)
    assert plan is not None and plan.progress.attempts[-1].result == 'assisted', 'help before the answer'
    assert said == 'Great! Shopping, second exercise. Answer in Spanish: Ask how much it costs.', said
    assert line.call.state.agent == 'lesson', 'the next exercise is back with the examiner'
    await line.turn('¿Cuánto cuesta?', grade('¿Cuánto cuesta?', 'correct'))
    plan = await line.store.active_plan(line.call.state.facts.learner_id)
    assert plan is not None and plan.progress.attempts[-1].result == 'independent'
    said = await line.turn(
        'Quiero pan y leche. ¿Cuánto es en total?',
        grade('Quiero pan y leche. ¿Cuánto es en total?', 'correct', 'Perfect!'),
    )
    plan = await line.store.active_plan(line.call.state.facts.learner_id)
    assert plan is not None and plan.progress.completed[-1] == 'es-daily-2', 'the check completed the lesson'
    assert said.startswith('Perfect! Lesson complete: shopping. Next is talking about your routine.'), said
    said = await line.turn('Yes, go on.', call('continue_lesson'))
    assert 'Talking about your routine, first exercise' in said, 'the next exercise, not the same one again'

    restart()
    line = Line(database(), EMMA)
    await line.turn('Yes.', YES)
    await line.turn("Let's carry on.", call('continue_lesson'))
    await line.turn('Help?', call('hand_over', to='help', because='help'), reply('Try "manzanas".'))
    await line.turn(
        'Actually, a recap.', call('hand_over', to='menu', because='recap'), call('read_progress')
    )
    await line.turn('Carry on.', call('continue_lesson'))
    assert line.call.state.agent == 'lesson'
    await line.turn('Un kilo de manzanas.', grade('Un kilo de manzanas.', 'correct'))
    plan = await line.store.active_plan(line.call.state.facts.learner_id)
    assert plan is not None and plan.progress.attempts[-1].result == 'assisted', (
        'leaving the coach and coming back does not make a helped exercise independent'
    )


async def a_language_switch_keeps_the_question_answerable():
    """F3: asking to switch language puts the question waiting again in the new language, so the next yes counts."""
    restart()
    line = Line(database(), NEW)
    said = await line.turn('¿Podemos hablar en español?', record(asked_language='español'))
    assert (
        said == f'A ver si lo tengo bien: ¿tu número es {curriculum.spaced(NEW)}?' and line.requests == 1
    ), said
    said = await line.turn('Sí.', YES)
    assert 'No encuentro un perfil' in said, 'the yes answered the question put again'

    restart()
    line = Line(database(), EMMA)
    await line.turn('Yes.', YES)
    await line.turn("Let's carry on.", call('continue_lesson'))
    said = await line.turn('¿Podemos hablar en español?', collect(asked_language='ES'))
    assert said == curriculum.exercise('ES', 'es-daily-2-1') and line.requests == 1, said


async def hostile_model_output_is_refused_where_it_runs():
    """F4: a model that grades a question, invents an answer, calls what code carries out or moves where it may not."""
    restart()
    line = Line(database(), LUCIA)
    await line.turn('Sí.', YES)
    await line.turn('Seguimos.', call('continue_lesson'))
    learner = line.call.state.facts.learner_id
    before = await line.store.active_plan(learner)
    await line.turn('¿Qué significa "two"?', grade('Two coffees, please.', 'correct'), call('wait'))
    await line.turn('Hmm.', call('record_attempt'), call('wait'))
    await line.turn('Hmm.', collect(answer='Hmm.', grade='excellent'), call('wait'))
    await line.turn('Hmm.', collect(learner_id=2), call('wait'))
    await line.turn('Hmm.', call('hand_over', to='plan', because='x'), call('wait'))
    await line.turn('Hmm.', call('continue_lesson'), call('wait'))
    after = await line.store.active_plan(learner)
    assert after == before and line.call.state.agent == 'lesson', 'nothing recorded, nobody moved'
    assert line.call.state.facts.learner_id == learner and not line.call.unresolved()

    restart()
    line = Line(database())
    await line.turn('Hola.', call('confirm_phone'), reply('¿Me dices tu número?'))
    await line.turn('Es el +34 600 111 222.', call('continue_lesson'), reply('¿Perdón?'))
    assert not line.call.state.facts.learner_id and not line.call.evidence, 'nothing looked up without a yes'
    before = line.call.state.facts
    for loaded in ({'learner_id': 1}, {'notes': 'Learner: Lucía.'}, {'signup_agreed': True}):
        try:
            line.call.update(**loaded)
            raise AssertionError(f'learner facts were held before a confirmed phone: {loaded}')
        except ValidationError:
            pass
    assert line.call.state.facts is before, 'a refused state changes nothing'


async def a_yes_binds_only_to_a_question_heard_in_full():
    """F5: a read-back with no playback evidence approves nothing; text acknowledgement is recorded as simulated."""
    restart()
    line = Line(database(), LUCIA, greeted=False)
    said = await line.turn('Sí.', YES)
    assert not line.call.state.facts.learner_id, 'a read-back with no playback evidence approves nothing'
    assert curriculum.spaced(LUCIA) in said, 'it is put again instead'
    await line.turn('Sí.', YES)
    assert line.call.state.facts.learner_id, 'the copy acknowledged in full is answerable'
    write = [e for e in line.call.evidence if e.operation == 'confirm_phone']
    assert write and line.call.simulated_for(write[-1].proposal), 'text delivery is recorded as simulated'


async def a_lost_reply_is_reconciled_once():
    """F6: an attempt whose reply is lost after the commit stays unknown, then is found by its intent: counted once."""
    restart()
    line = Line(database(), LUCIA)
    await line.turn('Sí.', YES)
    await line.turn('Seguimos.', call('continue_lesson'))
    learner = line.call.state.facts.learner_id
    real = line.store.record_attempt

    async def committed_then_lost(*args, **kwargs):
        await real(*args, **kwargs)
        raise TimeoutError('the store did not answer')

    with patch.object(line.store, 'record_attempt', committed_then_lost):
        said = await line.turn('Two coffees, please.', grade('Two coffees, please.', 'correct'))
    assert said == SPANISH.unsettled and line.call.unresolved(), said
    said = await line.turn('¿Hola?', call('wait'))
    plan = await line.store.active_plan(learner)
    assert plan is not None and [a.item_id for a in plan.progress.attempts].count('en-travel-2-2') == 1
    assert not line.call.unresolved() and 'Pide dos bebidas' in said, said
    await line.turn('Two teas, please.', grade('Two teas, please.', 'correct'))
    plan = await line.store.active_plan(learner)
    assert plan is not None and [a.item_id for a in plan.progress.attempts].count('en-travel-2-2') == 1
    assert 'en-travel-2' in plan.progress.completed, 'the next answer, to the check, was recorded as usual'


async def calls_side_by_side_share_nothing():
    """F7: two learners in lessons at once, on one flow and one store: their answers, facts and questions stay theirs.

    Each call's script is keyed by what its step sees: the number at identify, the learner's name after it.
    """
    restart()
    script = Script()
    flow = faro.SPEC.compile(model=script.model)
    path = database()
    lucia, emma = Line(path, LUCIA, flow, script), Line(path, EMMA, flow, script)
    await asyncio.gather(lucia.turn('Sí.', YES, key=LUCIA), emma.turn('Yes.', YES, key=EMMA))
    await asyncio.gather(
        lucia.turn('Seguimos.', call('continue_lesson'), key='Lucía'),
        emma.turn("Let's carry on.", call('continue_lesson'), key='Emma'),
    )
    await asyncio.gather(
        lucia.turn('Two coffees, please.', grade('Two coffees, please.', 'correct'), key='Lucía'),
        emma.turn('Un kilo de manzanas.', grade('Un kilo de manzanas.', 'wrong'), key='Emma'),
    )
    first = await lucia.store.active_plan(lucia.call.state.facts.learner_id)
    second = await emma.store.active_plan(emma.call.state.facts.learner_id)
    assert first is not None and first.progress.attempts[-1].answer == 'Two coffees, please.'
    assert second is not None and second.progress.attempts[-1].answer == 'Un kilo de manzanas.'
    assert first.waiting() == 'en-travel-2-3' and second.waiting() == 'es-daily-2-1', (
        'each answer moved its own plan'
    )
    assert 'Lucía' not in emma.call.state.facts.notes and 'Emma' not in lucia.call.state.facts.notes
    assert lucia.call.locale.goodbye != emma.call.locale.goodbye, 'each call is in its own language'
    again = Line(path, LUCIA, flow, script)
    await again.turn('Sí.', YES, reply('Ahora mismo no puedo, perdona.'), key=LUCIA)
    assert not again.call.state.facts.learner_id, 'a second call for a learner already on one is refused'


async def approval_is_this_calls_only():
    """F8: a yes to the sign-up offer on one call is not a yes on the next: the profile needs a fresh one."""
    restart()
    path = database()
    line = Line(path, NEW)
    await line.turn('Yes.', YES)
    await line.turn('Yes, please.', call('agree_signup'))
    assert line.call.state.facts.signup_agreed
    await close_call(line.call)
    restart()
    line = Line(path, NEW)
    await line.turn('Yes.', YES)
    await line.turn('Call me Nico.', collect(name='Nico'), say('First, shall I create a profile?'))
    assert (await line.store.counts())['learners'] == 3 and not line.call.state.facts.signup_agreed


def no_reader_or_legacy_module_is_imported():
    """F9: Faro's flow, in a fresh process, imports none of core's retired reader or orchestration modules."""
    probe = (
        'import sys, faro\n'
        'faro.FLOW\n'
        "legacy = ['core.classifiers', 'core.readings', 'core.conversation', 'core.interpreters', 'core.desk', 'core.decisions']\n"
        'print(sorted(m for m in legacy if m in sys.modules))\n'
    )
    result = subprocess.run(
        [sys.executable, '-c', probe],
        env={**os.environ, 'GOOGLE_API_KEY': 'x'},
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).parents[1],
    )
    assert result.returncode == 0 and result.stdout.strip() == '[]', (result.stdout, result.stderr[-2000:])


GOLDEN = Path(__file__).parent / 'golden' / 'payloads.json'


async def requests_are_pinned():
    """Faro's requests whole (instructions, history, tool and output schemas), pinned."""
    restart()
    line = Line(database(), LUCIA, clock=lambda: datetime.fromisoformat('2026-09-28T08:30:00+02:00'))
    await line.turn('Sí, es ese.', YES)
    requests = {'identify, the yes': line.script.payloads[0]}
    await line.turn('Seguimos.', call('continue_lesson'))
    requests['menu, carrying on'] = line.script.payloads[0]
    await line.turn('Two coffees, please.', grade('Two coffees, please.', 'correct', '¡Muy bien!'))
    requests['lesson, an answer graded'] = line.script.payloads[0]
    text = json.dumps(
        {k: p.pinned() for k, p in requests.items()}, indent=2, sort_keys=True, ensure_ascii=False
    )
    if os.environ.get('BLESS_PAYLOADS'):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(text + '\n')
    assert GOLDEN.exists() and GOLDEN.read_text() == text + '\n', (
        'a request changed: review it, then BLESS_PAYLOADS=1'
    )


async def zero_is_a_value_the_state_validates():
    """`collect` on the prose onboarding: 0 is not dropped as empty; the state's `ge=5` refuses it, and nothing is recorded."""
    restart()
    line = Line(database(), NEW)
    await line.turn('Yes.', YES)
    await line.turn('Yes.', call('agree_signup'))
    await line.turn('Ana.', collect(name='Ana'))
    await line.turn('Zero minutes.', collect(length=0), say('Between five and sixty minutes?'))
    assert line.call.state.facts.length is None, line.call.state.facts.length
    assert 'Nothing was recorded' in line.script.payloads[1].history, 'the refusal went back to the model'


async def structured_steps_record_and_code_speaks():
    """F10: an update rides its read-back, one request; `record` only where code carries out; a question is no yes."""
    restart()
    line = Line(database())
    said = await line.turn(
        'Más treinta y cuatro, seis cero cero, uno uno uno, dos dos dos.',
        call('confirm_phone', update={'phone': '+34 600 111 222'}),
    )
    assert said == f'Let me check: is your number {curriculum.spaced(LUCIA)}?' and line.requests == 1, said
    offered = line.script.offered[0]
    assert 'collect' not in offered and {'record', 'reply', 'confirm_phone'} <= offered, (
        'the number goes with its own operation; `record` for a language, which confirm_phone cannot carry'
    )
    said = await line.turn('Sí.', YES)
    assert 'Lucía' in said and line.call.state.agent == 'menu', said

    restart()
    line = Line(database(), NEW)
    await to_assessment(line)
    for words in ('Hola.', 'Un café.', 'Fui a Roma.'):
        await line.turn(words, grade(words, 'partly'))
    assert line.call.state.agent == 'plan', line.call.state.agent
    proposal = line.call.open_question()
    learner = line.call.state.facts.learner_id
    said = await line.turn('How long is each lesson?', reply('About ten minutes a day. Shall we go ahead?'))
    assert said == 'About ten minutes a day. Shall we go ahead?' and line.requests == 1, said
    assert 'record' in line.script.offered[0], (
        'plan carries out `decline_plan` by code: it keeps its silent record'
    )
    assert await line.store.active_plan(learner) is None
    assert [i.status for i in line.call.intents if i.operation == 'activate_plan'] == ['prepared'], (
        'a side question neither agrees nor declines'
    )
    said = await line.turn('Yes.', call('activate_plan'))
    assert said == proposal.spoken and await line.store.active_plan(learner) is None, (
        "a yes to the teacher's own question is no yes to the plan: it is read back again"
    )
    said = await line.turn('Yes.', call('activate_plan'))
    assert 'your plan is active' in said and await line.store.active_plan(learner) is not None, said


TEACHING = ('Yes.', "Let's carry on.", 'How do I say apples?', 'Un kilo de manzanas, por favor.')
"""Emma's turns from the greeting to an answer after help."""
TEACHING_MOVES = (
    (YES,),
    (call('continue_lesson'),),
    (
        call('hand_over', to='help', because='a question about the exercise'),
        reply('Apples is "manzanas". Try it!'),
    ),
    (record(answer='Un kilo de manzanas, por favor.', grade='correct', feedback='Great!'),),
)
"""The step agents' moves for `TEACHING`: the coach's update inside `record`."""


async def teaching() -> tuple[Line, list[list[Payload]], list[str], list[list[tuple]]]:
    """`TEACHING` from a fresh copy of the fixture: the line, each turn's requests and words."""
    restart()
    line = Line(database(), EMMA)
    sent, said, given = [], [], []
    for words, moves in zip(TEACHING, TEACHING_MOVES, strict=True):
        said.append(await line.turn(words, *moves))
        sent.append(list(line.script.payloads))
        given.append(list(line.script.given))
    return line, sent, said, given


async def version_2_keeps_teaching_apart_from_planning():
    """F11: a lesson with help: what each step sees, help, grade and receipt, routing.

    Lesson (rendered, its own turns) is never sent the number read back at
    the greeting. The coach sees the whole conversation, since the exercise
    it helps with was said on the lesson's turn; its facts hold the
    exercise's id, never the number. Help is the store's record before the
    coach speaks. The coach's `record` carries the answer and the grade
    that is its own judgement; `record_attempt`'s receipt is the store's,
    `assisted`, said by code, and its route moves the learner back to the
    lesson.
    """
    line, sent, said, _ = await teaching()
    number = curriculum.spaced(EMMA)
    assert [len(requests) for requests in sent] == [1, 1, 2, 1], [len(r) for r in sent]
    assert (
        'Shopping, first exercise. Answer in Spanish: Ask for a kilo of apples at the market.' in said[1]
    ), said
    assert said[2] == 'Apples is "manzanas". Try it!' and said[3].startswith(
        'Great! Shopping, second exercise.'
    )
    lesson, coach = sent[2]
    assert number not in lesson and EMMA not in lesson, 'the lesson sees its own turns: no number read back'
    assert '"waiting":"es-daily-2-1"' in coach.instructions and EMMA not in coach.instructions, (
        coach.instructions
    )
    assert 'Ask for a kilo of apples at the market.' in coach.history, 'the exercise, from the conversation'
    assert number in coach.history, 'the conversation is sent as said: the greeting named the number'
    assert '"confirmed"' not in coach.instructions and '"draft_' not in coach.instructions, (
        'no account or plan'
    )
    assert 'collect' not in SCRIPT.offered[-1] and 'record' in SCRIPT.offered[-1]
    plan = await line.store.active_plan(line.call.state.facts.learner_id)
    assert plan is not None and plan.progress.attempts[-1].result == 'assisted', 'the store says assisted'
    assert line.call.state.facts.grade == 'correct', "the grade in state is the coach's judgement"
    assert line.call.state.agent == 'lesson', "the receipt's route: back to the lesson for the next exercise"
    switched = await line.turn('¿Podemos hablar en español?', collect(asked_language='ES'))
    assert (
        switched == curriculum.exercise('ES', 'es-daily-2-2') and line.call.state.facts.explanation == 'ES'
    ), 'the rendered lesson puts the exercise waiting again, in the new language'

    restart()
    line = Line(database(), NEW)
    await to_assessment(line)
    for words in ('Hola.', 'Un café.', 'Fui a Roma.'):
        await line.turn(words, grade(words, 'partly'))
    await line.turn('How long is each lesson?', reply('About ten minutes a day.'))
    plan_request = line.script.payloads[-1]
    for hidden in ('"answer"', '"grade"', '"feedback"', '"evidence"', '"phone"', '"confirmed"'):
        assert hidden not in plan_request.instructions, (hidden, plan_request.instructions)
    assert '"draft_minutes"' in plan_request.instructions, 'the plan sees the proposal it owns'


async def adapter_bytes(messages: list[Any], info: AgentInfo) -> dict[str, int]:
    """One request as the installed Gemini adapter maps it, offline, sending nothing: system, contents, tools.

    The same measure as the new salon's (`GoogleModel._build_content_and_config`),
    before the google-genai client's own HTTP encoding. Bytes, not tokens.
    """
    from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
    from pydantic_ai.providers.google import GoogleProvider

    model = GoogleModel('gemini-3.5-flash-lite', provider=GoogleProvider(api_key='offline'))
    contents, config = await model._build_content_and_config(
        messages, GoogleModelSettings(**(info.model_settings or {})), info.model_request_parameters
    )

    def size(value: Any) -> int:
        return len(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode())

    found = {
        'system': size(config.get('system_instruction')),
        'contents': size(contents),
        'tools': size([config.get('tools'), config.get('tool_config')]),
    }
    return found | {'total': sum(found.values())}


async def table() -> None:
    """`--table`: `TEACHING`, request by request, in bytes (not tokens).

    The first columns are the Pydantic AI model boundary (`Payload`), the last
    four the Gemini adapter's mapping of the same request (`adapter_bytes`).
    """
    print(
        '| Turn | Req | Instructions | History | of which results | Tools | Outputs | Total |'
        ' Adapter system | Adapter contents | Adapter tools | Adapter total |'
    )
    print('|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|')
    _, sent, _, given = await teaching()
    for words, requests, raw in zip(TEACHING, sent, given, strict=True):
        for number, (payload, (messages, info)) in enumerate(zip(requests, raw, strict=True), 1):
            size, wire = payload.sizes(), await adapter_bytes(messages, info)
            print(
                f'| {words} | {number} | {size["instructions"]} | {size["history"]} |'
                f' {payload.results()} | {size["tools"]} | {size["outputs"]} | {size["total"]} |'
                f' {wire["system"]} | {wire["contents"]} | {wire["tools"]} | {wire["total"]} |'
            )


async def main():
    await requests_are_pinned()
    await structured_steps_record_and_code_speaks()
    await version_2_keeps_teaching_apart_from_planning()
    await zero_is_a_value_the_state_validates()
    await seeds_are_identified_only_after_the_yes()
    await a_wrong_or_foreign_number_discloses_nothing()
    await an_unknown_caller_signs_up_and_agrees_a_plan()
    await declines_conditions_and_revisions_activate_nothing()
    await onboarding_resumes_after_a_restart()
    await lucia_carries_on_from_exercise_two()
    await a_recap_changes_nothing()
    await emma_reassesses_and_the_old_plan_stands_until_a_yes()
    await honest_evidence_moves_only_what_it_shows()
    await a_goodbye_during_an_exercise_is_not_graded()
    await a_proposal_talked_over_activates_nothing()
    await every_ending_saves_the_session()
    await replays_failures_and_stale_writes_change_nothing()
    await memory_is_the_confirmed_learners_and_corrections_win()
    await nothing_is_completed_without_answers()
    await a_fixture_is_fresh_and_a_restart_keeps_everything()
    await an_acknowledged_answer_survives_a_killed_process()
    await each_step_offers_only_its_own_tools()
    await a_coached_answer_is_assisted_and_the_check_completes_the_lesson()
    await a_language_switch_keeps_the_question_answerable()
    await hostile_model_output_is_refused_where_it_runs()
    await a_yes_binds_only_to_a_question_heard_in_full()
    await a_lost_reply_is_reconciled_once()
    await calls_side_by_side_share_nothing()
    await approval_is_this_calls_only()
    no_reader_or_legacy_module_is_imported()
    print('faro ok: L01-L16 and F1-F11 on the public path, a real SQLite file, no reader imported')


if __name__ == '__main__':
    asyncio.run(table() if '--table' in sys.argv else main())
