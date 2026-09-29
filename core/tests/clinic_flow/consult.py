"""The clinic with a check-in prerequisite, a nurse to consult and a deferred refund policy (`consult.yaml`).

For `tests.test_context`: history and facts per step, a capability loaded
once and kept, and a consultation that comes back to the step that asked,
with the prerequisite it was waiting on still waiting.
"""

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.capabilities import Capability

import core
from tests.clinic_flow.clinic import Backend, PatientId, Slot


class CheckedIn(core.Receipt):
    """The front desk found the patient: set whole into the state by the lookup (`checked_in`)."""

    patient: PatientId


class DeskState(BaseModel):
    model_config = ConfigDict(extra='forbid')

    patient: PatientId | None = Field(None, description='The patient number, like P-1234.')
    checked_in: CheckedIn | None = None
    slot: Slot | None = Field(
        None, description='The id of the time the caller chose, from the times offered.'
    )
    reference: str = ''
    note: str = Field('', description='Anything else the caller mentioned.')


class CheckIn(core.Lookup[Backend]):
    """Look the patient up: a read with no read-back, whose receipt sets `checked_in`."""

    patient: PatientId
    say = 'Thanks, I have you checked in.'
    matches = ('patient',)

    def run(self, clinic: Backend, intent: core.Intent) -> CheckedIn:
        return CheckedIn(patient=self.patient)


REFUND_POLICY = 'A cancelled appointment is refunded in full up to 24 hours before; after that, half.'

refunds = Capability(
    id='refunds',
    description='The clinic refund policy, for questions about money back.',
    instructions=REFUND_POLICY,
    defer_loading=True,
)
"""Loaded on demand: most calls never ask about refunds, so its words stay out of the prompt until one does."""
