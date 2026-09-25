"""Faro's authored material: the placement bank, the lessons, the level rule and the lines code says itself.

Everything a benchmark run has to repeat is here, in code, and versioned:
which placement task comes next, which items a lesson has and in what order,
which item is the independent check, and the words of every question the
application puts to the learner. A grading reading judges answers; no model
chooses an item id or the order.

Every instruction is written twice, in English and in Spanish. The learner
hears it in their explanation language (their mother tongue) and answers in
the language they are learning. The lessons are the same for both target
languages, so each is written once per goal and given an id per target:
`en-travel-2` is the café lesson for someone learning English, `es-travel-2`
the same lesson for someone learning Spanish.

ponytail: one item set per lesson for every level. The grading reading and
the companion adapt to the learner's level. Author per-level variants when a
live review shows the gap matters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type Lang = Literal['EN', 'ES']
type Goal = Literal['travel', 'work', 'daily']
type Result = Literal['independent', 'assisted', 'not_yet', 'insufficient_evidence']
type Level = Literal['A1', 'A2', 'B1', 'B2']

BANK_VERSION = 'faro-bank-2026-09-25'
"""Recorded with every piece of evidence, so a result can be traced to the exact prompts it answered."""

LANGUAGE_NAMES: dict[Lang, dict[Lang, str]] = {
    'ES': {'EN': 'inglés', 'ES': 'español'},
    'EN': {'EN': 'English', 'ES': 'Spanish'},
}
"""A target language's name, said in the explanation language."""


@dataclass(frozen=True)
class Task:
    """One placement task: what the learner is asked to do, in each explanation language, and what it shows.

    `{language}` in an instruction is the target language's name, said in the
    explanation language.
    """

    id: str
    instruction: dict[Lang, str]
    evidence: str

    def said(self, explanation: Lang, target: Lang) -> str:
        return self.instruction[explanation].format(language=LANGUAGE_NAMES[explanation][target])


def _tasks(target: Lang, *specs: tuple[str, str, str, str]) -> tuple[Task, ...]:
    code = target.lower()
    return tuple(Task(f'{code}-{name}', {'EN': en, 'ES': es}, evidence) for name, en, es, evidence in specs)


_CORE = (
    (
        'intro',
        'Tell me about yourself and why you want to learn {language}.',
        'Háblame de ti y de por qué quieres aprender {language}.',
        'communicating familiar information',
    ),
    (
        'order',
        'Order a drink and ask how much it costs.',
        'Pide una bebida y pregunta cuánto cuesta.',
        'task completion and response to a follow-up',
    ),
    (
        'trip',
        'Tell me about a recent trip, and what you would change.',
        'Cuéntame un viaje reciente y qué cambiarías.',
        'connected speech, time reference and explanation',
    ),
)
_EXTRA = (
    (
        'simple',
        'Say your name and where you live.',
        'Di cómo te llamas y dónde vives.',
        'supported A1 information',
    ),
    (
        'harder',
        'Give your opinion on working from home, with two reasons.',
        'Da tu opinión sobre trabajar desde casa, con dos razones.',
        'opinion with reasons',
    ),
)

PLACEMENT: dict[Lang, tuple[Task, ...]] = {'EN': _tasks('EN', *_CORE), 'ES': _tasks('ES', *_CORE)}
"""Three core tasks per target language, always in this order."""

FOLLOW_UPS: dict[Lang, tuple[Task, ...]] = {'EN': _tasks('EN', *_EXTRA), 'ES': _tasks('ES', *_EXTRA)}
"""At most two, asked only where the core three leave the evidence unclear (`next_task`)."""

SCORE: dict[Result, int] = {'independent': 2, 'assisted': 1, 'not_yet': 0}
"""What each result counts towards a level. `insufficient_evidence` counts for nothing either way."""


def task(task_id: str) -> Task:
    """The task with this id, from either language's bank."""
    for bank in (*PLACEMENT.values(), *FOLLOW_UPS.values()):
        for found in bank:
            if found.id == task_id:
                return found
    raise KeyError(task_id)


def target_of(record_id: str) -> Lang:
    """The target language a task, lesson or item id belongs to: its prefix."""
    return 'EN' if record_id.startswith('en-') else 'ES'


def next_task(target: Lang, results: list[tuple[str, Result]]) -> Task | None:
    """The next placement task, given the results so far, or None when the placement is done.

    The three core tasks in order. Then one follow-up where the evidence is
    unclear: the simpler one if nothing was done independently, the harder
    one if everything was, and a repeat chance when a core answer gave no
    evidence at all. Never more than two follow-ups.
    """
    done = [task_id for task_id, _ in results]
    for core in PLACEMENT[target]:
        if core.id not in done:
            return core
    simple, harder = FOLLOW_UPS[target]
    asked = [t for t in (simple, harder) if t.id in done]
    if len(asked) >= 2:
        return None
    scored = [r for _, r in results if r != 'insufficient_evidence']
    if simple.id not in done and (not scored or all(r == 'not_yet' for r in scored)):
        return simple
    if harder.id not in done and scored and all(r == 'independent' for r in scored):
        return harder
    if len(scored) < 3 and not asked:
        return simple if simple.id not in done else harder
    return None


def level(results: list[tuple[str, Result]]) -> tuple[Level, str]:
    """A provisional starting difficulty from placement results, and how sure it is.

    The mean score of the answers that gave evidence. Fewer than three such
    answers is marked uncertain. A learner who did everything independently,
    the harder follow-up included, is recorded as beyond the calibrated bank,
    never as C1 or C2.
    """
    scores = [SCORE[r] for _, r in results if r != 'insufficient_evidence']
    if not scores:
        return 'A1', 'high: no answer gave evidence, starting with supported A1 material'
    mean = sum(scores) / len(scores)
    found: Level = 'A1' if mean < 0.75 else 'A2' if mean < 1.25 else 'B1' if mean < 1.75 else 'B2'
    if len(scores) < 3:
        return found, f'high: only {len(scores)} answers gave evidence'
    if found == 'B2' and any(t.endswith('-harder') and r == 'independent' for t, r in results):
        return found, 'evidence beyond B2, outside the calibrated bank; starting at B2'
    return found, 'low'


@dataclass(frozen=True)
class Item:
    """One practice item: what to do, in each explanation language. `check` marks the lesson's independent check."""

    id: str
    instruction: dict[Lang, str]
    check: bool = False


@dataclass(frozen=True)
class Lesson:
    """One lesson: its topic said in each explanation language, its can-do objective, and its items."""

    id: str
    topic: dict[Lang, str]
    objective: str
    items: tuple[Item, ...]


@dataclass(frozen=True)
class _Plan:
    """One lesson as written: topic in each language, objective, and (English, Spanish) instructions."""

    es: str
    en: str
    objective: str
    items: tuple[tuple[str, str], ...]


_GOALS: dict[Goal, tuple[_Plan, ...]] = {
    'travel': (
        _Plan(
            'presentarte',
            'introducing yourself',
            'I can introduce myself and say where I am from.',
            (
                ('Say hello and tell me your name.', 'Saluda y dime cómo te llamas.'),
                (
                    'Tell me where you are from and where you live now.',
                    'Dime de dónde eres y dónde vives ahora.',
                ),
                (
                    'Introduce yourself to a new friend at a hostel: your name, your city and one hobby.',
                    'Preséntate a alguien en un albergue: tu nombre, tu ciudad y una afición.',
                ),
            ),
        ),
        _Plan(
            'pedir en una cafetería',
            'ordering at a café',
            'I can order food and drink politely and ask for the total.',
            (
                (
                    'You are at a café. Ask politely for a drink.',
                    'Estás en una cafetería. Pide una bebida con educación.',
                ),
                (
                    'Now say how many you want: ask for two of something.',
                    'Ahora di cuántos quieres: pide dos de algo.',
                ),
                (
                    'Order two drinks and ask how much it is in total.',
                    'Pide dos bebidas y pregunta cuánto es en total.',
                ),
            ),
        ),
        _Plan(
            'resolver un problema en el hotel',
            'solving a problem at the hotel',
            'I can explain a simple problem and ask for help.',
            (
                (
                    'Tell the receptionist that your room is too cold.',
                    'Dile al recepcionista que tu habitación está muy fría.',
                ),
                ('Ask if you can change to another room.', 'Pregunta si puedes cambiar de habitación.'),
                (
                    'Your shower does not work. Explain the problem and ask when it will be fixed.',
                    'La ducha no funciona. Explica el problema y pregunta cuándo la arreglan.',
                ),
            ),
        ),
        _Plan(
            'practicar una conversación completa',
            'practising a full conversation',
            'I can handle a short travel conversation from start to finish.',
            (
                (
                    'Ask a stranger for directions to the train station.',
                    'Pregunta a alguien cómo llegar a la estación de tren.',
                ),
                ('Buy a return ticket to the airport.', 'Compra un billete de ida y vuelta al aeropuerto.'),
                (
                    'Check in at a hotel: give your name, ask about breakfast and say thank you.',
                    'Haz el registro en un hotel: di tu nombre, pregunta por el desayuno y da las gracias.',
                ),
            ),
        ),
    ),
    'work': (
        _Plan(
            'presentarte en el trabajo',
            'introducing yourself at work',
            'I can introduce myself and my role at work.',
            (
                ('Tell me your name and your job.', 'Dime tu nombre y tu trabajo.'),
                (
                    'Explain what you do on a normal work day.',
                    'Explica qué haces en un día normal de trabajo.',
                ),
                (
                    'Introduce yourself to a new colleague: your name, your role and your team.',
                    'Preséntate a un compañero nuevo: tu nombre, tu puesto y tu equipo.',
                ),
            ),
        ),
        _Plan(
            'hablar de lo que hiciste',
            'talking about what you did',
            'I can talk about completed work in the past tense.',
            (
                (
                    'Tell me three things you did yesterday at work.',
                    'Dime tres cosas que hiciste ayer en el trabajo.',
                ),
                (
                    'Tell me about a problem you solved last week.',
                    'Cuéntame un problema que resolviste la semana pasada.',
                ),
                (
                    'Give a short update: what you finished this week and what went wrong.',
                    'Da una actualización corta: qué terminaste esta semana y qué salió mal.',
                ),
            ),
        ),
        _Plan(
            'describir un proyecto',
            'describing a project',
            'I can describe a project: its goal, my part and its status.',
            (
                (
                    'Say what the goal of a project you work on is.',
                    'Di cuál es el objetivo de un proyecto en el que trabajas.',
                ),
                ('Say what your part in that project is.', 'Di cuál es tu parte en ese proyecto.'),
                (
                    'Describe the project to a new manager: its goal, your role and the next step.',
                    'Describe el proyecto a una jefa nueva: su objetivo, tu papel y el siguiente paso.',
                ),
            ),
        ),
        _Plan(
            'participar en una reunión',
            'taking part in a meeting',
            'I can give an opinion and agree or disagree politely in a meeting.',
            (
                (
                    'Say that you agree with an idea, and why.',
                    'Di que estás de acuerdo con una idea y por qué.',
                ),
                (
                    'Disagree politely with a suggestion.',
                    'Muestra tu desacuerdo con una propuesta, con educación.',
                ),
                (
                    'In a meeting, suggest a new deadline and give one reason.',
                    'En una reunión, propón una nueva fecha de entrega y da una razón.',
                ),
            ),
        ),
    ),
    'daily': (
        _Plan(
            'presentarte',
            'introducing yourself',
            'I can introduce myself and ask simple questions.',
            (
                ('Say hello and tell me your name.', 'Saluda y dime cómo te llamas.'),
                ('Ask me my name and where I live.', 'Pregúntame cómo me llamo y dónde vivo.'),
                (
                    'Introduce yourself to a neighbour and ask one question.',
                    'Preséntate a un vecino y hazle una pregunta.',
                ),
            ),
        ),
        _Plan(
            'ir de compras',
            'shopping',
            'I can ask for things in a shop and ask the price.',
            (
                ('Ask for a kilo of apples at the market.', 'Pide un kilo de manzanas en el mercado.'),
                ('Ask how much it costs.', 'Pregunta cuánto cuesta.'),
                (
                    'Buy bread and milk and ask how much it is in total.',
                    'Compra pan y leche y pregunta cuánto es en total.',
                ),
            ),
        ),
        _Plan(
            'hablar de tu rutina',
            'talking about your routine',
            'I can describe my daily routine.',
            (
                ('Say what time you get up.', 'Di a qué hora te levantas.'),
                ('Say what you do in the evening.', 'Di qué haces por la tarde.'),
                (
                    'Describe a normal Saturday, from morning to night.',
                    'Describe un sábado normal, de la mañana a la noche.',
                ),
            ),
        ),
        _Plan(
            'hacer planes',
            'making plans',
            'I can make and accept plans with a friend.',
            (
                ('Invite a friend to the cinema.', 'Invita a un amigo al cine.'),
                ('Suggest a time and a place to meet.', 'Propón una hora y un lugar para quedar.'),
                (
                    'Make a plan for Sunday with a friend: what, when and where.',
                    'Haz un plan para el domingo con un amigo: qué, cuándo y dónde.',
                ),
            ),
        ),
    ),
}
"""Four lessons per goal, written once. The last item of each is the independent check."""


def _built(target: Lang, goal: Goal, number: int, plan: _Plan) -> Lesson:
    lesson_id = f'{target.lower()}-{goal}-{number}'
    items = tuple(
        Item(f'{lesson_id}-{n}', {'EN': en, 'ES': es}, check=n == len(plan.items))
        for n, (en, es) in enumerate(plan.items, start=1)
    )
    return Lesson(lesson_id, {'ES': plan.es, 'EN': plan.en}, plan.objective, items)


LESSONS: dict[tuple[Lang, Goal], tuple[Lesson, ...]] = {
    (target, goal): tuple(_built(target, goal, n, plan) for n, plan in enumerate(plans, start=1))
    for target in ('EN', 'ES')
    for goal, plans in _GOALS.items()
}
"""Every plan there is: four lessons per target language and goal, in this order."""


def lessons(target: Lang, goal: Goal) -> tuple[Lesson, ...]:
    return LESSONS[target, goal]


def lesson(lesson_id: str) -> Lesson:
    """The lesson with this id, from any plan."""
    for plan in LESSONS.values():
        for found in plan:
            if found.id == lesson_id:
                return found
    raise KeyError(lesson_id)


def item(item_id: str) -> tuple[Lesson, Item]:
    """The item with this id and the lesson it belongs to."""
    lesson_id, _, _ = item_id.rpartition('-')
    found = lesson(lesson_id)
    return found, next(i for i in found.items if i.id == item_id)


GOAL_NAMES: dict[Lang, dict[Goal, str]] = {
    'ES': {'travel': 'para viajar', 'work': 'para el trabajo', 'daily': 'para el día a día'},
    'EN': {'travel': 'for travel', 'work': 'for work', 'daily': 'for daily life'},
}

NUMBERS: dict[Lang, dict[int, str]] = {
    'ES': {1: 'primer', 2: 'segundo', 3: 'tercer'},
    'EN': {1: 'first', 2: 'second', 3: 'third'},
}


def spaced(phone: str) -> str:
    """A Spanish number the way it is read out: +34 600 111 222."""
    digits = phone.removeprefix('+34')
    return f'+34 {digits[:3]} {digits[3:6]} {digits[6:]}'


def greeting(caller_id: str) -> str:
    """The opening line, in English, offering Spanish. With a caller ID it reads the number back."""
    opener = "Hi, I'm Faro, your language companion. Si prefieres hablar en español, dímelo."
    if caller_id:
        return f'{opener} Can you confirm your number is {spaced(caller_id)}?'
    return f'{opener} What is your full phone number, starting with plus thirty-four?'


def ask_number(explanation: Lang) -> str:
    if explanation == 'EN':
        return 'What is your full phone number, starting with plus thirty-four?'
    return '¿Me dices tu número de teléfono completo, empezando por más treinta y cuatro?'


def read_back(phone: str, explanation: Lang) -> str:
    if explanation == 'EN':
        return f'Let me check: is your number {spaced(phone)}?'
    return f'A ver si lo tengo bien: ¿tu número es {spaced(phone)}?'


def signup_offer(explanation: Lang) -> str:
    if explanation == 'EN':
        return "I can't find a profile with that number. Would you like to create one and prepare a learning plan?"
    return 'No encuentro un perfil con ese número. ¿Quieres crear uno y preparar un plan de aprendizaje?'


def ask_name(explanation: Lang) -> str:
    if explanation == 'EN':
        return 'Great. What would you like me to call you?'
    return 'Genial. ¿Cómo quieres que te llame?'


ONBOARDING: dict[str, dict[Lang, str]] = {
    'target': {
        'EN': 'Which language would you like to learn: English or Spanish?',
        'ES': '¿Qué idioma quieres aprender: inglés o español?',
    },
    'goal': {
        'EN': 'What is it for: travel, work or daily life?',
        'ES': '¿Para qué lo quieres: para viajar, para el trabajo o para el día a día?',
    },
    'minutes': {
        'EN': 'How long should a session be? I suggest ten minutes.',
        'ES': '¿Cuánto debería durar cada sesión? Te sugiero diez minutos.',
    },
}
"""The onboarding questions, asked one at a time in this order."""


def only_two(explanation: Lang) -> str:
    """A language Faro does not teach was asked for: name the two it does."""
    if explanation == 'EN':
        return 'I can only teach English or Spanish. Which one would you like?'
    return 'Solo puedo enseñar inglés o español. ¿Cuál prefieres?'


def welcome(name: str, explanation: Lang) -> str:
    """A new profile's first line: its name, then the first onboarding question."""
    hello = f'Nice to meet you, {name}.' if explanation == 'EN' else f'Encantado, {name}.'
    return f'{hello} {ONBOARDING["target"][explanation]}'


def status(name: str, explanation: Lang, lesson_id: str, item_index: int, previous: str, asked: bool) -> str:
    """The returning learner's opening: where they stopped, and the two choices every return offers.

    `item_index` is the exercise waiting, and `asked` whether it was already
    put to them: either means the lesson is under way. `previous` is the
    lesson finished before this one, or ''.
    """
    topic = lesson(lesson_id).topic[explanation]
    ordinal = NUMBERS[explanation].get(item_index + 1, str(item_index + 1))
    before = lesson(previous).topic[explanation] if previous else ''
    if explanation == 'EN':
        if item_index or asked:
            where = f'You were practising {topic}. We stopped at the {ordinal} exercise.'
        elif before:
            where = f'You finished {before}. The next lesson, {topic}, is ready to start.'
        else:
            where = f'Your first lesson, {topic}, is ready to start.'
        return f'Hi, {name}. {where} Shall we carry on, or would you like a recap first?'
    if item_index or asked:
        where = f'Estabas practicando cómo {topic}. Nos quedamos en el {ordinal} ejercicio.'
    elif before:
        where = f'Terminaste la lección de {before}. La siguiente, {topic}, está lista.'
    else:
        where = f'Tu primera lección, {topic}, está lista.'
    return f'Hola, {name}. {where} ¿Seguimos o prefieres repasar primero?'


def resume_onboarding(name: str, explanation: Lang) -> str:
    """A learner who stopped before agreeing a plan. The step they stopped at is asked straight after."""
    if explanation == 'EN':
        return f'Hi, {name}. We had started preparing your plan; let us carry on.'
    return f'Hola, {name}. Habíamos empezado a preparar tu plan; sigamos.'


def plan_proposal(target: Lang, goal: Goal, level: Level, minutes: int, explanation: Lang) -> str:
    """The exact words of a plan put to the learner for agreement: goal, level, the four lessons, the length."""
    topics = [plan_lesson.topic[explanation] for plan_lesson in lessons(target, goal)]
    language = LANGUAGE_NAMES[explanation][target]
    purpose = GOAL_NAMES[explanation][goal]
    if explanation == 'EN':
        outline = f'{", ".join(topics[:-1])} and {topics[-1]}'
        return (
            f'We will start with {language} {level} {purpose}: {outline}. '
            f'Sessions will be about {minutes} minutes. Does that sound good?'
        )
    outline = f'{", ".join(topics[:-1])} y {topics[-1]}'
    return (
        f'Empezaremos con {language} {level} {purpose}: {outline}. '
        f'Serán sesiones de unos {minutes} minutos. ¿Te parece bien?'
    )


def plan_active(explanation: Lang) -> str:
    if explanation == 'EN':
        return 'Done: your plan is active. Shall we start the first lesson now?'
    return 'Hecho: tu plan ya está activo. ¿Empezamos ahora la primera lección?'


def exercise(explanation: Lang, item_id: str, *, again: bool = False) -> str:
    """An exercise as the learner hears it: in their own language, saying which language to answer in.

    The first time with where they are in the lesson; asked again, only the
    short retry, so a second try never repeats the whole introduction.
    """
    found, waiting = item(item_id)
    language = LANGUAGE_NAMES[explanation][target_of(item_id)]
    instruction = waiting.instruction[explanation]
    if again:
        return (
            f'Try again, in {language}: {instruction}'
            if explanation == 'EN'
            else f'Otra vez, en {language}: {instruction}'
        )
    ordinal = NUMBERS[explanation].get(found.items.index(waiting) + 1, '')
    topic = found.topic[explanation].capitalize()
    if explanation == 'EN':
        return f'{topic}, {ordinal} exercise. Answer in {language}: {instruction}'
    return f'{topic}, {ordinal} ejercicio. Responde en {language}: {instruction}'


def placement(explanation: Lang, task_id: str, *, first: bool = False, again: bool = False) -> str:
    """A placement task as the learner hears it: in their own language, answered in the one they learn."""
    target = target_of(task_id)
    language = LANGUAGE_NAMES[explanation][target]
    said = task(task_id).said(explanation, target)
    if again:
        return (
            f'Try again, in {language}: {said}' if explanation == 'EN' else f'Otra vez, en {language}: {said}'
        )
    if first:
        opener = (
            f'Now three short tasks, to find your level. Answer in {language}.'
            if explanation == 'EN'
            else f'Ahora tres tareas cortas, para ver tu nivel. Responde en {language}.'
        )
        return f'{opener} {said}'
    return f'{"In" if explanation == "EN" else "En"} {language}: {said}'


def lesson_done(explanation: Lang, finished: str, following: str) -> str:
    if explanation == 'EN':
        if not following:
            return f'Lesson complete: {finished}. That was the last lesson of your plan. Well done!'
        return f'Lesson complete: {finished}. Next is {following}. Shall we go on, or stop here for today?'
    if not following:
        return f'Lección completada: {finished}. Era la última lección de tu plan. ¡Muy bien!'
    return f'Lección completada: {finished}. La siguiente es {following}. ¿Seguimos o lo dejamos por hoy?'


def next_step(explanation: Lang, item_id: str) -> str:
    """Where the next session starts, named by lesson and exercise number rather than by the prompt itself."""
    found, waiting = item(item_id)
    ordinal = NUMBERS[explanation].get(found.items.index(waiting) + 1, '')
    topic = found.topic[explanation]
    if explanation == 'EN':
        return f'Next time: the {ordinal} exercise of {topic}.'
    return f'La próxima vez: el {ordinal} ejercicio de {topic}.'


def saved(explanation: Lang, learned: int) -> str:
    """What the goodbye says was saved: progress only when this session recorded some."""
    if explanation == 'EN':
        if learned:
            return 'Your progress is saved. See you next time!'
        return "I've saved today's summary; your next step stays the same. See you next time!"
    if learned:
        return 'Tu progreso está guardado. ¡Hasta la próxima!'
    return 'He guardado el resumen de hoy; tu siguiente paso sigue igual. ¡Hasta la próxima!'


def goodbye(explanation: Lang, summary: str, learned: int) -> str:
    return f'{summary} {saved(explanation, learned)}'
