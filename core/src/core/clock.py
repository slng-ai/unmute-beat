"""Today's date, read once per call and told to every step's model.

The graph build pre-fetches `booking_date`, `booking_weekday` and
`salon_local_time` before the greeting, and its prompt says to work a
relative day out from them. This is the same fact for any use case, plus a
short calendar with each day's label worked out here, because counting days
is arithmetic a model gets wrong ("Saturday next week" said on a Friday).

In trace bbe42ad4 (2026-09-25) the salon told the model no date at all. The
caller said "Saturday next week", then "October the 3rd", and the only reply
was "Which day would you like?", twice.

A use case still decides a day in code where it can (the salon's
`resolve_day`); this is what the model knows when it talks about one, or
when the caller's words are beyond the code.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from datetime import datetime, timedelta

WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')


def label(day: dt.date, today: dt.date) -> str:
    """How a person names `day`, said on `today`: today, tomorrow, this Saturday, Saturday next week, or further.

    A week runs Monday to Sunday, so "next week" is the one after the week
    today is in, whatever weekday today is.
    """
    ahead = (day - today).days
    if ahead == 0:
        return 'today'
    if ahead == 1:
        return 'tomorrow'
    weeks = (day - timedelta(days=day.weekday()) - (today - timedelta(days=today.weekday()))).days // 7
    name = WEEKDAYS[day.weekday()]
    if weeks == 0:
        return f'this {name}'
    if weeks == 1:
        return f'{name} next week'
    if weeks == 2:
        return f'{name} the week after next'
    return ''


@dataclass(frozen=True)
class Today:
    """The call's clock, read once: the date, weekday and time in its timezone, and the days ahead.

    A call that crosses midnight keeps the day it started on. A date changing
    underneath a conversation leaves the caller and the agent disagreeing
    about what "tomorrow" means halfway through.
    """

    at: datetime
    """When the clock was read, in the call's timezone."""
    ahead: int = 0
    """How many days after today the calendar lists. 0 sends only the "Today is" line."""

    @property
    def date(self) -> dt.date:
        return self.at.date()

    def days(self) -> list[tuple[dt.date, str]]:
        """The next `ahead` days after today, each with its label."""
        return [
            (day, label(day, self.date))
            for day in (self.date + timedelta(days=n) for n in range(1, self.ahead + 1))
        ]

    def instructions(self) -> str:
        """What a step's model is told. The same text all call, so a provider's prompt cache still holds."""
        zone = str(self.at.tzinfo or '')
        text = (
            f'Today is {WEEKDAYS[self.date.weekday()]} {self.date.isoformat()} and the clock reads '
            f'{self.at:%H:%M}{f" ({zone})" if zone else ""}.'
        )
        if not self.ahead:
            return text
        lines = [
            f'- {WEEKDAYS[day.weekday()]} {day.isoformat()}{f": {name}" if name else ""}'
            for day, name in self.days()
        ]
        return '\n'.join(
            [
                f'{text} The next {self.ahead} days:',
                *lines,
                'Work a day out from this list, never by counting.',
            ]
        )
