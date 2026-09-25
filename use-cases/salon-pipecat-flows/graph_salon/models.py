"""What one call is made of, as Pydantic models: the call's state, the question on the table, the tools' arguments.

Everything here is data that crosses a trust boundary. Tool arguments come
from the model, so each tool validates them into one of these before a field
is read. The state is replaced whole, one validated candidate at a time
(`SalonCall.commit`), so no half-applied update is ever visible.

The value types (`Phone`, `SubmittedPhone`, `Service`) and the diary's
receipts are the salon's own (`salon.models`): the same business, the same
refusals, whichever conversation layer is in front of it.
"""

from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from salon.models.receipts import ComplaintRecord, Saved
from salon.models.shapes import Phone, SubmittedPhone

Step = Literal['concierge', 'verify', 'booking', 'complaints']
"""A conversational step. Each has two nodes in `flow.yaml`: the model speaks on entering it, or its `_listen` twin waits."""
NodeName = Literal[
    'concierge',
    'concierge_listen',
    'verify',
    'verify_listen',
    'booking',
    'booking_listen',
    'complaints',
    'complaints_listen',
    'confirm',
    'end',
]
"""Every node in `flow.yaml`, and nothing else: `graph_salon.flow` checks the two agree when it loads."""
Origin = Literal['verify', 'booking', 'complaints']
"""A step that can put a question to the caller, and so the one a no goes back to."""
Move = NodeName | Literal['stay', 'wait']
"""Where a tool sends the call: a node its branch table in `flow.yaml` declares, or `stay`, which no table does.

`wait` is Pipecat's `NO_RESPONSE`: the call stays, and the model does not answer.
An `answer` to a question another reply already settled waits: code has
spoken that answer's receipt, and a node that runs the model would have it
said again (Langfuse trace 8b6ea51b).
"""
Words = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
"""Text that has to say something: a summary or a goodbye made of spaces is refused before it is used."""

FROZEN = ConfigDict(frozen=True, extra='forbid', revalidate_instances='always')
"""Every model here is a value: built whole, never edited, and re-checked when nested in another."""


class PhoneChoice(BaseModel):
    """A number read back for the caller to agree to before it is looked up."""

    model_config = FROZEN
    kind: Literal['phone'] = 'phone'
    phone: SubmittedPhone


class BookingChoice(BaseModel):
    """A booking, move or cancellation read back for the caller to agree to before it is written."""

    model_config = FROZEN
    kind: Literal['booking'] = 'booking'
    action: Literal['book', 'move', 'cancel']
    slot_id: str = ''
    booking_id: str = ''
    additional: bool = False


class ComplaintChoice(BaseModel):
    """A complaint read back for the caller to agree to before it is recorded."""

    model_config = FROZEN
    kind: Literal['complaint'] = 'complaint'
    summary: Words
    requested_resolution: str = ''


Choice = Annotated[PhoneChoice | BookingChoice | ComplaintChoice, Field(discriminator='kind')]


class Proposal(BaseModel):
    """The one question the call is waiting on: what would be done, in the words this code chose.

    The caller's yes can only ever authorise this, never arguments a model
    passes at approval time: the confirm node's one tool takes a reply, not a
    booking.
    """

    model_config = FROZEN
    choice: Choice
    question: Words
    """What the caller heard, word for word. Code wrote it, `tts_say` spoke it."""
    asked_turn: int
    """The caller turn it was put in. Only a reply in a later turn can answer it."""
    phone: Phone
    """Who it was put to. A different verified number cannot answer it."""
    origin: Origin
    """The step that asked, where the conversation goes back to on a no."""
    token: str = Field(default_factory=lambda: uuid4().hex)
    """The write's identity at the diary, so a retry cannot write twice."""


class ReadDiary(BaseModel):
    model_config = FROZEN
    day: str = Field(description='The caller\'s own day words, like "Friday" or "tomorrow".')
    service: Literal['haircut', 'hair-color', 'blowout', 'any'] = Field(
        description='The service, or any when they have not said.'
    )
    prefer: str = Field(default='', description='The caller\'s own time words, like "morning", if any.')


class CallState(BaseModel):
    """Everything the application knows about this call, in one frozen value."""

    model_config = FROZEN
    turn: int = 0
    """How many caller turns the context holds. Read from the context, never counted from events."""
    heard: str = ''
    """What the caller said last."""
    phone: Phone = ''
    """The verified number, '' until a lookup landed. Nothing reads or writes the diary without it."""
    caller_id: Phone = ''
    """The carrier's number, until it has been read back once. Never trusted without a yes."""
    resume: Literal['booking', 'complaints'] | None = None
    """Where to go once verification lands: the request the caller made before being asked for a number."""
    pending_read: ReadDiary | None = None
    """A diary read asked for before anybody was identified. Code runs it when the lookup lands."""
    proposal: Proposal | None = None
    appointment: Saved | None = None
    """The last booking receipt. A booking step is done only when this exists."""
    complaint: ComplaintRecord | None = None
    """The last complaint receipt. A complaint step is done only when this exists."""
    receipt_turn: int = -1
    """The caller turn the last write landed in, so speech may mention it."""
    tool_calls: int = 0
    """Tool calls in this caller turn, for the budget."""
    claimed_node: str = ''
    """The node the last action ran on. Every action moves the call, so another one there is the same reply."""


class Outcome(BaseModel):
    """What a tool decided: its result for the model, where the call goes, and what code says on arrival.

    `next` is the field every branch table in `flow.yaml` reads, so Pipecat,
    not this code, turns it into a node; `stay` matches no case and leaves
    the call where it is, with the model to answer. `wait` never reaches a
    table: the call stays, silent, until the caller speaks.
    """

    model_config = FROZEN
    result: dict[str, Any] = {}
    next: Move = 'stay'
    say: str = ''
    """What the arriving node's `say` action speaks. Only a listening node speaks it."""


class NoArgs(BaseModel):
    """For a tool that takes nothing."""

    model_config = FROZEN


class HandOver(BaseModel):
    model_config = FROZEN
    to: Step = Field(
        description='booking to book, move or cancel; complaints for a complaint, refund or manager request; '
        'verify to correct a number already confirmed; concierge for anything else.'
    )


class AskNumber(BaseModel):
    model_config = FROZEN
    phone: SubmittedPhone


class AskBooking(BaseModel):
    model_config = FROZEN
    action: Literal['book', 'move', 'cancel']
    slot_id: str = Field(default='', description='The slot id from `read_diary`, to book or move to.')
    booking_id: str = Field(default='', description='The booking to move or cancel.')
    additional: bool = Field(default=False, description='True only when the caller asked for a second one.')


class AskComplaint(BaseModel):
    model_config = FROZEN
    summary: Words = Field(description="What went wrong, in the caller's own words.")
    requested_resolution: str = Field(default='', description='What the caller wants the salon to do.')


class Answer(BaseModel):
    model_config = FROZEN
    reply: Literal['yes', 'no', 'change', 'other'] = Field(
        description='yes: a plain yes to the question. no: a plain no. change: they want different details. '
        'other: anything else, such as a question, a condition, or a new subject.'
    )


class EndCall(BaseModel):
    model_config = FROZEN
    goodbye: Words = Field(description='One short goodbye. It is the last thing they hear.')
