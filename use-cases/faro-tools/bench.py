"""Faro's benchmark cases, live: scripted text calls on the real model, one Langfuse session per call.

    cd use-cases/faro-tools
    uv run --env-file ../../.env bench.py              # every case
    uv run --env-file ../../.env bench.py L03 L07-ES   # some of them

The keys come from `--env-file`: importing core reads no `.env`.

Makes billable model requests. There is no audio: each caller line goes
through the real turn engine (`core.turn.run_turn`) with Faro's real step
agents, so what is measured is the harness and the model, not STT or TTS.

Each case starts from a new fixture database. Every call in a case runs in a
process of its own, on the same file, so a resume after a call is a resume
after a real restart; one case kills its process straight after an
acknowledged save. The caller is a scripted learner (`Learner`): it answers
what the call is asking, read from the step it is in, so a model that words a
question differently still gets a sensible reply. The checks read the
database and what the caller heard, never the model's own account.

The printout ends with every session id. Read cost and latency with:

    cd use-cases/concierge-tools && uv run ledger.py faro session:<id> ...

ponytail: one pass per case and language. Enough to find failures, not a pass
rate; repeat with `--repeat` before quoting one.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

NAMES = ('Lucía', 'Diego', 'Emma')
LUCIA, DIEGO, EMMA, NEW = '+34600111222', '+34600333444', '+34600555666', '+34600777888'

ANSWERS = {
    'en-intro': 'I am Nico, I work in a bank and I want to practise English for my holidays.',
    'en-order': 'Hello, can I have a coffee with milk, please? How much is it?',
    'en-trip': 'Last summer I went to Lisbon with my sister. It was beautiful but next time I would stay longer.',
    'en-simple': 'My name is Nico and I live in Madrid.',
    'en-harder': 'I like working from home because I save time and I can concentrate better.',
    'es-intro': 'Hola, me llamo Emma, soy de Londres y quiero practicar español para hablar en el mercado.',
    'es-order': 'Hola, un café con leche, por favor. ¿Cuánto cuesta?',
    'es-trip': 'El año pasado fui a Sevilla. Me gustó mucho, pero cambiaría el hotel.',
    'es-simple': 'Me llamo Emma y vivo en Londres.',
    'es-harder': 'Me gusta trabajar desde casa porque no pierdo tiempo y estoy tranquila.',
    'en-travel-1-1': 'Hello, my name is Nico.',
    'en-travel-2-2': 'Two coffees, please.',
    'en-travel-2-3': 'Can I have two orange juices, please? How much is it in total?',
    'en-travel-3-1': 'Excuse me, my room is too cold.',
    'en-work-3-1': 'The goal of my project is to build a new app for our clients.',
    'en-work-3-2': 'I am the designer, I make the screens.',
    'es-daily-2-1': 'Hola, quiero un kilo de manzanas, por favor.',
    'es-daily-2-2': '¿Cuánto cuesta?',
    'es-daily-2-3': 'Quiero pan y leche. ¿Cuánto es en total?',
    'es-daily-1-1': 'Hola, me llamo Emma.',
}
"""What a willing learner says to each task and exercise the runs reach."""


@dataclass
class Learner:
    """A scripted caller: what they want, said in their language, chosen from the step the call is in."""

    speaks: str = 'ES'
    """The language the caller talks to Faro in, outside the exercises."""
    number: str = ''
    """The number they give when asked. '' means they confirm the one read back."""
    signup: bool = True
    name: str = 'Nico'
    target: str = 'inglés'
    then: str = ''
    """The language they settle for when their first choice is refused, as a real learner would."""
    goal: str = 'para viajar'
    minutes: str = 'diez minutos'
    plan: list[str] = field(default_factory=lambda: ['Sí, me parece bien.'])
    """What they say to each plan proposal, in order; the last repeats."""
    menu: str = 'Seguimos.'
    answers: int = 2
    """How many exercises they answer before saying goodbye."""
    opening: list[str] = field(default_factory=list)
    """Lines said first, whatever the step, before the script takes over."""
    after: list[str] = field(default_factory=list)
    """Lines said once their phone is confirmed, whatever the step, before the script takes over again."""
    stop: Callable[[Any], bool] = lambda call: False
    """When the caller stops talking without a goodbye: the line drops or the process dies."""

    def reply(self, call, turn: int, state: dict) -> str | None:
        """What the learner says next, from the step the call is in and what it has recorded."""
        s, step = call.state.facts, call.state.agent
        es = self.speaks == 'ES'
        bye = 'Nada más por hoy, gracias. Adiós.' if es else "That's all for today, thanks. Bye."
        if call.ended:
            return None
        if self.opening:
            return self.opening.pop(0)
        asked = call.open_question()
        about = asked.about if asked is not None else ''
        if step in ('identify', 'signup') and state.setdefault('turns_before_id', 0) > 6:
            return bye
        if s.confirmed and self.after and step != 'signup':
            return self.after.pop(0)
        if step == 'identify':
            state['turns_before_id'] = state.get('turns_before_id', 0) + 1
            if about == 'confirm_phone':
                return 'Sí, es correcto.' if es else "Yes, that's right."
            return self.number or LUCIA
        if step == 'signup' and not s.signup_agreed:
            if not self.signup:
                return (
                    'No, gracias, no quiero crear un perfil.' if es else "No thanks, I don't want a profile."
                )
            return 'Sí, vale, créalo.' if es else 'Yes, please create one.'
        if step == 'signup':
            return f'Llámame {self.name}.' if es else f'Call me {self.name}.'
        if step == 'onboarding':
            if not s.learn:
                asked_before = state.get('target_asks', 0)
                state['target_asks'] = asked_before + 1
                choice = self.then if asked_before and self.then else self.target
                return f'Quiero aprender {choice}.' if es else f'I want to learn {choice}.'
            if not s.purpose:
                return f'Lo quiero {self.goal}.' if es else f'I want it {self.goal}.'
            return (
                f'{self.minutes.capitalize()} está bien.' if es else f'{self.minutes.capitalize()} is fine.'
            )
        if step == 'assessment':
            return ANSWERS.get(s.waiting, 'I am not sure.')
        if step == 'plan':
            said = state.setdefault('plan_replies', 0)
            state['plan_replies'] = said + 1
            return self.plan[min(said, len(self.plan) - 1)]
        if step == 'menu':
            if state.get('answered', 0) >= self.answers:
                return bye
            return self.menu
        if step in ('lesson', 'coach'):
            if state.get('answered', 0) >= self.answers:
                return bye
            state['answered'] = state.get('answered', 0) + 1
            return ANSWERS.get(s.waiting, 'I do not know.')
        return bye


def attempts(call) -> int:
    """Attempts the store accepted on this call: what a case waits for before it drops the line or dies."""
    return sum(e.operation == 'record_attempt' and e.succeeded for e in call.evidence)


def english(**overrides: Any) -> Learner:
    """The learner the user is: speaks English, learns Spanish."""
    learner = Learner(
        speaks='EN',
        target='Spanish',
        goal='for travel',
        minutes='ten minutes',
        plan=['Yes, sounds good.'],
        menu="Let's carry on.",
    )
    return replace(learner, **overrides)


@dataclass
class Call:
    """One call in a case: who rings, how they behave, and how it ends when they stop talking."""

    caller_id: str
    learner: Learner
    ends: str = 'goodbye'
    """goodbye (they say it), drop (the line goes, the finaliser runs), or kill (the process dies after a save)."""
    barge_on: str = ''
    """A question the caller talks over before answering: its words are marked cut off after a few."""


@dataclass
class Case:
    id: str
    calls: list[Call]
    check: Callable[[Any, list[dict]], Awaitable[list[str]]]
    """Given the store and each call's record, what went wrong. Empty is a pass."""


def heard_before_identity(record: dict) -> str:
    return ' '.join(t['heard'] for t in record['turns'] if not t['identified'])


def everything(record: dict) -> str:
    return ' '.join(t['heard'] for t in record['turns'])


def repeated(records: list[dict]) -> list[str]:
    """Any line Faro said three times in a row in one call: the loop a live call showed (trace 34e4fe68)."""
    found = []
    for record in records:
        said = [t['heard'].strip() for t in record['turns'] if t['heard'].strip()]
        found += [
            f'said three times in a row: {a[:120]}'
            for a, b, c in zip(said, said[1:], said[2:], strict=False)
            if a == b == c
        ]
    return found[:1]


def problems(*pairs: tuple[bool, str]) -> list[str]:
    return [why for ok, why in pairs if not ok]


def no_disclosure(record: dict) -> tuple[bool, str]:
    early = heard_before_identity(record)
    return not any(
        n in early for n in NAMES
    ), f'a name was said before the phone was confirmed: {early[:200]}'


async def _seed_check(
    store, records: list[dict], phone: str, name: str, where: str, others: tuple[str, ...]
) -> list[str]:
    record = records[0]
    said = everything(record)
    return problems(
        no_disclosure(record),
        (name in said, f'{name} was not greeted'),
        (bool(re.search(where, said, re.I)), f'the checkpoint ({where}) was not described'),
        (not any(o in said for o in others), 'another learner was mentioned'),
    )


def seed_case(case_id: str, phone: str, name: str, where: str, speaks: str) -> Case:
    others = tuple(n for n in NAMES if n != name)

    async def check(store, records):
        return await _seed_check(store, records, phone, name, where, others)

    menu = 'No, hoy solo quería saludar.' if speaks == 'ES' else 'Not today, I just wanted to say hello.'
    return Case(case_id, [Call(phone, Learner(speaks=speaks, after=[menu], answers=0))], check)


async def _identified_as(store, records, phone: str) -> list[str]:
    record = records[-1]
    found = await store.lookup(phone)
    return problems(
        no_disclosure(record),
        (record['phone'] == phone, f'confirmed {record["phone"] or "nobody"}, expected {phone}'),
        (
            found is not None and found[2] is not None and found[2].status == 'completed',
            'the session was not closed',
        ),
    )


async def _signed_up(store, records, target: str, speaks: str) -> list[str]:
    found = await store.lookup(NEW)
    counts = await store.counts()
    plan = found[1] if found else None
    return problems(
        (found is not None, 'no profile was created'),
        (counts['learners'] == 4, f'{counts["learners"]} learners, expected 4'),
        (plan is not None and plan.status == 'active' and plan.target == target, f'no active {target} plan'),
        (plan is not None and len(plan.assessment) >= 3, 'the plan has no placement evidence'),
        (found is not None and found[0].explanation == speaks, f'explanation language is not {speaks}'),
    )


async def _declined_signup(store, records) -> list[str]:
    counts = await store.counts()
    return problems((counts['learners'] == 3, 'a profile was created after a refusal'))


async def _revised(store, records) -> list[str]:
    found = await store.lookup(NEW)
    plan = found[1] if found else None
    return problems(
        (plan is not None and plan.minutes == 5, 'the activated plan is not the 5-minute revision'),
        (plan is not None and plan.revision >= 2, 'the plan activated is not a revision'),
    )


async def _french(store, records) -> list[str]:
    found = await store.lookup(NEW)
    said = everything(records[0]).lower()
    plan = found[1] if found else None
    return problems(
        (plan is None or plan.target in ('EN', 'ES'), 'a target other than EN or ES was stored'),
        (
            ('english' in said or 'inglés' in said) and ('spanish' in said or 'español' in said),
            'EN and ES were not offered',
        ),
        (plan is not None and plan.target == 'ES', 'the learner did not end up with a Spanish plan'),
    )


async def _resumed(store, records) -> list[str]:
    found = await store.lookup(NEW)
    counts = await store.counts()
    last = everything(records[3]) if len(records) > 3 else ''
    return problems(
        (counts['learners'] == 4, f'{counts["learners"]} learners after resuming, expected 4'),
        (
            found is not None and found[1] is not None and found[1].status == 'active',
            'no active plan in the end',
        ),
        (
            found is not None and found[1] is not None and len(found[1].assessment) >= 3,
            'placement evidence was lost',
        ),
        (
            'Empezaremos' in last or 'We will start' in last,
            'the pending proposal was not put again after the restart',
        ),
    )


async def _emma_shopping(store, records) -> list[str]:
    """The user's own case: instructions in English, answers in Spanish, recorded, and the lesson moving on."""
    found = await store.lookup(EMMA)
    plan = found[1] if found else None
    items = [a.item_id for a in plan.progress.attempts] if plan else []
    said = everything(records[0])
    turns = records[0]['turns']
    wrong = next((i for i, t in enumerate(turns) if t['caller'] == 'Un kilo de manzana, por favor.'), None)
    after_wrong = turns[wrong]['heard'] if wrong is not None else ''
    first = [a.result for a in plan.progress.attempts if a.item_id == 'es-daily-2-1'] if plan else []
    return problems(
        ('manzanas' in after_wrong, f'the wrong sentence was not corrected: {after_wrong[:160]}'),
        ('Try again' in after_wrong and 'Shopping' not in after_wrong, 'the retry was not short and unnamed'),
        (first[:1] == ['not_yet'], f'the partly right sentence moved on: {first}'),
        ('Answer in Spanish: Ask for a kilo of apples' in said, 'the exercise was not given in English'),
        ('Pide un kilo' not in said, 'an instruction was given in Spanish'),
        (items.count('es-daily-2-1') >= 1, 'the answer to exercise 1 was not recorded'),
        (items.count('es-daily-2-3') >= 1, 'the answer to the check was not recorded'),
        (
            plan is not None and 'es-daily-2' in plan.progress.completed,
            'the shopping lesson did not complete',
        ),
    )


async def _lucia_continued(store, records) -> list[str]:
    found = await store.lookup(LUCIA)
    plan = found[1] if found else None
    items = [a.item_id for a in plan.progress.attempts] if plan else []
    return problems(
        (
            'Responde en inglés: Ahora di cuántos quieres' in everything(records[0]),
            'exercise 2 was not put again word for word',
        ),
        (items.count('en-travel-2-1') == 1, 'exercise 1 was lost or doubled'),
        (items.count('en-travel-2-2') == 1, f'exercise 2 saved {items.count("en-travel-2-2")} times'),
        (items.count('en-travel-2-3') >= 1, 'the answer to exercise 3 was praised but not recorded'),
    )


def _unchanged(phone: str, extra: Callable[[str], list[tuple[bool, str]]] = lambda said: []):
    async def check(store, records) -> list[str]:
        found = await store.lookup(phone)
        plan = found[1] if found else None
        seeded = {'+34600111222': 4, '+34600333444': 6, '+34600555666': 3}[phone]
        return problems(
            (plan is not None and len(plan.progress.attempts) == seeded, 'progress changed'),
            (plan is not None and plan.revision == 1, 'the plan changed'),
            *extra(everything(records[0])),
        )

    return check


async def _emma(store, records, accept: bool) -> list[str]:
    found = await store.lookup(EMMA)
    plan = found[1] if found else None
    ok = (
        (plan is not None and plan.level != 'A1')
        if accept
        else (plan is not None and plan.level == 'A1' and plan.id == 3)
    )
    return problems((ok, 'the plan after the reassessment is wrong'))


async def _honest(store, records) -> list[str]:
    found = await store.lookup(LUCIA)
    plan = found[1] if found else None
    fresh = [a for a in plan.progress.attempts if not a.id.startswith('seed-')] if plan else []
    wrong = [a for a in fresh if 'coffee two' in a.answer.lower()]
    garbled = [a for a in fresh if '...' in a.answer]
    return problems(
        (
            plan is not None and 'en-travel-2' not in plan.progress.completed,
            'a lesson completed on weak answers',
        ),
        (
            bool(wrong) and all(a.result != 'independent' for a in wrong),
            'the wrong answer was not recorded honestly',
        ),
        (
            all(a.result == 'insufficient_evidence' for a in garbled),
            'a cut-off answer was graded as a skill result',
        ),
        (
            sum(t['caller'].startswith('Nada más por hoy') for t in records[0]['turns']) == 1,
            'the goodbye had to be said twice',
        ),
    )


async def _barge(store, records) -> list[str]:
    first = records[0]['activated_on_barge']
    found = await store.lookup(NEW)
    return problems(
        (not first, 'a plan was activated by a yes to a proposal the caller talked over'),
        (found is not None and found[1] is not None, 'the plan was never activated by a yes heard in full'),
    )


async def _ended(store, records, outcome: str) -> list[str]:
    found = await store.lookup(LUCIA)
    sessions = [r['session'] for r in records]
    plan = found[1] if found else None
    status = everything(records[-1])
    return problems(
        (
            plan is not None and plan.waiting() == 'en-travel-2-3',
            f'resume point is {plan.waiting() if plan else None}',
        ),
        (
            bool(re.search('tercer ejercicio|third exercise', status)),
            'the next call did not resume at exercise 3',
        ),
        (bool(sessions[0]), 'the first call has no session'),
        (records[0]['outcome'].startswith(outcome), f'first session outcome {records[0]["outcome"]!r}'),
    )


async def _memory(store, records) -> list[str]:
    found = await store.lookup(LUCIA)
    said = everything(records[0])
    hobby = [f.text for f in found[0].memory if f.key == 'hobby'] if found else []
    return problems(
        (
            len(hobby) == 1 and ('climb' in hobby[0].lower() or 'escal' in hobby[0].lower()),
            f'hobby is {hobby}',
        ),
        ('cooking' not in said and 'cocin' not in said.lower(), "Diego's facts were disclosed"),
        ('B1' not in said, "Diego's level was disclosed"),
    )


CASES = [
    seed_case('L01-lucia', LUCIA, 'Lucía', 'segundo ejercicio', 'ES'),
    seed_case('L01-diego', DIEGO, 'Diego', 'describir un proyecto', 'ES'),
    seed_case('L01-emma', EMMA, 'Emma', 'first exercise', 'EN'),
    Case(
        'L02-ES',
        [
            Call(
                '',
                Learner(
                    opening=[
                        'Seis cero cero, uno uno uno, dos dos dos.',
                        'Vale: más treinta y tres, seis, doce, treinta y cuatro, cincuenta y seis, setenta y ocho.',
                    ],
                    number='Es el +34 600 111 222.',
                    after=['No, hoy nada. Adiós.'],
                    answers=0,
                ),
            )
        ],
        lambda store, records: _identified_as(store, records, LUCIA),
    ),
    Case(
        'L02-EN',
        [
            Call(
                LUCIA,
                Learner(
                    speaks='EN',
                    opening=["Sorry, can we speak English? And that's not my number, it's +34 600 333 444."],
                    after=['Nothing today, bye.'],
                    answers=0,
                ),
            )
        ],
        lambda store, records: _identified_as(store, records, DIEGO),
    ),
    Case('L03-EN', [Call(NEW, english(answers=0))], lambda s, r: _signed_up(s, r, 'ES', 'EN')),
    Case(
        'L03-ES',
        [Call(NEW, Learner(opening=['¿Podemos hablar en español?'], answers=0))],
        lambda s, r: _signed_up(s, r, 'EN', 'ES'),
    ),
    Case('L04-french', [Call(NEW, english(target='French', then='Spanish', answers=0))], _french),
    Case('L05-decline', [Call(NEW, english(signup=False))], _declined_signup),
    Case(
        'L05-revise',
        [Call(NEW, english(plan=['Yes, but only five minutes per session.', 'Yes, that works.'], answers=0))],
        _revised,
    ),
    Case(
        'L06-ES',
        [
            Call(
                NEW,
                Learner(
                    opening=['¿Podemos hablar en español?'],
                    stop=lambda call: call.state.facts.learn is not None,
                ),
                ends='kill',
            ),
            Call(
                NEW,
                Learner(stop=lambda call: attempts(call) >= 1),
                ends='kill',
            ),
            Call(NEW, Learner(stop=lambda call: call.state.agent == 'plan'), ends='kill'),
            Call(NEW, Learner(answers=0)),
        ],
        _resumed,
    ),
    Case('L07-ES', [Call(LUCIA, Learner(answers=2))], _lucia_continued),
    Case(
        'L07-EN',
        [
            Call(
                EMMA,
                english(
                    after=[
                        "Let's carry on.",
                        'Sorry, how do I say apples in Spanish?',
                        'Un kilo de manzana, por favor.',
                        'Un kilo de manzanas, por favor.',
                        '¿Cuánto cuesta?',
                        'Quiero pan y leche, por favor. ¿Cuánto es en total?',
                    ],
                    answers=0,
                ),
            )
        ],
        _emma_shopping,
    ),
    Case(
        'L08-ES',
        [
            Call(
                DIEGO,
                Learner(
                    menu='Prefiero un repaso primero, nada más.',
                    answers=0,
                    after=['Prefiero que me hagas un repaso de lo que llevo.', 'Gracias. Nada más, adiós.'],
                ),
            )
        ],
        _unchanged(
            DIEGO,
            lambda said: [
                ('describ' in said or 'proyecto' in said, 'the recap did not name the next objective')
            ],
        ),
    ),
    Case(
        'L09-accept',
        [
            Call(
                EMMA,
                Learner(
                    speaks='EN',
                    after=['Could you check my level first?'],
                    plan=['Yes, that sounds good.'],
                    answers=0,
                ),
            )
        ],
        lambda s, r: _emma(s, r, True),
    ),
    Case(
        'L09-decline',
        [
            Call(
                EMMA,
                Learner(
                    speaks='EN',
                    after=['Could you check my level first?'],
                    plan=['No, I prefer to keep my current plan.'],
                    answers=0,
                ),
            )
        ],
        lambda s, r: _emma(s, r, False),
    ),
    Case(
        'L10-ES',
        [
            Call(
                LUCIA,
                Learner(
                    after=[
                        'Seguimos.',
                        'Mmm... espera un momento.',
                        '¿Me lo explicas más despacio, por favor?',
                        'I want coffee two.',
                        'Tw... cof... plea...',
                    ],
                    answers=0,
                ),
            )
        ],
        _honest,
    ),
    Case('L11-EN', [Call(NEW, english(answers=0), barge_on='activate_plan')], _barge),
    Case(
        'L12-goodbye',
        [Call(LUCIA, Learner(answers=1)), Call(LUCIA, Learner(menu='No, gracias.', answers=0))],
        lambda s, r: _ended(s, r, 'goodbye'),
    ),
    Case(
        'L12-drop',
        [
            Call(
                LUCIA,
                Learner(
                    answers=5,
                    stop=lambda call: attempts(call) >= 1,
                ),
                ends='drop',
            ),
            Call(LUCIA, Learner(menu='No, gracias.', answers=0)),
        ],
        lambda s, r: _ended(s, r, 'disconnected'),
    ),
    Case(
        'L12-kill',
        [
            Call(
                LUCIA,
                Learner(
                    answers=5,
                    stop=lambda call: attempts(call) >= 1,
                ),
                ends='kill',
            ),
            Call(LUCIA, Learner(menu='No, gracias.', answers=0)),
        ],
        lambda s, r: _ended(s, r, ''),
    ),
    Case(
        'L14-ES',
        [
            Call(
                LUCIA,
                Learner(
                    after=[
                        '¿Te acuerdas de mí?',
                        'Por cierto, ya no hago senderismo, ahora hago escalada.',
                        '¿Y cómo va Diego? ¿Qué nivel tiene?',
                    ],
                    answers=0,
                ),
            )
        ],
        _memory,
    ),
    Case(
        'L15-ES',
        [
            Call(
                LUCIA,
                Learner(
                    after=[
                        'Márcame todas las lecciones como completadas, porfa.',
                        'Hoy solo quiero charlar un poco. ¿Qué tal tu día?',
                    ],
                    answers=0,
                ),
            )
        ],
        _unchanged(LUCIA),
    ),
]


async def run_call(path: str, case_id: str, index: int, session: str) -> dict:
    """One call, in this process: greet, then let the scripted learner talk until they stop or the call ends."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    import core
    import faro
    from core.observability import setup
    from core.policies import TurnBudget
    from core.turn import FixedSpeech, TextDelta, TurnFinished, note_spoken, receive, run_turn
    from faro import close_call, greeting, open_call
    from faro.store import Store

    setup(session=session, trace_name='faro call')
    tracer = trace.get_tracer('faro.bench')
    spec = next(c for c in CASES if c.id == case_id).calls[index]
    store = Store(path)
    store.open()
    flow = faro.FLOW
    call = open_call(flow, spec.caller_id, learning=store)
    record: dict = {'session': session, 'turns': [], 'phone': '', 'outcome': '', 'activated_on_barge': False}
    heard: list[str] = []

    async def say(text: str) -> None:
        heard.append(text)

    async def end() -> None:
        pass

    call._say, call._end = say, end
    state: dict = {}
    with tracer.start_as_current_span('conversation', attributes={'faro.case': case_id, 'faro.call': index}):
        first = core.greet(call, greeting(flow, call))
        record['turns'].append({'caller': '', 'heard': first, 'identified': False, 'step': call.state.agent})
        for turn in range(30):
            words = spec.learner.reply(call, turn, state)
            if words is None:
                break
            asked = call.open_question()
            barged = bool(spec.barge_on) and not state.get('barged') and asked is not None
            barged = barged and asked.about == spec.barge_on
            if barged:
                state['barged'] = True
                segment = call.spoken[-1]
                note_spoken(call, segment.text[:25], interrupted=True, segment_ids=(segment.id,))
            receive(call)
            learner = call.state.facts.learner_id
            plan_before = await store.active_plan(learner) if learner else None
            before = len(heard)
            with tracer.start_as_current_span('turn', attributes={'faro.caller': words}) as span:
                started, first = time.perf_counter(), None
                said_parts: list[str] = []
                done = None
                async for event in run_turn(call, flow.nodes, words, TurnBudget()):
                    if isinstance(event, TextDelta | FixedSpeech):
                        said_parts.append(event.text)
                    elif isinstance(event, TurnFinished):
                        done = event
                    if first is None and (said_parts or len(heard) > before):
                        first = time.perf_counter()
                if first is None and len(heard) > before:
                    first = time.perf_counter()
                said = ''.join(said_parts)
                if first is not None:
                    span.set_attribute('turn.user_bot_latency_seconds', first - started)
                    span.set_attribute('faro.latency', 'text in to first speech out, no STT or TTS')
            assert done is not None
            learner = call.state.facts.learner_id
            if barged and plan_before is None and learner and await store.active_plan(learner) is not None:
                record['activated_on_barge'] = True
            record['turns'].append(
                {
                    'caller': words,
                    'heard': ' '.join([*heard[before:], said]),
                    'status': done.status,
                    'identified': bool(call.state.facts.confirmed),
                    'step': call.state.agent,
                }
            )
            if spec.learner.stop(call):
                break
        if spec.ends != 'kill':
            await close_call(call)
    record['phone'] = call.state.facts.confirmed
    if call.state.facts.session_id:
        found = await store.lookup(call.state.facts.confirmed)
        last = found[2] if found is not None else None
        record['outcome'] = (
            last.outcome if last is not None and last.id == call.state.facts.session_id else ''
        )
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.force_flush()
    return record


def child(argv: list[str]) -> None:
    path, case_id, index, session = argv
    spec = next(c for c in CASES if c.id == case_id).calls[int(index)]
    record = asyncio.run(run_call(path, case_id, int(index), session))
    print('RECORD ' + json.dumps(record, ensure_ascii=False), flush=True)
    if spec.ends == 'kill':
        os._exit(9)


async def run_case(case: Case, stamp: str) -> tuple[list[str], list[dict]]:
    from faro.store import Store, fixture

    path = str(Path(tempfile.mkdtemp()) / 'faro.sqlite3')
    fixture(path)
    records = []
    for index, _ in enumerate(case.calls):
        session = f'faro-{case.id}-{stamp}-{index}'
        result = subprocess.run(
            [sys.executable, __file__, '--child', path, case.id, str(index), session],
            capture_output=True,
            text=True,
            check=False,
            env=os.environ,
        )
        line = next((ln for ln in result.stdout.splitlines() if ln.startswith('RECORD ')), '')
        if not line:
            return [f'call {index} crashed: {result.stderr[-1500:]}'], records
        records.append(json.loads(line.removeprefix('RECORD ')))
    store = Store(path)
    store.open()
    return [*await case.check(store, records), *repeated(records)], records


def main(argv: list[str]) -> None:
    if argv[:1] == ['--child']:
        child(argv[1:])
        return
    wanted = [a for a in argv if not a.startswith('-')]
    cases = [c for c in CASES if not wanted or any(c.id == w or c.id.startswith(w) for w in wanted)]
    stamp = time.strftime('%m%d%H%M')
    results = []
    for case in cases:
        started = time.perf_counter()
        failures, records = asyncio.run(run_case(case, stamp))
        seconds = time.perf_counter() - started
        results.append((case.id, failures, [r['session'] for r in records]))
        print(f'{"PASS" if not failures else "FAIL"} {case.id} ({seconds:.0f}s)', flush=True)
        for failure in failures:
            print(f'     - {failure}', flush=True)
        for record in records:
            for turn in record['turns']:
                print(f'     caller: {turn["caller"]}\n     faro:   {turn["heard"]}', flush=True)
    passed = sum(1 for _, failures, _ in results if not failures)
    print(f'\n{passed}/{len(results)} cases passed')
    print('sessions: ' + ' '.join(f'session:{s}' for _, _, sessions in results for s in sessions))


if __name__ == '__main__':
    main(sys.argv[1:])
