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
* P2/P7: an unheard receipt is said again with no model request and no write.

Scripted models prove runtime properties, not language understanding.
"""

import asyncio
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
from core.policies import TurnBudget
from core.steps import ConversationStep, StepFlow, _offer
from core.turn import take_turn
from tests.clinic_flow.clinic import Backend, ClinicState

HERE = Path(__file__).parent / 'clinic_flow'


class Script:
    """Each step agent's request takes the next move, and records which step asked. Nothing else may run.

    A move is `('say', words)`, `('call', tool, args)`, or `('calls', [(tool, args), ...])`
    for one response carrying several tool calls in that order.
    """

    def __init__(self) -> None:
        self.moves: list[tuple[Any, ...]] = []
        self.steps: list[str] = []
        self.call: core.FlowCall | None = None
        self.delay = 0.0
        self.model = FunctionModel(self.reading, stream_function=self.agent)

    def reading(self, messages: Any, info: AgentInfo) -> ModelResponse:
        raise AssertionError('a separate reading was requested; only step agents may run')

    async def agent(self, messages: Any, info: AgentInfo):
        self.steps.append(self.call.state.agent if self.call is not None else '?')
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
        ('model', (('call', 'goodbye', {'words': 'Goodbye.'}),), 0),
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
        call._offered = {'slot': {'1000': 'Tuesday at 10:00 AM'}}
        release.clear()
        looking = asyncio.create_task(_offer(call, step.choices))
        await asyncio.sleep(0)
        if moved_on == 'request':
            call.collect(patient='P-4321')
            call._offered = {'slot': {'1000': 'Tuesday at 10:00 AM'}}
        else:
            call.commit(agent='elsewhere')
        release.set()
        result = await looking
        assert 'stale' in result, (moved_on, result)
        assert call._offered == {'slot': {'1000': 'Tuesday at 10:00 AM'}}, (
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


async def main() -> None:
    await no_reader_runs_in_any_pattern()
    public_path_loads_no_reader()
    await hostile_calls_are_refused()
    await a_change_in_the_same_reply_spends_no_yes()
    await holding_covers_work_not_thinking()
    await a_code_node_returns_with_no_model()
    await late_options_offer_nothing()
    await an_unheard_receipt_is_said_again()
    print(
        'nodes ok: no reader, hostile calls, mixed reply, holding, code node, late options, unheard receipt'
    )


if __name__ == '__main__':
    asyncio.run(main())
