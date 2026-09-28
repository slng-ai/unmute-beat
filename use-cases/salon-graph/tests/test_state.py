"""The salon's state and value types, proved without audio, network or a model.

Two contracts, as before session 4:

* **Every write is validated, and a failed one changes nothing.** The state
  is one Pydantic model that core replaces whole (`FlowCall.update`,
  `collect`); a bad value anywhere in a batch leaves the old state standing.
* **Structural validity is not identity.** A real number can be the wrong
  person's. What the caller said (`customer_phone`) and what a lookup proved
  (`verified_phone`, `customer_status`) are separate fields, and only a
  receipt may set the second pair: the manifest declares them as the lookup's
  `assign:` targets, core refuses them to `collect`, and core clears them when
  the number they were proved for changes. Session 3's hand-written
  validator on the state is gone; the rule is the shared contract's.
"""

from datetime import date, datetime, time

from pydantic import TypeAdapter, ValidationError

from salon.diary import Diary
from salon.functions import Written
from salon.models.receipts import Held, Saved
from salon.models.shapes import Phone, Spoken, SubmittedPhone, phone_length
from salon.models.state import SalonState, prefetch_number
from tests.script import PHONE, opened

OTHER = '+34934567890'


def refused(fn, *args, **kwargs) -> ValidationError:
    """Run something that must raise a ValidationError, and hand it back."""
    try:
        fn(*args, **kwargs)
    except ValidationError as error:
        return error
    raise AssertionError(f'{fn} accepted {args or kwargs}, and must not have')


def a_failed_batch_commits_nothing():
    """The defect the whole state design exists for: two fields, one bad, and neither is written."""
    _, _, call, _ = opened(phone='')
    before = call.state
    refused(call.collect, customer_phone=PHONE, booking_time='half two')
    assert call.state is before and call.state.facts.customer_phone == ''
    call.collect(customer_phone=PHONE, booking_time='14:30')
    assert call.state.facts.booking_time == '14:30' and call.state is not before


def a_malformed_service_response_writes_nothing():
    """The diary handing back nonsense never reaches the state: it is validated into a receipt first."""
    for model, malformed in (
        (
            Saved,
            {
                'status': 'booked',
                'booking_id': 'BK1',
                'service': 'nails',
                'date': 'next Tuesday',
                'time': 'half nine',
                'spoken': '',
            },
        ),
        (Held, {'booking_id': '', 'service': 'haircut', 'date': '2026-02-30', 'time': '09:30', 'spoken': ''}),
    ):
        refused(model.model_validate, malformed)
    refused(Written.model_validate, {'status': 'maybe', 'phone': PHONE})


def only_a_receipt_sets_identity():
    """A model cannot write verification, however well formed: `collect` refuses the fields a receipt owns."""
    _, _, call, _ = opened(phone='')
    for owned in (
        {'verified_phone': PHONE},
        {'customer_status': 'existing'},
        {'booked': 'BK0001'},
        {'complaint_reference': 'CX1'},
    ):
        try:
            call.collect(**owned)
        except ValueError as error:
            assert 'not collected from the caller' in str(error), error
        else:
            raise AssertionError(f'{owned} was collected from a model')
    assert call.state.facts.verified_phone == '' and call.state.facts.customer_status == ''


def correcting_the_number_withdraws_the_identity():
    """Verified as one number, then they give another: the lookup's facts go with the old one, in the same write."""
    _, _, call, _ = opened(verified_phone=PHONE, customer_status='existing')
    call.collect(customer_phone=OTHER)
    facts = call.state.facts
    assert (facts.customer_phone, facts.verified_phone, facts.customer_status) == (OTHER, '', '')
    _, _, call, _ = opened(verified_phone=PHONE, customer_status='existing')
    call.collect(booking_day='Friday')
    assert call.state.facts.verified_phone == PHONE, 'a change to something else kept the identity'


def a_changed_request_drops_the_pick():
    """A pick belongs to the request it was offered for: a new day takes the old slot with it."""
    _, _, call, _ = opened(verified_phone=PHONE)
    call._offered = {'slot': {'2026-10-02T1400|haircut': 'Friday at 2'}}
    call.collect(slot='2026-10-02T1400|haircut')
    call.collect(booking_day='Saturday')
    assert call.state.facts.slot == '' and call._offered == {}


def the_historical_short_number_is_refused():
    r"""+348340464: eight national digits, and a real call offered it. Refused before any lookup."""
    refused(SalonState, customer_phone='+348340464')
    assert phone_length('+348340464') == 'too short'


def numbers_from_any_country_and_never_completed():
    """Any country's number, read out by its own grouping; digits with no country code are a question."""
    assert SalonState(customer_phone=PHONE).customer_phone == PHONE
    assert SalonState(customer_phone=OTHER).customer_phone == OTHER, 'landlines are customers too'
    for phone, spoken in (
        ('+12025550123', '+1 202-555-0123'),
        ('+442079460958', '+44 20 7946 0958'),
        ('+353861234567', '+353 86 123 4567'),
        ('+34671273454', '+34 671 27 34 54'),
    ):
        assert SalonState(customer_phone=phone).customer_phone == phone
        assert Spoken.validate_python(phone) == spoken, Spoken.validate_python(phone)
    for wrong in (
        '600111222',
        '0600111222',
        'not a number',
        '+999123456789',
        '+34500111222',
        '+3460011122',
        '+346001112223',
    ):
        refused(SalonState, customer_phone=wrong)
    assert SalonState().customer_phone == '', 'absence is not a wrong value'


def a_wrong_number_says_what_to_ask():
    """The reason reaches the verify agent through `collect`, worded as the next question, never a tool name.

    A number with a digit too many says the digits heard and asks which part
    is wrong: "say the whole number again" cost 85 s in Coval call UD4fMgUZ,
    the caller starting over in pieces that were each too short.
    """
    for bad, says in (
        ('600111222', 'Ask which country'),
        ('+3467127345', 'Ask for the rest of the digits'),
        ('+346712734544', 'I heard +34 671 273 454 4'),
        ('+346712734544', 'ask which part is wrong'),
        ('+999123456789', 'Ask for the whole number again'),
    ):
        error = str(refused(SalonState, customer_phone=bad))
        assert says in error, (bad, error)
    assert 'whole number again' not in str(refused(SalonState, customer_phone='+346712734544'))
    submitted = TypeAdapter(SubmittedPhone)
    assert 'never look up an empty number' in str(refused(submitted.validate_python, ''))
    assert TypeAdapter(Phone).validate_python('') == ''


def a_withheld_caller_id_is_absence():
    """Three ways a carrier says "no number", all meaning the same; a real number that starts with those digits is real."""
    assert prefetch_number('') == ''
    assert prefetch_number('unknown') == ''
    assert prefetch_number('+8656696') == '', 'the keypad spelling of "unknown"'
    assert prefetch_number(PHONE) == PHONE
    assert prefetch_number('+8656696123') == '+8656696123'


def times_and_days_are_the_salons():
    """A time wanted is HH:MM or a part of the day; a diary day is a real date; a slot time is local, on the grid."""
    assert SalonState(booking_time='14:00').booking_time == '14:00'
    for wrong in ('2pm', 'half two', '14:00:00'):
        refused(SalonState, booking_time=wrong)
    saved = {
        'status': 'booked',
        'booking_id': 'BK1',
        'service': 'haircut',
        'date': '2026-09-25',
        'spoken': 'x',
        'revision': 1,
    }
    assert Saved.model_validate(saved | {'time': '09:30'}).time == time(9, 30)
    for wrong in ('09:30:15', '09:30+02:00', '9:30', 1700000000):
        refused(Saved.model_validate, saved | {'time': wrong})
    for wrong in ('2026-02-30', '2027-02-29', 1700000000, datetime(2026, 9, 25, 10)):
        refused(Saved.model_validate, saved | {'time': '09:30', 'date': wrong})
    assert Saved.model_validate(saved | {'time': '09:30', 'date': '2028-02-29'}).date == date(2028, 2, 29)


def asking_to_complain_is_not_a_complaint():
    """A complaint made only of the words for wanting to complain describes nothing, and is refused with what to ask."""
    assert 'Ask them what happened' in str(
        refused(SalonState, complaint='I would like to make a complaint please')
    )
    assert SalonState(complaint='The colour came out patchy').complaint == 'The colour came out patchy'


def two_calls_share_nothing_but_the_diary():
    """P11: one compiled flow, two calls: their state, offers, questions and evidence are their own."""
    diary = Diary()
    _, _, first, _ = opened(diary=diary)
    _, _, second, _ = opened(diary=diary)
    first.collect(booking_day='Friday')
    first._offered = {'slot': {'x': 'y'}}
    assert second.state.facts.booking_day == '' and second._offered == {}
    assert first.services.call != second.services.call and first.services.diary is second.services.diary


def main() -> None:
    a_failed_batch_commits_nothing()
    a_malformed_service_response_writes_nothing()
    only_a_receipt_sets_identity()
    correcting_the_number_withdraws_the_identity()
    a_changed_request_drops_the_pick()
    the_historical_short_number_is_refused()
    numbers_from_any_country_and_never_completed()
    a_wrong_number_says_what_to_ask()
    a_withheld_caller_id_is_absence()
    times_and_days_are_the_salons()
    asking_to_complain_is_not_a_complaint()
    two_calls_share_nothing_but_the_diary()
    print('state ok: whole writes, owned identity, corrections, phone and time types, isolation')


if __name__ == '__main__':
    main()
