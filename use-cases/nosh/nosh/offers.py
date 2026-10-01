"""What Nosh offers a caller: the location it found for their words, then the places there.

Each is a choice function (`choices:` in `agent.yaml`). A pick counts only if
it was offered, so the model cannot invent a location or a place. Its `say`
is code's next line (`offers:`): the location read back, the places, or the
question for what is still missing.
"""

from pydantic import BaseModel, TypeAdapter

from nosh import speech
from nosh.operations import Nosh
from nosh.receipts import Located, Place
from nosh.state import NoshState
from nosh.tools import NotFoundError

OFFER_AT_MOST = 6
"""How many places the agent is shown. The caller hears the first three."""
OFFER_SAID = 3
PLACES = TypeAdapter(list[Place])


class Offer(BaseModel):
    """The ids on offer with their words, what the agent should know, and code's line."""

    options: dict[str, str] = {}
    note: str = ''
    say: str = ''
    pick: str = ''


def resolve_area(nosh: Nosh, state: NoshState) -> Offer:
    """The one point for the caller's words, read back as a question. Their yes is the model recording it."""
    if not state.area:
        return Offer(say='Whereabouts are you looking? An address or an area is fine.')
    try:
        located = Located.model_validate(nosh.tools.post('resolve-location', {'query': state.area}))
    except NotFoundError:
        return Offer(
            note=f'Nothing matched "{state.area}". Ask for something nearby; never guess.',
            say="Hmm, I couldn't place that. Could you give me a nearby cross street, a neighbourhood or the city?",
        )
    spot = nosh.spot_for(located)
    return Offer(
        options={spot: located.formatted_address},
        note='A yes records this id as spot. A different place is a new area, not this spot.',
        say=f"So that's {located.formatted_address}, right?",
    )


def _search(nosh: Nosh, located: Located, state: NoshState) -> list[str]:
    """The ids of the places for this point and craving, asked of the tools once per call."""
    craving = (state.craving or '').strip()
    anything = craving.lower() in ('', 'no preference')
    nearby = bool(state.at_address) and anything
    query = '' if nearby else f'{"restaurants and bars" if anything else craving} in {state.area}'
    key = (located.formatted_address, query)
    if key not in nosh.searches:
        point = {'latitude': located.latitude, 'longitude': located.longitude}
        raw = (
            nosh.tools.post('search-nearby', point)
            if nearby
            else nosh.tools.post('search-text', {'query': query, **point})
        )
        nosh.searches[key] = [nosh.place_for(place) for place in PLACES.validate_python(raw)[:OFFER_AT_MOST]]
    return nosh.searches[key]


def find_places(nosh: Nosh, state: NoshState) -> Offer:
    """Up to three places for the agreed location and craving, in code's words: name, kind, rating, reviews.

    An exact address with no craving searches nearby restaurants and bars. An
    area with no craving asks what they fancy first. Never price or hours:
    a search does not have them.
    """
    located = nosh.spots.get(state.spot or '')
    if located is None:
        return Offer(note='No location agreed yet.')
    if not state.craving and not state.at_address:
        return Offer(
            note='Ask what they are in the mood for before searching.',
            say='What are you in the mood for? A cuisine, a bar, or no preference?',
        )
    found = _search(nosh, located, state)
    if not found:
        return Offer(
            note='Nothing found. Offer one change at a time: a wider area first, then a looser type. '
            'Never swap in another city.',
            say="Hmm, I couldn't find anything like that right there. Want me to try a bit wider, or a different kind of place?",
        )
    places = {id: nosh.places[id] for id in found}
    said = [speech.short(place) for place in list(places.values())[:OFFER_SAID]]
    listed = said[0] if len(said) == 1 else f'{"; ".join(said[:-1])}; and {said[-1]}'
    return Offer(
        options={id: place.name for id, place in places.items()},
        note=' '.join(f'{id}: {speech.short(place)}, at {place.address}.' for id, place in places.items()),
        say=f'I found {listed}. Do any of those sound good?',
    )
