"""Typed proposals and semantic interpretations; authorization remains in application code."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class OfferedTime(BaseModel):
    """One time the salon has just offered this caller, with the id a pick must name."""

    model_config = ConfigDict(extra='forbid', frozen=True)
    slot_id: str
    spoken: str


class ConsentInput(BaseModel):
    """A snapshot of the accepted reply and its application-owned context."""

    model_config = ConfigDict(extra='forbid', frozen=True)
    utterance: str
    previous_speech: str
    owner: str
    pending: Asked | None = None
    retained_complaint: Asked | None = None
    just_changed: str = ''
    """What the salon told the caller it had just changed, in its own words, on the turn before this reply. Empty when nothing changed."""
    offered: tuple[OfferedTime, ...] = ()
    """The times on the table: the pending offer's, or else those the diary has just offered this call."""
    holding: tuple[str, ...] = ()
    """The appointments the caller already holds, as the salon says them, so "the same time" can be read."""
    detour_request: str = ''
    """What the caller asked for before the salon stopped to check who they are. Empty outside that check."""
    detour_words: tuple[str, ...] = ()
    """Everything the caller has said since that check began, one turn per item, this reply last."""


type Confirmation = (
    Annotated[
        Literal['accept'],
        Field(description='Clearly approves the exact pending proposal without conditions or questions.'),
    ]
    | Annotated[Literal['reject'], Field(description='Declines the pending proposal.')]
    | Annotated[Literal['revise'], Field(description='Asks to change the pending proposal.')]
    | Annotated[Literal['conditional'], Field(description='Agrees only under an additional condition.')]
    | Annotated[
        Literal['unclear'], Field(description='Asks a question, or the answer is ambiguous or incomplete.')
    ]
    | Annotated[Literal['not_applicable'], Field(description='No confirmation is pending.')]
)

type AppointmentChoice = (
    Annotated[Literal['move'], Field(description='Replace an existing appointment with the proposed one.')]
    | Annotated[Literal['add'], Field(description='Keep the existing appointment and add another.')]
    | Annotated[
        Literal['unclear'],
        Field(description='Both, neither, conditional, or ambiguous; a bare yes cannot choose.'),
    ]
    | Annotated[
        Literal['not_applicable'], Field(description='The answer is unrelated to choosing an appointment.')
    ]
)

type Conversation = (
    Annotated[
        Literal['end'],
        Field(
            description='Ends the whole call with no remaining request; interpret refusals against the previous question.'
        ),
    ]
    | Annotated[
        Literal['continue'],
        Field(description='Has more to discuss, asks a question, or only declines a proposal.'),
    ]
    | Annotated[Literal['unclear'], Field(description='Whether the caller wants to end the call is unclear.')]
)

type RetainedApproval = (
    Annotated[
        Literal['keep'],
        Field(description='The approved complaint is unchanged while identity verification continues.'),
    ]
    | Annotated[
        Literal['withdraw'],
        Field(description='Withdraws or changes the complaint or its resolution, or switches tasks.'),
    ]
    | Annotated[
        Literal['unclear'], Field(description='There is no retained complaint approval, or it is uncertain.')
    ]
)


type LastChange = (
    Annotated[
        Literal['keep'],
        Field(description='Keeps the change the salon just confirmed, or does not take it back.'),
    ]
    | Annotated[
        Literal['withdraw'],
        Field(
            description='Takes back the change the salon just confirmed: asks to undo or cancel it, or says '
            'to leave it or that they no longer want it.'
        ),
    ]
    | Annotated[
        Literal['unclear'],
        Field(description='Nothing was just changed, or whether they take it back is unclear.'),
    ]
)


type RequestChange = (
    Annotated[
        Literal['kept'],
        Field(
            description='In `detour_words` the caller only answered what they were asked (a phone number, '
            'a yes or no, a correction of it) and changed or added nothing to `detour_request`.'
        ),
    ]
    | Annotated[
        Literal['changed'],
        Field(
            description='Somewhere in `detour_words` the caller changed `detour_request` or asked for '
            'something more: another day, time or service, a question, a complaint.'
        ),
    ]
    | Annotated[Literal['unclear'], Field(description='There is no `detour_request`, or it is uncertain.')]
)


class ReplyClassification(BaseModel):
    """Interpretation only: these labels cannot name or execute a business action."""

    model_config = ConfigDict(extra='forbid', frozen=True)
    confirmation: Confirmation = 'unclear'
    appointment_choice: AppointmentChoice = 'unclear'
    conversation: Conversation = 'unclear'
    retained_approval: RetainedApproval = 'unclear'
    last_change: LastChange = 'unclear'
    """Whether the reply takes back the change the salon confirmed on the turn before. Read only against `just_changed`."""
    chosen_offer: str = Field(
        default='',
        description='The slot_id of the one time in `offered` the caller plainly picks or asks for (a time they '
        'name, or "the same time" as one in `holding`), copied exactly from `offered`. Empty if they pick none, '
        'attach a condition, ask a question first, or it is unclear which one.',
    )
    """Checked against the pending offer by the application: a slot that was not on the table authorises nothing."""
    request_change: RequestChange = 'unclear'
    """Whether the request the identity check interrupted still stands. Only `kept` lets a return skip the model."""


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


class SolverInput(BaseModel):
    """A snapshot of the reply and every application-owned fact needed to read it as one decision.

    `delivered` is False when the caller spoke over the proposal, so it may be
    corrected or abandoned but never accepted. Destinations are what the
    caller could switch to; whether a switch is legal right now is checked
    again when it is applied.
    """

    model_config = ConfigDict(extra='forbid', frozen=True)
    utterance: str
    previous_speech: str
    step: Literal['concierge', 'verify', 'booking', 'complaints']
    owner: str
    proposal: Asked | None = None
    delivered: bool = False
    retained_complaint: Asked | None = None
    destinations: tuple[Destination, ...] = ()
