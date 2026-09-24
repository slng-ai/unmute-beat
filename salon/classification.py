"""The salon's semantic decision boundary, independent of its conversational agents.

A caller's reply is read by narrow readings (`core.readings`), each one small
classifier for one act, run only when the call's state says that act can
happen this turn:

| Reading | Runs when | Classes |
|---|---|---|
| `CONFIRMATION` | a phone, booking or complaint read-back is waiting | accept, reject, revise, conditional, unclear |
| `APPOINTMENT_CHOICE` | the caller was asked to move or add, or holds a booking while times are on the table; on "anything else?" only once the booking tool asks | move, add, or neither |
| `CHOSEN_OFFER` | times are on the table, and no read-back or choice is waiting; on "anything else?" only once the booking tool asks | one slot id, or none |
| `LAST_CHANGE` | the caller still holds a booking made in this call | keep, withdraw, unclear |
| `RETAINED_APPROVAL` | a complaint approved on an earlier turn is kept through the number check | keep, withdraw, unclear |
| `CONVERSATION` | a question other than a read-back is waiting, or the model asks to hang up | end, continue, unclear |
| `DETOUR` | the number read-back is waiting during the check | kept, changed, unclear |

The classes and their descriptions are core's, the same for every business.
The salon writes only when each reading runs, one context sentence, and what
the application does with each label (`salon.nodes.ACTS`). The readings that
apply run side by side, so a turn waits for the slowest, not for their sum.

Whichever readings ran, the tool guards read one merged view,
`SalonCall.classification` (`reading_of`). `SOLVER`, behind
`SALON_INTERPRETER=solver`, reads the reply as one decision from a wider set
instead, and writes into the same view, so no guard changes either way.
"""

import os
from typing import TYPE_CHECKING, Literal

from pydantic_ai.models import infer_model
from pydantic_ai.models.google import GoogleModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits

from core.classifiers import Classifier, Exchange
from core.decisions import Accept, Conditional, EndCall, NoDecision, Reject, Revise
from core.interpreters import ClassifierInterpreter
from core.readings import (
    CLOSING,
    CONFIRM,
    REQUEST,
    STILL_AGREED,
    TAKE_BACK,
    ChoiceInput,
    DetourInput,
    Option,
    ReadBackInput,
    Reading,
    TakeBackInput,
    choose,
    classify,
    read,
)
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

if TYPE_CHECKING:
    from salon.models.state import SalonCall


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

SALON = 'A caller is on the phone with the receptionist of Sage and Stone, a hair salon.'
"""Who is talking. Every reading's context starts here. Prompt text."""

READ_BACKS = ('phone', 'booking', 'complaint')
"""The questions that wait on a yes to an exact proposal."""


def _read_back(call: SalonCall) -> ReadBackInput | None:
    question = call.pending_question()
    if question is None or question.about not in READ_BACKS:
        return None
    return ReadBackInput(question=call.said_last_turn(), answer=call.heard, proposal=question.spoken)


def _approve_complaint(call: SalonCall, label: str) -> None:
    """A yes to the complaint read-back is kept, bound to the caller it was put to, until it is recorded or withdrawn."""
    if (question := call.pending('complaint')) and label == 'accept':
        call._complaint = question.model_copy(update={'owner': call.state.customer_phone})


CONFIRMATION = classify(
    'confirmation',
    CONFIRM,
    ReadBackInput,
    _read_back,
    context=f'{SALON} The receptionist read something back and is waiting for a yes.',
    model=MODEL,
    settings=SETTINGS,
    settle=_approve_complaint,
)

MOVE_OR_ADD = (
    Option(id='move', said='Move the appointment they already hold to the new time.'),
    Option(id='add', said='Keep the appointment they hold and add the new one as well.'),
)


def _move_or_add(call: SalonCall) -> ChoiceInput | None:
    """When the caller was asked to move or add, or holds a booking and could be picking a time to move it to.

    The second is the caller's own "can we move it to Saturday, same time?":
    no question asked it, and a move needs their word that it is a move.
    """
    question = call.pending_question()
    asked = question is not None and question.about == 'move_or_add'
    picking = (question is None or question.about in ('offer', 'closing')) and bool(call.table())
    if not asked and not (picking and call.holding()):
        return None
    return ChoiceInput(question=call.said_last_turn(), answer=call.heard, options=MOVE_OR_ADD)


APPOINTMENT_CHOICE = choose(
    'appointment_choice',
    ChoiceInput,
    _move_or_add,
    context=f'{SALON} The caller already holds an appointment. Read whether the answer moves it or adds another. '
    'A bare yes picks neither.',
    model=MODEL,
    settings=SETTINGS,
)


def _offers(call: SalonCall) -> OfferInput | None:
    """The times on the table, unless a read-back or a move-or-add question is what the caller is answering."""
    question = call.pending_question()
    if question is not None and question.about not in ('offer', 'closing'):
        return None
    if not (table := call.table()):
        return None
    return OfferInput(
        question=call.said_last_turn(), answer=call.heard, options=table, holding=call.holding()
    )


CHOSEN_OFFER = choose(
    'chosen_offer',
    OfferInput,
    _offers,
    context=f'{SALON} The caller was offered appointment times. A time they name, or "the same time" as one '
    'in `holding`, picks it.',
    model=MODEL,
    settings=SETTINGS,
)


def _held_change(call: SalonCall) -> TakeBackInput | None:
    if not call.undo_booking or call._undo is None:
        return None
    return TakeBackInput(question=call.said_last_turn(), answer=call.heard, agreed=call._undo[1])


LAST_CHANGE = classify(
    'last_change',
    TAKE_BACK,
    TakeBackInput,
    _held_change,
    context=f'{SALON} Earlier in this call the salon booked an appointment for the caller, which they can still '
    'take back.',
    model=MODEL,
    settings=SETTINGS,
)


def _kept_complaint(call: SalonCall) -> TakeBackInput | None:
    """A complaint approved on an earlier turn. Not on the turn of the yes itself, which that reading already read."""
    complaint = call._complaint
    if complaint is None or complaint.turn + 1 >= call.turn:
        return None
    return TakeBackInput(question=call.said_last_turn(), answer=call.heard, agreed=complaint.spoken)


def _forget_complaint(call: SalonCall, label: str) -> None:
    """Anything but a clear keep drops the approval: the complaint is read back again."""
    if label != 'keep':
        call._complaint = None


RETAINED_APPROVAL = classify(
    'retained_approval',
    STILL_AGREED,
    TakeBackInput,
    _kept_complaint,
    context=f'{SALON} The caller approved a complaint to record, and the salon is checking their number before it '
    'records it.',
    model=MODEL,
    settings=SETTINGS,
    settle=_forget_complaint,
)


def _closing(call: SalonCall) -> Exchange:
    """The words of the question waiting, which is what the caller answers, or else the last thing said."""
    question = call.pending_question()
    said = question.spoken if question is not None and question.spoken else call.said_last_turn()
    return Exchange(question=said, answer=call.heard)


CONVERSATION = classify(
    'conversation',
    CLOSING,
    Exchange,
    _closing,
    context=SALON,
    model=MODEL,
    settings=SETTINGS,
)


def _detour(call: SalonCall) -> DetourInput | None:
    """Only on the reply to the number read-back: the turn a return from the check can happen."""
    if not call.detour_request or call.pending('phone') is None:
        return None
    return DetourInput(
        question=call.detour_speech(), answer=' '.join(call.detour_words()), request=call.detour_request
    )


def _request_kept(call: SalonCall, label: str) -> None:
    if label == 'kept':
        call.note_request_kept()


DETOUR = classify(
    'request_change',
    REQUEST,
    DetourInput,
    _detour,
    context=f'{SALON} The caller asked for something, then the salon stopped to check who they are.',
    model=MODEL,
    settings=SETTINGS,
    settle=_request_kept,
)

READINGS: tuple[Reading, ...] = (
    CONFIRMATION,
    APPOINTMENT_CHOICE,
    CHOSEN_OFFER,
    LAST_CHANGE,
    RETAINED_APPROVAL,
    DETOUR,
)
"""Every reading that runs on its own when it applies. `CONVERSATION` joins them only as `classify_reply` says."""

CLOSING_DEFERS = (APPOINTMENT_CHOICE, CHOSEN_OFFER)
"""The readings `advance` leaves to the booking tool on "anything else?" (see `classify_reply`)."""


def reading_of(call: SalonCall) -> ReplyClassification:
    """This turn's readings as the one view the tool guards read. A reading that did not run reads as its default."""
    if INTERPRETER == 'solver':
        return consent_of(SOLVER.current(call), call._complaint is not None)
    choice = APPOINTMENT_CHOICE.label(call)
    return ReplyClassification.model_validate(
        {
            'confirmation': CONFIRMATION.label(call),
            'appointment_choice': choice if choice in ('move', 'add') else 'unclear',
            'conversation': CONVERSATION.label(call),
            'retained_approval': RETAINED_APPROVAL.label(call),
            'last_change': LAST_CHANGE.label(call),
            'chosen_offer': CHOSEN_OFFER.label(call),
        }
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


class SalonSolver(ClassifierInterpreter['SalonCall', SolverInput, SalonSolution]):
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
            retained_complaint=call._complaint,
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
        reading = consent_of(result, call._complaint is not None)
        if call._complaint is not None and reading.retained_approval != 'keep':
            call._complaint = None
        _approve_complaint(call, reading.confirmation)
        return result


SOLVER = SalonSolver('solver', SOLVER_CLASSIFIER, SalonSolution(decision=NoDecision()))


async def classify_reply(
    call: SalonCall,
    *,
    usage: RunUsage | None = None,
    usage_limits: UsageLimits | None = None,
    closing: bool = False,
    before_model: bool = False,
) -> ReplyClassification:
    """Run every reading that applies to this turn, then return the merged view. Each runs at most once per turn.

    Whether the call is over is read on its own only while a question other
    than a read-back waits ("anything else?", an offer, move or add). A reply
    to a read-back is read only as that; if it also says goodbye, the step's
    model hangs up, and `closing` reads it then. Consumers
    recheck the view against the live call through
    `SalonCall.classification`; a late or rebound answer is never published.

    `before_model` is `advance` reading the turn before any model runs. On
    "anything else?" it leaves out the two choice readings: across the Coval
    runs of 24 September they ran on every closing reply and never decided
    one (0 of 6), while a goodbye paid for both. A move asked for there
    ("can we move it to Saturday, same time?") still gets them, read by the
    booking tool before it writes (`WriteBooking._authorise`), against the
    times the model has just put on the table.
    """
    if INTERPRETER == 'solver':
        await SOLVER.interpret(call, usage=usage, usage_limits=usage_limits)
        return call.classification
    question = call.pending_question()
    ending = closing or (question is not None and question.about not in READ_BACKS)
    deferred = before_model and question is not None and question.about == 'closing'
    readings = [r for r in READINGS if not (deferred and r in CLOSING_DEFERS)]
    await read(
        call, (*readings, *((CONVERSATION,) if ending else ())), usage=usage, usage_limits=usage_limits
    )
    return call.classification
