"""Nosh's services and its one operation: the details of the place the caller picked.

Nosh changes nothing anywhere. Its only operation is a `core.Lookup` that code
carries out as soon as the caller picks a place (`carries_out:` in
`agent.yaml`), with no read-back and no model request. Its receipt's words
are code's (`speech.details`), so a price or an opening time is never the
model's.

The services also hold what this call has been offered: each location and
each place gets a short id (`s1`, `p1`) the first time it is offered, and
keeps it. A search is asked once per location and craving, so the same
place keeps the same id however often it is offered again.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import Field, StringConstraints

import core
from nosh import speech
from nosh.receipts import Details, Located, Missing, Place, PlaceInfo
from nosh.tools import NotFoundError, Tools

TZ = ZoneInfo('Europe/Madrid')
"""The call's clock zone, as in `agent.yaml`. Today's hours are this zone's today."""


@dataclass
class Nosh:
    """One call's services: the place tools, the clock, and the ids this call has offered."""

    tools: Tools
    clock: Callable[[], datetime] | None = None
    spots: dict[str, Located] = field(default_factory=dict)
    places: dict[str, Place] = field(default_factory=dict)
    searches: dict[tuple[str, str], list[str]] = field(default_factory=dict)

    def now(self) -> datetime:
        """The call's clock: core reads the call's today from it once, when the call opens."""
        return self.clock() if self.clock else datetime.now(TZ)

    def spot_for(self, located: Located) -> str:
        """A location's short id, the same each time the same point is offered."""
        found = next((id for id, seen in self.spots.items() if seen == located), '')
        if not found:
            found = f's{len(self.spots) + 1}'
            self.spots[found] = located
        return found

    def place_for(self, place: Place) -> str:
        """A place's short id, the same each time Google's place id is offered again."""
        found = next((id for id, seen in self.places.items() if seen.place_id == place.place_id), '')
        if not found:
            found = f'p{len(self.places) + 1}'
            self.places[found] = place
        return found


Picked = Annotated[str, StringConstraints(min_length=1)]
Outcome = Annotated[Details | Missing, Field(discriminator='status')]


class PlaceDetails(core.Lookup[Nosh]):
    """The full details of the place the caller picked: address, rating, price, today's hours, phone, website."""

    place: Picked
    matches = ('place',)
    say = '{spoken}'

    def run(self, nosh: Nosh, intent: core.Intent) -> Outcome:
        offered = nosh.places.get(self.place)
        if offered is None:
            return Missing(
                status='missing', place=self.place, reason='that place was never offered on this call'
            )
        try:
            raw = nosh.tools.post('place-details', {'place_id': offered.place_id})
        except NotFoundError:
            return Missing(status='missing', place=self.place, reason='Google has no details for it')
        except Exception as error:
            raise core.OutcomeUnknownError(f'the place tools did not answer: {error}') from error
        info = PlaceInfo.model_validate(raw)
        spoken, price, today = speech.details(info, nosh.now())
        return Details(
            status='found',
            place=self.place,
            name=info.name,
            address=info.address,
            rating=info.rating,
            price=price,
            today=today,
            phone=info.phone,
            website=info.website,
            spoken=spoken,
        )
