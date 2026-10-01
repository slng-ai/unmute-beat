"""Class operations the loader must refuse (`tests.test_actions`): each breaks one rule it cannot infer around."""

from typing import Annotated, Literal

from pydantic import AliasPath, BaseModel, Field

import core
from tests.clinic_flow.clinic import Backend


class Yes(core.Receipt):
    status: Literal['yes']


class No(core.Rejection):
    status: Literal['no']


class Undiscriminated(core.Lookup[Backend]):
    patient: str

    def run(self, clinic: Backend, intent: core.Intent) -> Yes | No:
        return Yes(status='yes')


class NoSource(core.Lookup[Backend]):
    phone: str = Field(validation_alias=AliasPath('contact', 'mobile'))

    def run(self, clinic: Backend, intent: core.Intent) -> Yes:
        return Yes(status='yes')


class Unread(core.Write[Backend]):
    patient: str

    def run(self, clinic: Backend, intent: core.Intent) -> Yes:
        return Yes(status='yes')


class Plain(BaseModel):
    status: str


class PlainReceipt(core.Lookup[Backend]):
    patient: str

    def run(self, clinic: Backend, intent: core.Intent) -> Plain:  # ty: ignore[invalid-method-override]
        return Plain(status='yes')


class Changed(core.Receipt):
    reference: Annotated[str, core.Subject('booking')]


class Unversioned(core.Write[Backend]):
    patient: str

    def run(self, clinic: Backend, intent: core.Intent) -> Changed:
        return Changed(reference='CL001')

    def readback(self, clinic: Backend) -> str:
        return 'Shall I?'


class Routed(core.Receipt):
    status: Literal['routed']
    next: Literal['desk']


class HalfRouted(core.Lookup[Backend]):
    """Two success receipts, only one with the route field: the other could never move the caller."""

    route = 'next'

    def run(
        self, clinic: Backend, intent: core.Intent
    ) -> Annotated[Routed | Yes, Field(discriminator='status')]:
        return Yes(status='yes')
