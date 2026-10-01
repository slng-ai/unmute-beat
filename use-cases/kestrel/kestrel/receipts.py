"""What Kestrel's claims desk may answer, as `core.Receipt` models."""

from typing import Annotated, Literal

import core


class Vehicle(core.Receipt):
    plate: str
    words: str


class PolicyFound(core.Receipt):
    """The policy matched its holder's date of birth: the caller may report a claim on it."""

    status: Literal['found']
    policy_number: str
    first_name: str
    vehicles: list[Vehicle]
    next: Literal['incident'] = 'incident'


class Filed(core.Receipt):
    """A claim filed, with its number said digit by digit."""

    status: Literal['filed']
    policy_number: str
    claim_number: Annotated[str, core.Subject('claim')]
    spoken: str
    revision: Annotated[int, core.Version()] = 1
    """A claim is filed once and never changed here, so its version is always 1."""
