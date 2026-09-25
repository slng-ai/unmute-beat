"""The three things that change the salon, each run the one safe way.

`Write.run` is the only path to the diary's write methods: take the approved
proposal off the table, call the diary once under the proposal's token,
validate what comes back into its receipt, and only then commit it to the
call. Each attempt leaves a `write` span shaped like the harness's
(`core/operations.py`), so the ledger (`concierge-tools/ledger.py`) matches every write to the
question it answered.
"""

import json
from abc import ABC, abstractmethod
from typing import Any, ClassVar, final

from opentelemetry import trace
from pydantic import BaseModel, ValidationError

from graph_salon.call import RefusalError, SalonCall
from graph_salon.models import BookingChoice, ComplaintChoice, PhoneChoice, Proposal
from salon.diary import RefusedError
from salon.models.receipts import ComplaintRecord, CustomerLookup, Saved

UNCLEAR = (
    'It is not clear whether that saved. Tell the caller you will check and confirm it, '
    'and do not try it again on this call.'
)

_tracer = trace.get_tracer('voice.graph_salon')


class Write[ReceiptT: BaseModel](ABC):
    """One business action. Subclasses say how to call the diary and what the receipt changes; `run` does the rest."""

    name: ClassVar[str]
    """The tool name the ledger knows this write by."""
    kind: ClassVar[str]
    """The kind of proposal that authorises it."""
    receipt: ClassVar[type[BaseModel]]

    @final
    def run(self, call: SalonCall) -> ReceiptT:
        """Do it, or raise `RefusalError` with something the model can say. Nothing is committed unless a receipt validates.

        Raises:
            RefusalError: No approved proposal, the diary said no, or its answer did not validate.
        """
        proposal = call.approve(self.kind)
        audit: dict[str, Any] = {
            'action': self.name,
            'args': proposal.choice.model_dump(mode='json'),
            'question': proposal.question,
            'about': proposal.choice.kind,
            'asked_turn': proposal.asked_turn,
            'turn': call.state.turn,
            'heard': call.state.heard,
            'status': 'failed',
        }
        with _tracer.start_as_current_span('write') as span:
            try:
                raw = self._execute(call, proposal)
                receipt: ReceiptT = self.receipt.model_validate(raw)  # ty: ignore[invalid-assignment]
                self._record(call, proposal, receipt)
                audit.update(status='ok', receipt=receipt.model_dump(mode='json'))
                return receipt
            except RefusedError as error:
                raise RefusalError(str(error)) from error
            except ValidationError as error:
                audit['status'] = 'unknown'
                raise RefusalError(UNCLEAR) from error
            finally:
                span.set_attribute('write.audit', json.dumps(audit, default=str))

    @abstractmethod
    def _execute(self, call: SalonCall, proposal: Proposal) -> object:
        """Call the diary once, under the proposal's token. Return its raw answer."""

    @abstractmethod
    def _record(self, call: SalonCall, proposal: Proposal, receipt: ReceiptT) -> None:
        """Commit what the receipt proves. Raise `RefusalError` if it proves the write did not happen."""


class LookupCustomer(Write[CustomerLookup]):
    name = 'confirm_customer'
    kind = 'phone'
    receipt = CustomerLookup

    def _execute(self, call: SalonCall, proposal: Proposal) -> object:
        assert isinstance(proposal.choice, PhoneChoice)
        return call.diary.find_or_create_customer(proposal.choice.phone)

    def _record(self, call: SalonCall, proposal: Proposal, receipt: CustomerLookup) -> None:
        call.commit(phone=receipt.customer_phone)


class SaveBooking(Write[Saved]):
    name = 'save_booking'
    kind = 'booking'
    receipt = Saved

    def _execute(self, call: SalonCall, proposal: Proposal) -> object:
        choice = proposal.choice
        assert isinstance(choice, BookingChoice)
        return call.diary.save_booking(
            call.state.phone,
            choice.action,
            choice.slot_id,
            choice.booking_id,
            choice.additional,
            call.diary.now(),
            operation_token=proposal.token,
            offered_to=call.id,
        )

    def _record(self, call: SalonCall, proposal: Proposal, receipt: Saved) -> None:
        """A move or cancel must come back about the booking it named; a book or move spends the times offered."""
        choice = proposal.choice
        assert isinstance(choice, BookingChoice)
        if choice.action != 'book' and receipt.booking_id != choice.booking_id:
            raise RefusalError(UNCLEAR)
        if choice.action != 'cancel':
            call.diary.withdraw_offers(call.id)
        call.commit(appointment=receipt, receipt_turn=call.state.turn)


class RecordComplaint(Write[ComplaintRecord]):
    name = 'record_complaint'
    kind = 'complaint'
    receipt = ComplaintRecord

    def _execute(self, call: SalonCall, proposal: Proposal) -> object:
        choice = proposal.choice
        assert isinstance(choice, ComplaintChoice)
        return call.diary.record_complaint(
            call.state.phone, choice.summary, choice.requested_resolution, operation_token=proposal.token
        )

    def _record(self, call: SalonCall, proposal: Proposal, receipt: ComplaintRecord) -> None:
        if not receipt.recorded:
            raise RefusalError('The complaint book did not take it. Nothing was recorded.')
        call.commit(complaint=receipt, receipt_turn=call.state.turn)


WRITES: dict[str, Write] = {
    write.kind: write for write in (LookupCustomer(), SaveBooking(), RecordComplaint())
}
"""The write each kind of proposal authorises. One instance each: they hold no call data."""
