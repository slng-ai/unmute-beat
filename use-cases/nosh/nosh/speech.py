"""How code says what the place tools return: ratings, prices, today's hours, addresses. Plain text for TTS.

The model never words a price or an opening time: these functions do, from
the tool's own fields, so nothing is said that the tool did not return.
"""

from datetime import datetime

from nosh.receipts import Place, PlaceInfo

PRICES = {
    'PRICE_LEVEL_FREE': 'free to get in',
    'PRICE_LEVEL_INEXPENSIVE': 'budget-friendly',
    'PRICE_LEVEL_MODERATE': 'mid-range',
    'PRICE_LEVEL_EXPENSIVE': 'on the pricier side',
    'PRICE_LEVEL_VERY_EXPENSIVE': 'quite upscale, fine-dining territory',
}
"""Google's price buckets in words. A bucket not listed here is not said."""
MANY_REVIEWS = 1000


def rating(value: float | None) -> str:
    """ "a 4 out of five", "a 4.3 out of five"; '' with no rating. `:g` drops a trailing zero."""
    return f'a {value:g} out of five' if value is not None else ''


def reviews(count: int | None) -> str:
    """ "lots of reviews" for a well-known spot, the count otherwise; '' with none."""
    if not count:
        return ''
    return 'lots of reviews' if count >= MANY_REVIEWS else f'{count} reviews'


def address(text: str) -> str:
    """An address as directions: street and number, then the area. Country and numeric postcode dropped.

    ponytail: only numeric postcodes (08009, 75011) are dropped; a UK postcode
    stays. Use the tool's address components if that ever matters.
    """
    parts = text.split(', ')[:-1] or [text]
    kept = [' '.join(w for w in part.split() if not (w.isdigit() and len(w) >= 4)) for part in parts]
    return ', '.join(part for part in kept if part)


def today(lines: list[str] | None, now: datetime) -> str:
    """Today's hours from Google's weekday lines ("Monday: 9:00 AM - 1:00 AM"), as one clause; '' with none."""
    day = now.strftime('%A')
    line = next((line for line in lines or [] if line.startswith(f'{day}:')), '')
    if not line:
        return ''
    hours = ' '.join(line.split(':', 1)[1].replace('\u2013', ' to ').split())
    if hours.lower() == 'closed':
        return "They're closed today."
    if 'open 24 hours' in hours.lower():
        return "They're open all day today."
    return f"Today they're open {hours}."


def kind(place: Place | PlaceInfo) -> str:
    """ "a tapas bar"-style label from Google's type name, or '' with none."""
    label = (place.primary_type_display_name or '').lower()
    return f'{"an" if label[:1] in "aeiou" else "a"} {label}' if label else ''


def short(place: Place) -> str:
    """One search result in a breath: name, then what it is and its rating. Never price or hours."""
    said = [kind(place), rating(place.rating) and f'with {rating(place.rating)}']
    if (many := reviews(place.user_rating_count)) and place.rating is not None:
        said.append(f'from {many}')
    return ', '.join([place.name, ' '.join(s for s in said if s)]).rstrip(', ')


def details(info: PlaceInfo, now: datetime) -> tuple[str, str, str]:
    """The details said by code, with the price and today's hours in words: (spoken, price, today)."""
    price = PRICES.get(info.price_level or '', '')
    hours = today(info.opening_hours, now)
    about = ', and '.join(s for s in (rating(info.rating), price) if s)
    have = ' and '.join(
        s for s, there in (('their number', info.phone), ('a website', info.website)) if there
    )
    spoken = ' '.join(
        s
        for s in (
            f'{info.name} is on {address(info.address)}.',
            f"It's {about}." if about else '',
            hours,
            f"I've got {have} too, if you'd like them." if have else '',
            'Want to hear about another one, or is this the one?',
        )
        if s
    )
    return spoken, price, hours
