"""What a Nosh call knows: one plain Pydantic model. Everything else is core's.

The caller says where and what they fancy. The location they agree to and
the place they pick are ids from what code offered (`offers.py`), so the
model cannot invent either. `details` is the details lookup's receipt, set
whole and only by it.
"""

from pydantic import BaseModel, ConfigDict, Field

from nosh.receipts import Details


class NoshState(BaseModel):
    """The facts of one Nosh call. Every field has a default: a call starts empty."""

    model_config = ConfigDict(extra='forbid')

    area: str | None = Field(
        None,
        description='Where the caller wants to eat or drink, as they said it: a street address, a cross street, '
        'a landmark, a neighbourhood or a town. Add the city when they named one.',
    )
    at_address: bool | None = Field(
        None,
        description='True when the area is an exact street address or a point they are at; false for a '
        'neighbourhood, an area, a town or a venue by name.',
    )
    craving: str | None = Field(
        None,
        description='What they are in the mood for, in a few words: a cuisine, "bar", "restaurant", '
        '"cocktail bar", a venue by name, or "no preference".',
    )
    spot: str | None = Field(
        None, description='The id of the location the caller agreed to, from the location on offer.'
    )
    place: str | None = Field(
        None, description='The id of the place the caller picked, from the places on offer.'
    )
    details: Details | None = None
    """Set only by the details lookup, for the place picked. Cleared when the place changes."""
