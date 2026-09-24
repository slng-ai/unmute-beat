"""The salon's conversation: core's acts (`core.conversation`), given the salon's information.

Everything here is information a use case gives core's classes: who is
talking, which questions are read back and what a yes runs, where the times
on the table come from, what a pick and a take-back do. The readings, the
order the acts are tried in and how a reply is read are core's, the same for
every business. Each act's reading runs only when the call's state says the
act can happen this turn:

| Reading | Act | Runs when | Classes |
|---|---|---|---|
| `CONFIRMATION` | `ReadBack` | a phone, booking or complaint read-back is waiting | accept, reject, revise, conditional, unclear |
| `APPOINTMENT_CHOICE` | `FixedChoice` | the caller was asked to move or add, or holds a booking while times are on the table; on "anything else?" only once the booking tool asks | move, add, or neither |
| `CHOSEN_OFFER` | `Offer` | times are on the table, and no read-back or choice is waiting; on "anything else?" only once the booking tool asks | one slot id, or none |
| `LAST_CHANGE` | `TakeBack` | the caller still holds a booking made in this call | keep, withdraw, unclear |
| `RETAINED_APPROVAL` | `HeldApproval` | a complaint approved on an earlier turn is kept through the number check | keep, withdraw, unclear |
| `CONVERSATION` | `Closing` | a question other than a read-back is waiting, or the model asks to hang up | end, continue, unclear |
| `DETOUR` | `Detour` | the number read-back is waiting during the check | kept, changed, unclear |

A number said in full while nobody is identified is read back by code, with
no reading at all (`HeardValue`).

Whichever readings ran, the tool guards read one merged view,
`SalonCall.classification` (`reading_of`). `SOLVER`, behind
`SALON_INTERPRETER=solver`, reads the reply as one decision from a wider set
instead, and writes into the same view, so no guard changes either way.
"""

import os
from typing import Literal

from pydantic_ai.models import infer_model
from pydantic_ai.models.google import GoogleModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits

from core.classifiers import Classifier
from core.conversation import (
    Closing,
    Conversation,
    Detour,
    FixedChoice,
    HeardValue,
    HeldApproval,
    Offer,
    ReadBack,
    TakeBack,
)
from core.decisions import Accept, Conditional, EndCall, NoDecision, Reject, Revise
from core.graph import LocalAction
from core.interpreters import ClassifierInterpreter
from core.readings import Option, Reading
from core.state import Held
from salon.models.consent import (
    Asked,
    ChooseAppointment,
    CorrectPhone,
    Destination,
    OfferInput,
    ReplyClassification,
    SalonSolution,
    SalonSwitch,
    SolverInput,
)
from salon.models.state import SalonCall
from salon.tools.asking import Ask
from salon.tools.booking import booking_question, cancel_question, write_booking
from salon.tools.complaints import write_complaint
from salon.tools.desk import end_conversation
from salon.tools.identity import lookup_customer, number_heard, readback_number

MODEL = infer_model('google-cloud:gemini-3.5-flash-lite')
"""One model for now. `build` takes it per agent, so giving one step a different model is a one-word edit."""
SETTINGS = GoogleModelSettings(
    service_tier='priority',
    google_thinking_config={'thinking_level': 'MINIMAL'},  # ty: ignore[invalid-argument-type]
)
"""Thinking pinned off rather than left out.

Left out, no `thinking_config` is sent and whatever Google currently defaults
this model to is what a call gets, a setting that can change under us on the
one dimension a voice turn cannot absorb.
"""

INTERPRETER: Literal['classifier', 'solver'] = (
    'solver' if os.environ.get('SALON_INTERPRETER') == 'solver' else 'classifier'
)
"""Which interpreter reads replies. Read at call time, so a test can switch it with `patch.object`.

ponytail: the solver stays opt-in until `evals/compare.py` has run live on
the reviewed cases and its false acceptances are no worse than the
classifier's. Flip the default here once they are.
"""


def _verified(call: SalonCall) -> bool:
    return bool(call.state.customer_verified)


def _add(call: SalonCall, question: Asked) -> LocalAction:
    return LocalAction('save_booking', lambda: write_booking(call, 'book', question.value, ''))


def _move(call: SalonCall, question: Asked) -> LocalAction | None:
    if not question.booking_id:
        return None
    return LocalAction(
        'save_booking', lambda: write_booking(call, 'move', question.value, question.booking_id)
    )


def _could_move(call: SalonCall) -> bool:
    """A caller who holds a booking and names a time on the table may be moving it: "Saturday, same time?".

    No question asked it, and a move needs their word that it is a move.
    """
    question = call.pending_question()
    picking = (question is None or question.about in ('offer', 'closing')) and bool(call.table())
    return picking and bool(call.holding())


MOVE_OR_ADD = (
    Option(id='move', said='Move the appointment they already hold to the new time.'),
    Option(id='add', said='Keep the appointment they hold and add the new one as well.'),
)


def _picked(call: SalonCall, slot: str) -> LocalAction | None:
    """A plain pick of a time on the table is read back, with no model request; only the yes to it books.

    A caller who already holds a booking is asked first whether this moves it
    or adds another: the write's own guard puts that question and refuses.
    """
    if not call.state.customer_verified:
        return None
    if call.holding():
        return LocalAction('save_booking', lambda: write_booking(call, 'book', slot, ''))
    return LocalAction('ask_booking', lambda: _read_back_pick(call, slot))


async def _read_back_pick(call: SalonCall, slot: str) -> Ask:
    return booking_question(call, 'book', slot, '')


def _still_booked(call: SalonCall, held: Held) -> bool:
    return call.undo_booking == held.key


def _took_back(call: SalonCall, held: Held) -> LocalAction | None:
    """A booking made in this call that the caller takes back is offered for cancelling, on whatever turn.

    Coval run Y47Ut7oC, K2: "Leave it, thanks" two turns after the booking was
    read as the goodbye it also was, and hung up on it. Not while that cancel
    is itself the question waiting: then the answer answers it, and a yes has
    already cancelled (Coval run gdw96FkH, "Yes, please cancel it" re-asked
    three times).
    """
    question = call.pending_question()
    if question is not None and question.action == 'cancel' and question.booking_id == held.key:
        return None
    return LocalAction('ask_booking', lambda: _cancel_question(call, held.key))


async def _cancel_question(call: SalonCall, booking_id: str) -> Ask:
    """The application's own cancel read-back for a booking the caller has just taken back.

    A cancel question can be answered from whichever step the caller is in,
    because the question is the authority and the write's own guard checks it:
    the caller who took back a picked time is back at the front desk by then.
    """
    return cancel_question(call, booking_id)


async def _record_approved(call: SalonCall, question: Asked) -> object:
    approved = call.approved_complaint
    assert approved is not None
    return await write_complaint(call, approved.summary, approved.requested_resolution)


def _unidentified(call: SalonCall) -> bool:
    """A number said in full is read back with no model only while the caller is being identified."""
    return call.state.agent == 'verify' and not call.state.customer_verified


COMPLAINT_KEPT = HeldApproval[SalonCall](
    name='retained_approval',
    kind='complaint',
    about='complaint',
    context='The caller approved a complaint to record, and the salon is checking their number before it '
    'records it.',
    keep=lambda call, question: call.approve_complaint(question),
)
"""A yes to the complaint read-back, kept, bound to the caller it was put to, until it is recorded or withdrawn."""


class SalonConversation(Conversation[SalonCall]):
    """The salon's acts, whose rules read the merged view, so the opt-in solver stands in for every reading."""

    def label(self, call: SalonCall, name: str) -> str:
        return getattr(call.classification, name)

    def view(self, call: SalonCall) -> ReplyClassification:
        """This turn's readings as the one view the tool guards read. A reading that did not run reads as its default."""
        if INTERPRETER == 'solver':
            return consent_of(SOLVER.current(call), call.held('complaint') is not None)
        labels = self.labels(call)
        return ReplyClassification.model_validate(
            {
                'confirmation': labels['confirmation'],
                'appointment_choice': labels['appointment_choice'] or 'unclear',
                'conversation': labels['conversation'],
                'retained_approval': labels['retained_approval'],
                'last_change': labels['last_change'],
                'chosen_offer': labels['chosen_offer'],
            }
        )

    async def read(
        self,
        call: SalonCall,
        *,
        usage: RunUsage | None = None,
        usage_limits: UsageLimits | None = None,
        closing: bool = False,
        before_model: bool = False,
    ) -> None:
        """Read the reply with whichever interpreter is live: the narrow readings, or the opt-in solver."""
        if INTERPRETER == 'solver':
            await SOLVER.interpret(call, usage=usage, usage_limits=usage_limits)
            return
        await super().read(
            call, usage=usage, usage_limits=usage_limits, closing=closing, before_model=before_model
        )

    async def read_reply(
        self,
        call: SalonCall,
        *,
        usage: RunUsage | None = None,
        usage_limits: UsageLimits | None = None,
        closing: bool = False,
        before_model: bool = False,
    ) -> ReplyClassification:
        """Read the reply, then return the merged view.

        `before_model` is `advance` reading the turn before any model runs. On
        "anything else?" it leaves out the two choice readings: across the Coval
        runs of 24 September they ran on every closing reply and never decided
        one (0 of 6), while a goodbye paid for both. A move asked for there
        ("can we move it to Saturday, same time?") still gets them, read by the
        booking tool before it writes (`WriteBooking._authorise`), against the
        times the model has just put on the table. Consumers recheck the view
        against the live call through `SalonCall.classification`; a late or
        rebound answer is never published.
        """
        await self.read(
            call, usage=usage, usage_limits=usage_limits, closing=closing, before_model=before_model
        )
        return call.classification


SALON = SalonConversation(
    context='A caller is on the phone with the receptionist of Sage and Stone, a hair salon.',
    model=MODEL,
    settings=SETTINGS,
    read_back_context='The receptionist read something back and is waiting for a yes.',
    read_backs=(
        ReadBack(
            about='phone',
            action='confirm_customer',
            run=lambda call, question: lookup_customer(call, question.value),
            when=lambda call, question: call.state.agent == 'verify',
        ),
        ReadBack(
            about='booking',
            action='save_booking',
            run=lambda call, question: write_booking(
                call, question.action, question.value, question.booking_id
            ),
            when=lambda call, question: _verified(call) and question.action in ('book', 'move', 'cancel'),
        ),
        ReadBack(
            about='complaint',
            action='record_complaint',
            run=_record_approved,
            when=lambda call, question: call.state.agent == 'complaints' and call.complaint_approved,
        ),
    ),
    choices=(
        FixedChoice(
            name='appointment_choice',
            about='move_or_add',
            options=MOVE_OR_ADD,
            context='The caller already holds an appointment. Read whether the answer moves it or adds another. '
            'A bare yes picks neither.',
            actions={'add': _add, 'move': _move},
            also=_could_move,
            when=_verified,
        ),
    ),
    offers=(
        Offer[SalonCall, OfferInput](
            name='chosen_offer',
            about='offer',
            also_on=('closing',),
            options=SalonCall.table,
            input_type=OfferInput,
            facts=lambda call: {'holding': call.holding()},
            context='The caller was offered appointment times. A time they name, or "the same time" as one '
            'in `holding`, picks it.',
            on_pick=_picked,
        ),
    ),
    values=(
        HeardValue(
            about='phone',
            action='ask_number',
            parse=number_heard,
            read_back=readback_number,
            when=_unidentified,
        ),
    ),
    take_backs=(
        TakeBack(
            name='last_change',
            kind='undo',
            context='Earlier in this call the salon booked an appointment for the caller, which they can still '
            'take back.',
            on_withdraw=_took_back,
            still_holds=_still_booked,
        ),
    ),
    held=(COMPLAINT_KEPT,),
    detour=Detour(
        name='request_change',
        ends_on='phone',
        context='The caller asked for something, then the salon stopped to check who they are.',
    ),
    closing=Closing(
        goodbye=lambda call: end_conversation(call, 'Thanks for calling. Goodbye.'),
        skip_before_model=('appointment_choice', 'chosen_offer'),
    ),
)
"""The salon, as information given to core's acts. The order inside each slot is the order tried."""

CONFIRMATION = SALON.reading('confirmation')
APPOINTMENT_CHOICE = SALON.reading('appointment_choice')
CHOSEN_OFFER = SALON.reading('chosen_offer')
LAST_CHANGE = SALON.reading('last_change')
RETAINED_APPROVAL = SALON.reading('retained_approval')
CONVERSATION = SALON.reading('conversation')
DETOUR = SALON.reading('request_change')
READINGS: tuple[Reading, ...] = SALON.readings
"""Every reading that runs on its own when it applies. `CONVERSATION` joins them only as `classify_reply` says."""
READ_BACKS = SALON.read_back_abouts
"""The questions that wait on a yes to an exact proposal."""


def reading_of(call: SalonCall) -> ReplyClassification:
    """This turn's readings as the one view the tool guards read. See `SalonConversation.view`."""
    return SALON.view(call)


async def classify_reply(
    call: SalonCall,
    *,
    usage: RunUsage | None = None,
    usage_limits: UsageLimits | None = None,
    closing: bool = False,
    before_model: bool = False,
) -> ReplyClassification:
    """Run every reading that applies to this turn, then return the merged view. See `SalonConversation.read_reply`."""
    return await SALON.read_reply(
        call, usage=usage, usage_limits=usage_limits, closing=closing, before_model=before_model
    )


SOLVER_INSTRUCTIONS = (
    'Read the caller utterance as exactly one decision about the pending proposal, using the class '
    'descriptions in the output schema. Accept only when delivered is true and the caller clearly '
    'approves that exact proposal. Use switch_task only for one of the listed destinations. '
    'Never invent digits.'
)
"""The solver's whole prompt. The decisions' own descriptions carry the rest, in the schema.

ponytail: a new prompt, checked only offline. It must pass the live
comparison before `INTERPRETER` defaults to the solver.
"""

SOLVER_CLASSIFIER = Classifier(
    MODEL, SolverInput, SalonSolution, context=SOLVER_INSTRUCTIONS, model_settings=SETTINGS
)

DESTINATIONS = (
    Destination(
        name='booking',
        purpose='Book, move or cancel an appointment.',
        needs='The caller is identified by phone number first.',
    ),
    Destination(
        name='complaints',
        purpose='Record a complaint about a past visit.',
        needs='The caller is identified by phone number first.',
    ),
    Destination(name='concierge', purpose='General questions: opening hours, prices, services, location.'),
)
"""Where a caller can switch to, as the solver is told. Legality right now is checked again when a switch is applied."""


def consent_of(solution: SalonSolution, retained: bool) -> ReplyClassification:
    """The consent reading a decision amounts to, so every existing guard reads the solver unchanged."""
    decision = solution.decision
    confirmation = {
        Accept: 'accept',
        Reject: 'reject',
        Revise: 'revise',
        CorrectPhone: 'revise',
        Conditional: 'conditional',
    }.get(type(decision), 'unclear')
    withdrawn = isinstance(decision, SalonSwitch | EndCall)
    return ReplyClassification.model_validate(
        {
            'confirmation': confirmation,
            'appointment_choice': decision.choice if isinstance(decision, ChooseAppointment) else 'unclear',
            'conversation': 'end' if isinstance(decision, EndCall) else 'continue',
            'retained_approval': ('withdraw' if withdrawn else 'keep') if retained else 'unclear',
        }
    )


class SalonSolver(ClassifierInterpreter[SalonCall, SolverInput, SalonSolution]):
    """One decision per reply, from the proposal, the step, and where the caller could go instead."""

    def _input(self, call: SalonCall) -> SolverInput:
        last = call.last_proposal()
        return SolverInput(
            answer=call.heard,
            question=call.said_last_turn(),
            step=call.state.agent,
            owner=call.state.customer_phone,
            proposal=last[0] if last else None,
            delivered=bool(last and last[1]),
            retained_complaint=call.approved_complaint,
            destinations=tuple(d for d in DESTINATIONS if d.name != call.state.agent),
        ).model_copy(deep=True)

    def _still_valid(self, call: SalonCall, value: SolverInput) -> bool:
        last = call.last_proposal()
        proposal: Asked | None = last[0] if last else None
        return (
            value.answer == call.heard
            and value.owner == call.state.customer_phone
            and value.step == call.state.agent
            and value.proposal == proposal
        )

    def _settle(self, call: SalonCall, value: SolverInput, result: SalonSolution) -> SalonSolution:
        """An undelivered proposal cannot be agreed to, whatever the model read; then keep or drop the complaint."""
        if not value.delivered and isinstance(result.decision, Accept | ChooseAppointment):
            result = SalonSolution(decision=NoDecision())
        reading = consent_of(result, call.held('complaint') is not None)
        if call.held('complaint') is not None and reading.retained_approval != 'keep':
            call.release('complaint')
        COMPLAINT_KEPT.approve(call, reading.confirmation)
        return result


SOLVER = SalonSolver('solver', SOLVER_CLASSIFIER, SalonSolution(decision=NoDecision()))
