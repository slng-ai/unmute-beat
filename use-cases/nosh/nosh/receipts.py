"""What the place tools may answer, and what Nosh's one lookup returns: Pydantic models on `core.Receipt`.

`core.Receipt` is strict and refuses unknown fields. The tools' own answers
(`Located`, `Place`, `PlaceInfo`) are parsed with `extra='ignore'`, since the
service may add a field without telling anyone; only the fields read here are
kept. `Details` is the receipt core keeps in the state.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

import core


class _Answer(BaseModel):
    """A tools answer: strict about the fields read here, quiet about any others the service adds."""

    model_config = ConfigDict(extra='ignore', strict=True)


class Located(_Answer):
    """`resolve-location`: one point for what the caller said."""

    latitude: float
    longitude: float
    formatted_address: str


class Place(_Answer):
    """One result of `search-nearby` or `search-text`. A search knows nothing about price or hours."""

    place_id: str
    name: str
    address: str
    rating: float | None = None
    user_rating_count: int | None = None
    primary_type_display_name: str | None = None


class PlaceInfo(_Answer):
    """`place-details`: the only answer that has price, hours, phone and website."""

    place_id: str
    name: str
    address: str
    rating: float | None = None
    price_level: str | None = None
    opening_hours: list[str] | None = None
    phone: str | None = None
    website: str | None = None
    primary_type_display_name: str | None = None


class Details(core.Receipt):
    """The full details of the place the caller picked, and how code says them."""

    status: Literal['found']
    place: str
    """The short id the caller picked (`p1`), never Google's place id."""
    name: str
    address: str
    rating: float | None = None
    price: str = ''
    """The price in words ("mid-range"), or '' when Google gave none."""
    today: str = ''
    """Today's opening hours in words, or '' when Google gave none."""
    phone: str | None = None
    website: str | None = None
    spoken: str


class Missing(core.Rejection):
    """Google has no details for that place any more: nothing to say about it."""

    status: Literal['missing']
    place: str
