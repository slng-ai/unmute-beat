"""Stage B1: one intent per business action, and what may happen to it after it could have been dispatched.

Every check drives `Operation.run` through a declared `FunctionOperation`,
the path every manifest operation takes, with a plain in-memory backend:

* I1: a commit followed by a cancellation leaves the intent unknown, is
  re-raised, and is never sent again until reconciliation settles it;
* I2: a reconciliation answer that does not validate, is about somebody else,
  or is None leaves the guard in place; a valid one settles it once;
* I3: an idempotent backend is sent the same intent again, as itself;
* I4: a function running in a thread that commits after its awaiting task
  was cancelled is found by reconciliation, and not sent twice;
* I5: a correction is a new intent, a replay is the same one, and a new yes
  after a success is a new intent with the same arguments;
* I6: while one intent of an operation is unknown, a different one is refused;
* I7: a success whose recording fails is kept, and recorded again with
  nothing sent;
* I8: intents restored in a fresh session are reconciled first, and an id
  reused for different arguments is refused;
* I9: after the caller said goodbye no new change starts, and reconciliation
  of an earlier one still runs;
* I10: the manifest checks the `Intent` parameter, `subject` and `idempotent`.
"""

import asyncio
import threading
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import ModelRetry

import core
from core import Intent, OutcomeUnknownError
from core.steps import FlowCall, FlowState, FunctionOperation

HERE = Path(__file__).parent / 'clinic_flow'


class Args(BaseModel):
    item: str = 'A'


class Receipt(BaseModel):
    item: str
    status: str


class Store:
    """A backend that keeps what it did by intent id, as a real one would keep an idempotency key."""

    def __init__(self) -> None:
        self.done: dict[str, dict[str, str]] = {}
        self.sent: list[str] = []


def operation(function, *, reconcile=None, idempotent=False, changes=True, takes_intent=True):
    class Probe(FunctionOperation):
        name = 'probe'
        args_type = Args
        receipt_type = Receipt
        needs_approval = False

    Probe.matches, Probe.succeeds = {'item': 'item'}, {'status': 'done'}
    Probe.function = staticmethod(function)
    Probe.function_takes_intent = takes_intent
    Probe.idempotent = idempotent
    Probe.changes = changes
    if reconcile is not None:
        Probe.reconcile = staticmethod(reconcile)  # ty: ignore[invalid-assignment]
        Probe.reconcile_takes_intent = True
    return Probe()


def session(store: Store) -> FlowCall:
    call = FlowCall(state=FlowState(agent='desk', facts=Args()))
    call._services = store
    return call


def find(store: Store, args: Args, intent: Intent) -> dict[str, str] | None:
    return store.done.get(intent.id)


async def outcome(op, call, args: Args | None = None) -> str:
    try:
        await op.run(call, args or Args())
    except asyncio.CancelledError:
        return 'cancelled'
    except OutcomeUnknownError:
        return 'unknown'
    except ModelRetry:
        return 'refused'
    return 'ok'


async def committed_then_cancelled():
    """I1."""
    store = Store()

    async def commit_then_cancel(store: Store, args: Args, intent: Intent):
        store.sent.append(intent.id)
        store.done[intent.id] = {'item': args.item, 'status': 'done'}
        raise asyncio.CancelledError

    op = operation(commit_then_cancel, reconcile=find)
    call = session(store)
    assert await outcome(op, call) == 'cancelled', 'a cancellation after dispatch was swallowed'
    [intent] = call.intents
    assert intent.status == 'unknown', intent
    assert await outcome(op, call) == 'ok', 'reconciliation did not settle it'
    assert store.sent == [intent.id], 'the intent was sent again'
    assert [i.status for i in call.intents] == ['succeeded']
    assert [(e.intent, e.succeeded) for e in call.evidence] == [(intent.id, True)]


async def reconciliation_must_validate():
    """I2."""
    for answer, name in (
        ({'wrong': 'shape'}, 'malformed'),
        ({'item': 'B', 'status': 'done'}, 'about another item'),
        (None, 'not found'),
    ):
        store = Store()

        def lost(store: Store, args: Args, intent: Intent):
            store.sent.append(intent.id)
            raise TimeoutError('no answer')

        op = operation(lost, reconcile=lambda store, args, intent, answer=answer: answer)
        call = session(store)
        assert await outcome(op, call) == 'unknown', name
        for _ in range(3):
            assert await outcome(op, call) == 'unknown', name
        settle = call.settlement()
        assert settle is not None, name
        await settle()
        assert len(store.sent) == 1, (name, 'the guard was cleared and the intent sent again')
        assert [i.status for i in call.intents] == ['unknown'], name
        assert not call.evidence, (name, 'an unusable answer was filed as evidence')


async def idempotent_backend_gets_the_same_intent():
    """I3."""
    store = Store()
    first = True

    def dedups(store: Store, args: Args, intent: Intent):
        nonlocal first
        store.sent.append(intent.id)
        store.done.setdefault(intent.id, {'item': args.item, 'status': 'done'})
        if first:
            first = False
            raise ConnectionError('dropped')
        return store.done[intent.id]

    op = operation(dedups, idempotent=True)
    call = session(store)
    assert await outcome(op, call) == 'unknown'
    assert await outcome(op, call) == 'ok'
    assert len(set(store.sent)) == 1 and len(store.sent) == 2, 'an idempotent retry must reuse the intent id'
    assert len(store.done) == 1


async def a_thread_commits_after_cancellation():
    """I4: the awaiting task is cancelled while the function's thread is still working; the thread then commits."""
    store = Store()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow(store: Store, args: Args, intent: Intent):
        store.sent.append(intent.id)
        started.set()
        release.wait(5)
        store.done[intent.id] = {'item': args.item, 'status': 'done'}
        finished.set()
        return store.done[intent.id]

    op = operation(slow, reconcile=find)
    call = session(store)
    task = asyncio.create_task(op.run(call, Args()))
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError('the cancellation did not reach the caller')
    assert [i.status for i in call.intents] == ['unknown'] and not store.done
    release.set()
    await asyncio.to_thread(finished.wait, 5)
    assert store.done, 'the thread was expected to commit after the cancellation'
    assert await outcome(op, call) == 'ok'
    assert len(store.sent) == 1 and [i.status for i in call.intents] == ['succeeded']


async def one_intent_per_action():
    """I5: the same arguments while open are one intent; a changed argument, or a new yes after success, is another."""
    call = session(Store())
    a = call.intent_for('probe', Args(item='A').model_dump_json())
    again = call.intent_for('probe', Args(item='A').model_dump_json())
    corrected = call.intent_for('probe', Args(item='B').model_dump_json())
    assert a.id == again.id and corrected.id != a.id
    call.set_intent(a.id, status='succeeded')
    later = call.intent_for('probe', Args(item='A').model_dump_json())
    assert later.id != a.id, 'a new action after a success reused the finished intent'
    try:
        call.set_intent(a.id, status='unknown')
    except ValueError:
        pass
    else:
        raise AssertionError('a terminal intent was reopened')


async def a_different_intent_waits_for_an_unknown_one():
    """I6."""
    store = Store()

    def lost(store: Store, args: Args, intent: Intent):
        store.sent.append(args.item)
        raise TimeoutError

    op = operation(lost, reconcile=find)
    call = session(store)
    assert await outcome(op, call, Args(item='A')) == 'unknown'
    assert await outcome(op, call, Args(item='B')) == 'unknown'
    assert store.sent == ['A'], 'a second request went out while the first one was unknown'


async def a_success_that_fails_to_record_is_repaired():
    """I7."""
    store = Store()

    def works(store: Store, args: Args, intent: Intent):
        store.sent.append(intent.id)
        return {'item': args.item, 'status': 'done'}

    op = operation(works)
    type(op).assign = {'missing_field': 'item'}
    call = session(store)
    try:
        await op.run(call, Args())
    except ValueError:
        pass
    else:
        raise AssertionError('the failing projection was expected to raise')
    assert [i.status for i in call.intents] == ['succeeded'] and len(call.evidence) == 1
    assert call._unpresented, 'the success was not kept for repair'
    type(op).assign = {}
    assert await outcome(op, call) == 'ok'
    assert len(store.sent) == 1 and not call._unpresented, 'the repair sent it again'


async def restored_intents_reconcile_first():
    """I8."""
    store = Store()
    op = operation(lambda store, args, intent: store.sent.append(intent.id), reconcile=find)
    lost = Intent(id='f' * 32, operation='probe', args=Args().model_dump_json(), status='dispatched')
    store.done[lost.id] = {'item': 'A', 'status': 'done'}
    call = session(store)
    call.restore([lost], {'probe': (op, Args)})
    assert [i.status for i in call.intents] == ['unknown'], 'a dispatched intent must come back unknown'
    assert await outcome(op, call) == 'ok'
    assert store.sent == [] and [e.intent for e in call.evidence] == [lost.id]
    try:
        call.restore(
            [lost.model_copy(update={'args': Args(item='B').model_dump_json()})], {'probe': (op, Args)}
        )
    except ValueError:
        pass
    else:
        raise AssertionError('one intent id was accepted for two different arguments')
    try:
        Intent.model_validate({'id': 'x', 'operation': 'probe', 'args': '{}', 'readbacks': '1'})
    except ValueError:
        pass
    else:
        raise AssertionError('an intent from a store was coerced instead of refused')


async def no_new_change_after_goodbye():
    """I9."""
    store = Store()

    def lost(store: Store, args: Args, intent: Intent):
        store.sent.append(intent.id)
        store.done[intent.id] = {'item': args.item, 'status': 'done'}
        raise TimeoutError

    op = operation(lost, reconcile=find)
    call = session(store)
    assert await outcome(op, call, Args(item='A')) == 'unknown'
    call.note_departure()
    assert await outcome(op, call, Args(item='A')) == 'ok', (
        'reconciling earlier work after goodbye must still run'
    )
    assert await outcome(op, call, Args(item='C')) == 'refused', 'a new change started after goodbye'
    assert len(store.sent) == 1


def manifest_checks_the_intent_parameter():
    """I10."""
    text = (HERE / 'agent.yaml').read_text()
    broken = HERE / '_intent_broken.yaml'
    for change, expected in (
        (('    subject: reference\n', '    subject: nowhere\n'), "subject 'nowhere'"),
        (
            ('reconcile: clinic.py:find_booking', 'reconcile: clinic.py:find_booking\n    idempotent: true'),
            'Intent',
        ),
    ):
        broken.write_text(text.replace(*change, 1))
        try:
            core.load(broken).compile(model='test')
        except core.FlowSpecError as error:
            assert expected in str(error), str(error)
        else:
            raise AssertionError(f'{change} was accepted')
        finally:
            broken.unlink()


def main() -> None:
    for check in (
        committed_then_cancelled,
        reconciliation_must_validate,
        idempotent_backend_gets_the_same_intent,
        a_thread_commits_after_cancellation,
        one_intent_per_action,
        a_different_intent_waits_for_an_unknown_one,
        a_success_that_fails_to_record_is_repaired,
        restored_intents_reconcile_first,
        no_new_change_after_goodbye,
    ):
        asyncio.run(check())
    manifest_checks_the_intent_parameter()
    print('intents ok: I1-I10')


if __name__ == '__main__':
    main()
