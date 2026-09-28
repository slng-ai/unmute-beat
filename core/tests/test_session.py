"""The session's guarantees on the public clinic slice: atomic state, owned fields, isolation, stale work, evidence.

Each check is one of session 2's required negative or positive cases, run
through the real engine with a scripted model (`tests.test_public.Script`).
A scripted yes tests authorisation, not whether a model understands consent.
"""

import asyncio
from contextlib import suppress

from pydantic import ValidationError

import core
from core.state import turn_generation
from tests.clinic_flow.clinic import Backend, ClinicState, Contact
from tests.test_public import Script, delivered, opened, turn


def raises(error: type[Exception], fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except error:
        return
    raise AssertionError(f'{fn.__name__} accepted what it must refuse')


async def at_the_read_back(script: Script, patient: str = 'P-1234', *, heard: bool = True):
    """A clinic call whose last turn read back a Tuesday 10:00 booking; `heard` says the reader got it."""
    flow, call, backend = opened(script)
    delivered(call, await turn(flow, call, script, f'{patient}.', patient=patient))
    reply = await turn(flow, call, script, 'Ten.', slot='1000')
    assert call.state.agent == 'confirm', call.state
    if heard:
        delivered(call, reply)
    return flow, call, backend


async def invalid_updates_change_nothing():
    """A multi-field update with one bad value, a bad nested value, an unknown nested key: the original stands."""
    _, call, _ = opened(Script())
    call.update(contact=Contact(phone='555 0100'), notes=['kept'])
    before, facts = call.state, call.state.facts
    raises(ValidationError, call.update, patient='P-1234', slot='1300')
    raises(ValidationError, call.update, contact={'phone': '1'})
    raises(ValidationError, call.update, contact={'phone': '555 0199', 'fax': 'yes'})
    raises(ValueError, call.update, nickname='Dr Who')
    assert call.state is before and call.state.facts is facts
    assert facts.patient is None and facts.slot is None and facts.contact == Contact(phone='555 0100')
    assert facts.notes == ['kept'], facts


async def the_model_writes_only_collected_fields():
    """A protected field through collection is refused before the backend is asked anything."""
    script = Script()
    flow, call, backend = opened(script)
    raises(ValueError, call.collect, reference='CL999')
    raises(ValueError, call.collect, patient='P-1234', reference='CL999')
    assert call.state.facts.patient is None and call.state.facts.reference == ''
    await turn(flow, call, script, 'P-1234, reference CL999.', patient='P-1234', reference='CL999')
    assert call.state.facts.reference == '' and backend.calls == 0, (call.state.facts, backend.calls)


async def what_the_caller_keeps_cannot_reach_the_state():
    """Mutating an object after it was committed, or after it opened a session, changes nothing saved."""
    script = Script()
    notes, contact = ['first'], Contact(phone='555 0100')
    _, call, _ = opened(script)
    call.update(notes=notes, contact=contact)
    notes.append('second')
    contact.phone = '555 0199'
    assert call.state.facts.notes == ['first'] and call.state.facts.contact.phone == '555 0100'
    flow = core.load('tests/clinic_flow/flow.yaml').compile(model=script.model)
    given = ClinicState(notes=['given'])
    session = flow.open(given, services=Backend())
    given.notes.append('later')
    assert session.state.facts.notes == ['given'], session.state.facts


async def two_calls_on_one_flow_share_nothing():
    """Two sessions interleaved on one compiled flow: separate proposals, readings, histories, state and receipts."""
    script = Script()
    flow = core.load('tests/clinic_flow/flow.yaml').compile(model=script.model)
    backend = Backend()
    one, two = flow.open(ClinicState(), services=backend), flow.open(ClinicState(), services=backend)
    delivered(one, await turn(flow, one, script, 'P-1111.', patient='P-1111'))
    delivered(two, await turn(flow, two, script, 'P-2222.', patient='P-2222'))
    delivered(one, await turn(flow, one, script, 'Nine.', slot='0900'))
    delivered(two, await turn(flow, two, script, 'Ten.', slot='1000'))
    assert one._asked.owner == 'P-1111' and two._asked.owner == 'P-2222'
    script.label = 'accept'
    await turn(flow, one, script, 'Yes.')
    assert two._interpretations == {} or 'confirm' not in two._interpretations, (
        'a reading leaked across calls'
    )
    await turn(flow, two, script, 'Yes.')
    assert backend.bookings == {'CL001': ('P-1111', '0900'), 'CL002': ('P-2222', '1000')}, backend.bookings
    assert [e.receipt['reference'] for e in one.evidence] == ['CL001']
    assert [e.receipt['reference'] for e in two.evidence] == ['CL002']
    assert one.messages is not two.messages and one._interpretations is not two._interpretations
    assert one.state.facts.slot == '0900' and two.state.facts.slot == '1000'


async def only_the_delivered_current_proposal_authorises():
    """Identity and argument revisions, a stale proposal, an unrelated yes, a conditional yes, a replay: no write."""
    script = Script()
    for change in ({'patient': 'P-9999'}, {'slot': '0900'}):
        flow, call, backend = await at_the_read_back(script)
        call.collect(**change)
        script.label = 'accept'
        reply = await turn(flow, call, script, 'Yes.')
        assert backend.calls == 0, f'a yes to the old read-back booked after {change}'
        assert reply.segments[-1].text.startswith('Shall I book'), 'the new arguments are read back instead'

    flow, call, backend = await at_the_read_back(script)
    script.label = 'unclear'
    await turn(flow, call, script, 'Hmm, let me think.')
    await turn(flow, call, script, 'Right.')
    script.label = 'accept'
    await turn(flow, call, script, 'Yes.')
    assert backend.calls == 0, 'a stale read-back was answered'

    flow, call, backend = opened(script)
    delivered(call, await turn(flow, call, script, 'P-1234.', patient='P-1234'))
    script.label = 'accept'
    await turn(flow, call, script, 'Yes.')
    assert backend.calls == 0 and call.state.agent == 'slot', 'a yes to nothing booked'

    flow, call, backend = await at_the_read_back(script)
    script.label = 'conditional'
    reply = await turn(flow, call, script, 'Yes, if it is not raining.')
    assert backend.calls == 0 and reply.segments[-1].text.startswith('Shall I book'), (
        'a conditional yes booked'
    )

    flow, call, backend = await at_the_read_back(script)
    script.label = 'accept'
    delivered(call, await turn(flow, call, script, 'Yes.'))
    await turn(flow, call, script, 'Yes.')
    assert backend.calls == 1 and len(call.evidence) == 1, 'a replayed yes booked twice'


async def a_late_reading_does_not_act():
    """A reading still in flight when a newer turn begins is dropped: no yes, no write, no move."""
    script = Script()
    flow, call, backend = await at_the_read_back(script)
    started, release = asyncio.Event(), asyncio.Event()

    async def held(value, *, usage, usage_limits):
        started.set()
        await release.wait()
        return type(flow.interpreter.fallback)(decision=core.steps.Accept())

    call.begin_turn()
    call.turn_started('Yes.')
    flow.interpreter._decide = held
    reading = asyncio.create_task(flow.interpreter.interpret(call))
    await started.wait()
    call.begin_turn()
    release.set()
    assert not isinstance((await reading).decision, core.steps.Accept), 'a superseded reading was published'
    assert backend.calls == 0 and call.state.agent == 'confirm'


async def a_cut_confirmation_keeps_the_booking_once():
    """The caller cuts in while the receipt is said: the booking stands, is on record, and is never made again."""
    script = Script()
    flow, call, backend = await at_the_read_back(script)
    script.label = 'accept'
    speaking = asyncio.Event()

    async def cut_off(text: str) -> None:
        if text.startswith('Booked'):
            speaking.set()
            await asyncio.Event().wait()

    call._say = cut_off
    running = asyncio.create_task(turn(flow, call, script, 'Yes.'))
    await asyncio.wait_for(speaking.wait(), 2)
    running.cancel()
    with suppress(asyncio.CancelledError):
        await running
    assert backend.bookings == {'CL001': ('P-1234', '1000')} and len(call.evidence) == 1
    call._say = None
    await turn(flow, call, script, 'Hello? Is it booked?')
    assert backend.calls == 1 and len(call.evidence) == 1, 'the cut-off booking was made again'
    assert flow.by_id['book'].done(call)


async def a_correction_invalidates_completion_not_the_record():
    """Changing the booked slot means the booking no longer completes this request, and it stays on record."""
    script = Script()
    flow, call, backend = await at_the_read_back(script)
    script.label = 'accept'
    await turn(flow, call, script, 'Yes.')
    assert flow.by_id['book'].done(call)
    call.collect(slot='0900')
    assert not flow.by_id['book'].done(call) and not flow.by_id['confirm'].done(call)
    assert [e.args for e in call.evidence] == [{'patient': 'P-1234', 'slot': '1000'}], call.evidence
    assert backend.bookings == {'CL001': ('P-1234', '1000')}


async def late_work_for_somebody_else_writes_nothing_visible():
    """A superseded turn that began under another caller cannot write their state, and its receipt is still filed."""
    _, call, _ = opened(Script())
    call.update(patient='P-1234')
    old = call.begin_turn()
    call.update(patient='P-9999')
    call.begin_turn()
    token = turn_generation.set((call.id, old))
    try:
        call.update(notes=['from the old turn'])
        call.note_evidence('book', {'patient': 'P-1234', 'slot': '1000'}, {'reference': 'CL007'})
    finally:
        turn_generation.reset(token)
    assert call.state.facts.notes == [] and call.state.facts.patient == 'P-9999'
    [filed] = call.evidence
    assert filed.generation == old and filed.receipt == {'reference': 'CL007'}


async def main() -> None:
    await invalid_updates_change_nothing()
    await the_model_writes_only_collected_fields()
    await what_the_caller_keeps_cannot_reach_the_state()
    await two_calls_on_one_flow_share_nothing()
    await only_the_delivered_current_proposal_authorises()
    await a_late_reading_does_not_act()
    await a_cut_confirmation_keeps_the_booking_once()
    await a_correction_invalidates_completion_not_the_record()
    await late_work_for_somebody_else_writes_nothing_visible()
    print('session ok: atomic state, owned fields, isolation, freshness, stale work, evidence')


if __name__ == '__main__':
    asyncio.run(main())
