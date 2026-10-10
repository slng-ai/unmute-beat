"""Run: uv run python -m tests.test_store

The store's promises, on both stores: the memory store always, and Redis with Postgres when the studio's
services are up (`slng studio up`); without them that half says it was skipped, never passed.

* a run's row, its events in order and its snapshots come back as they went in;
* the Runs list is newest first and filters by agent;
* Postgres holds what Redis held: with Redis emptied, the same run, events and snapshot read back;
* runs a stopped studio left live are closed as stopped (the memory store: on the real one this would
  close the runs of a studio that is up, so its test rows are removed instead);
* config versions come back with the version each was edited from.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from studio.runs.store import ConfigRow, MemoryStore, RunRow, ServiceStore, Store
from studio.settings import Settings


async def row(store: Store, run: str) -> RunRow:
    found = await store.run(run)
    assert found is not None, f'no row for {run}'
    return found


async def the_store_keeps_runs(store: Store, *, postgres: bool = False) -> None:
    tag = uuid4().hex[:8]
    first, second = f'run_t{tag}a', f'run_t{tag}b'
    now = datetime.now(UTC)
    await store.save_run(
        RunRow(id=first, agent=f'desk-{tag}', channel='text', started_at=now - timedelta(minutes=1))
    )
    await store.save_run(RunRow(id=second, agent=f'other-{tag}', channel='voice', started_at=now))
    events = [
        {
            'run': first,
            'seq': n,
            'turn': n // 2,
            'type': 'caller.speech',
            't_ms': float(n),
            'text': f'line {n}',
        }
        for n in range(5)
    ]
    for event in events:
        await store.add_event(first, event)
    snapshot = {'run_id': first, 'turn': 1, 'agent': 'front', 'facts': {'topic': 'billing'}, 'messages': []}
    await store.save_snapshot(first, 1, snapshot)
    await store.save_config(ConfigRow(agent=f'desk-{tag}', version='v1', path='a.yaml', manifest='x: 1'))
    await store.save_config(
        ConfigRow(agent=f'desk-{tag}', version='v2', path='a.yaml', manifest='x: 2', parent='v1')
    )

    assert (await row(store, first)).agent == f'desk-{tag}'
    assert await store.events(first) == events, 'events in order, as they went in'
    assert await store.snapshot(first, 1) == snapshot and await store.snapshot_turns(first) == [1]
    listed = [r.id for r in await store.runs() if r.id in (first, second)]
    assert listed == [second, first], 'newest first'
    assert [r.id for r in await store.runs(f'desk-{tag}')] == [first], 'filtered by agent'

    if postgres:
        assert isinstance(store, ServiceStore)
        await asyncio.sleep(0.5)
        for key in (f'studio:run:{first}', f'studio:run:{first}:events', f'studio:run:{first}:snap:1'):
            await store.redis.delete(key)
        assert (await row(store, first)).agent == f'desk-{tag}', 'the row read back from Postgres'
        assert await store.events(first) == events, 'the events read back from Postgres'
        assert await store.snapshot(first, 1) == snapshot, 'the snapshot read back from Postgres'

    versions = await store.configs(f'desk-{tag}')
    assert {(c.version, c.parent) for c in versions} == {('v1', None), ('v2', 'v1')}, versions
    if postgres:
        assert isinstance(store, ServiceStore)
        async with store.pool.acquire() as db:
            await db.execute('DELETE FROM runs WHERE id = ANY($1)', [first, second])
            await db.execute('DELETE FROM agent_configs WHERE agent_id = $1', f'desk-{tag}')
        await store.redis.delete(f'studio:run:{second}', f'studio:run:{first}')
        await store.redis.zrem('studio:runs', first, second)
        return
    closed = await store.close_unfinished()
    assert closed == 2 and (await row(store, first)).outcome == 'stopped', (
        'left live by a stopped studio: closed'
    )


async def main() -> None:
    await the_store_keeps_runs(MemoryStore())
    settings = Settings.from_env()
    store = ServiceStore(settings.redis_url, settings.database_url)
    try:
        await asyncio.wait_for(store.open(), 5)
    except Exception as error:
        print(
            f'skipped Redis and Postgres: not reachable ({type(error).__name__}); `slng studio up` starts them'
        )
        return
    try:
        await the_store_keeps_runs(store, postgres=True)
        print('redis and postgres: the same, and Postgres holds what Redis held')
    finally:
        await store.close()


if __name__ == '__main__':
    asyncio.run(main())
    print(
        'store ok: rows, events in order, snapshots, newest first by agent, closed when left live, versions'
    )
