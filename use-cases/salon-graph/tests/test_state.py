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

`tests/__main__.py` runs each module with `run_name='__main__'`, so this still runs there -- and a
debugging session can import one module without running every check in it.
"""

import asyncio
from datetime import date, datetime, time

from pydantic import BaseModel, ValidationError

from core.state import STRICT, CallContext, VoiceState, present
from salon.models.shapes import unsupported_region
from salon.models.state import (
    Appointment,
    Phone,
    SalonCall,
    SalonState,
    open_call,
    prefetch_number,
)

PHONE = '+34600111222'
OTHER = '+34934567890'


def _refused(fn, *args, **kwargs) -> ValidationError:
    """Run something that must raise a ValidationError, and hand it back."""
    try:
        fn(*args, **kwargs)
    except ValidationError as error:
        return error
    raise AssertionError(f'{fn} accepted {args or kwargs}, and must not have')


def an_authored_default_is_checked():
    """A default that breaks its own type must fail here, not on the first call.

    `validate_default` is off in Pydantic unless asked for, so without it a
    field declared `Phone = 'oops'` sits there looking fine until something
    reads it -- on a live call, in front of a caller.

    And the real state's own defaults are legal, which is the same check pointed at the code that
    ships.
    """

    class Broken(SalonState):
        customer_phone: Phone = 'oops'

    _refused(Broken)

    assert SalonState().customer_phone == ''


def assignment_is_checked():
    """Not a step in this agency"""
    state = SalonState()
    _refused(setattr, state, 'customer_verified', 'probably')
    _refused(setattr, state, 'customer_phone', '+348340464')
    _refused(setattr, state, 'pending', 'accounts')


def a_nested_model_cannot_be_edited_past_its_validator():
    """Reaching into `state.appointment` must not dodge the type.

    The parent's `validate_assignment` does not cover a write to a field of a
    model it holds: that assignment happens on the child, so the child needs
    its own. This is the case the contract calls out by name.
    """
    booked = Appointment(
        booking_id='BK0001',
        service='haircut',
        date='2026-09-21',
        time='09:00',
        spoken='Monday at 9:00 AM',
        action='book',
    )
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
        booking_id='BK0001',
        service='nails',
        date='not-a-date',
        time='half nine',
        spoken='x',
        action='book',
    )
    _refused(SalonState, appointment=smuggled)


def a_failed_batch_commits_nothing():
    """The defect this whole change exists for.

    `confirm_customer` wrote the phone number and then the verification status
    as two statements. A status the service could not produce failed the
    second one and left the first committed: a caller with a number on file
    who was never identified, which every prompt and guard downstream reads as
    further along than it is.

    The same batch with a legal status commits both.
    """
    call = open_call()
    before = call.state
    error = _refused(call.commit, customer_phone=PHONE, customer_verified='probably')
    assert 'customer_verified' in str(error)

    assert call.state is before, 'the failed commit must not have replaced the state'
    assert call.state.customer_phone == '', 'and the first field of the batch is not on it'
    assert call.state.customer_verified == ''

    call.commit(
        customer_phone=PHONE, phone_confirmed=True, customer_verified='existing', verified_phone=PHONE
    )
    assert (call.state.customer_phone, call.state.customer_verified) == (PHONE, 'existing')


def a_commit_replaces_the_state_object():
    """The state is swapped, not edited, and the context around it is stable.

    Which is the whole reason tools take the context: anything that captured
    `call.state` and wrote to it later would be writing to a snapshot nobody
    reads. There is no way to hold one, because `commit` is the only write.
    """
    call = open_call()
    first = call.state
    call.commit(customer_verified='existing', verified_phone=PHONE)
    assert call.state is not first, 'a commit builds a new candidate'
    assert first.customer_verified == '', 'and the old one is untouched, not mutated'


def a_commit_validates_rather_than_copies():
    """`model_copy(update=...)` would take any of this. `commit` takes none of it.

    Pydantic's own copy-with-update does no validation at all -- shown here so the difference is on
    the record rather than asserted from memory.
    """
    call = open_call()
    assert call.state.model_copy(update={'customer_verified': 'nonsense'}).customer_verified == 'nonsense'
    _refused(call.commit, customer_verified='nonsense')


def the_waiting_step_is_a_validated_name():
    call = open_call()
    call.commit(pending='booking')
    assert call.state.pending == 'booking'
    _refused(call.commit, pending='accounts')
    assert call.state.pending == 'booking', 'the refused name did not land'


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
    from core.graph import Node, route

    class Answered(VoiceState):
        model_config = STRICT
        agent: str = 'ask'
        wants_reminder: bool | None = None

    nodes = {
        'ask': Node(agent=None),  # ty: ignore[invalid-argument-type]
        'act': Node(agent=None, requires=('wants_reminder',), satisfied_by='ask'),  # ty: ignore[invalid-argument-type]
    }
    assert route(Answered(), nodes, 'act') == ('ask', 'act'), 'unanswered, so ask first'
    assert route(Answered(wants_reminder=False), nodes, 'act') == ('act', None), (
        'they said no, which is an answer'
    )
    assert route(Answered(wants_reminder=True), nodes, 'act') == ('act', None)


def a_malformed_service_response_writes_nothing():
    """The diary handing back nonsense must not reach the state.

    An annotation on the service function proves nothing: Python does not
    check it. What holds here is that the value is validated into
    `Appointment` before anything is committed, and the commit never runs.

    Nothing was committed, because the value never became an Appointment.
    """
    call = open_call(PHONE)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    before = call.state

    malformed = {
        'booking_id': 'BK0001',
        'service': 'nails',
        'date': 'next Tuesday',
        'time': 'half nine',
        'spoken': '',
        'action': 'book',
    }
    _refused(Appointment, **malformed)
    assert call.state is before and call.state.appointment is None


def the_historical_short_number_is_refused():
    r"""+348340464: eight national digits, and a real call offered it.

    The old `^\+[1-9]\d{6,14}$` took it -- nine digits is inside 7 to 15 --
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

    eight national digits

    ten
    """
    _refused(SalonState, customer_phone='+34500111222')
    _refused(SalonState, customer_phone='+3460011122')
    _refused(SalonState, customer_phone='+346001112223')


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
    """They said one number, then a different one. The second is what is held.

    And a correction to something invalid does not silently keep the old one *and* claim it was
    confirmed again: the commit fails whole.
    """
    call = open_call()
    call.commit(customer_phone=PHONE, phone_confirmed=True)
    call.commit(customer_phone=OTHER, phone_confirmed=True)
    assert call.state.customer_phone == OTHER
    _refused(call.commit, customer_phone='+34500111222', phone_confirmed=True)
    assert call.state.customer_phone == OTHER


def a_withheld_caller_id_is_absence():
    """Three ways a carrier says "no number", all of which mean the same thing.

    A real number that merely starts with those digits is a real number.
    """
    assert prefetch_number('') == ''
    assert prefetch_number('unknown') == ''
    assert prefetch_number('+8656696') == '', 'the keypad spelling of "unknown"'
    assert prefetch_number(PHONE) == PHONE
    assert prefetch_number('+8656696123') == '+8656696123'


def agreeing_to_a_wrong_number_is_not_identity():
    """The case structural validity cannot reach, kept visible instead of hidden.

    A caller who says yes to a readback of somebody else's number leaves a
    state that is valid in every way and wrong in the way that matters. So the
    three facts stay three fields: the digits are legal, the caller agreed,
    and the lookup found a record. None of them is the claim that this is the
    person, and nothing in the code pretends otherwise.

    Three separate fields, so nothing downstream can read "valid" as "theirs".
    """
    call = open_call()
    call.commit(
        customer_phone=OTHER, phone_confirmed=True, customer_verified='existing', verified_phone=OTHER
    )
    assert call.state.customer_phone == OTHER
    assert call.state.phone_confirmed, 'they said yes'
    assert call.state.customer_verified == 'existing', 'and a record was found'
    fields = SalonState.model_fields
    assert {'customer_phone', 'phone_confirmed', 'customer_verified'} <= set(fields)


def the_prefetched_number_is_not_confirmed():
    """A carrier's caller ID is a guess until the caller agrees to it out loud."""
    call = open_call(PHONE)
    assert call.state.customer_phone == PHONE
    assert not call.state.phone_confirmed, 'nobody has said yes to it yet'
    assert call.state.customer_verified == ''


def a_forged_verification_status_does_not_verify_anybody():
    """A correctly typed `existing` is not evidence that a lookup happened.

    Verification is a fact the customer service established, not a value
    anything upstream may assert. The status and the number it was established
    for are one fact, so a status arriving without its identity is not a
    weaker claim -- it is no claim, and the state drops it on construction.

    This is what stops a finisher, a replayed message or a hand-edited state
    from satisfying `booking`'s prerequisite without the lookup ever running.

    The same status, with the identity the lookup actually used, stands.
    """
    forged = SalonState(customer_phone=PHONE, customer_verified='existing')
    assert forged.customer_verified == '', forged
    assert not present(forged.customer_verified), 'so the booking prerequisite is unmet'

    real = SalonState(customer_phone=PHONE, customer_verified='existing', verified_phone=PHONE)
    assert real.customer_verified == 'existing'


def correcting_the_number_withdraws_the_old_authorisation():
    """Verified as one number, then they give a different one.

    Until the new number is looked up they are a different person, so the
    authority earned by the old one goes with it -- along with the readback
    they confirmed, which was a readback of something else. Their previous
    business record is not touched; only the authority to act on this call.

    And an old successful result cannot be replayed to get it back: the status still has to arrive
    with the number now on the call.
    """
    call = open_call()
    call.commit(
        customer_phone=PHONE, phone_confirmed=True, customer_verified='existing', verified_phone=PHONE
    )
    assert call.state.customer_verified == 'existing'

    call.commit(customer_phone=OTHER)
    assert call.state.customer_phone == OTHER
    assert call.state.customer_verified == '', 'the old lookup does not carry over'
    assert not call.state.phone_confirmed, 'nor does the old readback'
    assert call.state.verified_phone == ''

    call.commit(customer_verified='existing', verified_phone=PHONE)
    assert call.state.customer_verified == '', 'a stale identity is still stale'


def no_tool_but_the_verifier_takes_a_phone_number():
    """State-supplied identity is injected, never offered to the model.

    Every tool that touches a customer record gets `customer_phone` from the
    application. If the model could send one it could act on somebody else's
    record -- which is a forged identity arriving through the schema instead
    of through a status.

    Read off the **offered** schema, not the Python signature: a
    schema-authored tool takes the injected value as an ordinary parameter
    and simply does not put it in front of the model, and a check on the
    signature would call that a defect.

    `confirm_customer` is the exception by design: establishing the number is
    its whole job, and what it produces is checked by the lookup.

    And what each step actually offers, which is the thing the model reads.
    """
    import inspect

    from salon import agents

    allowed = {'confirm_customer'}
    for name in agents.ACTIONS - allowed:
        tool = getattr(agents, name, None)
        if tool is None:
            continue
        taken = set(inspect.signature(tool).parameters)
        assert not (taken & {'customer_phone', 'number'}), f'{name}: {sorted(taken)}'

    for step, node in agents.AGENTS.items():
        for tool in node.agent._function_toolset.tools.values():
            offered = set(tool.function_schema.json_schema.get('properties') or {})
            assert not (offered & {'phone', 'customer_phone', 'number'}), f'{step}.{tool.name}: {offered}'


async def a_schema_authored_tool_validates_what_a_typed_one_does():
    """S20. `Tool.from_schema` in 2.46.0 validates nothing on its own.

    Without an `args_validator` the handler is called with whatever the model
    sent -- the schema is advertising, not enforcement. `schema_tool` builds
    the validator from the schema, refuses a model that tries to fill in a
    field the application supplies, injects the current value, and validates
    the whole effective set before the handler is reached.

    The counter is the assertion. Every refusal below has to leave it at
    zero: a tool that validates *after* it has acted has not validated.

    The injected field is not in front of the model at all.

    A missing field, and a field of the wrong type. Neither reaches it.

    The model filling in what the application supplies.

    And a legal call, with the salon's own value in it and not the model's.

    A schema this cannot honour is refused when the tool is built, not when it is called: a
    converter that quietly drops a keyword produces a tool that validates less than its schema
    claims and nobody can tell.
    """
    from pydantic_ai import ModelRetry, RunContext

    from core.policies import UnsupportedSchemaError, schema_tool
    from salon.models.state import SalonCall, SalonState

    calls: list[dict] = []

    async def handler(ctx, **arguments):
        calls.append(arguments)
        return 'done'

    schema = {
        'type': 'object',
        'properties': {
            'note': {'type': 'string', 'description': 'what happened'},
            'urgent': {'type': 'boolean', 'description': 'whether it is urgent'},
            'phone': {'type': 'string', 'description': 'supplied by the salon'},
        },
        'required': ['note', 'urgent', 'phone'],
    }
    tool = schema_tool(
        'log_it', 'Log it.', schema, handler, injected={'phone': lambda call: call.state.customer_phone}
    )
    call = SalonCall(state=SalonState(agent='complaints', customer_phone=PHONE, verified_phone=PHONE))
    ctx = RunContext(deps=call, model=None, usage=None)  # ty: ignore[invalid-argument-type]
    validate, invoke = tool.args_validator, tool.function

    assert set(tool.function_schema.json_schema['properties']) == {'note', 'urgent'}

    for wrong in ({'note': 'a mess'}, {'note': 'a mess', 'urgent': 'very'}):
        try:
            validate(ctx, **wrong)
        except ValidationError:
            pass
        else:
            raise AssertionError(f'invalid arguments were accepted: {wrong}')
    assert calls == [], calls

    try:
        validate(ctx, note='a mess', urgent=True, phone='+34934567890')
    except ModelRetry as refused:
        assert 'supplied by the salon' in str(refused), refused
    else:
        raise AssertionError('the model overrode an injected field')
    assert calls == [], calls

    good = validate(ctx, note='a mess', urgent=True)
    assert await invoke(ctx, **good) == 'done'  # ty: ignore[invalid-argument-type]
    assert calls == [{'note': 'a mess', 'urgent': True, 'phone': PHONE}], calls

    for bad in (
        {'type': 'object', 'properties': {'x': {'type': 'object'}}},
        {'type': 'object', 'properties': {'x': {'type': 'string', 'pattern': '^a'}}},
        {'type': 'object', 'oneOf': [{'required': ['x']}], 'properties': {}},
        {'type': 'array', 'items': {'type': 'string'}},
    ):
        try:
            schema_tool('nope', '', bad, handler)
        except UnsupportedSchemaError:
            continue
        raise AssertionError(f'an unsupported schema became a tool: {bad}')


def the_business_state_carries_no_history_or_handles():
    """History and callbacks live on the context, which is why `commit` can work.

    A state that carried a message list would be rebuilt from `model_dump`
    every commit, and one that carried a bound method could not be rebuilt at
    all. Both are also JSON-schema poison: the prompts compile against this.

    Nothing unrenderable reached the schema the prompts are compiled against.
    """
    assert 'messages' not in SalonState.model_fields
    assert 'messages' in CallContext.model_fields
    for private in ('_say', '_end', '_covered'):
        assert private not in SalonState.model_fields
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


def a_commit_refuses_a_field_that_does_not_exist():
    """A typo in a tool was a write that silently did not happen.

    Pydantic drops an unknown keyword by default, so `commit(phone_confirmd=
    True)` rebuilt the state from its own values, changed nothing, and
    returned as if it had. Every other write path here raises on bad input;
    this one accepted a name that means nothing and said so to nobody.

    The correctly spelled one still works, which is what makes the refusal a typo check rather than
    a wall.
    """
    call = open_call(PHONE)
    before = call.state
    try:
        call.commit(booking_dai='2026-09-25')
    except ValueError as refused:
        assert 'no such field' in str(refused), refused
    else:
        raise AssertionError('a keyword naming no field must not be accepted')
    assert call.state is before, 'and nothing was rebuilt on the way to refusing'
    call.commit(booking_day='2026-09-25')
    assert call.state.booking_day == date(2026, 9, 25)


def a_day_that_does_not_exist_is_refused():
    """`2026-02-30` is the right shape and not a date.

    The validator checked spelling with a regular expression, so a day the
    calendar does not have passed into state and only the diary's own parser
    later objected -- by which point it is a value the prompts have rendered
    and the model has been told is real.

    A real `date` now, parsed from the ISO string a service sends.

    A leap day the calendar has, and one it does not.

    Not a day, however plausible the number looks: Pydantic's ordinary lax mode reads 1700000000 as
    a date in 2023, and a number that reached this field by accident would become a real, wrong
    appointment.

    And a day round-trips through JSON as the string it arrived as.
    """
    _refused(SalonState, agent='concierge', booking_day='2026-02-30')
    _refused(SalonState, agent='concierge', booking_day='2026-13-01')
    assert SalonState(booking_day='2026-09-25').booking_day == date(2026, 9, 25)
    assert SalonState(booking_day=date(2026, 9, 25)).booking_day == date(2026, 9, 25)
    assert SalonState(booking_day=None).booking_day is None, 'absence is still not a wrong value'
    assert SalonState(booking_day='2028-02-29').booking_day == date(2028, 2, 29)
    _refused(SalonState, agent='concierge', booking_day='2027-02-29')
    _refused(SalonState, agent='concierge', booking_day=1700000000)
    _refused(SalonState, agent='concierge', booking_day=datetime(2026, 9, 25, 10, 0))
    assert SalonState(booking_day='2026-09-25').model_dump(mode='json')['booking_day'] == '2026-09-25'


def a_time_is_a_local_salon_time():
    """No timezone on it, no seconds, and never a number.

    The salon has one timezone and a half-hour grid, so a time arriving with
    an offset is asserting something about where it is rather than answering
    the diary, and a time with seconds on it did not come off that grid.

    And out again as the `HH:MM` the wire uses.
    """
    at = time(9, 30)
    assert (
        Appointment(
            booking_id='BK1', service='haircut', date='2026-09-25', time='09:30', spoken='x', action='book'
        ).time
        == at
    )
    for wrong in ('09:30:15', '09:30+02:00', '9:30', 1700000000):
        _refused(
            Appointment,
            booking_id='BK1',
            service='haircut',
            date='2026-09-25',
            time=wrong,
            spoken='x',
            action='book',
        )
    booked = Appointment(
        booking_id='BK1', service='haircut', date='2026-09-25', time=at, spoken='x', action='book'
    )
    assert booked.model_dump(mode='json') | {} == {
        'booking_id': 'BK1',
        'service': 'haircut',
        'date': '2026-09-25',
        'time': '09:30:00',
        'spoken': 'x',
        'action': 'book',
    }


def a_submitted_number_is_not_the_same_type_as_a_stored_one():
    """The state may hold no number. A lookup may not be given none.

    One type did both, and `confirm_customer(phone='')` was the result: an
    empty string submitted as an identity, looked up, and answered
    'created'. `tests.test_salon` runs that through the real invocation; this
    is the type rule underneath it.

    And the stored one still permits absence, because that is what a field holds before anybody has
    said a number.
    """
    from pydantic import TypeAdapter

    from salon.models.shapes import SubmittedPhone

    submitted = TypeAdapter(SubmittedPhone)
    assert submitted.validate_python(PHONE) == PHONE
    _refused(submitted.validate_python, '')
    assert TypeAdapter(Phone).validate_python('') == ''

    too_long = str(_refused(submitted.validate_python, '+346721273454'))
    assert '10 digits' in too_long and "'correction'" in too_long, (
        'a misheard extra digit is not a missing one'
    )
    too_short = str(_refused(submitted.validate_python, '+348340464'))
    assert '7 digits' in too_short and "'incomplete'" in too_short, too_short


async def main():
    an_authored_default_is_checked()
    assignment_is_checked()
    a_nested_model_cannot_be_edited_past_its_validator()
    a_prebuilt_model_is_revalidated()
    a_failed_batch_commits_nothing()
    a_commit_replaces_the_state_object()
    a_commit_validates_rather_than_copies()
    a_commit_refuses_a_field_that_does_not_exist()
    a_day_that_does_not_exist_is_refused()
    a_time_is_a_local_salon_time()
    a_submitted_number_is_not_the_same_type_as_a_stored_one()
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
    a_forged_verification_status_does_not_verify_anybody()
    correcting_the_number_withdraws_the_old_authorisation()
    no_tool_but_the_verifier_takes_a_phone_number()
    await a_schema_authored_tool_validates_what_a_typed_one_does()
    the_business_state_carries_no_history_or_handles()
    every_business_model_opted_in()
    print('ok')


if __name__ == '__main__':
    asyncio.run(main())
