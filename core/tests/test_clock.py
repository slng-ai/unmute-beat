"""Run: uv run python -m tests.test_clock

The date every step's model is told (`core.clock`), with no use case at all.

Trace bbe42ad4 (2026-09-25, a Friday): the caller said "Saturday next week",
and the salon's model had no date to work it out from. Asserts, no pytest.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import Field

from core.clock import Today, label
from core.state import CallContext, VoiceState

FRIDAY = date(2026, 9, 25)


def labels_follow_the_week_the_call_is_in():
    """Monday-to-Sunday weeks, whatever weekday the call starts on."""
    assert label(FRIDAY, FRIDAY) == 'today'
    assert label(date(2026, 9, 26), FRIDAY) == 'tomorrow'
    assert label(date(2026, 9, 27), FRIDAY) == 'this Sunday'
    assert label(date(2026, 10, 3), FRIDAY) == 'Saturday next week'
    assert label(date(2026, 10, 5), FRIDAY) == 'Monday the week after next'
    assert label(date(2026, 10, 12), FRIDAY) == ''
    sunday = date(2026, 9, 27)
    assert label(date(2026, 9, 28), sunday) == 'tomorrow'
    assert label(date(2026, 10, 3), sunday) == 'Saturday next week', 'on a Sunday, next week starts tomorrow'
    for start in range(7):
        today = date(2026, 9, 21) + timedelta(days=start)
        next_monday = today + timedelta(days=7 - today.weekday())
        for offset in range(7):
            day = next_monday + timedelta(days=offset)
            if (day - today).days > 1:
                assert label(day, today).endswith('next week'), (today, day, label(day, today))


def the_calendar_is_what_the_model_reads():
    at = datetime(2026, 9, 25, 9, 30, tzinfo=ZoneInfo('Europe/Madrid'))
    only_today = Today(at).instructions()
    assert only_today == 'Today is Friday 2026-09-25 and the clock reads 09:30 (Europe/Madrid).', only_today
    two_weeks = Today(at, 14).instructions()
    assert '- Saturday 2026-10-03: Saturday next week' in two_weeks, two_weeks
    assert two_weeks.count('\n- ') == 14 and two_weeks.endswith('never by counting.'), two_weeks


def a_call_keeps_the_day_it_started_on():
    """Read once: a call across midnight does not change what "tomorrow" means halfway through."""

    class Late(CallContext):
        state: VoiceState = Field(default_factory=lambda: VoiceState(agent='desk'))
        clock: list[datetime] = Field(default_factory=list)

        def now(self) -> datetime:
            return self.clock.pop(0)

    call = Late(clock=[datetime(2026, 9, 25, 23, 59, tzinfo=UTC), datetime(2026, 9, 26, 0, 1, tzinfo=UTC)])
    assert call.today.date == FRIDAY
    assert call.today.date == FRIDAY, 'the clock was read a second time'
    assert CallContext.calendar_days == 0 and CallContext.timezone is UTC


if __name__ == '__main__':
    labels_follow_the_week_the_call_is_in()
    the_calendar_is_what_the_model_reads()
    a_call_keeps_the_day_it_started_on()
    print('clock ok: labels, calendar, one reading per call')
