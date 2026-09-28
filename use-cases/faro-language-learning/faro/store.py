"""Faro's learning store: learners, plans and sessions in one local SQLite file.

    uv run python -m faro fixture data/bench.sqlite3   # a new disposable DB with the three seeds
    uv run python -m faro backup SRC DST              # a live snapshot, with SQLite's backup API
    uv run python -m faro bench data/bench.sqlite3     # p50/p95 lookup and checkpoint

Every connection runs in WAL mode with `synchronous=FULL` and foreign keys
on. Each operation opens its own connection, does one short transaction and
closes it, and a success is returned only after the commit. Everything read
back is validated into `faro.models` before a field is used.

All of it runs on one worker thread, off the event loop. One thread also
means two connections are never open at once, which keeps clear of the
WAL-reset bug in SQLite before 3.51.3 (https://www.sqlite.org/wal.html#walresetbug).

ponytail: one writer thread for the whole process. Allow concurrent readers
once the runtime ships SQLite 3.51.3 or a patched backport.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import sqlite3
import statistics
import time
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from faro import curriculum
from faro.curriculum import Goal, Lang, Level
from faro.models import Attempt, Checkpoint, Learner, MemoryFact, Plan, Progress, Session

DEFAULT_PATH = 'data/language_companion.sqlite3'
_WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix='faro-db')

SCHEMA = """
CREATE TABLE IF NOT EXISTS learners (
    id INTEGER PRIMARY KEY,
    phone TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    explanation TEXT NOT NULL CHECK (explanation IN ('EN', 'ES')),
    status TEXT NOT NULL CHECK (status IN ('onboarding', 'active')),
    memory TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY,
    learner_id INTEGER NOT NULL REFERENCES learners(id),
    target TEXT NOT NULL CHECK (target IN ('EN', 'ES')),
    status TEXT NOT NULL CHECK (status IN ('draft', 'active', 'archived', 'declined')),
    revision INTEGER NOT NULL,
    progress_revision INTEGER NOT NULL,
    body TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_plan ON plans(learner_id) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    learner_id INTEGER NOT NULL REFERENCES learners(id),
    plan_id INTEGER REFERENCES plans(id),
    predecessor INTEGER REFERENCES sessions(id),
    status TEXT NOT NULL CHECK (status IN ('active', 'completed', 'interrupted')),
    revision INTEGER NOT NULL,
    started TEXT NOT NULL,
    ended TEXT,
    checkpoint TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS sessions_by_learner ON sessions(learner_id, id);
"""
"""Three tables. A plan's goal, lessons, evidence and progress are one validated JSON body."""

_BODY = frozenset({'goal', 'level', 'uncertainty', 'minutes', 'lessons', 'assessment', 'progress', 'summary'})
"""The plan fields kept in its JSON body; the rest are columns the database constrains."""


class StaleCheckpointError(Exception):
    """The session moved on since this copy was read: the write is refused and nothing changed."""


class OwnershipError(Exception):
    """The record asked for does not belong to this learner, or is not in the state the write needs."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec='seconds')


def _offloaded[**P, R](fn: Callable[P, R]) -> Callable[P, Awaitable[R]]:
    """Run a blocking store method on the store's one worker thread, and await it."""

    @functools.wraps(fn)
    async def run(*args: P.args, **kwargs: P.kwargs) -> R:
        return await asyncio.get_running_loop().run_in_executor(
            _WORKER, functools.partial(fn, *args, **kwargs)
        )

    return run


def _learner(row: sqlite3.Row) -> Learner:
    return Learner.model_validate({**dict(row), 'memory': json.loads(row['memory'])})


def _plan(row: sqlite3.Row) -> Plan:
    columns = {k: row[k] for k in ('id', 'learner_id', 'target', 'status', 'revision', 'progress_revision')}
    return Plan.model_validate({**columns, **json.loads(row['body'])})


def _session(row: sqlite3.Row) -> Session:
    return Session.model_validate(
        {
            **{
                k: row[k]
                for k in ('id', 'learner_id', 'plan_id', 'predecessor', 'status', 'revision', 'outcome')
            },
            'checkpoint': json.loads(row['checkpoint']),
        }
    )


def _body(plan: Plan) -> str:
    return plan.model_dump_json(include=set(_BODY))


class Store:
    """One learning database. Build it with `store(path)`, which opens each file once per process."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.interrupted = 0
        """Sessions found still active at startup, and marked interrupted."""

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One connection, one short transaction: committed on success, rolled back on any error, then closed."""
        db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
            except BaseException:
                db.execute('ROLLBACK')
                raise
            db.execute('COMMIT')
        finally:
            db.close()

    def open(self) -> None:
        """Create what is missing, seed the three learners only if absent, and close sessions a crash left open.

        Normal startup never resets anything: a seed whose phone is already
        there is left exactly as it is, progress included.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._tx() as db:
            for statement in SCHEMA.split(';'):
                if statement.strip():
                    db.execute(statement)
            for seed in SEEDS:
                if db.execute('SELECT 1 FROM learners WHERE phone = ?', (seed.phone,)).fetchone() is None:
                    seed.insert(db)
            self.interrupted = db.execute(
                "UPDATE sessions SET status = 'interrupted', ended = ? WHERE status = 'active'", (_now(),)
            ).rowcount
        logger.info(
            'learning store {} (SQLite {}, WAL, synchronous=FULL): {} unfinished sessions marked interrupted',
            self.path,
            sqlite3.sqlite_version,
            self.interrupted,
        )

    @_offloaded
    def lookup(self, phone: str) -> tuple[Learner, Plan | None, Session | None] | None:
        """The learner with exactly this number, their active plan and their latest session. Nobody else's."""
        with self._tx() as db:
            row = db.execute('SELECT * FROM learners WHERE phone = ?', (phone,)).fetchone()
            if row is None:
                return None
            learner = _learner(row)
            plan = db.execute(
                "SELECT * FROM plans WHERE learner_id = ? AND status = 'active'", (learner.id,)
            ).fetchone()
            last = db.execute(
                'SELECT * FROM sessions WHERE learner_id = ? ORDER BY id DESC LIMIT 1', (learner.id,)
            ).fetchone()
            return learner, _plan(plan) if plan else None, _session(last) if last else None

    @_offloaded
    def plan(self, learner_id: int, plan_id: int) -> Plan:
        with self._tx() as db:
            row = db.execute(
                'SELECT * FROM plans WHERE id = ? AND learner_id = ?', (plan_id, learner_id)
            ).fetchone()
            if row is None:
                raise OwnershipError(f'plan {plan_id} does not belong to this learner')
            return _plan(row)

    @_offloaded
    def session(self, session_id: int, learner_id: int) -> Session:
        """One of this learner's sessions, as the store has it now: what a write is built on and checked against."""
        with self._tx() as db:
            row = db.execute(
                'SELECT * FROM sessions WHERE id = ? AND learner_id = ?', (session_id, learner_id)
            ).fetchone()
            if row is None:
                raise OwnershipError(f'session {session_id} does not belong to this learner')
            return _session(row)

    @_offloaded
    def active_plan(self, learner_id: int) -> Plan | None:
        """This learner's agreed plan, or None before they agreed one."""
        with self._tx() as db:
            row = db.execute(
                "SELECT * FROM plans WHERE learner_id = ? AND status = 'active'", (learner_id,)
            ).fetchone()
            return _plan(row) if row else None

    @_offloaded
    def create_learner(self, phone: str, name: str, explanation: str) -> tuple[Learner, Session]:
        """A new onboarding profile and its first session, together. A second profile for one phone is refused."""
        with self._tx() as db:
            try:
                learner_id = (
                    db.execute(
                        "INSERT INTO learners (phone, name, explanation, status) VALUES (?, ?, ?, 'onboarding')",
                        (phone, name, explanation),
                    ).lastrowid
                    or 0
                )
            except sqlite3.IntegrityError:
                raise OwnershipError('a profile with that phone already exists') from None
            session = self._insert_session(db, learner_id, None, None, Checkpoint(phase='onboarding'))
            return _learner(
                db.execute('SELECT * FROM learners WHERE id = ?', (learner_id,)).fetchone()
            ), session

    @_offloaded
    def open_session(self, learner_id: int) -> Session:
        """A new session carrying on from the latest checkpoint, linked to the session it came from."""
        with self._tx() as db:
            last = db.execute(
                'SELECT * FROM sessions WHERE learner_id = ? ORDER BY id DESC LIMIT 1', (learner_id,)
            ).fetchone()
            active = db.execute(
                "SELECT id FROM plans WHERE learner_id = ? AND status = 'active'", (learner_id,)
            ).fetchone()
            carried = (
                _session(last).checkpoint.model_copy(update={'learned': 0, 'helped': ''})
                if last
                else Checkpoint()
            )
            return self._insert_session(
                db, learner_id, active['id'] if active else None, last['id'] if last else None, carried
            )

    def _insert_session(
        self,
        db: sqlite3.Connection,
        learner_id: int,
        plan_id: int | None,
        predecessor: int | None,
        checkpoint: Checkpoint,
    ) -> Session:
        session_id = (
            db.execute(
                'INSERT INTO sessions (learner_id, plan_id, predecessor, status, revision, started, checkpoint) '
                "VALUES (?, ?, ?, 'active', 0, ?, ?)",
                (learner_id, plan_id, predecessor, _now(), checkpoint.model_dump_json()),
            ).lastrowid
            or 0
        )
        return _session(db.execute('SELECT * FROM sessions WHERE id = ?', (session_id,)).fetchone())

    def _write_session(
        self, db: sqlite3.Connection, session: Session, checkpoint: Checkpoint, plan_id: int | None = None
    ) -> Session:
        """Save a checkpoint over exactly the revision it was built from, or refuse it as stale."""
        changed = db.execute(
            'UPDATE sessions SET checkpoint = ?, revision = revision + 1, plan_id = COALESCE(?, plan_id) '
            "WHERE id = ? AND learner_id = ? AND revision = ? AND status = 'active'",
            (checkpoint.model_dump_json(), plan_id, session.id, session.learner_id, session.revision),
        ).rowcount
        if not changed:
            raise StaleCheckpointError(f'session {session.id} is no longer at revision {session.revision}')
        return _session(db.execute('SELECT * FROM sessions WHERE id = ?', (session.id,)).fetchone())

    @_offloaded
    def save_checkpoint(self, session: Session, checkpoint: Checkpoint, explanation: str = '') -> Session:
        """A routine checkpoint: the onboarding field just given, the task waiting, the item put to them."""
        with self._tx() as db:
            if explanation:
                db.execute(
                    'UPDATE learners SET explanation = ? WHERE id = ?', (explanation, session.learner_id)
                )
            return self._write_session(db, session, checkpoint)

    @_offloaded
    def save_draft(self, session: Session, draft: Plan, checkpoint: Checkpoint) -> tuple[Plan, Session]:
        """A new plan proposal with its evidence, and the checkpoint that waits on it, in one transaction."""
        with self._tx() as db:
            plan_id = (
                db.execute(
                    'INSERT INTO plans (learner_id, target, status, revision, progress_revision, body) '
                    "VALUES (?, ?, 'draft', 1, 0, ?)",
                    (session.learner_id, draft.target, _body(draft)),
                ).lastrowid
                or 0
            )
            saved = self._write_session(db, session, checkpoint.model_copy(update={'draft_id': plan_id}))
            return _plan(db.execute('SELECT * FROM plans WHERE id = ?', (plan_id,)).fetchone()), saved

    @_offloaded
    def revise_draft(self, session: Session, plan_id: int, minutes: int, goal: str) -> Plan:
        """Change a proposal's length or goal. Its revision moves on, so a yes to the old words no longer counts."""
        with self._tx() as db:
            row = db.execute(
                "SELECT * FROM plans WHERE id = ? AND learner_id = ? AND status = 'draft'",
                (plan_id, session.learner_id),
            ).fetchone()
            if row is None:
                raise OwnershipError(f'plan {plan_id} is not a draft of this learner')
            plan = _plan(row)
            revised = Plan.model_validate(
                {**plan.model_dump(), 'minutes': minutes or plan.minutes, 'goal': goal or plan.goal}
            )
            revised = revised.model_copy(
                update={'lessons': [lesson.id for lesson in curriculum.lessons(revised.target, revised.goal)]}
            )
            db.execute(
                'UPDATE plans SET body = ?, revision = revision + 1 WHERE id = ?', (_body(revised), plan_id)
            )
            return _plan(db.execute('SELECT * FROM plans WHERE id = ?', (plan_id,)).fetchone())

    @_offloaded
    def activate(
        self, session: Session, plan_id: int, revision: int, checkpoint: Checkpoint
    ) -> tuple[Plan, Session]:
        """Make exactly this proposal revision the learner's plan, archiving the one before, in one transaction."""
        with self._tx() as db:
            row = db.execute(
                "SELECT * FROM plans WHERE id = ? AND learner_id = ? AND status = 'draft' AND revision = ?",
                (plan_id, session.learner_id, revision),
            ).fetchone()
            if row is None:
                raise OwnershipError(f'plan {plan_id} revision {revision} is not a draft of this learner')
            db.execute(
                "UPDATE plans SET status = 'archived' WHERE learner_id = ? AND status = 'active'",
                (session.learner_id,),
            )
            db.execute("UPDATE plans SET status = 'active' WHERE id = ?", (plan_id,))
            db.execute("UPDATE learners SET status = 'active' WHERE id = ?", (session.learner_id,))
            saved = self._write_session(db, session, checkpoint, plan_id=plan_id)
            return _plan(db.execute('SELECT * FROM plans WHERE id = ?', (plan_id,)).fetchone()), saved

    @_offloaded
    def decline(self, session: Session, plan_id: int, checkpoint: Checkpoint) -> Session:
        """A revised proposal the learner turned down: kept, with its evidence, as declined. The agreed plan stands."""
        with self._tx() as db:
            db.execute(
                "UPDATE plans SET status = 'declined' WHERE id = ? AND learner_id = ? AND status = 'draft'",
                (plan_id, session.learner_id),
            )
            return self._write_session(db, session, checkpoint)

    @_offloaded
    def record_attempt(self, session: Session, plan_id: int, attempt: Attempt) -> tuple[Plan, Session, bool]:
        """Save one accepted answer: evidence, progress, review and the next resume point, all or nothing.

        An attempt id already recorded returns the plan as it stands, and
        `True`: a replay is answered from the record, never counted twice.
        """
        with self._tx() as db:
            row = db.execute(
                "SELECT * FROM plans WHERE id = ? AND learner_id = ? AND status = 'active'",
                (plan_id, session.learner_id),
            ).fetchone()
            if row is None:
                raise OwnershipError(f'plan {plan_id} is not the active plan of this learner')
            plan = _plan(row)
            if any(a.id == attempt.id for a in plan.progress.attempts):
                current = db.execute('SELECT * FROM sessions WHERE id = ?', (session.id,)).fetchone()
                return plan, _session(current), True
            after = plan.after(attempt)
            db.execute(
                'UPDATE plans SET body = ?, progress_revision = progress_revision + 1 WHERE id = ?',
                (_body(after), plan_id),
            )
            learned = session.checkpoint.learned + (attempt.result != 'insufficient_evidence')
            checkpoint = session.checkpoint.model_copy(update={'item': after.waiting(), 'learned': learned})
            saved = self._write_session(db, session, checkpoint)
            return _plan(db.execute('SELECT * FROM plans WHERE id = ?', (plan_id,)).fetchone()), saved, False

    @_offloaded
    def remember(self, learner_id: int, fact: MemoryFact) -> Learner:
        """Keep a fact about the learner. A fact under the same key replaces the old one: corrections win."""
        with self._tx() as db:
            learner = _learner(db.execute('SELECT * FROM learners WHERE id = ?', (learner_id,)).fetchone())
            memory = [*[f for f in learner.memory if f.key != fact.key][-19:], fact]
            db.execute(
                'UPDATE learners SET memory = ? WHERE id = ?',
                (json.dumps([f.model_dump() for f in memory], ensure_ascii=False), learner_id),
            )
            return _learner(db.execute('SELECT * FROM learners WHERE id = ?', (learner_id,)).fetchone())

    @_offloaded
    def finalize(self, session_id: int, learner_id: int, outcome: str, summary: str) -> bool:
        """Close a session and write its summary onto the plan. Run twice, it changes nothing the second time."""
        with self._tx() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE id = ? AND learner_id = ? AND status = 'active'",
                (session_id, learner_id),
            ).fetchone()
            if row is None:
                return False
            db.execute(
                "UPDATE sessions SET status = 'completed', ended = ?, outcome = ?, revision = revision + 1 WHERE id = ?",
                (_now(), outcome, session_id),
            )
            if row['plan_id'] is not None:
                plan = _plan(db.execute('SELECT * FROM plans WHERE id = ?', (row['plan_id'],)).fetchone())
                db.execute(
                    'UPDATE plans SET body = ? WHERE id = ?',
                    (_body(plan.model_copy(update={'summary': summary})), plan.id),
                )
            return True

    @_offloaded
    def counts(self) -> dict[str, int]:
        """How many rows each table holds. For checks and the benchmark."""
        with self._tx() as db:
            return {
                t: db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                for t in ('learners', 'plans', 'sessions')
            }


@functools.cache
def store(path: str | None = None) -> Store:
    """The store at `path`, or at `LEARNING_DB_PATH`: opened, seeded and reconciled once per process."""
    opened = Store(path or os.getenv('LEARNING_DB_PATH', DEFAULT_PATH))
    opened.open()
    return opened


@dataclass(frozen=True)
class _Seed:
    """One synthetic learner at a fixed position. Inserted only when their phone is not in the file yet."""

    phone: str
    name: str
    explanation: Lang
    target: Lang
    goal: Goal
    level: Level
    memory: dict[str, str]
    progress: Progress
    checkpoint: Checkpoint

    def insert(self, db: sqlite3.Connection) -> None:
        learner_id = (
            db.execute(
                "INSERT INTO learners (phone, name, explanation, status) VALUES (?, ?, ?, 'active')",
                (self.phone, self.name, self.explanation),
            ).lastrowid
            or 0
        )
        plan = Plan(
            id=0,
            learner_id=learner_id,
            target=self.target,
            status='active',
            revision=1,
            progress_revision=0,
            goal=self.goal,
            level=self.level,
            uncertainty='seeded fixture: provisional, no placement evidence on file',
            minutes=10,
            lessons=[lesson.id for lesson in curriculum.lessons(self.target, self.goal)],
            progress=self.progress,
        )
        plan_id = (
            db.execute(
                "INSERT INTO plans (learner_id, target, status, revision, progress_revision, body) VALUES (?, ?, 'active', 1, 0, ?)",
                (learner_id, self.target, _body(plan)),
            ).lastrowid
            or 0
        )
        session_id = (
            db.execute(
                'INSERT INTO sessions (learner_id, plan_id, status, revision, started, ended, checkpoint, outcome) '
                "VALUES (?, ?, 'completed', 0, ?, ?, ?, 'seeded')",
                (learner_id, plan_id, _now(), _now(), self.checkpoint.model_dump_json()),
            ).lastrowid
            or 0
        )
        facts = [MemoryFact(key=key, text=text, session_id=session_id) for key, text in self.memory.items()]
        db.execute(
            'UPDATE learners SET memory = ? WHERE id = ?',
            (json.dumps([f.model_dump() for f in facts], ensure_ascii=False), learner_id),
        )


def _done(item_id: str, session: int = 0) -> Attempt:
    return Attempt(
        id=f'seed-{item_id}', item_id=item_id, answer='(seeded)', result='independent', session_id=session
    )


SEEDS = (
    _Seed(
        '+34600111222',
        'Lucía',
        'ES',
        'EN',
        'travel',
        'A2',
        {'hobby': 'Enjoys hiking.', 'corrections': 'Prefers corrections after answering.'},
        Progress(
            lesson=1,
            item=1,
            completed=['en-travel-1'],
            attempts=[*(_done(f'en-travel-1-{n}') for n in (1, 2, 3)), _done('en-travel-2-1')],
        ),
        Checkpoint(phase='lesson', target='EN', goal='travel', minutes=10, item='en-travel-2-2'),
    ),
    _Seed(
        '+34600333444',
        'Diego',
        'ES',
        'EN',
        'work',
        'B1',
        {'hobby': 'Enjoys cooking.', 'scenarios': 'Wants short work scenarios.'},
        Progress(
            lesson=2,
            item=0,
            completed=['en-work-1', 'en-work-2'],
            review=['en-work-2-1'],
            attempts=[
                *(_done(f'en-work-1-{n}') for n in (1, 2, 3)),
                *(_done(f'en-work-2-{n}') for n in (1, 2, 3)),
            ],
        ),
        Checkpoint(phase='menu', target='EN', goal='work', minutes=10),
    ),
    _Seed(
        '+34600555666',
        'Emma',
        'EN',
        'ES',
        'daily',
        'A1',
        {'hobby': 'Enjoys markets.', 'prompts': 'Prefers short prompts.'},
        Progress(
            lesson=1, item=0, completed=['es-daily-1'], attempts=[_done(f'es-daily-1-{n}') for n in (1, 2, 3)]
        ),
        Checkpoint(phase='lesson', target='ES', goal='daily', minutes=10, item='es-daily-2-1'),
    ),
)
"""Lucía, Diego and Emma at the positions the benchmark starts from. Synthetic people, test-format numbers."""


def fixture(path: str) -> Store:
    """A new disposable database holding exactly the three seeds. Refuses to touch a file that exists."""
    if Path(path).exists():
        raise SystemExit(f'{path} exists; a fixture is only ever created fresh. Pick a new path.')
    created = Store(path)
    created.open()
    return created


def backup(source: str, target: str) -> None:
    """A consistent snapshot of a live database, with SQLite's own backup API."""
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


async def bench(path: str, population: int = 10_000, runs: int = 1_000) -> dict[str, float]:
    """p50/p95 in milliseconds of a warm phone lookup and a checkpoint, on a fixture grown to `population` learners."""
    measured = fixture(path)
    with measured._tx() as db:
        db.executemany(
            "INSERT INTO learners (phone, name, explanation, status) VALUES (?, ?, 'ES', 'active')",
            [(f'+3461{n:07d}', f'Learner {n}') for n in range(population)],
        )
    found = await measured.lookup('+34600111222')
    assert found is not None and found[2] is not None
    session = await measured.open_session(found[0].id)
    timings: dict[str, list[float]] = {'lookup': [], 'checkpoint': []}
    for n in range(runs + 50):
        started = time.perf_counter()
        await measured.lookup(f'+3461{n % population:07d}')
        lookup = time.perf_counter() - started
        started = time.perf_counter()
        session = await measured.save_checkpoint(
            session, session.checkpoint.model_copy(update={'task': f't{n}'})
        )
        checkpoint = time.perf_counter() - started
        if n >= 50:
            timings['lookup'].append(lookup * 1000)
            timings['checkpoint'].append(checkpoint * 1000)
    result = {}
    for name, values in timings.items():
        quantiles = statistics.quantiles(values, n=100)
        result[f'{name}_p50_ms'], result[f'{name}_p95_ms'] = round(quantiles[49], 3), round(quantiles[94], 3)
    return result
