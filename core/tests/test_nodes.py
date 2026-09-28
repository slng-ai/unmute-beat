"""The node-agent contract on the public clinic: one agent per node, code nodes, and no hidden reader anywhere.

Session 4 took the classifiers out of the public path: a step's own agent
reads the caller's reply and answers through its outputs; code binds a yes to
the exact question, runs the operation and checks every move. What is checked:

* P1: every legacy reading entry point raises, and all three clinic patterns
  still book, decline, revise and take back; `import core` never loads them;
* P3/P9: an invented destination, a hidden tool, an unheard read-back and a
  protected field are all refused when the model tries them anyway;
* P5: a reply that changes the request and accepts the old read-back spends
  nothing, in either order of the two calls;
* P10: the holding line covers running work, never a model slow to say goodbye;
* P16: a manifest code node runs with no model request, applies its receipt
  once, returns to the agent node that handed in, and takes its declared
  error and unknown branches;
* P18: options looked up for a request the caller has since changed, or in a
  step they have since left, offer nothing;
* P2/P7: an unheard receipt is said again with no model request and no write;
* F3: words the model writes beside an operation call are not said: the
  operation says its own, once;
* F4: `reads_back`: a reply that only records an operation's field is read
  back by code, with no second request, and only a heard yes still books;
* F5: a reply that records the same value the caller was just read back, and
  heard in full, is their yes: code says so and carries it out, with no
  second request. A different value is read back again; the same value after
  a read-back nobody heard carries out nothing;
* P19 (session 4f): on a prose step too, a question asked in reply to a
  read-back leaves it waiting, and a goodbye says so; only the agent's
  `decline` turns it down (core's desk R16, on the clinic);
* silence: a caller quiet after a read-back is asked if they are still there
  and hears the read-back again, a yes to that copy books, and silence that
  goes on ends the call with a goodbye.

Scripted models prove runtime properties, not language understanding.
"""

import asyncio
import contextlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

import core
from core.flow import Flow
from core.locale import ENGLISH
from core.policies import TurnBudget
from core.steps import ConversationStep, StepFlow, _offer
from core.turn import REMINDERS, remind, take_turn
from tests.clinic_flow.clinic import Backend, ClinicState

HERE = Path(__file__).parent / 'clinic_flow'


class Script:
    """Each step agent's request takes the next move, and records which step asked. Nothing else may run.

    A move is `('say', words)`, `('call', tool, args)`, `('calls', [(tool, args), ...])`
    for one response carrying several tool calls in that order, or `('say+call', words, tool, args)`
    for one response that speaks and then calls a tool.
    """

    def __init__(self) -> None:
        self.moves: list[tuple[Any, ...]] = []
        self.steps: list[str] = []
        self.told: list[str] = []
        """Everything each request was sent (instructions and messages), as text, for checks on what a step was told."""
        self.call: core.FlowCall | None = None
        self.delay = 0.0
        self.model = FunctionModel(self.reading, stream_function=self.agent)

    def reading(self, messages: Any, info: AgentInfo) -> ModelResponse:
        raise AssertionError('a separate reading was requested; only step agents may run')

    async def agent(self, messages: Any, info: AgentInfo):
        self.steps.append(self.call.state.agent if self.call is not None else '?')
        self.told.append(f'{getattr(info, "instructions", "") or ""}\n{messages!r}')
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self.moves:
            yield 'Sorry?'
            return
        kind, *rest = self.moves.pop(0)
        if kind == 'say':
            yield rest[0]
        elif kind == 'call':
            yield {0: DeltaToolCall(name=rest[0], json_args=json.dumps(rest[1]))}
        elif kind == 'say+call':
            yield rest[0]
            yield {1: DeltaToolCall(name=rest[1], json_args=json.dumps(rest[2]))}
        else:
            yield {
                i: DeltaToolCall(name=name, json_args=json.dumps(args))
                for i, (name, args) in enumerate(rest[0])
            }


def opened(manifest: str, script: Script, backend: Backend | None = None):
    flow = core.load(HERE / manifest).compile(model=script.model)
    call = flow.open(ClinicState(), services=backend if backend is not None else Backend())
    script.call = call
    return flow, call


async def turn(flow, call, script: Script, words: str, *moves, heard: bool = True) -> list[str]:
    script.moves = list(moves)
    reply = await core.text_turn(flow, call, words)
    if heard:
        call.acknowledge(*(segment.id for segment in reply.segments))
    return [segment.text for segment in reply.segments]


FILL = (('call', 'options', {}), ('call', 'collect', {'values': {'patient': 'P-1234', 'slot': '1000'}}))


def raising(*_: Any, **__: Any) -> Any:
    raise AssertionError('a legacy reading entry point ran')


async def no_reader_runs_in_any_pattern():
    """P1: with every classifier, reading and interpreter entry point raising, the three patterns still work."""
    import core.classifiers
    import core.interpreters
    import core.readings

    with (
        patch.object(core.classifiers.Classifier, 'classify', raising),
        patch.object(core.interpreters.Interpreter, 'interpret', raising),
        patch.object(core.readings, 'read', raising),
    ):
        script = Script()
        flow, call = opened('agent.yaml', script)
        await turn(flow, call, script, 'P-1234.', ('call', 'collect', {'values': {'patient': 'P-1234'}}))
        await turn(flow, call, script, 'Ten.', ('call', 'collect', {'values': {'slot': '1000'}}))
        said = await turn(flow, call, script, 'Yes.', ('call', 'accept', {}))
        assert said[0].startswith('Booked for') and call.services.calls == 1, said
        said = await turn(flow, call, script, 'Cancel it.', ('call', 'take_back', {}))
        said = await turn(flow, call, script, 'Yes.', ('call', 'accept', {}))
        assert call.services.bookings == {} and call.state.outcome == 'cancelled', said

        for manifest, first in (
            ('one_agent.yaml', ()),
            ('handoff.yaml', (('call', 'hand_over', {'to': 'booking', 'because': 'a booking'}),)),
        ):
            script = Script()
            flow, call = opened(manifest, script)
            said = await turn(flow, call, script, 'P-1234 at ten.', *first, *FILL, ('call', 'book', {}))
            assert said[-1] == 'Shall I book Tuesday at 10:00 AM for P-1234?', (manifest, said)
            said = await turn(flow, call, script, 'Yes.', ('call', 'book', {}))
            assert call.services.bookings == {'CL001': ('P-1234', '1000')}, (manifest, said)
            assert set(script.steps) <= {'desk', 'front', 'booking'}, script.steps


def public_path_loads_no_reader():
    """P1: importing core and compiling a manifest never imports the legacy reading modules (Faro's, until session 5)."""
    code = (
        'import sys, core\n'
        f'core.load({str(HERE / "handoff.yaml")!r}).compile(model="test")\n'
        f'core.load({str(HERE / "agent.yaml")!r}).compile(model="test")\n'
        "legacy = ['core.classifiers', 'core.readings', 'core.conversation', 'core.interpreters', 'core.desk', 'core.decisions']\n"
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
        ('call', 'hand_over', {'to': 'billing', 'because': 'invented'}),
        ('say', 'How can I help?'),
    )
    assert call.state.agent == 'front', 'an invented destination moved the caller'
    await turn(flow, call, script, 'Book me.', ('call', 'book', {}), ('say', 'Let me hand you over.'))
    assert call.state.agent == 'front' and call.services.calls == 0, 'a tool the front desk does not have ran'

    script = Script()
    flow, call = opened('one_agent.yaml', script)
    await turn(flow, call, script, 'P-1234 at ten.', *FILL, ('call', 'book', {}), heard=False)
    said = await turn(flow, call, script, 'Yes.', ('call', 'book', {}))
    assert call.services.calls == 0 and said[-1].startswith('Shall I book'), (
        'a yes to an unheard read-back booked'
    )
    await turn(
        flow,
        call,
        script,
        'Ref CL999.',
        ('call', 'collect', {'values': {'reference': 'CL999'}}),
        ('say', 'Noted.'),
    )
    assert call.state.facts.reference == '', 'a model wrote a field only a receipt may set'


async def a_confirm_steps_answer_claims_nothing():
    """B2: a confirm step's `answer` is model words on a step that is about to write; the application's claim words hold there too."""
    text = (HERE / 'agent.yaml').read_text()
    claimed = HERE / '_claims_agent.yaml'
    claimed.write_text(
        text.replace('locale: english\n', "locale:\n  base: english\n  claims: ['\\bbooked\\b']\n", 1)
    )
    try:
        script = Script()
        flow, call = opened('_claims_agent.yaml', script)
        await turn(flow, call, script, 'P-1234.', ('call', 'collect', {'values': {'patient': 'P-1234'}}))
        await turn(flow, call, script, 'Ten.', ('call', 'collect', {'values': {'slot': '1000'}}))
        said = await turn(
            flow,
            call,
            script,
            'Is it booked?',
            ('call', 'answer', {'words': 'Yes, you are booked.'}),
            ('call', 'answer', {'words': 'Not yet: I need your yes first.'}),
        )
        assert not any('you are booked' in s for s in said), (
            'a confirm step claimed a booking nothing made',
            said,
        )
        assert call.services.calls == 0 and said[0] == 'Not yet: I need your yes first.', said
    finally:
        claimed.unlink()


async def a_change_in_the_same_reply_spends_no_yes():
    """P5: "yes, but at nine" as `collect` + `book` in one response, in both orders: nothing is booked.

    Collect first: the change lands, and nine is read back for a fresh yes.
    Book first: the SDK runs the output before the change, so `book` refuses
    with a retry, the change runs, and the model's next `book` reads nine
    back. Either way no approval is spent on arguments the same reply moved
    away from, and the caller hears the time they asked for.
    """
    for order in ('collect_first', 'book_first'):
        script = Script()
        flow, call = opened('one_agent.yaml', script)
        said = await turn(flow, call, script, 'P-1234 at ten.', *FILL, ('call', 'book', {}))
        assert said[-1] == 'Shall I book Tuesday at 10:00 AM for P-1234?'
        change = ('collect', {'values': {'slot': '0900'}})
        calls = [change, ('book', {})] if order == 'collect_first' else [('book', {}), change]
        said = await turn(
            flow, call, script, 'Yes, but make it nine.', ('calls', calls), ('call', 'book', {})
        )
        assert call.services.calls == 0, (order, 'booked on a read-back the same reply changed')
        slot, when = '0900', '9:00'
        assert call.state.facts.slot == slot, (order, call.state.facts)
        assert said[-1] == f'Shall I book Tuesday at {when} AM for P-1234?', (order, said)
        said = await turn(flow, call, script, 'Yes.', ('call', 'book', {}))
        assert call.services.bookings == {'CL001': ('P-1234', slot)}, (
            order,
            'the fresh read-back and yes book once',
        )


async def holding_covers_work_not_thinking():
    """P10: a slow tool gets one holding line before the answer; a model slow to say goodbye gets none."""

    async def slow_times(clinic: Any, state: Any) -> dict[str, str]:
        await asyncio.sleep(0.2)
        return {'0900': 'Tuesday at 9:00 AM'}

    for slow, moves, expected in (
        ('tool', (('call', 'options', {}), ('say', 'I have Tuesday at nine.')), 1),
        ('model', (('call', 'goodbye', {}),), 0),
    ):
        script = Script()
        step = ConversationStep(
            id='desk', instructions='Help.', fields=('patient', 'slot'), choices={'slot': slow_times}
        )
        flow = StepFlow([step], model=script.model, state_type=ClinicState)
        call = flow.open(ClinicState(), services=Backend())
        script.call, script.moves = call, list(moves)
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
        id='desk', instructions='Help.', fields=('patient', 'slot'), choices={'slot': hanging_times}
    )
    flow = StepFlow([step], model=script.model, state_type=ClinicState)
    call = flow.open(ClinicState(), services=Backend())
    script.call, script.moves = call, [('call', 'options', {})]
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
        ('call', 'collect', {'values': {'patient': 'P-1234'}}),
        ('call', 'hand_over', {'to': 'look_up', 'because': 'find the booking'}),
        ('say', 'You are booked for Tuesday at ten.'),
    )
    assert said == ['I found booking CL007.', 'You are booked for Tuesday at ten.'], said
    assert script.steps == ['front', 'front', 'front'], ('the code node made a request', script.steps)
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
            ('call', 'collect', {'values': {'patient': 'P-1234'}}),
            ('call', 'hand_over', {'to': 'look_up', 'because': 'find the booking'}),
            ('say', 'I could not find it.'),
        )
        assert call.state.agent == 'front' and call.state.facts.reference == '', (trouble, call.state)
        assert script.steps == ['front', 'front', 'front'], (trouble, script.steps)


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
            call.collect(patient='P-4321')
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
    """P2/P7: the receipt was cut before it reached the caller; their next words hear it again, no model, no write.

    With the rule switched off, the same reply goes to a model instead: the
    regression this check exists to catch.
    """
    for rule in (True, False):
        script = Script()
        flow, call = opened('agent.yaml', script)
        await turn(flow, call, script, 'P-1234.', ('call', 'collect', {'values': {'patient': 'P-1234'}}))
        await turn(flow, call, script, 'Ten.', ('call', 'collect', {'values': {'slot': '1000'}}))
        script.moves = [('call', 'accept', {})]
        reply = await core.text_turn(flow, call, 'Yes.')
        receipt = next(s for s in reply.segments if s.text.startswith('Booked'))
        call.acknowledge(receipt.id, heard='')
        requests = len(script.steps)
        if rule:
            said = await turn(flow, call, script, 'Yes, Tuesday at ten, great.')
        else:
            with patch.object(type(call), 'unheard_receipt', lambda self: ''):
                said = await turn(
                    flow, call, script, 'Yes, Tuesday at ten, great.', ('say', 'Anything else?')
                )
        assert call.services.calls == 1 and len(call.evidence) == 1, 'the booking was made again'
        if rule:
            assert said == ['Booked for Tuesday at 10:00 AM, reference CL001.'], said
            assert len(script.steps) == requests, 'saying the receipt again asked a model'
        else:
            assert len(script.steps) == requests + 1, 'without the rule a model answers, unaware'


async def model_words_beside_an_operation_are_not_said():
    """F3: text in the same reply as an operation call is dropped; the operation says its own words, once.

    Coval runs jCBGSipW and StET3kNN: in 4 of 11 calls the model wrote the
    read-back itself beside `book_appointment`, and the caller heard it twice.
    Text beside any other tool (`collect`), or alone, is said as before.
    """
    script = Script()
    flow, call = opened('one_agent.yaml', script)
    said = await turn(
        flow, call, script, 'P-1234 at ten.', *FILL, ('say+call', 'One moment. Shall I book ten?', 'book', {})
    )
    assert said == ['Shall I book Tuesday at 10:00 AM for P-1234?'], said
    assert not any('One moment' in s.text for s in call.all_spoken), 'dropped words were recorded as said'
    said = await turn(
        flow,
        call,
        script,
        'Actually nine.',
        ('say+call', 'Nine it is.', 'collect', {'values': {'slot': '0900'}}),
        ('call', 'book', {}),
    )
    assert said == ['Nine it is.', 'Shall I book Tuesday at 9:00 AM for P-1234?'], said
    said = await turn(flow, call, script, 'How long is it?', ('say', 'Thirty minutes.'))
    assert said == ['Thirty minutes.'], said


async def a_recorded_pick_is_read_back_by_code():
    """F4: `reads_back`: a reply that only records the slot gets its read-back from code, one request fewer.

    Coval runs jCBGSipW and StET3kNN: 21 turns spent a second request only to
    have `book_appointment` read back after `collect`. A reply with words of
    its own is left to the model, and only a heard yes still books.
    """
    script = Script()
    flow, call = opened('reads_back.yaml', script)
    said = await turn(flow, call, script, 'P-1234 at ten.', *FILL)
    assert said == ['Shall I book Tuesday at 10:00 AM for P-1234?'], said
    assert script.steps == ['desk', 'desk'], (
        'options, then collect: the read-back asked no model',
        script.steps,
    )
    said = await turn(flow, call, script, 'Yes.', ('call', 'book', {}))
    assert call.services.bookings == {'CL001': ('P-1234', '1000')}, said

    script = Script()
    flow, call = opened('reads_back.yaml', script)
    pick = {'values': {'patient': 'P-1234', 'slot': '1000'}}
    said = await turn(
        flow,
        call,
        script,
        'P-1234 at ten. How long is it?',
        ('call', 'options', {}),
        ('say+call', 'Thirty minutes.', 'collect', pick),
        ('call', 'book', {}),
    )
    assert said == ['Thirty minutes.', 'Shall I book Tuesday at 10:00 AM for P-1234?'], said
    assert len(script.steps) == 3, ('words beside the pick: the model reads back', script.steps)

    script = Script()
    flow, call = opened('reads_back.yaml', script)
    await turn(flow, call, script, 'P-1234 at ten.', *FILL, heard=False)
    said = await turn(flow, call, script, 'Yes.', ('call', 'book', {}))
    assert call.services.calls == 0 and said[-1].startswith('Shall I book'), (
        'a yes to an unheard read-back booked'
    )


async def the_same_value_said_again_is_a_yes():
    """F5: benchmark calls amAP5b4A and Tiv9sDHH (session 4c): "no, it's <the same value>" to a heard read-back.

    The caller's words decide nothing here: the scripted agent only records
    what it heard, and code compares the value with the read-back. Code
    carries it out only where the application's policy says the same value
    is a yes (`restated: confirms`, a lookup). For a booking, recording the
    same slot is only a record (session 4d, defect 1): the step's own agent
    reads the reply, and "No, do not book ten" books nothing.
    """
    patient = {'values': {'patient': 'P-1234'}}
    script = Script()
    flow, call = opened('restated.yaml', script)
    said = await turn(flow, call, script, 'P-1234.', ('call', 'collect', patient))
    assert said == ['Is your patient number P-1234?'], said
    before = len(script.steps)
    said = await turn(flow, call, script, 'No, it is P 1 2 3 4.', ('call', 'collect', patient))
    assert said[:2] == [call.locale.restated, 'Thanks, P-1234.'], said
    assert len(script.steps) - before == 1, ('the restated value asked no second request', script.steps)

    script = Script()
    flow, call = opened('restated.yaml', script)
    await turn(flow, call, script, 'P-1234.', ('call', 'collect', patient), heard=False)
    said = await turn(flow, call, script, 'No, it is P 1 2 3 4.', ('call', 'collect', patient))
    assert call.locale.restated not in said and said == ['Is your patient number P-1234?'], (
        'the same value after an unheard read-back carried it out',
        said,
    )

    ten = {'values': {'slot': '1000'}}
    for words, reply in (
        ('No, do not book ten. I am only checking if it is free.', 'Ten is free. Nothing is booked.'),
        ('Is ten free?', 'Ten is free.'),
        ('Only if ten is still free.', 'It is.'),
        ('"Ten", I said. Not booked yet.', 'Understood.'),
    ):
        script = Script()
        flow, call = opened('reads_back.yaml', script)
        await turn(flow, call, script, 'P-1234 at ten.', *FILL)
        said = await turn(flow, call, script, words, ('call', 'collect', ten), ('say', reply))
        assert call.services.calls == 0 and call.locale.restated not in said, (
            words,
            'a value said again booked',
        )
        assert said == [reply], (words, said)

    script = Script()
    flow, call = opened('reads_back.yaml', script)
    await turn(flow, call, script, 'P-1234 at ten.', *FILL)
    said = await turn(
        flow, call, script, 'Yes, ten.', ('calls', [('collect', ten), ('book', {})]), ('call', 'book', {})
    )
    assert call.services.calls == 0 and said[-1] == 'Shall I book Tuesday at 10:00 AM for P-1234?', (
        'a yes beside a recorded value is read back again, never approved in the same reply',
        said,
    )
    said = await turn(flow, call, script, 'Yes.', ('call', 'book', {}))
    assert call.services.bookings == {'CL001': ('P-1234', '1000')}, said

    script = Script()
    flow, call = opened('reads_back.yaml', script)
    await turn(flow, call, script, 'P-1234 at ten.', *FILL)
    said = await turn(flow, call, script, 'No, nine.', ('call', 'collect', {'values': {'slot': '0900'}}))
    assert call.services.calls == 0 and said == ['Shall I book Tuesday at 9:00 AM for P-1234?'], said

    script = Script()
    flow, call = opened('restated.yaml', script)
    await turn(flow, call, script, 'P-1234.', ('call', 'collect', patient))
    said = await turn(
        flow,
        call,
        script,
        'No, P-56. Well, P-1234 then.',
        ('call', 'collect', {'values': {'patient': 'P-56'}}),
        ('call', 'collect', patient),
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
    flow, call = opened('one_agent.yaml', script)
    await turn(flow, call, script, 'P-1234 at ten.', *FILL, ('call', 'book', {}))
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
    await turn(flow, call, script, 'Yes.', ('call', 'book', {}))
    assert call.services.bookings == {'CL001': ('P-1234', '1000')}, 'the yes to the repeated read-back books'

    for _ in range(REMINDERS):
        await remind(call)
    assert not call.ended and call.spoken[-1].text == ENGLISH.still_there, (
        'nothing left to ask: just the check'
    )
    await remind(call)
    assert call.ended == 'abandoned' and closed and call.spoken[-1].text == ENGLISH.idle_goodbye, call.ended


async def a_question_is_not_a_no_on_the_clinic():
    """P19: the decline rule outside the desk: a prose step with two operations and no hand-over."""
    for declined in (False, True):
        script = Script()
        flow, call = opened('one_agent.yaml', script)
        await turn(flow, call, script, 'P-1234 at ten.', *FILL, ('call', 'book', {}))
        if declined:
            said = await turn(
                flow, call, script, 'No, leave it. Bye.', ('calls', [('decline', {}), ('goodbye', {})])
            )
        else:
            await turn(flow, call, script, 'Before I say, do you take walk-ins?', ('say', 'We do, mornings.'))
            said = await turn(flow, call, script, 'Bye.', ('call', 'goodbye', {}))
        assert call.services.calls == 0, said
        assert (ENGLISH.left_undone in ' '.join(said)) is not declined, (declined, said)


async def main() -> None:
    await no_reader_runs_in_any_pattern()
    public_path_loads_no_reader()
    await hostile_calls_are_refused()
    await a_confirm_steps_answer_claims_nothing()
    await a_change_in_the_same_reply_spends_no_yes()
    await holding_covers_work_not_thinking()
    await a_cut_off_tool_leaves_no_timer()
    await a_code_node_returns_with_no_model()
    await late_options_offer_nothing()
    await an_unheard_receipt_is_said_again()
    await model_words_beside_an_operation_are_not_said()
    await a_recorded_pick_is_read_back_by_code()
    await the_same_value_said_again_is_a_yes()
    reads_back_is_checked_at_load()
    await silence_gets_a_reminder_then_a_goodbye()
    await a_question_is_not_a_no_on_the_clinic()
    print(
        'nodes ok: no reader, hostile calls, mixed reply, holding, code node, late options, unheard receipt, '
        'no echo, code read-back, a question is not a no'
    )


if __name__ == '__main__':
    asyncio.run(main())
