"""The session's guarantees on the public clinic slice: atomic state, owned fields, isolation, stale work, evidence.

Each check is one of session 2's required negative or positive cases, run
through the real engine with a scripted model (`tests.script.Script`) on the
one-agent clinic.
A scripted yes tests authorisation, not whether a model understands consent.
"""

import asyncio
import gc
import weakref
from contextlib import suppress
from unittest.mock import patch

from pydantic import ValidationError

import core
from core.flow import Flow
from core.flowspec import load as load_text
from core.state import turn_generation
from tests.clinic_flow.clinic import Backend, ClinicState, Contact
from tests.script import Call, Reply, Script
from tests.test_public import CLOCK, FLOW, delivered, offer, opened, turn


def raises(error: type[Exception], fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except error:
        return
    raise AssertionError(f'{fn.__name__} accepted what it must refuse')


def booking(flow: core.StepFlow) -> core.Operation:
    """The desk's `book` operation, run directly where a check needs no model."""
    return next(op for op in flow.by_id['desk'].operations if op.name == 'book')


async def at_the_read_back(script: Script, patient: str = 'P-1234', *, heard: bool = True):
    """A clinic call whose last turn read back a Tuesday 10:00 booking; `heard` says the reader got it."""
    flow, call, backend = opened(script)
    delivered(call, await turn(flow, call, script, f'{patient}.', *offer(patient)))
    reply = await turn(flow, call, script, 'Ten.', Call('book', update={'slot': '1000'}))
    assert reply.segments[-1].text.startswith('Shall I book Tuesday at 10:00 AM'), reply
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


async def the_model_writes_only_its_steps_fields():
    """A protected field in a model's update is refused whole, before the backend is asked anything."""
    script = Script()
    flow, call, backend = opened(script)
    await turn(
        flow, call, script, 'P-1234, reference CL999.', Reply('Noted.', patient='P-1234', reference='CL999')
    )
    assert call.state.facts.reference == '' and call.state.facts.patient is None, call.state.facts
    assert backend.calls == 0, backend.calls


async def what_the_caller_keeps_cannot_reach_the_state():
    """Mutating an object after it was committed, or after it opened a session, changes nothing saved."""
    script = Script()
    notes, contact = ['first'], Contact(phone='555 0100')
    _, call, _ = opened(script)
    call.update(notes=notes, contact=contact)
    notes.append('second')
    contact.phone = '555 0199'
    assert call.state.facts.notes == ['first'] and call.state.facts.contact.phone == '555 0100'
    flow = core.load('tests/clinic_flow/agent.yaml').compile(model=script.model)
    given = ClinicState(notes=['given'])
    session = flow.open(given, services=Backend())
    given.notes.append('later')
    assert session.state.facts.notes == ['given'], session.state.facts


async def two_calls_on_one_flow_share_nothing():
    """Two sessions interleaved on one compiled flow: separate proposals, approvals, histories, state and receipts."""
    script = Script()
    flow = core.load('tests/clinic_flow/agent.yaml').compile(model=script.model)
    backend = Backend()
    one, two = flow.open(ClinicState(), services=backend), flow.open(ClinicState(), services=backend)
    delivered(one, await turn(flow, one, script, 'P-1111.', *offer('P-1111')))
    delivered(two, await turn(flow, two, script, 'P-2222.', *offer('P-2222')))
    delivered(one, await turn(flow, one, script, 'Nine.', Call('book', update={'slot': '0900'})))
    delivered(two, await turn(flow, two, script, 'Ten.', Call('book', update={'slot': '1000'})))
    assert one._asked.owner == 'P-1111' and two._asked.owner == 'P-2222'
    await turn(flow, one, script, 'Yes.', Call('book'))
    assert not two.approved() and not backend.bookings.get('CL002'), 'a yes on one call approved the other'
    await turn(flow, two, script, 'Yes.', Call('book'))
    assert backend.bookings == {'CL001': ('P-1111', '0900'), 'CL002': ('P-2222', '1000')}, backend.bookings
    assert [e.receipt['reference'] for e in one.evidence] == ['CL001']
    assert [e.receipt['reference'] for e in two.evidence] == ['CL002']
    assert one.messages is not two.messages and one._proposals is not two._proposals
    assert one.state.facts.slot == '0900' and two.state.facts.slot == '1000'


async def only_the_delivered_current_proposal_authorises():
    """Identity and argument revisions, a stale proposal, an unrelated yes, a conditional yes, a replay: no write."""
    script = Script()
    for change in ({'patient': 'P-9999'}, {'slot': '0900'}):
        flow, call, backend = await at_the_read_back(script)
        delivered(
            call,
            await turn(
                flow, call, script, 'Actually, change that.', Call('reply', say='Noted.', update=change)
            ),
        )
        reply = await turn(flow, call, script, 'Yes.', Call('book'))
        assert backend.calls == 0, f'a yes to the old read-back booked after {change}'
        if 'patient' in change:
            assert call.state.facts.slot is None, 'a time picked by one patient was kept for another'
        else:
            assert reply.segments[-1].text == 'Shall I book Tuesday at 9:00 AM for P-1234?', (
                'the new arguments are read back instead'
            )

    flow, call, backend = await at_the_read_back(script)
    await turn(flow, call, script, 'Hmm, let me think.', Reply('Take your time.'))
    await turn(flow, call, script, 'Right.', Reply('Whenever you are ready.'))
    await turn(flow, call, script, 'Yes.', Call('book'))
    assert backend.calls == 0, 'a stale read-back was answered'

    flow, call, backend = opened(script)
    delivered(call, await turn(flow, call, script, 'P-1234.', *offer()))
    await turn(flow, call, script, 'Yes.', Call('book'))
    assert backend.calls == 0, 'a yes to nothing booked'

    flow, call, backend = await at_the_read_back(script)
    await turn(
        flow, call, script, 'Yes, if it is not raining.', Reply('Shall I book it whatever the weather?')
    )
    assert backend.calls == 0, 'a conditional yes booked'

    flow, call, backend = await at_the_read_back(script)
    delivered(call, await turn(flow, call, script, 'Yes.', Call('book')))
    await turn(flow, call, script, 'Yes.', Call('book'))
    assert backend.calls == 1 and len(call.evidence) == 1, 'a replayed yes booked twice'


async def a_late_approval_does_not_act():
    """A yes the step's agent gave in a turn the caller has since left authorises nothing: no write, no move.

    The node-agent version of the late reading: the approval is bound to the
    turn generation that gave it (`CallContext.approve`), so an agent run
    still finishing after a newer turn began cannot spend it.
    """
    script = Script()
    flow, call, backend = await at_the_read_back(script)
    old = call.begin_turn()
    call.turn_started('Yes.')
    proposal = call._asked
    token = turn_generation.set((call.id, old))
    try:
        assert call.approve(proposal.about, value=proposal.value)
    finally:
        turn_generation.reset(token)
    assert call.approved(), 'the yes counts in its own turn'
    call.begin_turn()
    assert not call.approved(), 'a yes from a superseded turn still counted'
    book = booking(flow)
    try:
        await book.run(call, core.steps._args(book, call))
    except core.steps.ModelRetry:
        pass
    else:
        raise AssertionError('a superseded yes booked')
    assert backend.calls == 0


async def a_cut_confirmation_keeps_the_booking_once():
    """The caller cuts in while the receipt is said: the booking stands, is on record, and is never made again."""
    script = Script()
    flow, call, backend = await at_the_read_back(script)
    speaking = asyncio.Event()

    async def cut_off(text: str) -> None:
        if text.startswith('Booked'):
            speaking.set()
            await asyncio.Event().wait()

    call._say = cut_off
    running = asyncio.create_task(turn(flow, call, script, 'Yes.', Call('book')))
    await asyncio.wait_for(speaking.wait(), 2)
    running.cancel()
    with suppress(asyncio.CancelledError):
        await running
    assert backend.bookings == {'CL001': ('P-1234', '1000')} and len(call.evidence) == 1
    call._say = None
    await turn(flow, call, script, 'Hello? Is it booked?', Call('book'))
    assert backend.calls == 1 and len(call.evidence) == 1, 'the cut-off booking was made again'
    assert core.steps.receipt_for(booking(flow), call) is not None


async def a_correction_invalidates_completion_not_the_record():
    """Changing the booked slot means the booking no longer completes this request, and it stays on record."""
    script = Script()
    flow, call, backend = await at_the_read_back(script)
    await turn(flow, call, script, 'Yes.', Call('book'))
    assert core.steps.receipt_for(booking(flow), call) is not None
    call.update(slot='0900')
    assert core.steps.receipt_for(booking(flow), call) is None, (
        'a receipt for other arguments still completes'
    )
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


def flows_alive() -> int:
    gc.collect()
    return sum(isinstance(o, core.StepFlow) for o in gc.get_objects())


async def a_discarded_flow_is_collected():
    """A compiled flow nobody holds goes with its graph and agents, and compiling again and again keeps none.

    The graph belongs to its steps table (`core.flow.Nodes`), not to a
    process-wide cache that held every table ever compiled.
    """
    script = Script()
    flow, call, _ = opened(script)
    delivered(call, await turn(flow, call, script, 'P-1234.', *offer()))
    held = [weakref.ref(flow), weakref.ref(Flow.of(flow.nodes)), weakref.ref(flow.nodes['desk'].agent)]
    del flow, call
    before = flows_alive()
    assert all(ref() is None for ref in held), 'a discarded flow, its graph or an agent is still held'
    for _ in range(50):
        core.load(FLOW).compile(model=script.model)
    assert flows_alive() == before, 'repeated compiles retained their flows'


async def a_call_carries_on_while_other_flows_come_and_go():
    """Other definitions compiled, used and dropped around a call waiting on a read-back leave it untouched."""
    script = Script()
    flow, call, backend = await at_the_read_back(script)
    other = Script()
    for _ in range(3):
        elsewhere, their_call, _ = opened(other)
        delivered(their_call, await turn(elsewhere, their_call, other, 'P-9999.', *offer('P-9999')))
    del elsewhere, their_call
    gc.collect()
    await turn(flow, call, script, 'Yes.', Call('book'))
    assert backend.bookings == {'CL001': ('P-1234', '1000')}, backend.bookings


async def the_same_routing_with_another_prompt_shares_nothing():
    """Two compiles with the same steps and moves, one prompt changed: each its own graph, agents, words and services."""
    text = FLOW.read_text()
    changed = text.replace('prompt: prompts/desk.md', 'prompt: prompts/front.md')
    assert changed != text
    one, two = Script(), Script()
    first = core.load(FLOW).compile(model=one.model)
    second = load_text(changed, root=FLOW.parent).compile(model=two.model)
    assert Flow.of(first.nodes).moves == Flow.of(second.nodes).moves, 'the same routing'
    assert Flow.of(first.nodes) is not Flow.of(second.nodes)
    assert first.nodes['desk'].agent is not second.nodes['desk'].agent
    ours, theirs = Backend(), Backend()
    a = first.open(ClinicState(), services=ours, clock=lambda: CLOCK)
    b = second.open(ClinicState(), services=theirs, clock=lambda: CLOCK)
    await turn(first, a, one, 'P-1111.', Reply('Noted.', patient='P-1111'))
    await turn(second, b, two, 'P-2222.', Reply('Noted.', patient='P-2222'))
    told = [r.instructions for r in one.requests], [r.instructions for r in two.requests]
    assert told[0] and all('patient number' in t and 'Greet the caller' not in t for t in told[0]), told
    assert told[1] and all('Greet the caller' in t for t in told[1]), told
    assert a.services is ours and b.services is theirs
    assert (a.state.facts.patient, b.state.facts.patient) == ('P-1111', 'P-2222')


async def the_graph_is_built_once_per_compile():
    """Turns and calls on a compiled flow walk the graph built with it: none is built per turn or per call."""
    with patch.object(Flow, '_build', autospec=True, side_effect=Flow._build) as build:
        script = Script()
        flow, call, _ = await at_the_read_back(script)
        await turn(flow, call, script, 'Yes.', Call('book'))
        again = flow.open(ClinicState(), services=Backend(), clock=lambda: CLOCK)
        delivered(again, await turn(flow, again, script, 'P-2222.', *offer('P-2222')))
    assert build.call_count == 1, build.call_count
    assert build.call_args.args[0] is Flow.of(flow.nodes)


async def main() -> None:
    await invalid_updates_change_nothing()
    await the_model_writes_only_its_steps_fields()
    await what_the_caller_keeps_cannot_reach_the_state()
    await two_calls_on_one_flow_share_nothing()
    await only_the_delivered_current_proposal_authorises()
    await a_late_approval_does_not_act()
    await a_cut_confirmation_keeps_the_booking_once()
    await a_correction_invalidates_completion_not_the_record()
    await late_work_for_somebody_else_writes_nothing_visible()
    await a_discarded_flow_is_collected()
    await a_call_carries_on_while_other_flows_come_and_go()
    await the_same_routing_with_another_prompt_shares_nothing()
    await the_graph_is_built_once_per_compile()
    print(
        'session ok: atomic state, owned fields, isolation, freshness, stale work, evidence, '
        'definitions owned and collected'
    )


if __name__ == '__main__':
    asyncio.run(main())
