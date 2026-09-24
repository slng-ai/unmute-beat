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


class ConsentInput(Exchange):
    """A snapshot of the accepted reply and its application-owned context.

    `question` is everything the salon said on the turn before the reply.
    The field descriptions are prompt text: the classifier lists them to the
    model.
    """

    owner: str = Field(description="The caller's phone number, once they have given one. Empty before that.")
    pending: Asked | None = Field(
        default=None,
        description='The question the salon is waiting on an answer to, as it recorded it: what it is about '
        '(`about`), its words (`spoken`) and what it proposes (`value`). Null when no question is waiting.',
    )
    retained_complaint: Asked | None = Field(
        default=None,
        description='A complaint the caller already approved, kept while their number is checked. Null if none.',
    )
    just_changed: str = Field(
        default='',
        description='What the salon told the caller it had just changed, on the turn before this answer. '
        'Empty when nothing changed.',
    )
    offered: tuple[OfferedTime, ...] = Field(
        default=(), description='The times the caller can pick from right now, each with its `slot_id`.'
    )
    holding: tuple[str, ...] = Field(
        default=(), description='The appointments the caller already holds, as the salon says them.'
    )


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
        description='The slot_id of the one time in `offered` that the answer picks or asks for, copied exactly '
        'from `offered`: a time they name, or "the same time" as one in `holding`. It still counts when the '
        'answer also says thanks, goodbye or that they need nothing else, whatever `question` was. Empty if '
        'the answer names no time in `offered`, puts a condition on it, or could mean more than one.',
    )
    """Checked against the pending offer by the application: a slot that was not on the table authorises nothing."""


type RequestChange = (
    Annotated[
        Literal['kept'],
        Field(
            description='In `answer` the caller only answered what they were asked (a phone number, '
            'a yes or no, a correction of it) and changed or added nothing to `request`.'
        ),
    ]
    | Annotated[
        Literal['changed'],
        Field(
            description='Somewhere in `answer` the caller changed `request` or asked for '
            'something more: another day, time or service, a question, a complaint.'
        ),
    ]
    | Annotated[Literal['unclear'], Field(description='There is no `request`, or it is uncertain.')]
)


class DetourInput(Exchange):
    """What was said while the salon checked who the caller is, and the request that check interrupted.

    `question` is everything the salon said during the check, `answer`
    everything the caller said, both in order. Its own snapshot and its own
    request, not fields of `ConsentInput`: added there, they cost the consent
    reading its accuracy (Coval run CFmgWMUy, "Yes. That's correct." to a
    read-back read as nothing pending, three of five calls).
    """

    request: str = Field(description='What the caller asked for before the check began, in their words.')


class DetourReading(BaseModel):
    """Interpretation only: whether the interrupted request still stands. It authorises no business action."""

    model_config = ConfigDict(extra='forbid', frozen=True)
    request_change: RequestChange = 'unclear'
    """Only `kept` lets a return from the check act on the request without a model run."""


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
