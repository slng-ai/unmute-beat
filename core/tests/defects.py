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
import os
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
    (no approval) files a receipt, and the model's goodbye then says an
    appointment was booked. No booking was made, so the caller must never
    hear that sentence.
    """
    import core
    from tests.clinic_flow.clinic import Backend, ClinicState
    from tests.test_nodes import Script

    claim = 'Your appointment is booked for Tuesday at ten. Goodbye!'
    manifest = HERE / '_defect_lookup_goodbye.yaml'
    manifest.write_text(
        (HERE / 'code_node.yaml')
        .read_text()
        .replace(
            "    exits:\n      - {to: look_up, purpose: find the caller's booking once their patient number is recorded}\n"
            '  - {id: look_up, kind: action, operation: look_up, returns: true, on_error: front, on_unknown: front}\n',
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
        script.moves = [
            ('call', 'collect', {'values': {'patient': 'P-1234'}}),
            ('call', 'look_up', {}),
        ]
        reply = await core.text_turn(flow, call, 'P-1234, where is my booking?')
        call.acknowledge(*(s.id for s in reply.segments))
        assert [e.operation for e in call.evidence] == ['look_up'], call.evidence
        script.moves = [('call', 'goodbye', {'words': claim}), ('call', 'goodbye', {'words': claim})]
        await core.text_turn(flow, call, 'Thanks, bye.')
        return any('booked for Tuesday' in s.text for s in call.all_spoken)

    try:
        return asyncio.run(check())
    finally:
        manifest.unlink()


DEFECTS: dict[str, tuple[Callable[[], bool], str]] = {
    'a write cancelled after its commit is sent again': (cancelled_after_commit_is_sent_again, '4b stage B1'),
    'an invalid reconciliation answer clears the retry guard': (
        invalid_reconciliation_clears_the_guard,
        '4b stage B1',
    ),
    'a lookup receipt backs a goodbye that claims a booking': (
        lookup_receipt_backs_a_booking_goodbye,
        '4b stage B2',
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
