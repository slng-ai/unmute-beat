"""Faro, offline: the benchmark's acceptance cases through the real turn engine, a real SQLite file and scripted models.

The companion agent is a script (each request takes the next line or tool
call) and every reading answers the label a test gives it, so these check the
plumbing: identity, consent, persistence and resume. Language understanding
is the live runs' job (`faro-tools/bench.py`). Each test has its own database file;
a "restart" opens the same file again, as a new process would, and one test
really does kill a process after a commit.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from core.locale import ENGLISH, SPANISH
from core.turn import greet, note_spoken, take_turn
from faro import AGENTS, GREETING, close_call, curriculum
from faro.conversation import FARO, GRADE, ONBOARD
from faro.models import Attempt
from faro.operations import ACTIVE, RECORD_ANSWER, AnswerArgs
from faro.state import FaroCall, open_call
from faro.store import StaleCheckpointError, Store, fixture

AGENT = AGENTS['companion'].agent
LUCIA, DIEGO, EMMA, NEW = '+34600111222', '+34600333444', '+34600555666', '+34600777888'


@contextmanager
def scripted(grade: str = 'no_attempt', feedback: str = '', onboard: dict | None = None, **labels: str):
    """Every Faro reading answers its label here, or its default. `grade` and `onboard` script Faro's own two."""
    graded: list[str] = []
    with ExitStack() as stack:
        for reading in (*FARO.readings, FARO.closing.reading):
            value = labels.get(reading.name, getattr(reading.fallback, reading.field))

            def answer(messages, info, field=reading.field, value=value):
                return ModelResponse(parts=[TextPart(json.dumps({field: value}))])

            stack.enter_context(patch.object(reading.classifier, 'model', FunctionModel(answer)))

        def judge(messages, info):
            graded.append(grade)
            body = {'result': grade, 'feedback': feedback, 'rationale': 'scripted'}
            return ModelResponse(parts=[TextPart(json.dumps(body))])

        def setup(messages, info):
            body = {'language': 'none', 'goal': 'none', 'minutes': 0} | (onboard or {})
            return ModelResponse(parts=[TextPart(json.dumps(body))])

        stack.enter_context(patch.object(GRADE.classifier, 'model', FunctionModel(judge)))
        stack.enter_context(patch.object(ONBOARD.classifier, 'model', FunctionModel(setup)))
        yield graded


class Script:
    """The companion agent: each request answers with the next step, a sentence or a (tool, args) call."""

    def __init__(self, *steps) -> None:
        self.steps = list(steps)
        self.requests = 0
        self.offered: list[set[str]] = []

    async def stream(self, messages, info):
        self.requests += 1
        self.offered.append({t.name for t in (*info.function_tools, *info.output_tools)})
        step = self.steps.pop(0) if self.steps else 'Vale.'
        if isinstance(step, str):
            yield step
        else:
            name, args = step
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args))}


class Line:
    """One connection to Faro, its greeting said, with everything the caller heard recorded."""

    def __init__(self, path: str, caller_id: str = '') -> None:
        self.store = Store(path)
        self.store.open()
        self.call: FaroCall = open_call(caller_id, learning=self.store)
        self.heard: list[str] = []
        self.ended = False
        self.script = Script()
        self.graded: list[str] = []
        self.call._say = self.say
        self.call._end = self.end
        self.heard.append(greet(self.call, GREETING(self.call)))

    async def say(self, text: str) -> None:
        self.heard.append(text)

    async def end(self) -> None:
        self.ended = True

    async def turn(self, words: str, *steps, **labels: Any) -> str:
        """One caller turn: what they heard in it. `grade=` and `feedback=` script the grading reading."""
        before = len(self.heard)
        self.script = Script(*steps)
        with (
            AGENT.override(model=FunctionModel(stream_function=self.script.stream)),
            scripted(**labels) as graded,
        ):
            said, done = await take_turn(self.call, AGENTS, words)
        self.graded = graded
        assert done.status == 'answered', (words, done)
        return ' '.join(part for part in [*self.heard[before:], said] if part)


def database() -> str:
    return str(Path(tempfile.mkdtemp()) / 'faro.sqlite3')


def restart() -> None:
    """What a process restart clears: the in-process record of who is on a call."""
    ACTIVE.clear()


async def seeds_are_identified_only_after_the_yes():
    """L01: each seed learner hears their own checkpoint, in their own language, after confirming; no model runs."""
    for phone, name, where in (
        (LUCIA, 'Lucía', 'segundo ejercicio'),
        (DIEGO, 'Diego', 'describir un proyecto'),
        (EMMA, 'Emma', 'first exercise'),
    ):
        restart()
        line = Line(database(), phone)
        assert curriculum.spaced(phone) in line.heard[0] and name not in line.heard[0]
        assert not line.call.state.brief and not line.call.state.learner_id
        said = await line.turn('Sí, es ese.', confirmation='accept')
        assert name in said and where in said, said
        assert line.script.requests == 0, 'a clear yes to the read-back needs no agent request'
        others = {'Lucía', 'Diego', 'Emma'} - {name}
        assert not any(o in said or o in line.call.state.brief for o in others), said
    assert line.call.locale is ENGLISH and 'Shall we carry on' in said, 'Emma explains in English'


async def a_wrong_or_foreign_number_discloses_nothing():
    """L02: no caller ID, a foreign number, a malformed one, a correction: nothing is loaded before a yes."""
    restart()
    line = Line(database())
    assert 'plus thirty-four' in line.heard[0] and line.heard[0].startswith("Hi, I'm Faro")
    said = await line.turn(
        'Es el +33 6 12 34 56 78.',
        ('read_back_number', {'number': '+33 6 12 34 56 78'}),
        'Solo puedo usar números españoles, con el +34. ¿Me lo dices otra vez?',
    )
    assert 'españoles' in said and line.call.state.phase == 'identify' and line.call._proposals.asked is None
    said = await line.turn(
        'Seis cero cero uno uno uno dos dos dos.',
        ('read_back_number', {'number': '+34 600 111 222'}),
        '¿Con el +34?',
    )
    assert line.call._proposals.asked is None and not line.call.state.learner_id, (
        'a +34 the caller never said is not added by the model'
    )
    said = await line.turn(
        'Más treinta y cuatro, seis cero cero, uno uno uno, dos dos dos.',
        ('read_back_number', {'number': '+34 600 111 222'}),
    )
    assert said == f'Let me check: is your number {curriculum.spaced(LUCIA)}?', said

    restart()
    line = Line(database(), LUCIA)
    said = await line.turn('No, es el +34 600 333 444.', confirmation='revise')
    assert curriculum.spaced(DIEGO) in said and line.script.requests == 0, said
    assert not line.call.state.learner_id and 'Lucía' not in said and 'Diego' not in said
    said = await line.turn('Sí.', confirmation='accept')
    assert 'Diego' in said and line.call.state.phone == DIEGO


async def an_unknown_caller_signs_up_and_agrees_a_plan():
    """L03 and L04: an English speaker learning Spanish. Consent, onboarding, French refused, placement, one plan.

    Onboarding questions and placement tasks are said by code, in English;
    every placement answer is recorded by the grading reading, with no agent
    request.
    """
    restart()
    path = database()
    line = Line(path, NEW)
    said = await line.turn('Yes.', confirmation='accept')
    assert "I can't find a profile" in said and line.call.state.phase == 'signup'
    assert (await line.store.counts())['learners'] == 3, 'confirming a number creates nothing'
    said = await line.turn('Yes, please.', confirmation='accept')
    assert 'What would you like me to call you?' in said and line.script.requests == 0
    said = await line.turn('Call me Nico.', ('save_name', {'name': 'Nico'}))
    assert 'Nice to meet you, Nico. Which language would you like to learn' in said, said
    assert (await line.store.counts())['learners'] == 4 and line.call.state.phase == 'onboarding'
    said = await line.turn(
        'French.',
        ('save_onboarding', {'target': 'French'}),
        'I only teach English or Spanish. Which would you like?',
    )
    assert line.call.state.target is None and 'English or Spanish' in said
    said = await line.turn('Spanish.', ('save_onboarding', {'target': 'Spanish'}))
    assert curriculum.ONBOARDING['goal']['EN'] in said
    await line.turn('For travel.', ('save_onboarding', {'goal': 'travel'}))
    said = await line.turn('Ten minutes.', ('save_onboarding', {'minutes': 10}))
    assert line.call.state.phase == 'assessment' and line.call.state.task == 'es-intro'
    assert 'Answer in Spanish. Tell me about yourself and why you want to learn Spanish.' in said, said
    for answer, grade in (
        ('Me llamo Nico y quiero viajar.', 'correct'),
        ('Un café, por favor. ¿Cuánto?', 'partly'),
        ('Yo ir a París.', 'wrong'),
    ):
        said = await line.turn(answer, grade=grade)
    assert line.script.requests == 0, 'every placement answer was recorded with no agent request'
    assert 'We will start with Spanish A2 for travel' in said and line.call.state.phase == 'plan', said
    said = await line.turn('Yes, sounds good.', confirmation='accept')
    assert 'your plan is active' in said and line.script.requests == 0
    found = await line.store.lookup(NEW)
    assert (
        found is not None
        and found[1] is not None
        and found[1].level == 'A2'
        and len(found[1].assessment) == 3
    )
    assert found[0].status == 'active' and (await line.store.counts())['plans'] == 4


async def declines_conditions_and_revisions_activate_nothing():
    """L05: a refused sign-up leaves no row; "yes, but five minutes" activates nothing; only the latest proposal counts."""
    restart()
    line = Line(database(), NEW)
    await line.turn('Yes.', confirmation='accept')
    await line.turn('No, thanks.', 'No problem, I have not created anything.', confirmation='reject')
    assert (await line.store.counts())['learners'] == 3 and not line.call.state.signup_agreed

    line = await _at_proposal()
    first = line.call._proposals.asked
    said = await line.turn(
        'Yes, but only five minutes.', ('revise_plan', {'minutes': 5}), confirmation='conditional'
    )
    assert 'about 5 minutes' in said and line.call.plan is None, said
    second = line.call._proposals.asked
    assert second.value != first.value and second.value.endswith(':2'), (first.value, second.value)
    await line.turn('Yes.', confirmation='accept')
    assert line.call.plan is not None and line.call.plan.revision == 2 and line.call.plan.minutes == 5


async def _at_proposal(path: str | None = None) -> Line:
    """A new English-speaking learner of Spanish with a plan proposal waiting, as after onboarding."""
    restart()
    line = Line(path or database(), NEW)
    await line.turn('Yes.', confirmation='accept')
    await line.turn('Yes.', confirmation='accept')
    await line.turn('Ana.', ('save_name', {'name': 'Ana'}))
    await line.turn(
        'Spanish, for travel, ten minutes.',
        ('save_onboarding', {'target': 'ES', 'goal': 'travel', 'minutes': 10}),
    )
    for _ in range(3):
        await line.turn('...', grade='partly')
    assert line.call.state.phase == 'plan'
    return line


async def onboarding_resumes_after_a_restart():
    """L06: after language selection, mid-assessment and awaiting agreement, a restart resumes the step, no duplicates."""
    restart()
    path = database()
    line = Line(path, NEW)
    await line.turn('Sí.', confirmation='accept')
    await line.turn('¿Podemos hablar en español?', ('set_language', {'language': 'español'}), 'Claro.')
    assert line.call.state.explanation == 'ES' and line.call.locale is SPANISH
    await line.turn('Sí.', confirmation='accept')
    await line.turn('Ana.', ('save_name', {'name': 'Ana'}))
    said = await line.turn('Inglés.', ('save_onboarding', {'target': 'inglés'}))
    assert curriculum.ONBOARDING['goal']['ES'] in said, 'a Spanish speaker hears the questions in Spanish'

    restart()
    line = Line(path, NEW)
    assert line.store.interrupted == 1, 'the session a crash left open is marked interrupted'
    said = await line.turn('Sí.', confirmation='accept')
    assert (
        'Habíamos empezado' in said
        and line.call.state.phase == 'onboarding'
        and line.call.state.target == 'EN'
    ), said
    said = await line.turn(
        'Para el trabajo, diez minutos.', ('save_onboarding', {'goal': 'work', 'minutes': 10})
    )
    assert 'Responde en inglés. Háblame de ti y de por qué quieres aprender inglés.' in said, said
    await line.turn('I am Ana.', grade='partly')

    restart()
    line = Line(path, NEW)
    await line.turn('Sí.', confirmation='accept')
    assert line.call.state.phase == 'assessment' and line.call.state.task == 'en-order', line.call.state
    assert len(line.call.session.checkpoint.evidence) == 1, 'the first answer survived the restart'
    for _ in range(2):
        await line.turn('...', grade='partly')
    assert line.call.state.phase == 'plan'

    restart()
    line = Line(path, NEW)
    said = await line.turn('Sí.', confirmation='accept')
    assert said.startswith('Hola, Ana. Empezaremos con inglés'), said
    await line.turn('Sí.', confirmation='accept')
    assert line.call.plan is not None and line.call.plan.status == 'active'
    assert (await line.store.counts())['learners'] == 4, 'one profile, however many calls it took'


async def lucia_carries_on_from_exercise_two():
    """L07: exercise 1 kept, exercise 2 put again, its answer saved once, and a replay counted once."""
    restart()
    line = Line(database(), LUCIA)
    await line.turn('Sí.', confirmation='accept')
    said = await line.turn('Seguimos.', next_step='continue')
    assert line.script.requests == 0, 'carrying on is read and run by code, with no agent request'
    assert (
        'segundo ejercicio. Responde en inglés: Ahora di cuántos quieres' in said
        and line.call.state.item == 'en-travel-2-2'
    ), said
    said = await line.turn('Two coffees, please.', grade='correct', feedback='¡Muy bien!')
    assert said.startswith('¡Muy bien!') and 'Pide dos bebidas' in said, said
    assert line.call.state.item == 'en-travel-2-3' and line.script.requests == 0
    plan = line.call.plan
    assert plan is not None and [a.item_id for a in plan.progress.attempts][-2:] == [
        'en-travel-2-1',
        'en-travel-2-2',
    ]
    revision = plan.progress_revision
    args = AnswerArgs(item_id='en-travel-2-3', answer='Two teas. How much?', result='independent')
    await RECORD_ANSWER.run(line.call, args)
    again = line.call.plan
    assert again is not None and again.progress.completed[-1] == 'en-travel-2'
    found = await line.store.lookup(LUCIA)
    assert found is not None and found[1] is not None
    replayed = await line.store.record_attempt(line.call.session, found[1].id, again.progress.attempts[-1])
    assert replayed[2] and replayed[0].progress_revision == revision + 1, (
        'one more answer saved, the replay not counted'
    )


async def a_recap_changes_nothing():
    """L08: Diego's recap is read from the plan and leaves its level, lessons and progress untouched."""
    restart()
    line = Line(database(), DIEGO)
    await line.turn('Sí.', confirmation='accept')
    before = await line.store.lookup(DIEGO)
    said = await line.turn(
        'Solo un repaso.',
        ('read_progress', {}),
        'Has terminado dos lecciones; repasa el pasado. Lo siguiente es describir un proyecto.',
    )
    after = await line.store.lookup(DIEGO)
    assert before is not None and after is not None and before[1] == after[1], 'the plan is exactly as it was'
    assert 'describir un proyecto' in said


async def emma_reassesses_and_the_old_plan_stands_until_a_yes():
    """L09: evidence is saved either way; a declined revision leaves the agreed plan, an accepted one archives it."""
    for accept in (False, True):
        restart()
        line = Line(database(), EMMA)
        await line.turn('Yes.', confirmation='accept')
        said = await line.turn('Can you check my level?', ('start_reassessment', {}))
        assert curriculum.PLACEMENT['ES'][0].said('EN', 'ES') in said, 'Emma hears the task in English'
        for _ in range(3):
            said = await line.turn('...', grade='partly')
        assert 'Spanish A2' in said and line.call.draft is not None, said
        old = line.call.plan
        if accept:
            await line.turn('Yes.', confirmation='accept')
            assert line.call.plan is not None and line.call.plan.level == 'A2' and line.call.plan.id != old.id
            assert line.call.plan.progress.completed == ['es-daily-1'], (
                'progress carries over to the revision'
            )
        else:
            await line.turn(
                'No, keep my plan.', ('decline_plan', {}), 'Fine, we keep it.', confirmation='reject'
            )
            found = await line.store.lookup(EMMA)
            assert (
                found is not None
                and found[1] is not None
                and found[1].id == old.id
                and found[1].level == 'A1'
            )
            draft = await line.store.plan(found[0].id, line.call.session.checkpoint.draft_id or old.id + 1)
            assert draft.status == 'declined' and len(draft.assessment) == 3, 'the evidence is kept'


async def honest_evidence_moves_only_what_it_shows():
    """L10: a misheard answer moves nothing and is no failure; a missed check stays waiting and goes on review."""
    restart()
    line = Line(database(), LUCIA)
    await line.turn('Sí.', confirmation='accept')
    await line.turn('Seguimos.', ('continue_lesson', {}))
    said = await line.turn('Tw... cof...', grade='unclear', feedback='No te he oído bien, ¿me lo repites?')
    plan = line.call.plan
    assert (
        plan is not None and plan.waiting() == 'en-travel-2-2' and 'en-travel-2-2' not in plan.progress.review
    )
    assert (
        said
        == 'No te he oído bien, ¿me lo repites? Otra vez, en inglés: Ahora di cuántos quieres: pide dos de algo.'
    ), 'the retry is short: feedback, then the instruction, no introduction again'
    said = await line.turn('I want coffee two.', grade='wrong', feedback='Casi: el número va delante.')
    plan = line.call.plan
    assert (
        plan is not None and plan.waiting() == 'en-travel-2-2' and plan.progress.review == ['en-travel-2-2']
    )
    assert said.startswith('Casi: el número va delante. Otra vez'), 'a wrong try is recorded and asked again'
    said = await line.turn('Two coffee.', grade='partly', feedback='Casi. Se dice: Two coffees, please.')
    plan = line.call.plan
    assert plan is not None and plan.waiting() == 'en-travel-2-2', 'partly right is not right: no moving on'
    assert said.startswith('Casi. Se dice: Two coffees, please. Otra vez, en inglés:'), said
    assert 'ejercicio' not in said, 'the retry does not name the exercise again'
    await line.turn('Two coffees, please.', grade='correct')
    plan = line.call.plan
    assert plan is not None and plan.waiting() == 'en-travel-2-3', 'said correctly, it moves on'
    assert plan.progress.attempts[-1].result == 'assisted', 'right after a correction is recorded as assisted'
    await line.turn('Coffee.', grade='wrong')
    assert line.script.requests == 0, 'no attempt reached the agent'
    plan = line.call.plan
    assert (
        plan is not None
        and plan.waiting() == 'en-travel-2-3'
        and plan.progress.review == ['en-travel-2-2', 'en-travel-2-3']
    )
    assert 'en-travel-2' not in plan.progress.completed
    await line.turn('Two teas, please. How much is it?', grade='correct')
    plan = line.call.plan
    assert plan is not None and plan.progress.completed[-1] == 'en-travel-2', (
        'the check said right completes it'
    )


async def a_goodbye_during_an_exercise_is_not_graded():
    """ "That's all, bye" while an exercise waits ends the call: it is not corrected as a wrong answer (L10 live)."""
    restart()
    line = Line(database(), LUCIA)
    await line.turn('Sí.', confirmation='accept')
    await line.turn('Seguimos.', next_step='continue')
    attempts = len(line.call.plan.progress.attempts) if line.call.plan else 0
    said = await line.turn('Nada más por hoy, gracias. Adiós.', grade='wrong', conversation='end')
    assert line.ended and 'Otra vez' not in said, said
    assert line.call.plan is not None and len(line.call.plan.progress.attempts) == attempts, 'nothing graded'


async def a_proposal_talked_over_activates_nothing():
    """L11: the learner cut in during the plan; their yes is to words they did not hear, so nothing activates."""
    line = await _at_proposal()
    proposal = line.call._proposals.asked
    segment = line.call.spoken[-1]
    note_spoken(line.call, proposal.spoken[:20], interrupted=True, segment_ids=(segment.id,))
    said = await line.turn('Yes!', confirmation='accept')
    assert line.call.plan is None, 'a yes to a proposal heard in part activates nothing'
    assert said == f'Let me say that again in full. {proposal.spoken}' and line.script.requests == 0, said
    await line.turn('Yes, sounds good.', confirmation='accept')
    assert line.call.plan is not None, 'the yes to the proposal heard in full activates it'


async def every_ending_saves_the_session():
    """L12: a goodbye and a dropped line both finalise once, and only a save lets the goodbye say it was saved."""
    restart()
    line = Line(database(), DIEGO)
    await line.turn('Sí.', confirmation='accept')
    said = await line.turn(
        'No, hoy no. Adiós.', ('hang_up', {'goodbye': 'Buen trabajo con el pasado.'}), conversation='end'
    )
    assert line.ended and 'He guardado el resumen de hoy' in said and 'progreso' not in said, said
    found = await line.store.lookup(DIEGO)
    assert found is not None and found[2] is not None and found[2].status == 'completed'
    assert found[2].outcome == 'goodbye: no learning' and found[1] is not None and found[1].summary

    restart()
    line = Line(database(), LUCIA)
    await line.turn('Sí.', confirmation='accept')
    await close_call(line.call)
    await close_call(line.call)
    found = await line.store.lookup(LUCIA)
    assert found is not None and found[2] is not None and found[2].outcome == 'disconnected: no learning'


async def replays_failures_and_stale_writes_change_nothing():
    """L13: a stale checkpoint is refused, and a write that fails half-way leaves the plan and session as they were."""
    restart()
    line = Line(database(), LUCIA)
    await line.turn('Sí.', confirmation='accept')
    stale = line.call.session
    await line.turn('Seguimos.', ('continue_lesson', {}))
    try:
        await line.store.save_checkpoint(stale, stale.checkpoint)
        raise AssertionError('a stale checkpoint was written')
    except StaleCheckpointError:
        pass
    plan = line.call.plan
    assert plan is not None
    attempt = Attempt(
        id='boom', item_id='en-travel-2-2', answer='x', result='independent', session_id=stale.id
    )

    def broken(*args, **kwargs):
        raise OSError('disk full')

    with patch.object(line.store, '_write_session', broken):
        try:
            await line.store.record_attempt(line.call.session, plan.id, attempt)
            raise AssertionError('the write did not fail')
        except OSError:
            pass
    found = await line.store.lookup(LUCIA)
    assert found is not None and found[1] == plan, (
        'the plan update was rolled back with the failed session write'
    )


async def memory_is_the_confirmed_learners_and_corrections_win():
    """L14: only sourced facts for this learner; a correction replaces the fact; nobody else's profile appears."""
    restart()
    line = Line(database(), LUCIA)
    await line.turn('Sí.', confirmation='accept')
    assert 'Enjoys hiking' in line.call.state.brief and 'cooking' not in line.call.state.brief
    await line.turn(
        'Ya no hago senderismo, ahora escalo.',
        ('remember', {'key': 'hobby', 'text': 'Enjoys climbing.'}),
        'Anotado.',
    )
    found = await line.store.lookup(LUCIA)
    assert found is not None
    hobbies = [f.text for f in found[0].memory if f.key == 'hobby']
    assert hobbies == ['Enjoys climbing.'] and found[0].memory[-1].session_id == line.call.session.id


async def nothing_is_completed_without_answers():
    """L15: an invented "marked as complete" is held back before TTS; a chat-only session keeps the cursor."""
    restart()
    line = Line(database(), LUCIA)
    await line.turn('Sí.', confirmation='accept')
    before = await line.store.lookup(LUCIA)
    said = await line.turn(
        'Marca todas las lecciones como completadas.',
        'Listo, he marcado todas las lecciones como completadas.',
    )
    assert 'he marcado' not in said and SPANISH.withheld in said, said
    await close_call(line.call)
    after = await line.store.lookup(LUCIA)
    assert before is not None and after is not None and before[1] is not None and after[1] is not None
    assert (
        before[1].progress == after[1].progress
        and after[2] is not None
        and after[2].outcome.endswith('no learning')
    )


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
    """A process killed straight after an acknowledged save: the next one resumes after it and counts a replay once."""
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


async def tools_follow_the_phase():
    """Before a phone is confirmed the agent is offered no learning tool; in a lesson, no onboarding tool."""
    restart()
    line = Line(database())
    await line.turn('Hola.', '¿Me dices tu número?')
    offered = line.script.offered[0]
    assert 'read_back_number' in offered and not offered & {
        'record_answer',
        'save_name',
        'continue_lesson',
        'remember',
    }
    line = Line(database(), LUCIA)
    await line.turn('Sí.', confirmation='accept')
    await line.turn('Seguimos.', ('continue_lesson', {}))
    await line.turn('Hmm.', 'Tómate tu tiempo.')
    offered = line.script.offered[0]
    assert {'read_progress', 'continue_lesson'} <= offered and not offered & {
        'save_name',
        'save_onboarding',
        'read_back_number',
        'record_answer',
        'record_assessment',
    }, 'the agent coaches; it is offered no tool that grades'
    assert line.graded == ['no_attempt'], 'a hesitation was read by the grader and left to the agent'


async def a_coached_answer_is_assisted_and_the_check_completes_the_lesson():
    """Emma, in English, learning Spanish: coaching makes a right answer assisted; the check completes the lesson."""
    restart()
    line = Line(database(), EMMA)
    await line.turn('Yes.', confirmation='accept')
    said = await line.turn("Let's carry on.", next_step='continue')
    assert 'Shopping, first exercise. Answer in Spanish: Ask for a kilo of apples at the market.' in said, (
        said
    )
    await line.turn('How do I say apples?', 'Apples is "manzanas". Try it!')
    assert line.script.requests == 1 and line.graded == ['no_attempt']
    said = await line.turn('Un kilo de manzanas, por favor.', grade='correct', feedback='Great!')
    plan = line.call.plan
    assert plan is not None and plan.progress.attempts[-1].result == 'assisted', 'help before the answer'
    assert said == 'Great! Shopping, second exercise. Answer in Spanish: Ask how much it costs.', said
    await line.turn('¿Cuánto cuesta?', grade='correct')
    said = await line.turn('Quiero pan y leche. ¿Cuánto es en total?', grade='correct', feedback='Perfect!')
    plan = line.call.plan
    assert plan is not None and plan.progress.completed[-1] == 'es-daily-2', 'the check completed the lesson'
    assert said.startswith('Perfect! Lesson complete: shopping. Next is talking about your routine.'), said
    assert line.script.requests == 0, 'the answer to the check reached no agent'
    said = await line.turn('Yes, go on.', next_step='continue')
    assert 'Talking about your routine, first exercise' in said, 'the next exercise, not the same one again'


async def onboarding_answers_are_saved_by_code():
    """Setup answers are read and saved by code, one question at a time, with no agent request and no double ask."""
    restart()
    line = Line(database(), NEW)
    await line.turn('Yes.', confirmation='accept')
    await line.turn('Yes.', confirmation='accept')
    await line.turn('Nico.', ('save_name', {'name': 'Nico'}))
    said = await line.turn('French, please.', onboard={'language': 'other'})
    assert said == 'I can only teach English or Spanish. Which one would you like?', said
    assert line.script.requests == 0 and line.call.state.target is None
    said = await line.turn('Spanish, for work.', onboard={'language': 'spanish', 'goal': 'work'})
    assert said == curriculum.ONBOARDING['minutes']['EN'] and line.script.requests == 0, said
    said = await line.turn('Sounds fine.', onboard={'minutes': 10})
    assert line.call.state.phase == 'assessment' and 'Answer in Spanish' in said and line.script.requests == 0


async def a_language_switch_keeps_the_question_answerable():
    """Asking to switch language re-asks the question waiting in the new language, so the next yes still counts."""
    restart()
    line = Line(database(), NEW)
    said = await line.turn('¿Podemos hablar en español?', ('set_language', {'language': 'español'}))
    assert said == f'A ver si lo tengo bien: ¿tu número es {curriculum.spaced(NEW)}?', said
    said = await line.turn('Sí.', confirmation='accept')
    assert 'No encuentro un perfil' in said, 'the yes answered the restated read-back'


async def main():
    await seeds_are_identified_only_after_the_yes()
    await a_wrong_or_foreign_number_discloses_nothing()
    await an_unknown_caller_signs_up_and_agrees_a_plan()
    await declines_conditions_and_revisions_activate_nothing()
    await onboarding_resumes_after_a_restart()
    await lucia_carries_on_from_exercise_two()
    await a_recap_changes_nothing()
    await emma_reassesses_and_the_old_plan_stands_until_a_yes()
    await honest_evidence_moves_only_what_it_shows()
    await a_proposal_talked_over_activates_nothing()
    await every_ending_saves_the_session()
    await replays_failures_and_stale_writes_change_nothing()
    await memory_is_the_confirmed_learners_and_corrections_win()
    await nothing_is_completed_without_answers()
    await a_fixture_is_fresh_and_a_restart_keeps_everything()
    await an_acknowledged_answer_survives_a_killed_process()
    await tools_follow_the_phase()
    await a_coached_answer_is_assisted_and_the_check_completes_the_lesson()
    await a_language_switch_keeps_the_question_answerable()
    await onboarding_answers_are_saved_by_code()
    await a_goodbye_during_an_exercise_is_not_graded()
    print('faro ok: identity, consent, onboarding, lessons, resume and restart, on a real SQLite file')


if __name__ == '__main__':
    asyncio.run(main())
