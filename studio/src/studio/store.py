"""Where runs are kept: every event and every turn's snapshot, in Redis for a week and in Postgres behind it.

Only the studio writes either (decision 3 of `docs/studio/plan.md`): runners send their events to the
studio, and the studio stores them here and fans them out to the live view (`studio.runs`).

    Redis      studio:run:{id}             the run's row, as JSON
               studio:run:{id}:events      a stream: one entry per event, in order
               studio:run:{id}:snap:{turn} the turn's snapshot (`core.snapshot.TurnSnapshot`), as JSON
               studio:runs                 a sorted set of run ids by start time
               every key expires after `KEEP` (7 days)
    Postgres   runs, events, snapshots, agent_configs (`schema.sql`, applied on every start)

A write goes to Redis first and is awaited; the same write is queued for Postgres and copied by a
task of its own, in order, so a slow database never holds a run up. A read asks Redis, then Postgres
for what has expired there. `MemoryStore` keeps the same in this process only: what tests use, and
what the studio uses when started with `--no-services`.
"""

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from typing import Any, Protocol

from loguru import logger
from pydantic import BaseModel

from studio.settings import PACKAGE

KEEP = 7 * 24 * 3600
"""How long Redis keeps a run, in seconds: a week, as agreed."""


class RunRow(BaseModel):
    """One run, as the Runs page lists it."""

    id: str
    agent: str
    config: str = ''
    channel: str
    started_at: datetime
    ended_at: datetime | None = None
    outcome: str | None = None
    forked_from: str | None = None
    forked_turn: int | None = None
    runner: str = ''
    turns: int = 0
    step: str = ''


class ConfigRow(BaseModel):
    agent: str
    version: str
    path: str
    manifest: str
    files: dict[str, str] = {}
    parent: str | None = None
    created_at: datetime | None = None


class Store(Protocol):
    name: str

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def save_run(self, row: RunRow) -> None: ...
    async def add_event(self, run: str, event: dict[str, Any]) -> None: ...
    async def save_snapshot(self, run: str, turn: int, snapshot: dict[str, Any]) -> None: ...
    async def save_config(self, row: ConfigRow) -> None: ...
    async def runs(self, agent: str = '', limit: int = 200) -> list[RunRow]: ...
    async def run(self, run: str) -> RunRow | None: ...
    async def events(self, run: str) -> list[dict[str, Any]]: ...
    async def snapshot(self, run: str, turn: int) -> dict[str, Any] | None: ...
    async def snapshot_turns(self, run: str) -> list[int]: ...
    async def configs(self, agent: str) -> list[ConfigRow]: ...
    async def config(self, agent: str, version: str) -> ConfigRow | None: ...
    async def close_unfinished(self) -> int: ...


async def _close_rows(store: Store, rows: list[RunRow]) -> int:
    now = datetime.now(UTC)
    for row in rows:
        await store.save_run(row.model_copy(update={'outcome': 'stopped', 'ended_at': now}))
    return len(rows)


class MemoryStore:
    """The store in this process's memory: the same reads and writes, kept until the studio stops."""

    name = 'memory'

    def __init__(self) -> None:
        self._runs: dict[str, RunRow] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._snapshots: dict[str, dict[int, dict[str, Any]]] = {}
        self._configs: dict[tuple[str, str], ConfigRow] = {}

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def save_run(self, row: RunRow) -> None:
        self._runs[row.id] = row

    async def add_event(self, run: str, event: dict[str, Any]) -> None:
        self._events.setdefault(run, []).append(event)

    async def save_snapshot(self, run: str, turn: int, snapshot: dict[str, Any]) -> None:
        self._snapshots.setdefault(run, {})[turn] = snapshot

    async def save_config(self, row: ConfigRow) -> None:
        stamped = row if row.created_at else row.model_copy(update={'created_at': datetime.now(UTC)})
        self._configs.setdefault((row.agent, row.version), stamped)

    async def runs(self, agent: str = '', limit: int = 200) -> list[RunRow]:
        rows = [r for r in self._runs.values() if not agent or r.agent == agent]
        return sorted(rows, key=lambda r: r.started_at, reverse=True)[:limit]

    async def run(self, run: str) -> RunRow | None:
        return self._runs.get(run)

    async def events(self, run: str) -> list[dict[str, Any]]:
        return list(self._events.get(run, []))

    async def snapshot(self, run: str, turn: int) -> dict[str, Any] | None:
        return self._snapshots.get(run, {}).get(turn)

    async def snapshot_turns(self, run: str) -> list[int]:
        return sorted(self._snapshots.get(run, {}))

    async def configs(self, agent: str) -> list[ConfigRow]:
        rows = [c for (a, _), c in self._configs.items() if a == agent]
        return sorted(rows, key=lambda c: c.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)

    async def config(self, agent: str, version: str) -> ConfigRow | None:
        return self._configs.get((agent, version))

    async def close_unfinished(self) -> int:
        """Runs a studio that stopped left live: no runner it started still carries them."""
        return await _close_rows(self, [r for r in self._runs.values() if r.outcome is None])


class ServiceStore:
    """Redis for a week, Postgres behind it (`redis.asyncio`, `asyncpg`)."""

    name = 'redis+postgres'

    def __init__(self, redis_url: str, database_url: str) -> None:
        self.redis_url = redis_url
        self.database_url = database_url
        self.redis: Any = None
        self.pool: Any = None
        self._queue: asyncio.Queue[tuple[str, tuple[Any, ...]] | None] = asyncio.Queue()
        self._writer: asyncio.Task[None] | None = None
        self.behind = 0
        """Postgres writes that failed; Redis still has them for a week."""

    async def open(self) -> None:
        import asyncpg
        import redis.asyncio as aioredis

        self.redis = aioredis.from_url(self.redis_url, decode_responses=True)
        await self.redis.ping()
        self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=4)
        async with self.pool.acquire() as db:
            await db.execute((PACKAGE / 'schema.sql').read_text())
        self._writer = asyncio.create_task(self._write())

    async def close(self) -> None:
        if self._writer is not None:
            await self._queue.put(None)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._writer, 5)
        if self.pool is not None:
            await self.pool.close()
        if self.redis is not None:
            await self.redis.aclose()

    @staticmethod
    def _key(run: str, *parts: str) -> str:
        return ':'.join(('studio', 'run', run, *parts))

    async def save_run(self, row: RunRow) -> None:
        key = self._key(row.id)
        async with self.redis.pipeline() as pipe:
            pipe.set(key, row.model_dump_json(), ex=KEEP)
            pipe.zadd('studio:runs', {row.id: row.started_at.timestamp()})
            await pipe.execute()
        self._queue.put_nowait(('run', (row,)))

    async def add_event(self, run: str, event: dict[str, Any]) -> None:
        key = self._key(run, 'events')
        async with self.redis.pipeline() as pipe:
            pipe.xadd(key, {'e': json.dumps(event)})
            pipe.expire(key, KEEP)
            await pipe.execute()
        self._queue.put_nowait(('event', (run, event)))

    async def save_snapshot(self, run: str, turn: int, snapshot: dict[str, Any]) -> None:
        await self.redis.set(self._key(run, 'snap', str(turn)), json.dumps(snapshot), ex=KEEP)
        self._queue.put_nowait(('snapshot', (run, turn, snapshot)))

    async def save_config(self, row: ConfigRow) -> None:
        async with self.pool.acquire() as db:
            await db.execute(
                'INSERT INTO agent_configs (agent_id, version, path, manifest, files, parent) '
                'VALUES ($1, $2, $3, $4, $5::jsonb, $6) ON CONFLICT DO NOTHING',
                row.agent,
                row.version,
                row.path,
                row.manifest,
                json.dumps(row.files),
                row.parent,
            )

    async def _write(self) -> None:
        """Copy each queued write into Postgres, in order. A failed one is logged and counted; the next goes on."""
        while (item := await self._queue.get()) is not None:
            kind, args = item
            try:
                async with self.pool.acquire() as db:
                    await getattr(self, f'_pg_{kind}')(db, *args)
            except Exception as error:
                self.behind += 1
                logger.warning('postgres write {} failed: {}', kind, error)

    async def _pg_run(self, db: Any, row: RunRow) -> None:
        await db.execute(
            'INSERT INTO runs (id, agent_id, config, channel, started_at, ended_at, outcome, forked_from, '
            'forked_turn, runner, turns) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) '
            'ON CONFLICT (id) DO UPDATE SET ended_at = EXCLUDED.ended_at, outcome = EXCLUDED.outcome, '
            'turns = EXCLUDED.turns, runner = EXCLUDED.runner, config = EXCLUDED.config',
            row.id,
            row.agent,
            row.config,
            row.channel,
            row.started_at,
            row.ended_at,
            row.outcome,
            row.forked_from,
            row.forked_turn,
            row.runner,
            row.turns,
        )

    async def _pg_event(self, db: Any, run: str, event: dict[str, Any]) -> None:
        await db.execute(
            'INSERT INTO events (run_id, seq, turn, type, t_ms, body) VALUES ($1, $2, $3, $4, $5, $6::jsonb) '
            'ON CONFLICT DO NOTHING',
            run,
            int(event['seq']),
            int(event['turn']),
            str(event['type']),
            float(event['t_ms']),
            json.dumps(event),
        )

    async def _pg_snapshot(self, db: Any, run: str, turn: int, snapshot: dict[str, Any]) -> None:
        await db.execute(
            'INSERT INTO snapshots (run_id, turn, step, state, history, snapshot) '
            'VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb) ON CONFLICT (run_id, turn) DO UPDATE SET '
            'step = EXCLUDED.step, state = EXCLUDED.state, history = EXCLUDED.history, snapshot = EXCLUDED.snapshot',
            run,
            turn,
            str(snapshot.get('agent', '')),
            json.dumps(snapshot.get('facts', {})),
            json.dumps(snapshot.get('messages', [])),
            json.dumps(snapshot),
        )

    async def runs(self, agent: str = '', limit: int = 200) -> list[RunRow]:
        found: dict[str, RunRow] = {}
        ids = await self.redis.zrevrange('studio:runs', 0, limit * 4)
        rows = await self.redis.mget([self._key(i) for i in ids]) if ids else []
        for raw in rows:
            if raw:
                row = RunRow.model_validate_json(raw)
                found[row.id] = row
        async with self.pool.acquire() as db:
            query = (
                'SELECT * FROM runs'
                + (' WHERE agent_id = $2' if agent else '')
                + ' ORDER BY started_at DESC LIMIT $1'
            )
            records = await db.fetch(query, limit, *([agent] if agent else []))
        for record in records:
            found.setdefault(record['id'], _row(record))
        rows = [r for r in found.values() if not agent or r.agent == agent]
        return sorted(rows, key=lambda r: r.started_at, reverse=True)[:limit]

    async def run(self, run: str) -> RunRow | None:
        if raw := await self.redis.get(self._key(run)):
            return RunRow.model_validate_json(raw)
        async with self.pool.acquire() as db:
            record = await db.fetchrow('SELECT * FROM runs WHERE id = $1', run)
        return _row(record) if record else None

    async def events(self, run: str) -> list[dict[str, Any]]:
        entries = await self.redis.xrange(self._key(run, 'events'))
        if entries:
            return [json.loads(fields['e']) for _, fields in entries]
        async with self.pool.acquire() as db:
            records = await db.fetch('SELECT body FROM events WHERE run_id = $1 ORDER BY seq', run)
        return [json.loads(r['body']) for r in records]

    async def snapshot(self, run: str, turn: int) -> dict[str, Any] | None:
        if raw := await self.redis.get(self._key(run, 'snap', str(turn))):
            return json.loads(raw)
        async with self.pool.acquire() as db:
            record = await db.fetchrow(
                'SELECT snapshot FROM snapshots WHERE run_id = $1 AND turn = $2', run, turn
            )
        return json.loads(record['snapshot']) if record and record['snapshot'] else None

    async def snapshot_turns(self, run: str) -> list[int]:
        keys = [k async for k in self.redis.scan_iter(self._key(run, 'snap', '*'))]
        turns = {int(k.rsplit(':', 1)[1]) for k in keys}
        async with self.pool.acquire() as db:
            records = await db.fetch('SELECT turn FROM snapshots WHERE run_id = $1', run)
        return sorted(turns | {r['turn'] for r in records})

    async def configs(self, agent: str) -> list[ConfigRow]:
        async with self.pool.acquire() as db:
            records = await db.fetch(
                'SELECT * FROM agent_configs WHERE agent_id = $1 ORDER BY created_at DESC', agent
            )
        return [_config(r) for r in records]

    async def close_unfinished(self) -> int:
        """Runs a studio that stopped left live, in Redis and in Postgres: no runner it started still carries them."""
        closed = await _close_rows(self, [r for r in await self.runs(limit=10_000) if r.outcome is None])
        async with self.pool.acquire() as db:
            await db.execute("UPDATE runs SET outcome = 'stopped', ended_at = now() WHERE outcome IS NULL")
        return closed

    async def config(self, agent: str, version: str) -> ConfigRow | None:
        async with self.pool.acquire() as db:
            record = await db.fetchrow(
                'SELECT * FROM agent_configs WHERE agent_id = $1 AND version = $2', agent, version
            )
        return _config(record) if record else None


def _row(record: Any) -> RunRow:
    return RunRow(
        id=record['id'],
        agent=record['agent_id'],
        config=record['config'],
        channel=record['channel'],
        started_at=record['started_at'],
        ended_at=record['ended_at'],
        outcome=record['outcome'],
        forked_from=record['forked_from'],
        forked_turn=record['forked_turn'],
        runner=record['runner'] or '',
        turns=record['turns'] or 0,
    )


def _config(record: Any) -> ConfigRow:
    files = record['files']
    return ConfigRow(
        agent=record['agent_id'],
        version=record['version'],
        path=record['path'],
        manifest=record['manifest'],
        files=json.loads(files) if isinstance(files, str) else dict(files or {}),
        parent=record['parent'],
        created_at=record['created_at'],
    )
