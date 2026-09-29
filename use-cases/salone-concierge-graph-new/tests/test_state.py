"""Run: uv run python -m tests.test_state

The salon's state on core's value types: a phone number, a date the call's
clock has not left behind, a time or a part of the day, a complaint in the
caller's own words, and the lookup's receipt held whole. Asserts, no pytest.
"""

from datetime import date, time

from pydantic import ValidationError

import core
from salon.receipts import Slots
from salon.state import SalonState
from tests.script import CLOCK, PHONE, opened

TODAY = CLOCK.date()


def refused(values: dict, context: dict | None = None) -> str:
    try:
        SalonState.model_validate(values, context=context)
    except ValidationError as error:
        return error.errors()[0]['type']
    return ''


def the_fields_hold_only_what_they_mean():
    assert SalonState(customer_phone='+34 600 111 222').customer_phone == PHONE
    assert refused({'customer_phone': '600111222'}) == 'phone_no_country_code'
    assert refused({'booking_day': '2026-09-01'}, {'today': TODAY}) == 'date_past'
    assert SalonState.model_validate(
        {'booking_day': '2026-12-18'}, context={'today': TODAY}
    ).booking_day == date(2026, 12, 18), 'no booking horizon: only the past is refused'
    assert SalonState(booking_time='14:00').booking_time == time(14, 0)
    assert SalonState(booking_time='afternoon').booking_time == 'afternoon'
    assert refused({'booking_time': 'teatime'}), 'a time that is neither a time nor a part of the day'
    assert refused({'service': 'perm'}), 'a service the salon does not offer'
    said = {'said': ['the colour came out orange'], 'before': SalonState(), 'today': TODAY}
    assert refused({'complaint': 'my haircut was uneven'}, said) == 'not_quoted'
    assert SalonState.model_validate({'complaint': 'the colour came out orange'}, context=said).complaint


def a_receipt_is_strict_about_its_days():
    try:
        Slots.model_validate({'status': 'ok', 'date': 1700000000})
    except ValidationError:
        pass
    else:
        raise AssertionError('a number became a day')
    try:
        Slots.model_validate({'status': 'ok', 'surprise': True})
    except ValidationError:
        pass
    else:
        raise AssertionError('a receipt took a field nobody declared')


def the_caller_id_and_a_corrected_number():
    _, _, call, _ = opened()
    assert call.state.facts.customer_phone == PHONE, 'the carrier sent a real number: it is the identity'
    _, _, withheld, _ = opened(phone='anonymous')
    assert withheld.state.facts.customer_phone is None


def the_manifest_is_short():
    """The salon's authoring surface, counted: what this build asked of its author."""
    spec = core.load(__import__('salon.app', fromlist=['SPEC']).SPEC._root / 'agent.yaml')
    assert not spec.problems()
    assert all(isinstance(ref, str) for ref in spec.operations.values()), 'every operation is one line'


if __name__ == '__main__':
    the_fields_hold_only_what_they_mean()
    a_receipt_is_strict_about_its_days()
    the_caller_id_and_a_corrected_number()
    the_manifest_is_short()
    print('state ok: phone, dates, times, quotes, strict receipts, caller id, one line per operation')
