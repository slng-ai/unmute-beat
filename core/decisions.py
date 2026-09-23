"""What a caller's reply means, as typed data. Interpretation only.

A decision says what the caller meant; it never names a function, a field or
a write. The application maps each kind to the executor that owns it, and
that executor still checks identity, freshness and the exact proposal before
anything happens. A model that returns `Accept` has proposed that the caller
agreed, nothing more.

The class docstrings are the descriptions in the schema a model is sent, so
editing one is a prompt change. Each business adds the kinds only it has by
subclassing `Decision` with its own `kind`, and joins them into one union
discriminated on `kind` (see `salon.models.consent.SalonDecision`).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Decision(BaseModel):
    """One meaning of the caller's reply."""

    model_config = ConfigDict(extra='forbid', frozen=True)


class Accept(Decision):
    """Clearly approves the exact pending proposal, without conditions or questions."""

    kind: Literal['accept'] = 'accept'


class Reject(Decision):
    """Declines the pending proposal and asks for nothing else."""

    kind: Literal['reject'] = 'reject'


class Revise(Decision):
    """Asks to change the pending proposal, for example another day or another outcome."""

    kind: Literal['revise'] = 'revise'


class Conditional(Decision):
    """Agrees only under an additional condition."""

    kind: Literal['conditional'] = 'conditional'


class Clarify(Decision):
    """The reply is ambiguous or incomplete in a way one short question would settle."""

    kind: Literal['clarify'] = 'clarify'
    question: str = Field(
        max_length=160, description='One short question to settle it. Never claim anything was done.'
    )


class SwitchTask(Decision):
    """Abandons the current task, without finishing it, to do a different one of the listed destinations.

    ponytail: abandon only. Pausing a task to resume it afterwards needs a
    stack of checkpoints, which `core.graph.route` deliberately does not have.
    """

    kind: Literal['switch_task'] = 'switch_task'
    target: str
    details: str = Field(
        default='',
        max_length=300,
        description='What the caller already said about the new task, in their words, so they are not asked again.',
    )


class EndCall(Decision):
    """Ends the whole call with no remaining request."""

    kind: Literal['end_call'] = 'end_call'


class NoDecision(Decision):
    """Anything else: a question, small talk, or nothing that answers the pending proposal."""

    kind: Literal['none'] = 'none'


class DecisionResult(BaseModel):
    """One decision, and a short reply for the kinds that need words the application did not write.

    A business subclass narrows `decision` to its own union. The reply is only
    ever spoken after the decision has been validated and applied, and never
    for a kind whose words the application owns (a readback, a confirmation).
    """

    model_config = ConfigDict(extra='forbid', frozen=True)

    decision: Decision
    reply: str = Field(
        default='',
        max_length=200,
        description='Only for switch_task: one short sentence to the caller. Otherwise empty. Never claim anything was done.',
    )
