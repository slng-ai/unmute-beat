"""Stage C0.1 to C0.4: what an answer settles, who may send an intent, and which change stands.

* O1: a receipt that says `pending` (or anything the application did not
  declare terminal) leaves the intent unknown: nothing new is sent, and a
  later reconciliation settles it;
* O2: a declared `rejects` is an authoritative no, so a new intent may go;
* O3: after dispatch a plain `ModelRetry` is unknown; the application's
  `Refused` is a no;
* O4: two runs of one intent on one call send it once; a cancellation
  before dispatch leaves it known not sent, and during it unknown;
* O5: a durable record that fails leaves it known not sent, and free to go;
* O6: a restored intent never resurrects a change a later one superseded,
  in any restore order, restored twice, or after a second change; a
  finished intent is never reopened by a restore;
* O7: ticket 1 and address 1 are two resources; resolve and reopen of one
  ticket still supersede each other;
* O8: the loader checks `rejects`, `version` and `resource`;
* O9: once a caller is known mid-call, what calls now over owe them is settled first (`take_owed`).

Every check drives `Operation.run` or a compiled manifest through `text_turn`.
"""

import asyncio
import tempfile
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import ModelRetry

import core
from core import Intent
from core.operations import Refused
from core.steps import FlowCall, FlowState, FunctionOperation
from tests.test_intents import Args, Store, operation, outcome, session
from tests.test_tickets import HERE as DESK
from tests.test_tickets import desk_in, opened, turn
from tests.ticket_flow.worker import to_read_back


def statuses(call) -> list[str]:
    return [i.status for i in call.intents]


async def a_pending_answer_settles_nothing():
    """O1."""
    store = Store()
    answers = ['pending', 'done']

    def slow(store: Store, args: Args, intent: Intent):
        store.sent.append(intent.id)
        return {'item': args.item, 'status': answers[0]}

    def later(store: Store, args: Args, intent: Intent):
        return {'item': args.item, 'status': answers.pop(0)}

    op = operation(slow, reconcile=later)
    call = session(store)
    assert await outcome(op, call) == 'unknown', 'a pending answer was taken as final'
    [intent] = call.intents
    assert intent.status == 'unknown' and not call.evidence, (intent, call.evidence)
    assert await outcome(op, call) == 'unknown', 'reconciliation said pending again, and it settled'
    assert store.sent == [intent.id] and len(call.intents) == 1, (
        'a pending intent was sent again, or a new one made'
    )
    assert await outcome(op, call) == 'ok'
    assert statuses(call) == ['succeeded'] and store.sent == [intent.id]


async def a_declared_no_is_final():
    """O2."""
    store = Store()

    def full(store: Store, args: Args, intent: Intent):
        store.sent.append(intent.id)
        return {'item': args.item, 'status': 'full'}

    op = operation(full)
    type(op).rejects = {'status': ['full', 'closed']}
    call = session(store)
    assert await outcome(op, call) == 'refused'
    assert statuses(call) == ['rejected'] and call.evidence[0].succeeded is False
    assert await outcome(op, call) == 'refused'
    assert statuses(call) == ['rejected', 'rejected'] and len(set(store.sent)) == 2, (
        'a new intent was not sent'
    )


async def a_retry_signal_after_dispatch_is_unknown():
    """O3."""
    for raised, expected, status in (
        (ModelRetry('try again'), 'unknown', 'unknown'),
        (Refused('no'), 'refused', 'rejected'),
    ):
        store = Store()

        def signal(store: Store, args: Args, intent: Intent, raised=raised):
            store.sent.append(intent.id)
            raise raised

        op = operation(signal)
        call = session(store)
        assert await outcome(op, call) == expected, raised
        assert statuses(call) == [status], (raised, call.intents)
        assert await outcome(op, call) in ('unknown', 'refused')
        assert len(store.sent) == (1 if status == 'unknown' else 2), (raised, store.sent)


class Slow:
    """A backend whose durable intent record takes a moment, so a second run can arrive while it waits."""

    def __init__(self) -> None:
        self.done: dict[str, dict[str, str]] = {}
        self.sent: list[str] = []
        self.noted: list[str] = []
        self.gate = asyncio.Event()

    async def note_intent(self, intent: Intent) -> None:
        self.noted.append(intent.id)
        await self.gate.wait()


async def one_intent_is_sent_once():
    """O4."""
    store = Slow()

    def send(store: Slow, args: Args, intent: Intent):
        store.sent.append(intent.id)
        return {'item': args.item, 'status': 'done'}

    op = operation(send)
    call = FlowCall(state=FlowState(agent='desk', facts=Args()))
    call._services = store
    first = asyncio.create_task(outcome(op, call))
    await asyncio.sleep(0)
    assert await outcome(op, call) == 'unknown', 'a second run of the same intent was not refused'
    store.gate.set()
    assert await first == 'ok' and len(store.sent) == 1, store.sent

    for where in ('record', 'send'):
        store = Slow()
        started = asyncio.Event()

        async def hang(store: Slow, args: Args, intent: Intent, started: asyncio.Event = started):
            store.sent.append(intent.id)
            started.set()
            await asyncio.sleep(10)

        op = operation(hang)
        call = FlowCall(state=FlowState(agent='desk', facts=Args()))
        call._services = store
        if where == 'send':
            store.gate.set()
        task = asyncio.create_task(outcome(op, call))
        if where == 'record':
            while not store.noted:
                await asyncio.sleep(0)
        else:
            await started.wait()
        task.cancel()
        assert await task == 'cancelled'
        [intent] = call.intents
        if where == 'record':
            assert intent.status == 'prepared' and not store.sent, ('cancelled before dispatch', intent)
            assert not call._claimed, 'the claim outlived its run'
        else:
            assert intent.status == 'unknown' and store.sent == [intent.id], (
                'cancelled during dispatch',
                intent,
            )


async def a_failed_record_sends_nothing():
    """O5."""

    class Broken(Store):
        def note_intent(self, intent: Intent) -> None:
            raise OSError('disk full')

    store = Broken()

    def send(store: Store, args: Args, intent: Intent):
        store.sent.append(intent.id)
        return {'item': args.item, 'status': 'done'}

    op = operation(send)
    call = session(store)
    assert await outcome(op, call) == 'refused' and not store.sent
    assert statuses(call) == ['prepared'] and not call._claimed


async def resolved_reopened(folder: Path):
    """A desk call that resolved T-101 (revision 2) and then reopened it (revision 3): its desk and intents."""
    desk = desk_in(folder)
    script, flow, call = opened(desk)
    await to_read_back(flow, call, script)
    await turn(flow, call, script, 'Sí.', ('call', 'resolve_ticket', {}))
    await turn(flow, call, script, 'Reábrelo.', ('call', 'reopen_ticket', {}))
    await turn(flow, call, script, 'Sí.', ('call', 'reopen_ticket', {}))
    assert desk.ticket('T-101') == (3, 'open')
    by = {i.operation: i.model_copy(update={'status': 'dispatched'}) for i in call.intents}
    return desk, by['resolve_ticket'], by['reopen_ticket']


async def a_restore_never_resurrects(folder: Path):
    """O6."""
    desk, resolve, reopen = await resolved_reopened(folder / 'one')
    for order in ([resolve], [reopen, resolve], [resolve, reopen], [resolve, resolve]):
        script, flow, call = opened(desk, intents=order)
        said = await turn(flow, call, script, 'Hola.', ('say', 'Dime.'))
        latest = {e.receipt['status'] for e in call.evidence if call.is_latest(e.kind, e.resource, e.version)}
        assert not any('resuelto' in s for s in said), (order, said)
        assert 'resolved' not in latest, (order, latest)
        assert all(i.status == 'succeeded' for i in call.intents), (order, call.intents)
        assert {e.receipt['status'] for e in call.evidence} == {'resolved', 'open'} & {
            'resolved' if resolve in order else '',
            'open' if reopen in order else '',
        }, ('history was not kept', order, call.evidence)
        if reopen in order:
            assert latest == {'open'} and 'Ticket T-101 reabierto. ¿Algo más?' in said, (order, said)
    assert desk.sent() == 2, 'a restore sent something'

    script, flow, call = opened(desk)
    await to_read_back(flow, call, script)
    await turn(flow, call, script, 'Sí.', ('call', 'resolve_ticket', {}))
    assert desk.ticket('T-101') == (4, 'resolved')
    script, flow, fresh = opened(desk, intents=[reopen])
    said = await turn(flow, fresh, script, 'Hola.', ('say', 'Dime.'))
    assert not any('reabierto' in s for s in said), ('a stale reopen was said', said)
    assert not [e for e in fresh.evidence if fresh.is_latest(e.kind, e.resource, e.version)], (
        'a stale reopen stood'
    )

    script, flow, call = opened(desk_in(folder / 'held'))
    await to_read_back(flow, call, script)
    await turn(flow, call, script, 'Sí.', ('call', 'resolve_ticket', {}))
    [done] = [i for i in call.intents if i.operation == 'resolve_ticket']
    call.restore([done.model_copy(update={'status': 'dispatched'})], {})
    assert [i.status for i in call.intents if i.id == done.id] == ['succeeded'], (
        'a restore reopened a finished intent'
    )


class Thing(BaseModel):
    id: str = '1'


class Changed(BaseModel):
    id: str
    status: str
    version: int


def change_of(name: str, kind: str):
    """An operation changing a `kind` resource by id, whose backend numbers versions per (kind, id)."""
    versions: dict[tuple[str, str], int] = {}

    def change(services, args: Thing):
        key = (kind, args.id)
        versions[key] = versions.get(key, 0) + 1
        return {'id': args.id, 'status': name, 'version': versions[key]}

    class Change(FunctionOperation):
        args_type = Thing
        receipt_type = Changed
        needs_approval = False

    Change.name, Change.subject, Change.resource, Change.version = name, 'id', kind, 'version'
    Change.function = staticmethod(change)
    return Change(), versions


async def kinds_keep_ids_apart():
    """O7."""
    call = FlowCall(state=FlowState(agent='desk', facts=Thing()))
    ticket, _ = change_of('resolve', 'ticket')
    address, _ = change_of('update', 'address')
    await ticket.run(call, Thing())
    await address.run(call, Thing())
    assert sorted((e.kind, e.resource) for e in call.effective()) == [('address', '1'), ('ticket', '1')]
    reopen, _ = change_of('reopen', 'ticket')
    reopen.function = staticmethod(lambda s, a: {'id': a.id, 'status': 'reopen', 'version': 2})
    await reopen.run(call, Thing())
    stands = sorted((e.kind, e.receipt['status']) for e in call.effective())
    assert stands == [('address', 'update'), ('ticket', 'reopen')], stands


def the_loader_checks_it(folder: Path):
    """O8."""
    base = (DESK / 'agent.yaml').read_text()
    cases = {
        'rejects on nothing': base.replace('rejects: {status: stale}', 'rejects: {colour: red}', 1),
        'succeeds and rejects': base.replace(
            'rejects: {status: stale}', 'rejects: {status: [stale, resolved]}', 1
        ),
        'no version': base.replace('    version: now_revision\n', '', 1),
        'version on nothing': base.replace('version: now_revision', 'version: colour', 1),
    }
    for name, text in cases.items():
        copy = folder / name.replace(' ', '_')
        copy.mkdir()
        for path in DESK.iterdir():
            if path.is_file():
                (copy / path.name).write_text(path.read_text())
        (copy / 'prompts').mkdir()
        for path in (DESK / 'prompts').iterdir():
            (copy / 'prompts' / path.name).write_text(path.read_text())
        (copy / 'agent.yaml').write_text(text)
        try:
            core.load(copy / 'agent.yaml').compile()
        except core.FlowSpecError:
            continue
        raise AssertionError(f'the loader took a manifest with {name}')


async def owed_once_known(folder: Path):
    """O9: a caller known only mid-call is owed what a dead worker left: settled first, never sent again.

    A worker commits a resolve and is killed before it answers (the R7
    worker). A new call opens knowing nobody. Once the caller's account is
    recorded, the desk's services say what is owed to it (`owed`), and the
    next turn reconciles it, by its intent, before any model: the caller hears
    the receipt, and the desk is not sent it again.
    """
    import subprocess
    import sys

    desk = desk_in(folder)
    died = subprocess.run(
        [sys.executable, '-m', 'tests.ticket_flow.worker', str(folder / 'desk.db'), 'after'],
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert died.returncode == 3, died.stderr[-2000:]
    asked: list[str] = []

    def owed(identity: str) -> list[Intent]:
        asked.append(identity)
        return [i for i in desk.unresolved() if i.subject == identity]

    desk.owed = owed
    script, flow, call = opened(desk)
    assert not call.intents and asked == [], 'owed was asked with nobody known'
    await turn(
        flow,
        call,
        script,
        'Hola, cuenta ACC-1234, el ticket T-101.',
        ('call', 'hand_over', {'to': 'desk', 'because': 'un ticket'}),
        ('call', 'collect', {'values': {'account_id': 'ACC-1234', 'ticket_id': 'T-101'}}),
        ('call', 'wait', {}),
    )
    assert asked == ['ACC-1234'] and [i.status for i in call.intents] == ['unknown'], (asked, call.intents)
    said = await turn(flow, call, script, '¿Se resolvió?', ('call', 'wait', {}))
    assert said[0] == 'Ticket T-101 resuelto. ¿Algo más?', said
    assert desk.sent() == 1 and [i.status for i in call.intents] == ['succeeded'], 'owed was sent again'


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        await owed_once_known(Path(tmp))
    await a_pending_answer_settles_nothing()
    await a_declared_no_is_final()
    await a_retry_signal_after_dispatch_is_unknown()
    await one_intent_is_sent_once()
    await a_failed_record_sends_nothing()
    with tempfile.TemporaryDirectory() as tmp:
        for sub in ('one', 'held'):
            (Path(tmp) / sub).mkdir()
        await a_restore_never_resurrects(Path(tmp))
    await kinds_keep_ids_apart()
    with tempfile.TemporaryDirectory() as tmp:
        the_loader_checks_it(Path(tmp))
    print('outcomes ok: O1-O9')


if __name__ == '__main__':
    asyncio.run(main())
