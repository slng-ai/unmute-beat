"""Kestrel's services and its two actions: find the caller's policy, and file their claim.

`LookupPolicy` is read back first (the policy number and date of birth), and
its receipt is the only way to `policy`, which moves the caller to the
incident questions. `FileClaim` is read back as a summary of every fact and
files once on a heard yes; its token is core's intent id, so a lost answer is
found again (`reconcile`) and never filed twice.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import AliasPath, Field

import core
from kestrel.claims import Claims
from kestrel.receipts import Filed, PolicyFound, Vehicle
from kestrel.state import PolicyNumber, WhenInDay

TZ = ZoneInfo('Europe/London')
"""The call's clock zone, as in `agent.yaml`."""


@dataclass
class Kestrel:
    """One call's services: the claims desk, the clock, and whether the safety line was said."""

    claims: Claims
    clock: Callable[[], datetime] | None = None
    safety_said: bool = False

    def now(self) -> datetime:
        """The call's clock: core reads the call's today from it once, when the call opens."""
        return self.clock() if self.clock else datetime.now(TZ)


DIGITS = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine']


def spelled(code: str) -> str:
    """ "KP482913" as "K P, four eight two, nine one three": letters, then the digits in threes."""
    words = [DIGITS[int(d)] for d in code[2:]]
    return f'{code[0]} {code[1]}, {" ".join(words[:3])}, {" ".join(words[3:])}'


def ordinal(n: int) -> str:
    return f'{n}{"th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")}'


def spoken_day(value: date) -> str:
    """ "Monday the 28th of September"."""
    return f'{value:%A} the {ordinal(value.day)} of {value:%B}'


def spoken_when(value: time | str) -> str:
    """ "at 6:30 PM", or "in the evening", "at night"."""
    if isinstance(value, str):
        return 'at night' if value == 'night' else f'in the {value}'
    hour = value.hour % 12 or 12
    return f'at {hour}{f":{value.minute:02d}" if value.minute else ""} {"AM" if value.hour < 12 else "PM"}'


class LookupPolicy(core.Lookup[Kestrel]):
    """Find the caller's policy by its number and the holder's date of birth. Run only on their yes to both."""

    policy_number: PolicyNumber
    birth_date: date
    matches = ('policy_number',)
    restated_confirms = True
    """The same number and date said again are a yes to their read-back."""
    route = 'next'
    say = 'Thank you, {first_name}, I have your policy.'

    def run(self, kestrel: Kestrel, intent: core.Intent) -> PolicyFound:
        """One receipt type, and a mismatch as `core.Refused`.

        ponytail: a union return would be the natural shape, but core reads the
        `route` field off the union's wrapper, not the receipt, so the caller
        would never move on. Go back to a union once core reads the inner one.
        """
        found = kestrel.claims.policy(self.policy_number, self.birth_date)
        if found is None:
            raise core.Refused(
                'No policy matches that number and date of birth. Say so kindly, and ask them to check '
                'the number on their policy documents. Reveal nothing about any policy.'
            )
        return PolicyFound(
            status='found',
            policy_number=found.policy_number,
            first_name=found.first_name,
            vehicles=[Vehicle(plate=c.plate, words=c.words) for c in found.cars],
        )

    def readback(self, kestrel: Kestrel) -> str:
        return (
            f'Is your policy number {spelled(self.policy_number)}, '
            f'with a date of birth of the {ordinal(self.birth_date.day)} of {self.birth_date:%B}, {self.birth_date.year}?'
        )


class FileClaim(core.Write[Kestrel]):
    """File the claim with every fact the caller gave. Run only on their yes to the summary read back."""

    policy_number: PolicyNumber = Field(validation_alias=AliasPath('policy', 'policy_number'))
    incident_day: date
    incident_time: WhenInDay
    place: str = Field(min_length=1)
    what_happened: str = Field(min_length=1)
    injuries: bool
    police_report: str = Field(min_length=1)
    other_party: bool
    vehicle_drivable: bool
    vehicle: str = Field(min_length=1)
    vehicles: list[Vehicle] = Field(validation_alias=AliasPath('policy', 'vehicles'))
    matches = ('policy_number',)
    idempotent = True
    say = '{spoken}'
    recap = 'Your claim number is {claim_number}.'

    def _filed(self, raw: dict[str, Any]) -> Filed:
        number = raw['claim_number']
        return Filed(
            status='filed',
            policy_number=raw['policy_number'],
            claim_number=number,
            spoken=f'Your claim is filed. Your claim number is {spelled(number)}. A claims handler will call '
            'you within one working day. Is there anything else I can help with?',
        )

    def run(self, kestrel: Kestrel, intent: core.Intent) -> Filed:
        facts = self.model_dump(mode='json', exclude={'policy_number', 'vehicles'})
        try:
            raw = kestrel.claims.file(intent.id, self.policy_number, facts)
        except Exception as error:
            found = kestrel.claims.outcome_of(intent.id)
            if found is None:
                raise core.OutcomeUnknownError(f'the claims desk did not answer: {error}') from error
            raw = found
        return self._filed(raw)

    def reconcile(self, kestrel: Kestrel, intent: core.Intent) -> Filed | None:
        found = kestrel.claims.outcome_of(intent.id)
        return self._filed(found) if found else None

    def readback(self, kestrel: Kestrel) -> str:
        car = next((v.words for v in self.vehicles if v.plate == self.vehicle), 'your car')
        police = (
            'no police report'
            if self.police_report.lower() == 'none'
            else f'police report {self.police_report}'
        )
        return ' '.join(
            (
                f'Shall I file your claim for {car}?',
                f'On {spoken_day(self.incident_day)} {spoken_when(self.incident_time)}, at {self.place.strip().rstrip(".")}:',
                f'{self.what_happened.strip().rstrip(".")}.',
                f'{"Someone was hurt" if self.injuries else "Nobody was hurt"}, {police},',
                f'{"another driver was involved" if self.other_party else "no other driver was involved"},',
                f'and the car {"can" if self.vehicle_drivable else "cannot"} be driven. Is that all right?',
            )
        )
