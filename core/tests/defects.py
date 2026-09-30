"""Known defects: checks that fail today on purpose, kept out of `python -m tests`.

Run: uv run python -m tests.defects

Each check states the behaviour the harness should have. It prints
`KNOWN DEFECT` and the session that owns the repair while the defect is still
there, and `fixed` once it is gone. The exit code is the number of defects
still present, so a repair can be seen landing, and then the check moves into
`tests/__main__.py`, where a regression fails the suite.

Not in the suite, because a suite that is red by design stops being read, and
not deleted, because a defect with no reproducible check is only a rumour.

Every check drives a public entry point (`Operation.run`, or a compiled clinic
manifest through `text_turn`), so the same check runs unchanged on the parent
and on the repair: the before-and-after evidence of session 4b stage B.
"""

import asyncio
import contextlib
import os
import time
from collections.abc import Callable
from pathlib import Path

import pydantic_ai.models
from pydantic import BaseModel

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
os.environ.setdefault('GOOGLE_API_KEY', 'offline-test-placeholder')

HERE = Path(__file__).parent / 'clinic_flow'


class _Args(BaseModel):
    value: str = 'sample'


class _Receipt(BaseModel):
    value: str
    status: str


def _probe(function: Callable, reconcile: Callable | None = None):
    """A declared operation that needs no approval, so the post-authorisation path is all that runs."""
    from core.steps import FlowCall, FlowState, FunctionOperation

    class Probe(FunctionOperation):
        name = 'probe'
        args_type = _Args
        receipt_type = _Receipt
        needs_approval = False

    Probe.function = staticmethod(function)
    if reconcile is not None:
        Probe.reconcile = staticmethod(reconcile)
    return Probe(), FlowCall(state=FlowState(agent='desk', facts=_Args()))


async def _runs(operation, call) -> str:
    try:
        await operation.run(call, _Args())
    except asyncio.CancelledError:
        return 'cancelled'
    except Exception as error:
        return type(error).__name__
    return 'ok'


def cancelled_after_commit_is_sent_again() -> bool:
    """Session-4 review finding 1: the backend commits, then the awaiting task is cancelled.

    Nobody knows the outcome, so the same operation for the same arguments
    must not be sent to the backend again until it is reconciled. The parent
    marked the attempt failed and forgot it, so a second run committed twice.
    """
    committed: list[str] = []

    async def commit_then_cancel(services, args):
        committed.append(args.value)
        raise asyncio.CancelledError

    async def check() -> bool:
        operation, call = _probe(commit_then_cancel)
        await _runs(operation, call)
        await _runs(operation, call)
        return len(committed) > 1

    return asyncio.run(check())


def invalid_reconciliation_clears_the_guard() -> bool:
    """Session-4 review finding 1, second half: a malformed reconciliation answer must keep the retry guard.

    The parent removed the unknown record before validating the answer, so
    the next run sent the operation again.
    """
    from core import OutcomeUnknownError

    sent: list[str] = []

    def lost(services, args):
        sent.append(args.value)
        raise OutcomeUnknownError('the backend did not answer')

    async def check() -> bool:
        operation, call = _probe(lost, reconcile=lambda services, args: {'wrong': 'receipt'})
        await _runs(operation, call)
        await _runs(operation, call)
        await _runs(operation, call)
        return len(sent) > 1

    return asyncio.run(check())


def lookup_receipt_backs_a_booking_goodbye() -> bool:
    """Session-4 review finding 3 (Coval call cYMMixAc): a lookup receipt let a goodbye claim a booking.

    The salon's `verify` step carries out `lookup_customer` itself and then
    says goodbye. Here the clinic's front desk does the same: its own lookup
    (no approval) files a receipt, and the goodbye then points at a recap
    saying an appointment was booked. No booking was made, so the caller
    must never hear that sentence.
    """
    import core
    from tests.clinic_flow.clinic import Backend, ClinicState
    from tests.script import Call, Script

    claim = 'Your appointment is booked for Tuesday at ten. Goodbye!'
    manifest = HERE / '_defect_lookup_goodbye.yaml'
    manifest.write_text(
        (HERE / 'code_node.yaml')
        .read_text()
        .replace(
            "    exits:\n      - {to: look_up, purpose: find the caller's booking once their patient number is recorded}\n"
            '  look_up: {kind: action, operation: look_up, returns: true, on_error: front, on_unknown: front}\n',
            '    operations: [look_up]\n',
        )
    )

    async def check() -> bool:
        backend = Backend()
        backend.bookings['CL007'] = ('P-1234', '1000')
        script = Script()
        flow = core.load(manifest).compile(model=script.model)
        call = flow.open(ClinicState(), services=backend)
        script.call = call
        script.moves = [Call('look_up', update={'patient': 'P-1234'})]
        reply = await core.text_turn(flow, call, 'P-1234, where is my booking?')
        call.acknowledge(*(s.id for s in reply.segments))
        assert [e.operation for e in call.evidence] == ['look_up'], call.evidence
        script.moves = [Call('goodbye', recap=[claim]), Call('goodbye', recap=[claim])]
        await core.text_turn(flow, call, 'Thanks, bye.')
        return any('booked for Tuesday' in s.text for s in call.all_spoken)

    try:
        return asyncio.run(check())
    finally:
        manifest.unlink()


def _desk(folder: Path, **open_with):
    """The support desk on a fresh SQLite file: its scripted model, compiled flow and one open call."""
    from tests.test_tickets import desk_in, opened

    desk = desk_in(folder)
    return (desk, *opened(desk, **open_with))


def pending_receipt_becomes_a_refusal() -> bool:
    """Stage B review finding 1: a schema-valid `pending` receipt is not an authoritative no.

    The parent filed it as `rejected`, which ended the intent and let a new
    intent with the same arguments be dispatched: a second effect while the
    first may still land.
    """
    sent: list[str] = []

    def pending(services, args):
        sent.append(args.value)
        return {'value': args.value, 'status': 'pending'}

    async def check() -> bool:
        operation, call = _probe(pending)
        type(operation).succeeds = {'status': 'done'}
        await _runs(operation, call)
        await _runs(operation, call)
        return len(sent) > 1

    return asyncio.run(check())


def concurrent_runs_dispatch_twice() -> bool:
    """Stage B review finding 2: two runs of one intent on one call both reach the backend.

    The parent checked eligibility, awaited the durable intent record, and
    only then marked the intent dispatched, so a second run passed the check
    during the first one's await.
    """
    sent: list[str] = []

    class Services:
        async def note_intent(self, intent):
            await asyncio.sleep(0.01)

    def once(services, args):
        sent.append(args.value)
        time.sleep(0.05)
        return {'value': args.value, 'status': 'done'}

    async def check() -> bool:
        operation, call = _probe(once)
        call._services = Services()
        await asyncio.gather(_runs(operation, call), _runs(operation, call))
        return len(sent) > 1

    return asyncio.run(check())


def restore_resurrects_a_superseded_effect() -> bool:
    """Stage B review finding 3: resolve, reopen, then a fresh session restores the old resolve intent.

    The ticket is open at revision 3. The parent reconciled the resolve last,
    so it stood as the current effect and was said to the caller.
    """
    import tempfile

    from tests.script import Call, Reply
    from tests.test_tickets import turn
    from tests.ticket_flow.worker import to_read_back

    async def check(folder: Path) -> bool:
        desk, script, flow, call = _desk(folder)
        await to_read_back(flow, call, script)
        await turn(flow, call, script, 'Sí.', Call('resolve_ticket'))
        await turn(flow, call, script, 'Reábrelo.', Call('reopen_ticket'))
        await turn(flow, call, script, 'Sí.', Call('reopen_ticket'))
        [resolve] = [i for i in call.intents if i.operation == 'resolve_ticket']
        assert desk.ticket('T-101') == (3, 'open')
        from tests.test_tickets import opened

        script, flow, fresh = opened(desk, intents=[resolve.model_copy(update={'status': 'dispatched'})])
        said = await turn(flow, fresh, script, 'Hola.', Reply('Dime.'))
        stands = [e.receipt.get('status') for e in fresh.effective()]
        return 'resolved' in stands or any('resuelto' in s for s in said)

    with tempfile.TemporaryDirectory() as tmp:
        return asyncio.run(check(Path(tmp)))


def resources_of_two_kinds_collide() -> bool:
    """Stage B review finding 4: ticket 1 and address 1 are one resource to the parent.

    Effective receipts were keyed by the subject's value alone, so a change
    to address 1 superseded the change to ticket 1.
    """
    from core.steps import FlowCall, FlowState, FunctionOperation

    class Thing(BaseModel):
        id: str = '1'

    class Changed(BaseModel):
        id: str
        status: str

    def change(services, args):
        return {'id': args.id, 'status': 'done'}

    def kind(name: str):
        class Change(FunctionOperation):
            args_type = Thing
            receipt_type = Changed
            needs_approval = False

        Change.name, Change.subject = name, 'id'
        Change.resource = name.removeprefix('update_')
        Change.function = staticmethod(change)
        return Change()

    async def check() -> bool:
        call = FlowCall(state=FlowState(agent='desk', facts=Thing()))
        await kind('update_ticket').run(call, Thing())
        await kind('update_address').run(call, Thing())
        return len(call.effective()) < 2

    return asyncio.run(check())


def estimated_playback_authorises() -> bool:
    """Stage B review finding 5: audio written out locally counted as a heard read-back.

    `LocalPlayout` and a far end with no marks record `local`. The parent
    counted it as delivered, so a voice call with no playback evidence could
    approve a write.
    """
    from tests.test_delivery import READBACK, Call, asked

    call = Call()
    call.turn_started('I want a haircut')
    copy = asked(call)
    name = call.mark_for(copy)
    call.note_mark_sent(name, 100.0)
    call.spoken[-1].at = 99.0
    call.note_mark_returned(name, 101.0, local=True)
    call.note_onset(101.5)
    call.turn_started('yes')
    return call.pending(READBACK.about, value=READBACK.value) is not None


def a_restated_value_books() -> bool:
    """Session 4d, defect 1: the caller refuses a heard booking read-back, and the model records the slot again.

    Recording a value is not agreeing to a write. The parent compared the
    recorded slot with the pending read-back, called the equal value a yes,
    and booked, saying "Yes, that's the same as I read back."
    """
    import core
    from tests.clinic_flow.clinic import Backend, ClinicState
    from tests.script import Call, Reply, Script, turn

    async def check() -> bool:
        script = Script()
        flow = core.load(HERE / 'reads_back.yaml').compile(model=script.model)
        call = flow.open(ClinicState(), services=Backend())
        script.call = call
        asked = await turn(
            flow,
            call,
            script,
            'P-1234 at ten.',
            Call('options', update={'patient': 'P-1234'}),
            Call('book', update={'slot': '1000'}),
        )
        assert asked[-1].startswith('Shall I book Tuesday at 10:00'), ('the booking was not read back', asked)
        said = await turn(
            flow,
            call,
            script,
            'No, do not book ten. I am only checking if it is free.',
            Reply('Ten is free. Nothing is booked.', slot='1000'),
        )
        return bool(call.services.bookings) or call.locale.restated in said

    return asyncio.run(check())


def a_pick_for_the_new_state_is_refused() -> bool:
    """R21 on a structured step: a category and the resolution right for it, in one update, are refused.

    "Es de acceso: que me desbloqueen la cuenta", with billing's resolutions
    on offer. The resolution rests on the category, and this update changes
    it, so the offer it could come from is access's. The prose `collect`
    path had the choice function make that offer first
    (`ConversationStep._record`); `StepFlow.record(atomic=True)` checks the
    pick against the offer standing now, billing's, and refuses the whole
    update. The caller is asked again for what they already said.
    """
    import tempfile

    from tests.script import Call, Reply
    from tests.test_tickets import HERE as DESK
    from tests.test_tickets import turn

    async def check(folder: Path) -> bool:
        _, script, flow, call = _desk(folder, manifest=DESK / 'choices.yaml')
        await turn(flow, call, script, 'Cuenta ACC-1234.', Reply('Vale.', account_id='ACC-1234'))
        await turn(flow, call, script, '¿Qué tickets tengo?', Call('options'), Reply('¿Cuál?'))
        await turn(flow, call, script, 'El T-101.', Reply('Vale.', ticket_id='T-101'))
        await turn(
            flow,
            call,
            script,
            'Facturación.',
            Call('options', update={'category': 'billing'}),
            Reply('¿Cuál?'),
        )
        await turn(
            flow,
            call,
            script,
            'No, es de acceso: que me desbloqueen la cuenta.',
            Reply('Vale.', category='access', resolution='unlock'),
        )
        facts = call.state.facts
        return (facts.category, facts.resolution) != ('access', 'unlock')

    with tempfile.TemporaryDirectory() as tmp:
        return asyncio.run(check(Path(tmp)))


def chained_picks_in_one_update_are_refused() -> bool:
    """R23 on a structured step: a ticket, its category and the category's resolution, supplied in one update.

    The prose `collect` path offered each child for its fresh parent, in
    dependency order, and kept every pick the new offers held
    (`ConversationStep._record`). `StepFlow.record(atomic=True)` checks each
    pick against the offers standing before the update, where there are
    none yet, and refuses the whole update.
    """
    import tempfile

    from tests.script import Reply
    from tests.test_tickets import HERE as DESK
    from tests.test_tickets import turn

    async def check(folder: Path) -> bool:
        _, script, flow, call = _desk(folder, manifest=DESK / 'chained.yaml')
        await turn(
            flow,
            call,
            script,
            'Cuenta ACC-1234, T-101, facturación, un reembolso.',
            Reply('Vale.', account_id='ACC-1234', ticket_id='T-101', category='billing', resolution='refund'),
        )
        facts = call.state.facts
        return (facts.ticket_id, facts.category, facts.resolution) != ('T-101', 'billing', 'refund')

    with tempfile.TemporaryDirectory() as tmp:
        return asyncio.run(check(Path(tmp)))


class _Versioned(BaseModel):
    value: str
    status: str
    rev: int = 0


def a_cancel_during_the_version_check_sends_again() -> bool:
    """Session 4d, defect 2: a lost answer is reconciled as a success; the current-version check is cancelled.

    The parent settled the intent as succeeded and forgot it was unknown
    before awaiting `current_version`, so a cancellation there left no
    evidence, no unknown record and no repair: the next run sent a new write.
    """
    from core import OutcomeUnknownError
    from core.steps import FlowCall, FlowState, FunctionOperation

    sent: list[str] = []

    def lost(services, args):
        sent.append(args.value)
        raise OutcomeUnknownError('the answer was lost after the commit')

    class Services:
        async def current_version(self, kind, resource):
            await asyncio.Event().wait()

    class Probe(FunctionOperation):
        name = 'probe'
        args_type = _Args
        receipt_type = _Versioned
        needs_approval = False
        subject, resource, version = 'value', 'thing', 'rev'

    Probe.succeeds = {'status': 'done'}

    Probe.function = staticmethod(lost)
    Probe.reconcile = staticmethod(lambda services, args: {'value': args.value, 'status': 'done', 'rev': 1})

    async def check() -> bool:
        operation = Probe()
        call = FlowCall(state=FlowState(agent='desk', facts=_Args()))
        call._services = Services()
        await _runs(operation, call)
        settle = call.settlement()
        assert settle is not None
        task = asyncio.ensure_future(settle())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await _runs(operation, call)
        return len(sent) > 1

    return asyncio.run(check())


def old_speech_is_sent_as_the_last_words() -> bool:
    """Live call CAd33c3c (session 4d, call 3): lines from turns 2 and 6 were sent as the agent's latest words.

    A line whose run was cancelled has no response of its own in the history.
    Projection put every such line just before the latest request, on every
    request for the rest of the call. The model read "sorry no" as the answer
    to "Anything else I can help with?", said one turn earlier, and said goodbye.
    """
    from pydantic_ai.messages import ModelResponse, TextPart

    from tests.test_history import drifted_history

    said = [
        p.content
        for m in drifted_history()
        if isinstance(m, ModelResponse)
        for p in m.parts
        if isinstance(p, TextPart)
    ]
    return said[-1] != 'For Wednesday I have nine.'


DEFECTS: dict[str, tuple[Callable[[], bool], str]] = {
    "old speech is sent as the agent's last words": (old_speech_is_sent_as_the_last_words, '4d live call 3'),
    'a restated value books without a yes': (a_restated_value_books, '4d defect 1'),
    'a cancel during the version check sends the write again': (
        a_cancel_during_the_version_check_sends_again,
        '4d defect 2',
    ),
    'a pending receipt becomes an authoritative refusal': (pending_receipt_becomes_a_refusal, '4c C0.1'),
    'two concurrent runs of one intent both dispatch': (concurrent_runs_dispatch_twice, '4c C0.2'),
    'a restored intent resurrects a superseded effect': (restore_resurrects_a_superseded_effect, '4c C0.3'),
    'resources of two kinds with one id collide': (resources_of_two_kinds_collide, '4c C0.4'),
    'estimated local playback authorises a write': (estimated_playback_authorises, '4c C0.5'),
    'a write cancelled after its commit is sent again': (cancelled_after_commit_is_sent_again, '4b stage B1'),
    'an invalid reconciliation answer clears the retry guard': (
        invalid_reconciliation_clears_the_guard,
        '4b stage B1',
    ),
    'a lookup receipt backs a goodbye that claims a booking': (
        lookup_receipt_backs_a_booking_goodbye,
        '4b stage B2',
    ),
    'a pick right for the state an update ends in is refused on a structured step': (
        a_pick_for_the_new_state_is_refused,
        'v2-only cleanup',
    ),
    'chained picks supplied in one update are refused on a structured step': (
        chained_picks_in_one_update_are_refused,
        'v2-only cleanup',
    ),
}
"""Each defect, its check (True while the defect is present) and the session that owns the repair.

Session 1's three (a read-back never recorded, one queued and never played,
and core loading `.env` at import) were repaired in session 2 and now fail
the suite if they return: `tests.test_delivery` and `tests.test_package`.
"""


def main() -> int:
    present = 0
    for name, (check, session) in DEFECTS.items():
        if check():
            present += 1
            print(f'KNOWN DEFECT (session {session}): {name}')
        else:
            print(f'fixed: {name}')
    return present


if __name__ == '__main__':
    raise SystemExit(main())
