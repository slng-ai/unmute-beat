"""The node-agent contract on the public clinic: one agent per node, code nodes, and no hidden reader anywhere.

Session 4 took the classifiers out of the public path: a step's own agent
reads the caller's reply and answers through its outputs; code binds a yes to
the exact question, runs the operation and checks every move. What is checked:

* P1: every legacy reading entry point raises, and both clinic patterns
  still book; `import core` never loads them;
* P3/P9: an invented destination, a hidden tool, an unheard read-back and a
  protected field are all refused when the model tries them anyway;
* P5: a reply that changes the request beside the old read-back spends
  nothing: the change is read back fresh;
* P10: the holding line covers running work, never a model slow to say goodbye;
* P16: a manifest code node runs with no model request, applies its receipt
  once, returns to the agent node that handed in, and takes its declared
  error and unknown branches;
* P18: options looked up for a request the caller has since changed, or in a
  step they have since left, offer nothing;
* P2/P7: an unheard receipt is said again with no model request and no write;
* F4: `reads_back`: a reply that only records an operation's field is read
  back by code, with no second request, and only a heard yes still books;
* F5: a reply that records the same value the caller was just read back, and
  heard in full, is their yes: code says so and carries it out, with no
  second request. A different value is read back again; the same value after
  a read-back nobody heard carries out nothing;
* P19 (session 4f): on the clinic too, a question asked in reply to a
  read-back leaves it waiting, and a goodbye says so; only the agent's
  `decline` turns it down (core's desk R16, on the clinic);
* silence: a caller quiet after a read-back is asked if they are still there
  and hears the read-back again, a yes to that copy books, and silence that
  goes on ends the call with a goodbye.

Scripted models prove runtime properties, not language understanding.
"""

import asyncio
import contextlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import core
from core.flow import Flow
from core.locale import ENGLISH
from core.policies import TurnBudget
from core.steps import ConversationStep, StepFlow, _offer
from core.turn import REMINDERS, remind, take_turn
from tests.clinic_flow.clinic import Backend, ClinicState
from tests.script import Call, Calls, Record, Reply, Script, turn

HERE = Path(__file__).parent / 'clinic_flow'


def opened(manifest: str, script: Script, backend: Backend | None = None):
    flow = core.load(HERE / manifest).compile(model=script.model)
    call = flow.open(ClinicState(), services=backend if backend is not None else Backend())
    script.call = call
    return flow, call


FILL = (Call('options'),)
"""The times on offer, looked up first: a time can be picked only from an offer that stands."""
BOOK_TEN = Call('book', update={'patient': 'P-1234', 'slot': '1000'})
"""P-1234 at ten, carried on the booking's own call: code reads it back."""


RETIRED = (
    'core.classifiers',
    'core.readings',
    'core.conversation',
    'core.interpreters',
    'core.desk',
    'core.decisions',
)
"""The legacy reader and orchestration modules, retired in session 5 with their last user, Faro's old step."""


async def no_reader_runs_in_any_pattern():
    """P1: both patterns work, and the retired reader modules are gone: nothing can run before a step."""
    assert not any(importlib.util.find_spec(name) for name in RETIRED), 'a retired reader module came back'
    for manifest, first in (
        ('agent.yaml', ()),
        ('handoff.yaml', (Call('hand_over', to='booking', because='a booking'),)),
    ):
        script = Script()
        flow, call = opened(manifest, script)
        said = await turn(flow, call, script, 'P-1234 at ten.', *first, *FILL, BOOK_TEN)
        assert said[-1] == 'Shall I book Tuesday at 10:00 AM for P-1234?', (manifest, said)
        said = await turn(flow, call, script, 'Yes.', Call('book'))
        assert call.services.bookings == {'CL001': ('P-1234', '1000')}, (manifest, said)
        assert set(script.steps) <= {'desk', 'front', 'booking'}, script.steps


def public_path_loads_no_reader():
    """P1: in a fresh process, importing core and compiling manifests imports none of the retired reader modules."""
    code = (
        'import sys, core\n'
        f'core.load({str(HERE / "handoff.yaml")!r}).compile(model="test")\n'
        f'core.load({str(HERE / "agent.yaml")!r}).compile(model="test")\n'
        f'legacy = {list(RETIRED)!r}\n'
        'print([name for name in legacy if name in sys.modules])\n'
    )
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == '[]', out.stdout + out.stderr


async def hostile_calls_are_refused():
    """P3/P9: what a model may try outside its schema fails when it runs, and nothing reaches the backend."""
    script = Script()
    flow, call = opened('handoff.yaml', script)
    await turn(
        flow,
        call,
        script,
        'Hi.',
        Call('hand_over', to='billing', because='invented'),
        Reply('How can I help?'),
    )
    assert call.state.agent == 'front', 'an invented destination moved the caller'
    await turn(flow, call, script, 'Book me.', Call('book'), Reply('Let me hand you over.'))
    assert call.state.agent == 'front' and call.services.calls == 0, 'a tool the front desk does not have ran'

    script = Script()
    flow, call = opened('agent.yaml', script)
    await turn(flow, call, script, 'P-1234 at ten.', *FILL, BOOK_TEN, heard=False)
    said = await turn(flow, call, script, 'Yes.', Call('book'))
    assert call.services.calls == 0 and said[-1].startswith('Shall I book'), (
        'a yes to an unheard read-back booked'
    )
    await turn(
        flow,
        call,
        script,
        'Ref CL999.',
        Call('reply', say='Noted.', update={'reference': 'CL999'}),
        Reply('Noted.'),
    )
    assert call.state.facts.reference == '', 'a model wrote a field only a receipt may set'


async def a_change_in_the_same_reply_spends_no_yes():
    """P5: "yes, but at nine" beside the ten o'clock read-back: nothing is booked, and nine is read back fresh.

    On the booking's own call, the update moves the arguments, so the call
    is a new question, not the yes. As a reply with the update and the
    booking after it in one response, the reply is the one decision and the
    booking does nothing. Either way no approval is spent on arguments the
    same reply moved away from, and the caller hears the time they asked for.
    """
    for order in ('on_the_booking', 'reply_first'):
        script = Script()
        flow, call = opened('agent.yaml', script)
        said = await turn(flow, call, script, 'P-1234 at ten.', *FILL, BOOK_TEN)
        assert said[-1] == 'Shall I book Tuesday at 10:00 AM for P-1234?'
        change = (
            Call('book', update={'slot': '0900'})
            if order == 'on_the_booking'
            else Calls(Reply('Nine, then.', slot='0900'), Call('book'))
        )
        said = await turn(flow, call, script, 'Yes, but make it nine.', change)
        assert call.services.calls == 0, (order, 'booked on a read-back the same reply changed')
        assert call.state.facts.slot == '0900', (order, call.state.facts)
        if order == 'reply_first':
            assert said == ['Nine, then.'], (order, said)
            said = await turn(flow, call, script, 'Yes, nine.', Call('book'))
        assert said[-1] == 'Shall I book Tuesday at 9:00 AM for P-1234?', (order, said)
        said = await turn(flow, call, script, 'Yes.', Call('book'))
        assert call.services.bookings == {'CL001': ('P-1234', '0900')}, (
            order,
            'the fresh read-back and yes book once',
        )


async def holding_covers_work_not_thinking():
    """P10: a slow tool gets one holding line before the answer; a model slow to say goodbye gets none."""

    async def slow_times(clinic: Any, state: Any) -> dict[str, str]:
        await asyncio.sleep(0.2)
        return {'0900': 'Tuesday at 9:00 AM'}

    for slow, moves, expected in (
        ('tool', (Call('options'), Reply('I have Tuesday at nine.')), 1),
        ('model', (Call('goodbye'),), 0),
    ):
        script = Script()
        step = ConversationStep(
            id='desk',
            instructions='Help.',
            fields=('patient', 'slot'),
            choices={'slot': slow_times},
        )
        flow = StepFlow([step], model=script.model, state_type=ClinicState)
        call = flow.open(ClinicState(), services=Backend())
        script.call = call
        script.moves = list(moves)
        script.delay = 0.2 if slow == 'model' else 0.0

        async def voice(text: str) -> None:
            """A runtime that takes fixed lines: without one, a covering line has nowhere to go."""

        call._say = voice
        await take_turn(call, flow.nodes, 'Hello.', TurnBudget(hold_after=0.05))
        fillers = [s for s in call.spoken if s.origin == 'tool_filler']
        assert len(fillers) == expected, (slow, [(s.origin, s.text) for s in call.spoken])
        if expected:
            assert call.spoken[0] is fillers[0], 'the holding line comes before the answer'


async def a_cut_off_tool_leaves_no_timer():
    """B4: a caller who cuts in while a slow tool runs ends the turn, and its holding-line timer with it, awaited."""
    started = asyncio.Event()

    async def hanging_times(clinic: Any, state: Any) -> dict[str, str]:
        started.set()
        await asyncio.Event().wait()
        return {}

    script = Script()
    step = ConversationStep(
        id='desk',
        instructions='Help.',
        fields=('patient', 'slot'),
        choices={'slot': hanging_times},
    )
    flow = StepFlow([step], model=script.model, state_type=ClinicState)
    call = flow.open(ClinicState(), services=Backend())
    script.call = call
    script.moves = [Call('options')]
    barge = core.turn.Barge()
    running = asyncio.create_task(
        take_turn(call, flow.nodes, 'Which times?', TurnBudget(hold_after=5.0), barge)
    )
    await asyncio.wait_for(started.wait(), 3)
    barge.fire()
    running.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(running, 3)
    await asyncio.sleep(0)
    left = [t for t in asyncio.all_tasks() if '_hold_after' in repr(t.get_coro())]
    assert not left, ('a holding-line timer outlived the turn that armed it', left)


async def a_code_node_returns_with_no_model():
    """P16: the agent hands to `look_up`; the code node asks no model, files its receipt once, and returns.

    Its declared error branch (no booking found) and unknown branch (the
    diary did not answer) also go back to the agent node, and nothing is
    assigned from either.
    """
    backend = Backend()
    backend.bookings['CL007'] = ('P-1234', '1000')
    script = Script()
    flow, call = opened('code_node.yaml', script, backend)
    assert flow.nodes['look_up'].returns and 'front' in Flow.of(flow.nodes).moves['look_up']
    said = await turn(
        flow,
        call,
        script,
        'P-1234, where is my booking?',
        Call('hand_over', to='look_up', because='find the booking', update={'patient': 'P-1234'}),
        Reply('You are booked for Tuesday at ten.'),
    )
    assert said == ['I found booking CL007.', 'You are booked for Tuesday at ten.'], said
    assert script.steps == ['front', 'front'], ('the code node made a request', script.steps)
    assert (
        call.state.agent == 'front' and call.state.pending is None and call.state.facts.reference == 'CL007'
    )
    assert [e.operation for e in call.evidence] == ['look_up']

    for trouble in ('none', 'unknown'):
        backend = Backend()
        backend.failing = trouble == 'unknown'
        script = Script()
        flow, call = opened('code_node.yaml', script, backend)
        await turn(
            flow,
            call,
            script,
            'P-1234.',
            Call('hand_over', to='look_up', because='find the booking', update={'patient': 'P-1234'}),
            Reply('I could not find it.'),
        )
        assert call.state.agent == 'front' and call.state.facts.reference == '', (trouble, call.state)
        assert script.steps == ['front', 'front'], (trouble, script.steps)


async def late_options_offer_nothing():
    """P18: options still being looked up when the caller changes the request, or leaves the step, are stale."""
    release = asyncio.Event()

    async def slow_times(clinic: Any, state: Any) -> dict[str, str]:
        await release.wait()
        return {'0900': 'Tuesday at 9:00 AM'}

    step = ConversationStep(
        id='desk',
        instructions='Help.',
        fields=('patient', 'slot'),
        choices={'slot': slow_times},
        exits=('desk',),
    )
    flow = StepFlow([step], model=Script().model, state_type=ClinicState)
    for moved_on in ('request', 'step'):
        call = flow.open(ClinicState(), services=Backend())
        call._offers.put('slot', {'1000': 'Tuesday at 10:00 AM'})
        release.clear()
        looking = asyncio.create_task(_offer(call, step.choices))
        await asyncio.sleep(0)
        if moved_on == 'request':
            call.update(patient='P-4321')
            call._offers.put('slot', {'1000': 'Tuesday at 10:00 AM'})
        else:
            call.commit(agent='elsewhere')
        release.set()
        result = await looking
        assert 'stale' in result, (moved_on, result)
        assert call._offers == {'slot': {'1000': 'Tuesday at 10:00 AM'}}, (
            moved_on,
            'a stale answer replaced the offer',
        )


async def an_unheard_receipt_is_said_again():
    """P2/P7: the receipt was cut before it reached the caller; their next words hear it again first, with no write.

    Then the step's agent reads what they said over it, in the same turn,
    with the receipt before their words (session 4g: before, a request said
    over a receipt was dropped). With the rule switched off, the agent
    answers unaware and the caller never hears what was done: the
    regression this check exists to catch.
    """
    for rule in (True, False):
        script = Script()
        flow, call = opened('agent.yaml', script)
        await turn(flow, call, script, 'P-1234 at ten.', *FILL, BOOK_TEN)
        script.moves = [Call('book')]
        reply = await core.text_turn(flow, call, 'Yes.')
        receipt = next(s for s in reply.segments if s.text.startswith('Booked'))
        call.acknowledge(receipt.id, heard='')
        requests = len(script.steps)
        if rule:
            said = await turn(flow, call, script, 'Yes, Tuesday at ten, great.', Reply('See you then.'))
        else:
            with patch.object(type(call), 'unheard_receipt', lambda self: ''):
                said = await turn(flow, call, script, 'Yes, Tuesday at ten, great.', Reply('Anything else?'))
        assert call.services.calls == 1 and len(call.evidence) == 1, 'the booking was made again'
        assert len(script.steps) == requests + 1, 'the words said over the receipt were not read'
        if rule:
            assert said == ['Booked for Tuesday at 10:00 AM, reference CL001.', 'See you then.'], said
            sent = script.told[-1]
            assert sent.rfind("TextPart(content='Booked for Tuesday") < sent.rfind(
                "UserPromptPart(content='Yes, Tuesday at ten, great."
            ), 'the receipt did not come before the words said over it'
        else:
            assert said == ['Anything else?'], 'without the rule the receipt was still said'


async def a_recorded_pick_is_read_back_by_code():
    """F4: the booking's own update reads it back in the request that picked it: no second request, no `record`.

    Coval runs jCBGSipW and StET3kNN: 21 turns spent a second request only to
    have `book_appointment` read back after the pick was recorded. A booking
    carries the slot itself, so `reads_back: {book: [slot]}` needs no
    `record`: it is offered only for a field the operation's update cannot
    carry. A reply with words of its own is left to the model, and only a
    heard yes still books.
    """
    script = Script()
    flow, call = opened('reads_back.yaml', script)
    said = await turn(flow, call, script, 'P-1234 at ten.', *FILL, BOOK_TEN)
    assert said == ['Shall I book Tuesday at 10:00 AM for P-1234?'], said
    assert script.steps == ['desk', 'desk'], ('options, then the booking read back by code', script.steps)
    assert 'record' not in script.requests[-1].offered, script.requests[-1].offered
    said = await turn(flow, call, script, 'Yes.', Call('book'))
    assert call.services.bookings == {'CL001': ('P-1234', '1000')}, said

    script = Script()
    flow, call = opened('reads_back.yaml', script)
    said = await turn(
        flow,
        call,
        script,
        'P-1234 at ten. How long is it?',
        *FILL,
        Reply('Thirty minutes.', patient='P-1234', slot='1000'),
    )
    assert said == ['Thirty minutes.'] and len(script.steps) == 2, (
        'words beside the pick: the model answers, and code reads nothing back',
        said,
    )
    assert call.state.facts.slot == '1000' and call.pending_question() is None

    script = Script()
    flow, call = opened('reads_back.yaml', script)
    await turn(flow, call, script, 'P-1234 at ten.', *FILL, BOOK_TEN, heard=False)
    said = await turn(flow, call, script, 'Yes.', Call('book'))
    assert call.services.calls == 0 and said[-1].startswith('Shall I book'), (
        'a yes to an unheard read-back booked'
    )


async def the_same_value_said_again_is_a_yes():
    """F5: benchmark calls amAP5b4A and Tiv9sDHH (session 4c): "no, it's <the same value>" to a heard read-back.

    The caller's words decide nothing here: the scripted agent only records
    what it heard, and code compares the value with the read-back. Code
    carries it out only where the application's policy says the same value
    is a yes (`restated: confirms`, a lookup). For a booking, the same slot
    in a reply is only a record (session 4d, defect 1): the step's own agent
    reads the reply, and "No, do not book ten" books nothing.
    """
    patient = Record(patient='P-1234')
    script = Script()
    flow, call = opened('restated.yaml', script)
    said = await turn(flow, call, script, 'P-1234.', patient)
    assert said == ['Is your patient number P-1234?'], said
    before = len(script.steps)
    said = await turn(flow, call, script, 'No, it is P 1 2 3 4.', patient)
    assert said[:2] == [call.locale.restated, 'Thanks, P-1234.'], said
    assert len(script.steps) - before == 1, ('the restated value asked no second request', script.steps)

    script = Script()
    flow, call = opened('restated.yaml', script)
    await turn(flow, call, script, 'P-1234.', patient, heard=False)
    said = await turn(flow, call, script, 'No, it is P 1 2 3 4.', patient)
    assert call.locale.restated not in said and said == ['Is your patient number P-1234?'], (
        'the same value after an unheard read-back carried it out',
        said,
    )

    for words, answer in (
        ('No, do not book ten. I am only checking if it is free.', 'Ten is free. Nothing is booked.'),
        ('Is ten free?', 'Ten is free.'),
        ('Only if ten is still free.', 'It is.'),
        ('"Ten", I said. Not booked yet.', 'Understood.'),
    ):
        script = Script()
        flow, call = opened('reads_back.yaml', script)
        await turn(flow, call, script, 'P-1234 at ten.', *FILL, BOOK_TEN)
        said = await turn(flow, call, script, words, Reply(answer, slot='1000'))
        assert call.services.calls == 0 and call.locale.restated not in said, (
            words,
            'a value said again booked',
        )
        assert said == [answer], (words, said)

    script = Script()
    flow, call = opened('reads_back.yaml', script)
    await turn(flow, call, script, 'P-1234 at ten.', *FILL, BOOK_TEN)
    said = await turn(flow, call, script, 'No, nine.', Call('book', update={'slot': '0900'}))
    assert call.services.calls == 0 and said == ['Shall I book Tuesday at 9:00 AM for P-1234?'], said

    script = Script()
    flow, call = opened('restated.yaml', script)
    await turn(flow, call, script, 'P-1234.', patient)
    said = await turn(
        flow,
        call,
        script,
        'No, P-56. Well, P-1234 then.',
        Record(patient='P-56'),
        patient,
    )
    assert call.locale.restated not in said and not call.evidence, (
        'a refused change, then the old value, was taken as a yes',
        said,
    )

    text = (HERE / 'reads_back.yaml').read_text()
    broken = HERE / '_restated_broken.yaml'
    broken.write_text(
        text.replace(
            '    readback: clinic.py:booking_words\n',
            '    readback: clinic.py:booking_words\n    restated: confirms\n',
            1,
        )
    )
    try:
        core.load(broken).compile(model='test')
    except core.FlowSpecError as error:
        assert 'restated: confirms is only for a read-back of a lookup' in str(error), str(error)
    else:
        raise AssertionError('restated: confirms was accepted on a booking')
    finally:
        broken.unlink()


def reads_back_is_checked_at_load():
    """F4: `reads_back` naming an operation the step does not carry out, or a field it does not record, fails at load."""
    text = (HERE / 'reads_back.yaml').read_text()
    for bad, expected in (('{cancel: [slot]}', "'cancel'"), ('{book: [patien]}', "'patien'")):
        broken = HERE / '_reads_back_broken.yaml'
        body = text.replace('operations: [book, cancel]', 'operations: [book]') if 'cancel' in bad else text
        broken.write_text(body.replace('reads_back: {book: [slot]}', f'reads_back: {bad}'))
        try:
            core.load(broken).compile(model='test')
        except core.FlowSpecError as error:
            assert expected in str(error) and 'reads back' in str(error), str(error)
        else:
            raise AssertionError(f'reads_back {bad} was accepted')
        finally:
            broken.unlink()


async def silence_gets_a_reminder_then_a_goodbye():
    """Coval runs 3hZu7PPk and np7G4Hty: a question, then a caller who said nothing until the call limit.

    "Are you still there?" and the read-back again, word for word, as a new
    copy of the same question: the caller's yes to it books, with no model
    request for the reminder. Silence that goes on gets a goodbye and the
    line closes. Speaking resets the count.
    """
    script = Script()
    flow, call = opened('agent.yaml', script)
    await turn(flow, call, script, 'P-1234 at ten.', *FILL, BOOK_TEN)
    closed = []

    async def hang_up() -> None:
        closed.append(True)

    call._end = hang_up
    before, requests = len(call.spoken), len(script.steps)
    await remind(call)
    reminded = call.spoken[before:]
    assert [s.text for s in reminded] == [
        ENGLISH.still_there,
        'Shall I book Tuesday at 10:00 AM for P-1234?',
    ], [s.text for s in reminded]
    assert len(script.steps) == requests, 'a reminder asked a model'
    call.acknowledge(*(s.id for s in reminded))
    await turn(flow, call, script, 'Yes.', Call('book'))
    assert call.services.bookings == {'CL001': ('P-1234', '1000')}, 'the yes to the repeated read-back books'

    for _ in range(REMINDERS):
        await remind(call)
    assert not call.ended and call.spoken[-1].text == ENGLISH.still_there, (
        'nothing left to ask: just the check'
    )
    await remind(call)
    assert call.ended == 'abandoned' and closed and call.spoken[-1].text == ENGLISH.idle_goodbye, call.ended


async def a_question_is_not_a_no_on_the_clinic():
    """P19: the decline rule outside the desk: a step with two operations and no hand-over."""
    for declined in (False, True):
        script = Script()
        flow, call = opened('agent.yaml', script)
        await turn(flow, call, script, 'P-1234 at ten.', *FILL, BOOK_TEN)
        if declined:
            said = await turn(flow, call, script, 'No, leave it. Bye.', Call('goodbye', decline=True))
        else:
            await turn(flow, call, script, 'Before I say, do you take walk-ins?', Reply('We do, mornings.'))
            said = await turn(flow, call, script, 'Bye.', Call('goodbye'))
        assert call.services.calls == 0, said
        assert (ENGLISH.left_undone in ' '.join(said)) is not declined, (declined, said)


async def main() -> None:
    await no_reader_runs_in_any_pattern()
    public_path_loads_no_reader()
    await hostile_calls_are_refused()
    await a_change_in_the_same_reply_spends_no_yes()
    await holding_covers_work_not_thinking()
    await a_cut_off_tool_leaves_no_timer()
    await a_code_node_returns_with_no_model()
    await late_options_offer_nothing()
    await an_unheard_receipt_is_said_again()
    await a_recorded_pick_is_read_back_by_code()
    await the_same_value_said_again_is_a_yes()
    reads_back_is_checked_at_load()
    await silence_gets_a_reminder_then_a_goodbye()
    await a_question_is_not_a_no_on_the_clinic()
    print(
        'nodes ok: no reader, hostile calls, mixed reply, holding, code node, late options, unheard receipt, '
        'code read-back, a question is not a no'
    )


if __name__ == '__main__':
    asyncio.run(main())
