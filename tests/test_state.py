"""Run: uv run --env-file .env python -m tests.test_state

State and phone capture, proved without audio, without network and without a
model. Everything here is a pure function over typed values, so it runs in
milliseconds and it fails for exactly one reason.

Two contracts:

* **Every supported write is validated, and a failed one changes nothing.**
  Not "the annotation says str": annotations alone validate nothing on
  assignment, nothing on a default, and nothing on a nested model handed in
  ready-made. Each of those is a separate opt-in and each has its own check
  below.
* **Structural validity is not identity.** A number can be a real Spanish
  number and still be the wrong person's. The three facts are kept apart and
  tested apart.

Asserts, no pytest, no fixtures, same as the rest.
"""

import asyncio

from pydantic import BaseModel, ValidationError

from salon.state import (
    Appointment,
    Phone,
    SalonCall,
    SalonState,
    open_call,
    prefetch_number,
    unsupported_region,
)
from voice.harness import STRICT, CallContext, VoiceState, present

PHONE = '+34600111222'
OTHER = '+34934567890'


def _refused(fn, *args, **kwargs) -> ValidationError:
    """Run something that must raise a ValidationError, and hand it back."""
    try:
        fn(*args, **kwargs)
    except ValidationError as error:
        return error
    raise AssertionError(f'{fn} accepted {args or kwargs}, and must not have')


# --- defaults are validated --------------------------------------------------


def an_authored_default_is_checked():
    """A default that breaks its own type must fail here, not on the first call.

    `validate_default` is off in Pydantic unless asked for, so without it a
    field declared `Phone = 'oops'` sits there looking fine until something
    reads it -- on a live call, in front of a caller.
    """
    class Broken(SalonState):
        customer_phone: Phone = 'oops'

    _refused(Broken)

    # And the real state's own defaults are legal, which is the same check
    # pointed at the code that ships.
    assert SalonState().customer_phone == ''


# --- assignment and nested assignment are validated --------------------------


def assignment_is_checked():
    state = SalonState()
    _refused(setattr, state, 'customer_verified', 'probably')
    _refused(setattr, state, 'customer_phone', '+348340464')
    _refused(setattr, state, 'pending', 'accounts')  # not a step in this agency


def a_nested_model_cannot_be_edited_past_its_validator():
    """Reaching into `state.appointment` must not dodge the type.

    The parent's `validate_assignment` does not cover a write to a field of a
    model it holds: that assignment happens on the child, so the child needs
    its own. This is the case the contract calls out by name.
    """
    booked = Appointment(booking_id='BK0001', service='haircut', date='2026-09-21',
                         time='09:00', spoken='Monday at 9:00 AM', action='book')
    call = SalonCall(state=SalonState(appointment=booked))
    _refused(setattr, call.state.appointment, 'time', 'half nine')
    _refused(setattr, call.state.appointment, 'service', 'nails')


def a_prebuilt_model_is_revalidated():
    """An instance of the right class is not evidence that its contents are legal.

    `model_construct` skips validation, which is exactly how a bad value gets
    into a well-typed object. `revalidate_instances='always'` is what catches
    it on the way into the state.
    """
    smuggled = Appointment.model_construct(
        booking_id='BK0001', service='nails', date='not-a-date',
        time='half nine', spoken='x', action='book',
    )
    _refused(SalonState, appointment=smuggled)


# --- one commit, or none of it -----------------------------------------------


def a_failed_batch_commits_nothing():
    """The defect this whole change exists for.

    `confirm_customer` wrote the phone number and then the verification status
    as two statements. A status the service could not produce failed the
    second one and left the first committed: a caller with a number on file
    who was never identified, which every prompt and guard downstream reads as
    further along than it is.
    """
    call = open_call()
    before = call.state
    error = _refused(call.commit, customer_phone=PHONE, customer_verified='probably')
    assert 'customer_verified' in str(error)

    assert call.state is before, 'the failed commit must not have replaced the state'
    assert call.state.customer_phone == '', 'and the first field of the batch is not on it'
    assert call.state.customer_verified == ''

    # The same batch with a legal status commits both.
    call.commit(customer_phone=PHONE, phone_confirmed=True, customer_verified='existing')
    assert (call.state.customer_phone, call.state.customer_verified) == (PHONE, 'existing')


def a_commit_replaces_the_state_object():
    """The state is swapped, not edited, and the context around it is stable.

    Which is the whole reason tools take the context: anything that captured
    `call.state` and wrote to it later would be writing to a snapshot nobody
    reads. There is no way to hold one, because `commit` is the only write.
    """
    call = open_call()
    first = call.state
    call.commit(customer_verified='existing')
    assert call.state is not first, 'a commit builds a new candidate'
    assert first.customer_verified == '', 'and the old one is untouched, not mutated'


def a_commit_validates_rather_than_copies():
    """`model_copy(update=...)` would take any of this. `commit` takes none of it."""
    call = open_call()
    # Pydantic's own copy-with-update does no validation at all -- shown here
    # so the difference is on the record rather than asserted from memory.
    assert call.state.model_copy(update={'customer_verified': 'nonsense'}).customer_verified == 'nonsense'
    _refused(call.commit, customer_verified='nonsense')


def the_waiting_step_is_a_validated_name():
    call = open_call()
    call.commit(pending='booking')
    assert call.state.pending == 'booking'
    _refused(call.commit, pending='accounts')
    assert call.state.pending == 'booking', 'the refused name did not land'


# --- presence, spelled out ---------------------------------------------------


def presence_is_not_truthiness():
    """`False` and `0` are answers. `None` and a permitted '' are not.

    A prerequisite tested with plain truthiness treats a caller who said no as
    a caller who was never asked, and interposes the same step a second time.
    """
    assert present(False) and present(0) and present('no')
    assert not present(None) and not present('')


def presence_governs_the_prerequisite():
    """A boolean prerequisite filled with `False` is filled.

    Built on a throwaway agency rather than the salon's, which has no boolean
    prerequisite today -- the rule is in `route`, and it should be provable
    without waiting for a package that needs it.
    """
    from voice.harness import Node, route

    class Answered(VoiceState):
        model_config = STRICT
        agent: str = 'ask'
        wants_reminder: bool | None = None

    nodes = {
        'ask': Node(agent=None),  # type: ignore[arg-type]
        'act': Node(agent=None, requires=('wants_reminder',), satisfied_by='ask'),  # type: ignore[arg-type]
    }
    assert route(Answered(), nodes, 'act') == ('ask', 'act'), 'unanswered, so ask first'
    assert route(Answered(wants_reminder=False), nodes, 'act') == ('act', None), \
        'they said no, which is an answer'
    assert route(Answered(wants_reminder=True), nodes, 'act') == ('act', None)


# --- a service response is not trusted because a function returned it --------


def a_malformed_service_response_writes_nothing():
    """The diary handing back nonsense must not reach the state.

    An annotation on the service function proves nothing: Python does not
    check it. What holds here is that the value is validated into
    `Appointment` before anything is committed, and the commit never runs.
    """
    call = open_call(PHONE)
    call.commit(customer_verified='existing')
    before = call.state

    malformed = {'booking_id': 'BK0001', 'service': 'nails', 'date': 'next Tuesday',
                 'time': 'half nine', 'spoken': '', 'action': 'book'}
    _refused(Appointment, **malformed)
    # Nothing was committed, because the value never became an Appointment.
    assert call.state is before and call.state.appointment is None


# --- the phone policy --------------------------------------------------------


def the_historical_short_number_is_refused():
    """+348340464: eight national digits, and a real call offered it.

    The old `^\\+[1-9]\\d{6,14}$` took it -- nine digits is inside 7 to 15 --
    and it went to the customer lookup. Refused here, before any lookup: this
    is a validation error on the tool's own argument, so the tool body never
    runs.
    """
    error = _refused(SalonState, customer_phone='+348340464')
    assert not unsupported_region(error), 'it is malformed, not foreign'


def a_whole_valid_number_is_accepted():
    assert SalonState(customer_phone=PHONE).customer_phone == PHONE
    assert SalonState(customer_phone=OTHER).customer_phone == OTHER, 'landlines are customers too'


def absence_is_not_a_wrong_value():
    """'' is the one permitted sentinel, and it means nobody has said anything yet.

    Refusing it would deadlock `verify`, whose whole job is to fill the field
    and which has nothing else to put there on the way in.
    """
    assert SalonState().customer_phone == ''
    assert not present(SalonState().customer_phone)
    assert present(SalonState(customer_phone=PHONE).customer_phone)


def a_number_is_never_completed_for_the_caller():
    """Digits with no country code are a question, not a Spanish number.

    `PhoneNumberValidator(default_region='ES')` would have turned this into
    +34600111222 on its own. That is the guess `verify.md` tells the model
    never to make, so the validator is not given a default region either.
    """
    _refused(SalonState, customer_phone='600111222')
    _refused(SalonState, customer_phone='0600111222')


def another_country_is_a_different_conversation():
    """A valid US number is refused, and refused *differently*.

    One is "say that again", the other is "we can only look up Spanish
    numbers, which country are you calling from". The agent needs to tell them
    apart, so the distinction is carried and tested rather than collapsed.
    """
    foreign = _refused(SalonState, customer_phone='+16502530000')
    assert unsupported_region(foreign)
    malformed = _refused(SalonState, customer_phone='not a number')
    assert not unsupported_region(malformed)


def a_number_shaped_non_number_is_refused():
    """Right country, right length, and still nobody's number.

    +34500111222 is nine digits after +34 and passes every length rule anyone
    would write by hand. libphonenumber knows 5xx is not an ordinary Spanish
    contact range, which is the reason this is not a regex.
    """
    _refused(SalonState, customer_phone='+34500111222')
    _refused(SalonState, customer_phone='+3460011122')   # eight national digits
    _refused(SalonState, customer_phone='+346001112223')  # ten


def a_number_said_in_pieces_arrives_whole():
    """Split across utterances, the state only ever sees the finished number.

    Partial digits are not written down and then corrected: the field holds
    absence until there is a whole number to hold, so there is no half-number
    for a prompt to read back.
    """
    call = open_call()
    for fragment in ('+34', '+3460', '+346001112'):
        _refused(call.commit, customer_phone=fragment)
        assert call.state.customer_phone == '', 'a fragment is not a value'
    call.commit(customer_phone=PHONE, phone_confirmed=True)
    assert call.state.customer_phone == PHONE


def a_corrected_digit_group_replaces_the_whole_number():
    """They said one number, then a different one. The second is what is held."""
    call = open_call()
    call.commit(customer_phone=PHONE, phone_confirmed=True)
    call.commit(customer_phone=OTHER, phone_confirmed=True)
    assert call.state.customer_phone == OTHER
    # And a correction to something invalid does not silently keep the old one
    # *and* claim it was confirmed again: the commit fails whole.
    _refused(call.commit, customer_phone='+34500111222', phone_confirmed=True)
    assert call.state.customer_phone == OTHER


def a_withheld_caller_id_is_absence():
    """Three ways a carrier says "no number", all of which mean the same thing."""
    assert prefetch_number('') == ''
    assert prefetch_number('unknown') == ''
    assert prefetch_number('+8656696') == '', 'the keypad spelling of "unknown"'
    assert prefetch_number(PHONE) == PHONE
    # A real number that merely starts with those digits is a real number.
    assert prefetch_number('+8656696123') == '+8656696123'


def agreeing_to_a_wrong_number_is_not_identity():
    """The case structural validity cannot reach, kept visible instead of hidden.

    A caller who says yes to a readback of somebody else's number leaves a
    state that is valid in every way and wrong in the way that matters. So the
    three facts stay three fields: the digits are legal, the caller agreed,
    and the lookup found a record. None of them is the claim that this is the
    person, and nothing in the code pretends otherwise.
    """
    call = open_call()
    call.commit(customer_phone=OTHER, phone_confirmed=True, customer_verified='existing')
    assert call.state.customer_phone == OTHER
    assert call.state.phone_confirmed, 'they said yes'
    assert call.state.customer_verified == 'existing', 'and a record was found'
    # Three separate fields, so nothing downstream can read "valid" as "theirs".
    fields = SalonState.model_fields
    assert {'customer_phone', 'phone_confirmed', 'customer_verified'} <= set(fields)


def the_prefetched_number_is_not_confirmed():
    """A carrier's caller ID is a guess until the caller agrees to it out loud."""
    call = open_call(PHONE)
    assert call.state.customer_phone == PHONE
    assert not call.state.phone_confirmed, 'nobody has said yes to it yet'
    assert call.state.customer_verified == ''


# --- the context holds nothing a candidate could not be rebuilt from ---------


def the_business_state_carries_no_history_or_handles():
    """History and callbacks live on the context, which is why `commit` can work.

    A state that carried a message list would be rebuilt from `model_dump`
    every commit, and one that carried a bound method could not be rebuilt at
    all. Both are also JSON-schema poison: the prompts compile against this.
    """
    assert 'messages' not in SalonState.model_fields
    assert 'messages' in CallContext.model_fields
    for private in ('_say', '_end', '_covered'):
        assert private not in SalonState.model_fields
    # Nothing unrenderable reached the schema the prompts are compiled against.
    schema = SalonCall.model_json_schema()
    assert 'state' in schema['properties']


def every_business_model_opted_in():
    """The three settings, checked on the classes rather than trusted.

    A new state model that forgets one of them is the failure this catches:
    it would look right, pass its own tests, and silently accept an unvalidated
    assignment on a live call.
    """
    for model in (VoiceState, SalonState, Appointment):
        assert issubclass(model, BaseModel)
        for setting in ('validate_default', 'validate_assignment'):
            assert model.model_config.get(setting), f'{model.__name__} is missing {setting}'
        assert model.model_config.get('revalidate_instances') == 'always', model.__name__


async def main():
    an_authored_default_is_checked()
    assignment_is_checked()
    a_nested_model_cannot_be_edited_past_its_validator()
    a_prebuilt_model_is_revalidated()
    a_failed_batch_commits_nothing()
    a_commit_replaces_the_state_object()
    a_commit_validates_rather_than_copies()
    the_waiting_step_is_a_validated_name()
    presence_is_not_truthiness()
    presence_governs_the_prerequisite()
    a_malformed_service_response_writes_nothing()
    the_historical_short_number_is_refused()
    a_whole_valid_number_is_accepted()
    absence_is_not_a_wrong_value()
    a_number_is_never_completed_for_the_caller()
    another_country_is_a_different_conversation()
    a_number_shaped_non_number_is_refused()
    a_number_said_in_pieces_arrives_whole()
    a_corrected_digit_group_replaces_the_whole_number()
    a_withheld_caller_id_is_absence()
    agreeing_to_a_wrong_number_is_not_identity()
    the_prefetched_number_is_not_confirmed()
    the_business_state_carries_no_history_or_handles()
    every_business_model_opted_in()
    print('ok')


asyncio.run(main())
