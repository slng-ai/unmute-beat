"""Typed proposals and semantic interpretations; authorization remains in application code."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.classifiers import Exchange
from core.decisions import (
    Accept,
    Clarify,
    Conditional,
    Decision,
    DecisionResult,
    EndCall,
    NoDecision,
    Reject,
    Revise,
    SwitchTask,
)
from core.readings import ChoiceInput
from core.state import STRICT


class Asked(BaseModel):
    """A question this call put to the caller, and is waiting on the answer to.

    Application-owned. A tool may propose the question; a separate classifier
    interprets its answer, and the application checks the exact proposal.
    The turn it was asked on is part of it, so an agreement two turns later is
    not an answer to it.
    """

    model_config = STRICT

    about: Literal['phone', 'booking', 'move_or_add', 'complaint', 'closing', 'offer']
    spoken: str = ''
    value: str = ''
    """The exact thing agreement would be agreement *to*: the number read back, the slot offered, or the complaint summary's digest."""
    resolution: str = ''
    summary: str = ''
    requested_resolution: str = ''
    owner: str = ''
    """The caller identity the question was put under. A question put to one number is not answered by another."""
    action: Literal['book', 'move', 'cancel', ''] = ''
    booking_id: str = ''
    turn: int = 0
    """The caller turn it went out on. Set by `SalonCall.ask`, never by a tool."""


class OfferInput(ChoiceInput):
    """A reply read against the times on the table, and what the caller already holds, for "the same time"."""

    holding: tuple[str, ...] = Field(
        default=(), description='The appointments the caller already holds, as the salon says them.'
    )


class ReplyClassification(BaseModel):
    """This turn's readings, merged into the one view every tool guard reads. Interpretation only.

    Not a model output: each field is one narrow reading's label
    (`salon.classification.reading_of`), or its default when that reading did
    not apply, failed or went stale. These labels cannot name or execute a
    business action.
    """

    model_config = ConfigDict(extra='forbid', frozen=True)
    confirmation: Literal['accept', 'reject', 'revise', 'conditional', 'unclear'] = 'unclear'
    appointment_choice: Literal['move', 'add', 'unclear'] = 'unclear'
    conversation: Literal['end', 'continue', 'unclear'] = 'unclear'
    retained_approval: Literal['keep', 'withdraw', 'unclear'] = 'unclear'
    last_change: Literal['keep', 'withdraw', 'unclear'] = 'unclear'
    """Whether the reply takes back the booking made in this call. Only read while the caller holds it."""
    chosen_offer: str = ''
    """Checked against the table by the application: a slot that was not on it authorises nothing."""


class CorrectPhone(Decision):
    """Gives different digits for the number that was read back. Only for a phone readback."""

    kind: Literal['correct_phone'] = 'correct_phone'
    digits: str = Field(
        description=(
            'The whole corrected number in E.164, e.g. +34600111234. Keep the read-back digits the caller did '
            'not change. If you cannot tell which digits change, use clarify instead.'
        )
    )


class ChooseAppointment(Decision):
    """Chooses between moving the existing appointment and adding another. A bare yes cannot choose."""

    kind: Literal['choose_appointment'] = 'choose_appointment'
    choice: Literal['move', 'add']


class SalonSwitch(SwitchTask):
    """Abandons the current task, without finishing it, to do a different one of the listed destinations."""

    target: Literal['booking', 'complaints', 'concierge']


type SalonDecision = Annotated[
    Accept
    | Reject
    | Revise
    | Conditional
    | Clarify
    | CorrectPhone
    | ChooseAppointment
    | SalonSwitch
    | EndCall
    | NoDecision,
    Field(discriminator='kind'),
]
"""Every meaning the salon's solver may return. The application maps each one to its own executor."""


class SalonSolution(DecisionResult):
    """The caller's reply read as one decision, with a short reply only when switching task."""

    decision: SalonDecision


class Destination(BaseModel):
    """A task the caller may switch to, and what it needs before it can start."""

    model_config = ConfigDict(extra='forbid', frozen=True)
    name: Literal['booking', 'complaints', 'concierge']
    purpose: str
    needs: str = ''


class SolverInput(Exchange):
    """A snapshot of the reply and every application-owned fact needed to read it as one decision.

    `delivered` is False when the caller spoke over the proposal, so it may be
    corrected or abandoned but never accepted. Destinations are what the
    caller could switch to; whether a switch is legal right now is checked
    again when it is applied.
    """

    step: Literal['concierge', 'verify', 'booking', 'complaints'] = Field(
        description='The part of the call the caller is in.'
    )
    owner: str = Field(description="The caller's phone number, once they have given one. Empty before that.")
    proposal: Asked | None = Field(
        default=None, description='The proposal the salon put to the caller last. Null when there is none.'
    )
    delivered: bool = Field(
        default=False, description='Whether the caller heard the whole proposal before answering.'
    )
    retained_complaint: Asked | None = Field(
        default=None,
        description='A complaint the caller already approved, kept while their number is checked. Null if none.',
    )
    destinations: tuple[Destination, ...] = Field(
        default=(), description='The tasks the caller could switch to, and what each needs first.'
    )
