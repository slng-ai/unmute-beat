"""Core's own cost on fixed scripted calls: compile, turns, payload, memory and a profile, one member at a time.

Run from the member's folder, in its own environment, so each workload uses
that member's own application code and backend, driven by one scripted
model (`Scripted`, the same moves the members' own test scripts take):

    cd use-cases/salon-graph && uv run python ../../docs/research/reusable-core/optimization/bench.py turns salon
    cd core && uv run python ../docs/research/reusable-core/optimization/bench.py memory desk

Modes: `compile`, `turns`, `payload`, `memory`, `profile`, `apps` (salon and
Faro side by side, from salon-graph). Workloads: `salon` (the six-turn
booking walkthrough), `salon-long` (the same, then 24 complaint turns),
`desk` (the support desk's resolve, `core/tests/ticket_flow`), `faro` (a
coached lesson). Every call ends with the same checked outcome before and
after a change, so a timing is never of a different conversation.

Scripted models prove runtime cost and request counts, never language
understanding or provider latency. Payload sizes are bytes of what each
request carried, not tokens: a scripted model reports no usage.
"""

import argparse
import asyncio
import cProfile
import gc
import io
import json
import os
import platform
import pstats
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import weakref
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

os.environ.setdefault('GOOGLE_API_KEY', 'offline-bench-placeholder')
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')
sys.path.insert(0, os.getcwd())

import pydantic_ai.models

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

from loguru import logger  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel  # noqa: E402

import core  # noqa: E402
from core.turn import TurnFinished, run_turn  # noqa: E402

logger.remove()
logger.add(sys.stderr, level='WARNING')

type Moves = tuple[tuple[Any, ...], ...]
type Turns = list[tuple[str, Moves]]


@dataclass
class TurnRecord:
    """One caller turn's cost: wall and process CPU time, provider requests, and when useful speech began."""

    wall_ms: float
    cpu_ms: float
    requests: int
    first_output_ms: float | None
    """From the turn's start to its first segment that is not a holding line. None: it said nothing."""
    status: str


@dataclass
class Sent:
    """What one provider request carried, in UTF-8 bytes."""

    step: str
    instructions: int
    context: int
    """The part of the instructions `core.steps.context` wrote: from `## Application state` on."""
    history: int
    """The text of every message part a provider is sent: words, tool names, arguments, results. No metadata."""
    tools: int
    """Tools and outputs offered."""
    schemas: int
    """Their names, descriptions and JSON schemas."""


def _visible(message) -> list[str]:
    """What of one message a provider receives as text."""
    out = []
    for part in message.parts:
        if isinstance(part, UserPromptPart | TextPart):
            out.append(part.content if isinstance(part.content, str) else str(part.content))
        elif isinstance(part, ToolCallPart):
            out += [part.tool_name, part.args_as_json_str()]
        elif isinstance(part, ToolReturnPart):
            out += [part.tool_name, part.model_response_str()]
        elif isinstance(part, RetryPromptPart):
            out.append(part.model_response())
    return out


class Probe:
    """Wraps a scripted stream function: counts every request, and with `sizes` records what each carried."""

    def __init__(self, sizes: bool = False) -> None:
        self.sizes = sizes
        self.count = 0
        self.sent: list[Sent] = []

    def wrap(self, stream):
        async def probed(messages, info):
            self.count += 1
            if self.sizes:
                self.sent.append(self.measure(messages, info))
            async for part in stream(messages, info):
                yield part

        return probed

    @staticmethod
    def measure(messages, info) -> Sent:
        told = info.instructions or ''
        at = told.find('## Application state')
        tools = [*info.function_tools, *info.output_tools]
        schemas = json.dumps(
            [[t.name, t.description, t.parameters_json_schema] for t in tools], separators=(',', ':')
        )
        step = next(
            (line.split('`')[1] for line in told.splitlines() if line.startswith('You are the `')), '?'
        )
        return Sent(
            step=step,
            instructions=len(told.encode()),
            context=len(told[at:].encode()) if at >= 0 else 0,
            history=sum(len(text.encode()) for message in messages for text in _visible(message)),
            tools=len(tools),
            schemas=len(schemas.encode()),
        )


class Scripted:
    """Each step request takes the next move: `('say', words)`, `('call', tool, args)` or `('collect', values)`.

    Nothing else may run: a non-streamed request would be a second reading of
    the reply, which core never sends.
    """

    def __init__(self, probe: Probe) -> None:
        self.moves: list[tuple[Any, ...]] = []
        self.model = FunctionModel(self.reading, stream_function=probe.wrap(self.agent))

    def reading(self, messages: Any, info: AgentInfo):
        raise AssertionError('a separate reading was requested; only step agents may run')

    async def agent(self, messages: Any, info: AgentInfo):
        if not self.moves:
            yield 'Sorry?'
            return
        kind, *rest = self.moves.pop(0)
        if kind == 'say':
            yield rest[0]
        elif kind == 'collect':
            yield {0: DeltaToolCall(name='collect', json_args=json.dumps({'values': rest[0]}))}
        else:
            yield {0: DeltaToolCall(name=rest[0], json_args=json.dumps(rest[1] if len(rest) > 1 else {}))}


class Workload:
    """One application and one scripted call on it, with the outcome every run of it must end in."""

    name = ''

    def __init__(self, probe: Probe) -> None:
        self.probe = probe
        self.script = Scripted(probe)
        self.flow = self.compile(self.script.model)

    @staticmethod
    def manifest() -> Path:
        raise NotImplementedError

    def compile(self, model) -> core.StepFlow:
        return core.load(self.manifest()).compile(model=model)

    def turns(self) -> Turns:
        raise NotImplementedError

    def open(self) -> core.FlowCall:
        raise NotImplementedError

    def check(self, call: core.FlowCall, said: list[str]) -> None:
        """The outcome the scripted call must reach: raises if a change altered behaviour."""

    async def close(self, call: core.FlowCall) -> None:
        """Whatever the application does when its call ends."""

    async def turn(self, call: core.FlowCall, words: str, moves: Moves) -> tuple[TurnRecord, str]:
        """One caller turn through the engine `core.text_turn` runs, every segment then acknowledged."""
        self.script.moves = list(moves)
        before = self.probe.count
        finished: TurnFinished | None = None
        first: float | None = None
        cpu, wall = time.process_time(), time.perf_counter()
        async for event in run_turn(call, self.flow.nodes, words, core.TurnBudget()):
            if first is None and any(s.text and s.origin != 'tool_filler' for s in call.spoken):
                first = time.perf_counter() - wall
            if isinstance(event, TurnFinished):
                finished = event
        wall, cpu = time.perf_counter() - wall, time.process_time() - cpu
        segments = [s for s in call.spoken if s.text]
        call.acknowledge(*(s.id for s in segments))
        assert finished is not None
        record = TurnRecord(
            wall_ms=wall * 1e3,
            cpu_ms=cpu * 1e3,
            requests=self.probe.count - before,
            first_output_ms=None if first is None else first * 1e3,
            status=finished.status,
        )
        return record, ' '.join(s.text for s in segments)

    async def run(self, upto: int | None = None) -> tuple[core.FlowCall, list[TurnRecord]]:
        """One call, all its turns or the first `upto`; checked and closed only when it ran to the end."""
        call = self.open()
        records, said = [], []
        for words, moves in self.turns()[:upto]:
            record, text = await self.turn(call, words, moves)
            records.append(record)
            said.append(text)
        if upto is None:
            self.check(call, said)
            await self.close(call)
        return call, records


BOOKING: Turns = [
    (
        'Hi, can I book a haircut on Friday at two in the afternoon?',
        (
            ('collect', {'service': 'haircut', 'booking_day': 'Friday', 'booking_time': '14:00'}),
            ('call', 'hand_over', {'to': 'booking', 'because': 'a haircut on Friday at 2 PM'}),
        ),
    ),
    (
        "Yes, that's my number.",
        (('call', 'lookup_customer'), ('collect', {'booking_time': '14:00'}), ('call', 'wait')),
    ),
    ('Yes.', (('call', 'wait'),)),
    (
        'The later one, please.',
        (('collect', {'slot': '2026-10-02T1430|haircut'}), ('call', 'book_appointment')),
    ),
    ('Yes, that works.', (('call', 'book_appointment'),)),
    (
        'Actually, I want to complain about my last visit.',
        (
            ('call', 'hand_over', {'to': 'complaints', 'because': 'a complaint about a past visit'}),
            ('say', "I'm sorry something went wrong. What happened?"),
        ),
    ),
]
"""The session 4 walkthrough (`use-cases/salon-graph/tests/test_walkthrough.py`), move for move."""


SALON_CLOCK = (2026, 9, 28, 8, 30)
"""Monday 28 September 2026, before opening: `tests/script.py`'s clock."""
SALON_PHONE = '+34600111222'
"""The diary's seed customer."""


class Salon(Workload):
    name = 'salon'

    @staticmethod
    def manifest():
        import salon

        return Path(salon.__file__).parent / 'agent.yaml'

    def turns(self) -> Turns:
        return BOOKING

    def open(self):
        from datetime import datetime

        from salon.app import open_call
        from salon.diary import TZ, Diary

        self.clock = datetime(*SALON_CLOCK, tzinfo=TZ)
        self.diary = Diary(clock=self.clock)
        return open_call(self.flow, SALON_PHONE, self.diary)

    def check(self, call, said):
        [booked] = self.diary.bookings_for(SALON_PHONE, self.clock.date())
        assert (booked['date'], booked['time']) == ('2026-10-02', '14:30'), booked
        assert call.state.agent == 'complaints', call.state.agent
        assert said[4] == "You're all set for Friday the 2nd at 2:30 PM. Anything else I can help with?", said


class SalonLong(Salon):
    name = 'salon-long'

    def turns(self) -> Turns:
        more = [
            (f'And another thing, number {i}: the wait was long.', (('say', 'I see. Please go on.'),))
            for i in range(24)
        ]
        return [*BOOKING, *more]

    def check(self, call, said):
        super().check(call, said)
        assert said[-1] == 'I see. Please go on.', said[-1]


class Desk(Workload):
    name = 'desk'

    @staticmethod
    def manifest():
        from tests import ticket_flow

        return Path(ticket_flow.__file__).parent / 'agent.yaml'

    def turns(self) -> Turns:
        from tests.ticket_flow.worker import TO_READ_BACK

        return [
            ('¿Qué horario tenéis?', (('say', 'De nueve a cinco, de lunes a viernes.'),)),
            ('Hola, la impresora del ticket T-101 ya funciona. Cuenta ACC-1234.', TO_READ_BACK),
            ('Sí.', (('call', 'resolve_ticket', {}),)),
            ('Adiós.', (('call', 'goodbye', {}),)),
        ]

    def open(self):
        from tests.ticket_flow.tickets import Desk as Backend
        from tests.ticket_flow.tickets import DeskState

        self.desk = Backend(Path(tempfile.mkdtemp()) / 'desk.db')
        self.desk.seed('ACC-1234', 'T-101')
        return self.flow.open(DeskState(), services=self.desk)

    def check(self, call, said):
        assert self.desk.ticket('T-101') == (2, 'resolved') and self.desk.sent() == 1, self.desk.ticket(
            'T-101'
        )
        assert said[2] == 'Ticket T-101 resuelto. ¿Algo más?', said
        assert call.ended, 'the goodbye ended the call'


FARO_LEARNER = '+34600555666'
"""Emma, a seed learner mid-plan: `tests/test_faro.py`'s EMMA."""


class Faro(Workload):
    name = 'faro'

    @staticmethod
    def manifest():
        import faro

        return Path(faro.__file__).parent / 'agent.yaml'

    def turns(self) -> Turns:
        def grade(answer: str, result: str, feedback: str = '') -> tuple[str, dict[str, str]]:
            return ('collect', {'answer': answer, 'grade': result, 'feedback': feedback})

        return [
            ('Yes.', (('call', 'confirm_phone'),)),
            ("Let's carry on.", (('call', 'continue_lesson'),)),
            (
                'How do I say apples?',
                (
                    ('call', 'hand_over', {'to': 'help', 'because': 'a question about the exercise'}),
                    ('say', 'Try "manzanas".'),
                ),
            ),
            (
                'Un kilo de manzanas, por favor.',
                (grade('Un kilo de manzanas, por favor.', 'correct', 'Great!'),),
            ),
            ('¿Cuánto cuesta?', (grade('¿Cuánto cuesta?', 'correct'),)),
            (
                'Quiero pan y leche. ¿Cuánto es en total?',
                (grade('Quiero pan y leche. ¿Cuánto es en total?', 'correct', 'Perfect!'),),
            ),
            ('Yes, go on.', (('call', 'continue_lesson'),)),
        ]

    def open(self):
        import faro
        from faro.store import Store

        store = Store(str(Path(tempfile.mkdtemp()) / 'faro.sqlite3'))
        store.open()
        call = faro.open_call(self.flow, FARO_LEARNER, learning=store)
        core.greet(call, faro.greeting(self.flow, call))
        call.acknowledge(call.spoken[-1].id)
        return call

    def check(self, call, said):
        assert said[5].startswith('Perfect! Lesson complete: shopping.'), said[5]
        assert 'Talking about your routine, first exercise' in said[6], said[6]

    async def close(self, call):
        import faro

        await faro.close_call(call)


WORKLOADS: dict[str, type[Workload]] = {w.name: w for w in (Salon, SalonLong, Desk, Faro)}


def spread(values: list[float]) -> dict[str, float]:
    """Median, p90, min, max and standard deviation: never one best run."""
    ordered = sorted(values)
    return {
        'n': len(ordered),
        'median': statistics.median(ordered),
        'p90': ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))],
        'min': ordered[0],
        'max': ordered[-1],
        'stdev': statistics.pstdev(ordered),
    }


def environment() -> dict[str, Any]:
    """Where and on what this ran."""
    versions = {}
    for name in ('pydantic', 'pydantic-ai-slim', 'pydantic-graph', 'pipecat-ai', 'slng-voice-core'):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return {
        'python': sys.version.split()[0],
        'machine': platform.machine(),
        'platform': platform.platform(),
        'cpus': os.cpu_count(),
        'versions': versions,
        'core': str(Path(core.__file__).parent),
    }


def rss_mb() -> float:
    """This process's resident memory now, from `ps`: allocator and native memory included."""
    out = subprocess.run(['ps', '-o', 'rss=', '-p', str(os.getpid())], capture_output=True, text=True)
    return int(out.stdout.strip()) / 1024


def unused_model() -> FunctionModel:
    """A model for compiling only: importing a member's test script would compile its application first."""

    def never(messages, info):
        raise AssertionError('compile only')

    return FunctionModel(never)


def compile_once(name: str) -> dict[str, float]:
    """In a fresh interpreter: the import of core, then one load and one compile (`compile --cold`)."""
    started = time.perf_counter()
    workload = WORKLOADS[name]
    model = unused_model()
    loaded = time.perf_counter()
    spec = core.load(workload.manifest())
    compiling = time.perf_counter()
    spec.compile(model=model)
    done = time.perf_counter()
    return {
        'setup_ms': (loaded - started) * 1e3,
        'load_ms': (compiling - loaded) * 1e3,
        'compile_ms': (done - compiling) * 1e3,
    }


def measure_compile(name: str, reps: int) -> dict[str, Any]:
    workload = WORKLOADS[name]
    model = unused_model()
    core.load(workload.manifest()).compile(model=model)
    load, build = [], []
    for _ in range(reps):
        started = time.perf_counter()
        spec = core.load(workload.manifest())
        loaded = time.perf_counter()
        spec.compile(model=model)
        build.append((time.perf_counter() - loaded) * 1e3)
        load.append((loaded - started) * 1e3)
    cold = []
    for _ in range(reps):
        out = subprocess.run(
            [sys.executable, __file__, 'compile-once', name], capture_output=True, text=True, check=True
        )
        cold.append(json.loads(out.stdout.strip().splitlines()[-1]))
    return {
        'warm_load_ms': spread(load),
        'warm_compile_ms': spread(build),
        'cold_load_ms': spread([c['load_ms'] for c in cold]),
        'cold_compile_ms': spread([c['compile_ms'] for c in cold]),
    }


async def measure_turns(name: str, warmup: int, reps: int) -> dict[str, Any]:
    """The first call in a fresh process (cold), then `reps` calls after `warmup` more."""
    probe = Probe()
    workload = WORKLOADS[name](probe)
    _, cold = await workload.run()
    for _ in range(warmup):
        await workload.run()
    calls = []
    for _ in range(reps):
        _, records = await workload.run()
        calls.append(records)
    turns = len(calls[0])

    def per_turn(field_name: str) -> list[dict[str, float]]:
        return [spread([getattr(c[i], field_name) for c in calls]) for i in range(turns)]

    def whole(field_name: str) -> dict[str, float]:
        return spread([sum(getattr(r, field_name) for r in c) for c in calls])

    return {
        'cold_call': [asdict(r) for r in cold],
        'warm': {
            'wall_ms': per_turn('wall_ms'),
            'cpu_ms': per_turn('cpu_ms'),
            'wait_ms': [spread([c[i].wall_ms - c[i].cpu_ms for c in calls]) for i in range(turns)],
            'first_output_ms': [
                spread([c[i].first_output_ms for c in calls if c[i].first_output_ms is not None])
                for i in range(turns)
            ],
            'requests': [c.requests for c in calls[0]],
            'call_wall_ms': whole('wall_ms'),
            'call_cpu_ms': whole('cpu_ms'),
        },
    }


async def cold_once(name: str) -> dict[str, Any]:
    """In a fresh interpreter: compile, then the first call's turns (`cold`)."""
    workload = WORKLOADS[name](Probe())
    _, records = await workload.run()
    return {'turns': [asdict(r) for r in records]}


def measure_cold(name: str, reps: int) -> dict[str, Any]:
    """The first call of `reps` fresh processes: each turn's wall time, and the whole call's."""
    calls = []
    for _ in range(reps):
        out = subprocess.run(
            [sys.executable, __file__, 'cold-once', name], capture_output=True, text=True, check=True
        )
        calls.append(json.loads(out.stdout.strip().splitlines()[-1])['turns'])
    turns = len(calls[0])
    return {
        'wall_ms': [spread([c[i]['wall_ms'] for c in calls]) for i in range(turns)],
        'call_wall_ms': spread([sum(t['wall_ms'] for t in c) for c in calls]),
        'call_cpu_ms': spread([sum(t['cpu_ms'] for t in c) for c in calls]),
    }


async def measure_payload(name: str) -> dict[str, Any]:
    """What every request of one call carried, and the warm-up requests a launcher's `model_clients` sends.

    A warm-up is one non-streamed `ok` with `max_tokens: 1`, no instructions
    and no tools (`core.graph._warm`): the scripted model refuses it, so it is
    counted from the records `model_clients` yields, not from the probe.
    """
    from core.graph import model_clients

    probe = Probe(sizes=True)
    workload = WORKLOADS[name](probe)
    async with model_clients(workload.flow.nodes, warm=True) as warmed:
        await asyncio.sleep(0.05)
    await workload.run()
    caller = probe.sent
    total = {k: sum(getattr(s, k) for s in caller) for k in ('instructions', 'context', 'history', 'schemas')}
    return {
        'warmup_requests': len(warmed),
        'warmed': warmed,
        'requests': [asdict(s) for s in caller],
        'totals': total | {'requests': len(caller)},
    }


async def measure_memory(name: str, calls: int, active: int, compiles: int) -> dict[str, Any]:
    """Python memory held by definitions and calls, apart from RSS.

    Definitions: `compiles` compile-and-discard cycles, then how many
    `StepFlow`s are still alive and what Python still holds. Completed calls:
    `calls` calls run to the end and dropped. Active calls: `active` calls
    held open half way, then released.
    """
    probe = Probe()
    workload = WORKLOADS[name](probe)
    await workload.run()
    gc.collect()
    tracemalloc.start()
    rss = {'start': rss_mb()}

    gc.collect()
    before = tracemalloc.get_traced_memory()[0]
    flows = []
    for _ in range(compiles):
        flows.append(weakref.ref(workload.compile(workload.script.model)))
    workload.compile(workload.script.model)
    gc.collect()
    definitions = {
        'compiled': compiles,
        'alive': sum(ref() is not None for ref in flows),
        'retained_kib': (tracemalloc.get_traced_memory()[0] - before) / 1024,
    }
    rss['after_compiles'] = rss_mb()

    gc.collect()
    before = tracemalloc.get_traced_memory()[0]
    finished = []
    for _ in range(calls):
        call, _ = await workload.run()
        finished.append(weakref.ref(call))
        del call
    gc.collect()
    completed = {
        'calls': calls,
        'alive': sum(ref() is not None for ref in finished),
        'retained_kib': (tracemalloc.get_traced_memory()[0] - before) / 1024,
    }
    rss['after_calls'] = rss_mb()

    gc.collect()
    before = tracemalloc.get_traced_memory()[0]
    half = len(workload.turns()) // 2
    held = []
    for _ in range(active):
        held.append((await workload.run(upto=half))[0])
    gc.collect()
    holding = tracemalloc.get_traced_memory()[0] - before
    refs = [weakref.ref(c) for c in held]
    held.clear()
    gc.collect()
    open_calls = {
        'active': active,
        'turns_each': half,
        'held_kib_each': holding / 1024 / active,
        'alive_after_release': sum(ref() is not None for ref in refs),
        'retained_after_release_kib': (tracemalloc.get_traced_memory()[0] - before) / 1024,
    }
    rss['end'] = rss_mb()
    tracemalloc.stop()
    return {'definitions': definitions, 'completed': completed, 'open': open_calls, 'rss_mb': rss}


async def measure_profile(name: str, warmup: int, reps: int, top: int) -> dict[str, Any]:
    """A profile over `reps` warm calls: the functions that cost the most time of their own, and core's share."""
    probe = Probe()
    workload = WORKLOADS[name](probe)
    for _ in range(warmup):
        await workload.run()
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(reps):
        await workload.run()
    profiler.disable()
    stats = pstats.Stats(profiler)
    rows = []
    for (path, line, function), (_, calls, own, cumulative, _) in stats.stats.items():  # ty: ignore[unresolved-attribute]
        rows.append(
            {
                'where': f'{path}:{line}:{function}',
                'calls': calls,
                'own_ms': own * 1e3 / reps,
                'cum_ms': cumulative * 1e3 / reps,
            }
        )
    total = sum(r['own_ms'] for r in rows)
    in_core = sum(r['own_ms'] for r in rows if '/core/src/core/' in r['where'])
    text = io.StringIO()
    pstats.Stats(profiler, stream=text).sort_stats('tottime').print_stats(top)
    return {
        'per_call_own_ms_total': total,
        'per_call_own_ms_core': in_core,
        'top_own': sorted(rows, key=lambda r: -r['own_ms'])[:top],
        'top_core_cumulative': sorted(
            (r for r in rows if '/core/src/core/' in r['where']), key=lambda r: -r['cum_ms']
        )[:top],
        'pstats': text.getvalue(),
    }


async def measure_apps() -> dict[str, Any]:
    """The salon and Faro compiled in one process, their calls run side by side: each ends as it does alone."""
    salon, faro = Salon(Probe()), Faro(Probe())
    results = await asyncio.gather(salon.run(), faro.run(), return_exceptions=True)
    failures = [repr(r) for r in results if isinstance(r, BaseException)]
    assert not failures, failures
    salon_call, faro_call = (r[0] for r in results)  # ty: ignore[not-iterable]
    ops = {
        'salon': sorted({e.operation for e in salon_call.evidence}),
        'faro': sorted({e.operation for e in faro_call.evidence}),
    }
    assert not set(ops['salon']) & set(ops['faro']), ops
    assert salon_call.state.facts is not faro_call.state.facts
    return {'operations': ops, 'salon_requests': salon.probe.count, 'faro_requests': faro.probe.count}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        'mode',
        choices=[
            'compile',
            'compile-once',
            'cold',
            'cold-once',
            'turns',
            'payload',
            'memory',
            'profile',
            'apps',
        ],
    )
    parser.add_argument('workload', nargs='?', default='salon', choices=sorted(WORKLOADS))
    parser.add_argument('--reps', type=int, default=20)
    parser.add_argument('--warmup', type=int, default=3)
    parser.add_argument('--calls', type=int, default=20)
    parser.add_argument('--active', type=int, default=5)
    parser.add_argument('--compiles', type=int, default=50)
    parser.add_argument('--top', type=int, default=30)
    parser.add_argument('--label', default='')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    if args.mode == 'compile-once':
        print(json.dumps(compile_once(args.workload)))
        return
    if args.mode == 'cold-once':
        print(json.dumps(asyncio.run(cold_once(args.workload))))
        return
    started = time.perf_counter()
    if args.mode == 'compile':
        result = measure_compile(args.workload, args.reps)
    elif args.mode == 'cold':
        result = measure_cold(args.workload, args.reps)
    elif args.mode == 'turns':
        result = asyncio.run(measure_turns(args.workload, args.warmup, args.reps))
    elif args.mode == 'payload':
        result = asyncio.run(measure_payload(args.workload))
    elif args.mode == 'memory':
        result = asyncio.run(measure_memory(args.workload, args.calls, args.active, args.compiles))
    elif args.mode == 'profile':
        result = asyncio.run(measure_profile(args.workload, args.warmup, args.reps, args.top))
    else:
        result = asyncio.run(measure_apps())
    report = {
        'mode': args.mode,
        'workload': args.workload,
        'label': args.label,
        'seconds': time.perf_counter() - started,
        'environment': environment(),
        'result': result,
    }
    text = json.dumps(report, indent=1, default=str)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    print(text if args.mode != 'profile' else result.get('pstats', '')[:6000])


if __name__ == '__main__':
    main()
