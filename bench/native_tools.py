"""Native reference wire adapter over the POC's actual business service.

The compiler copies this file for each native tool. It uses the same diary and
phone validation, not the Pydantic AI agent, consent matcher or execution core.
Consent remains the native reference's model-authored `confirmed` argument.
"""

import os

from pydantic import TypeAdapter, ValidationError

from salon import diary
from salon.state import SubmittedPhone


def find_or_create_customer(phone):
    """Validate the POC's phone scope before invoking its customer service."""
    try:
        phone = TypeAdapter(SubmittedPhone).validate_python(phone)
    except ValidationError, ValueError:
        return {
            'customer_status': 'invalid',
            'customer_phone': '',
            'summary': 'A Spanish phone number is required.',
        }
    return {**diary.find_or_create_customer(phone), 'summary': 'Customer identified.'}


def find_slots(customer_phone, date='', service='', prefer=''):
    """Use the same clock, offered slots, preferences and existing bookings."""
    result = diary.find_slots(
        customer_phone,
        date,
        service,
        diary.now(),
        offered_to=os.getenv('NATIVE_CALL_ID', 'native'),
        prefer=diary.resolve_time(prefer),
    )
    result.setdefault('slots', [])
    return result


def save_booking(customer_phone, action, confirmed=False, slot_id='', booking_id='', additional=False):
    """Preserve the native consent flag; share the POC's actual diary invariants."""
    if confirmed is not True:
        return {'status': 'not_confirmed', 'summary': 'The caller has not approved this change.'}
    try:
        result = diary.save_booking(
            customer_phone,
            action,
            slot_id,
            booking_id,
            additional,
            diary.now(),
            offered_to=os.getenv('NATIVE_CALL_ID', 'native'),
        )
    except diary.RefusedError as exc:
        return {'status': 'invalid', 'summary': str(exc)}
    appointment = {key: result[key] for key in ('booking_id', 'service', 'date', 'time', 'spoken')}
    appointment['action'] = action
    return {**result, 'appointment': appointment, 'summary': result['status']}


def record_complaint(customer_phone, summary, requested_resolution=''):
    """Keep the native write interface but use identical complaint records/receipts."""
    if not summary.strip():
        return {'complaint_id': '', 'status': 'invalid'}
    result = diary.record_complaint(customer_phone, summary, requested_resolution)
    return {
        'complaint_id': result['reference'],
        'status': 'recorded',
        'complaint': {
            'complaint_id': result['reference'],
            'summary': summary,
            'requested_resolution': requested_resolution,
        },
    }


def _demo():
    """The reference sees the same phone validation and slot/write results."""
    from datetime import datetime

    diary.reset()
    diary.set_clock(datetime(2026, 9, 21, 10, tzinfo=diary.TZ))
    assert find_or_create_customer('+15005550006')['customer_status'] == 'invalid'
    assert find_or_create_customer('600111222')['customer_status'] == 'invalid'
    phone = find_or_create_customer('+34600111222')['customer_phone']
    assert phone == '+34600111222'
    rows = find_slots(phone, 'Friday', 'haircut', '15:00')
    slot = next(slot for slot in rows['slots'] if slot['time'] == '15:00')
    assert save_booking(phone, 'book', slot_id=slot['slot_id'])['status'] == 'not_confirmed'
    saved = save_booking(phone, 'book', True, slot['slot_id'])
    assert saved['appointment']['time'] == '15:00'
    assert len(diary.bookings_for(phone, diary.now().date())) == 1
    receipt = record_complaint(phone, 'Uneven haircut', 'Full refund')
    assert diary.complaints()[0]['reference'] == receipt['complaint_id']
    print('native business adapter ok: shared phone, slots, consent flag, diary and complaint receipt')


if __name__ == '__main__':
    _demo()
