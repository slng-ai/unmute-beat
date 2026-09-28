"""The public boundary, end to end: a clinic built on `import core` alone books one appointment over text.

The clinic (`tests/clinic_flow`) supplies a state model, a service, typed
domain functions and a manifest (`agent.yaml`). This drives it through `core.load`, `compile`,
`open`, `text_turn` and `acknowledge`, with a scripted model standing where
a provider would, and checks the backend, the receipt and the evidence.
It also counts what the clinic had to write, against what the native
Pipecat salon writes for the same guarantees (N13's session 2 foundation).
"""

import ast
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

import core
from tests.clinic_flow.clinic import Backend, ClinicState

FLOW = Path(__file__).parent / 'clinic_flow' / 'agent.yaml'
CLOCK = datetime.fromisoformat('2026-09-28T08:30:00+02:00')


class Script:
    """The scripted model: what each step's agent does next. There is nothing else to script.

    Every request is a step's own agent, streamed. A non-streamed request
    would be a separate reading of the reply, which the node-agent core never
    sends, so it fails the test (P1).
    """

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.label = 'unclear'
        self.agent_requests = 0
        self.model = FunctionModel(self.reading, stream_function=self.agent)

    def reading(self, messages: Any, info: AgentInfo) -> ModelResponse:
        raise AssertionError('a separate reading was requested; only step agents may run')

    async def agent(self, messages: Any, info: AgentInfo):
        self.agent_requests += 1
        tools = {tool.name for tool in info.output_tools}
        if 'collect' in tools and self.values:
            values, self.values = self.values, {}
            yield {0: DeltaToolCall(name='collect', json_args=json.dumps({'values': values}))}
            return
        if 'accept' in tools and self.label == 'accept':
            yield {0: DeltaToolCall(name='accept', json_args='{}')}
            return
        if 'answer' in tools:
            yield {
                0: DeltaToolCall(name='answer', json_args=json.dumps({'words': 'Could you say that again?'}))
            }
            return
        yield 'Sorry, could you say that again?'


def opened(script: Script, backend: Backend | None = None) -> tuple[core.StepFlow, core.FlowCall, Backend]:
    flow = core.load(FLOW).compile(model=script.model)
    backend = backend or Backend()
    call = flow.open(ClinicState(), services=backend, clock=lambda: CLOCK)
    return flow, call, backend


def delivered(call: core.FlowCall, reply: core.TextReply) -> None:
    """The reader got every segment of the reply."""
    call.acknowledge(*(segment.id for segment in reply.segments))


async def turn(flow, call, script: Script, words: str, **values: Any) -> core.TextReply:
    script.values = values
    return await core.text_turn(flow, call, words)


async def one_booking_through_the_public_boundary():
    script = Script()
    flow, call, backend = opened(script)
    core.greet(call, 'Hello, this is the clinic.')
    call.acknowledge(call.spoken[-1].id)

    reply = await turn(flow, call, script, 'My number is P-1234.', patient='P-1234')
    assert call.state.facts.patient == 'P-1234' and call.state.agent == 'slot', call.state
    assert reply.text == '' and reply.segments[-1].text == (
        'I have Tuesday at 9:00 AM or Tuesday at 10:00 AM. Which works for you?'
    ), reply
    delivered(call, reply)

    reply = await turn(flow, call, script, 'Ten, please.', slot='1000')
    assert (
        call.state.agent == 'confirm'
        and reply.segments[-1].text == 'Shall I book Tuesday at 10:00 AM for P-1234?'
    )
    assert backend.calls == 0, 'a pick is not a yes'
    delivered(call, reply)

    script.label = 'accept'
    reply = await turn(flow, call, script, 'Yes, please.')
    assert backend.bookings == {'CL001': ('P-1234', '1000')}, backend.bookings
    assert call.state.facts.reference == 'CL001' and call.state.outcome == 'completed', call.state
    assert [s.text for s in reply.segments] == [
        'Booked for Tuesday at 10:00 AM, reference CL001.',
        'Anything else?',
    ]
    assert script.agent_requests == 3, script.agent_requests
    [filed] = call.evidence
    assert filed.operation == 'book' and filed.receipt['reference'] == 'CL001' and filed.identity == 'P-1234'
    assert filed.proposal and filed.segments, 'the booking names the delivered read-back it answered'


async def an_unheard_read_back_books_nothing():
    """The same call, with the read-back generated but never acknowledged: the yes answers nothing."""
    script = Script()
    flow, call, backend = opened(script)
    delivered(call, await turn(flow, call, script, 'P-1234.', patient='P-1234'))
    await turn(flow, call, script, 'Ten.', slot='1000')
    script.label = 'accept'
    await turn(flow, call, script, 'Yes.')
    assert backend.calls == 0 and not call.evidence, backend.bookings


def what_the_application_wrote():
    """N13, session 2: the clinic's authored code, split into domain and orchestration, next to the native salon's.

    Domain is state, arguments, receipts, the service and the read-back's
    words. Orchestration is anything that runs the conversation: here none.
    The native build (`use-cases/salon-pipecat-flows/graph_salon`) writes
    its own consent guards, tool wrapper, speech filter and empty-reply
    guard to get the same guarantees; counted, not judged.
    """
    source = (Path(__file__).parent / 'clinic_flow' / 'clinic.py').read_text()
    classes = {node.name for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ClassDef)}
    assert classes == {
        'Contact',
        'ClinicState',
        'Appointment',
        'Booked',
        'Cancellation',
        'Cancelled',
        'PatientRef',
        'Found',
        'Backend',
    }
    assert 'CallContext' not in source and 'Operation' not in source.replace('OutcomeUnknownError', '')
    assert 'from core.' not in source and 'ProposalLedger' not in source
    native = Path(__file__).parents[2] / 'use-cases' / 'salon-pipecat-flows' / 'graph_salon'
    if native.exists():
        inventory = {
            name: len((native / name).read_text().splitlines())
            for name in ('call.py', 'handlers.py', 'speech.py')
        }
        print('authored orchestration, native salon:', inventory, '; clinic slice: 0 lines')


async def main() -> None:
    await one_booking_through_the_public_boundary()
    await an_unheard_read_back_books_nothing()
    what_the_application_wrote()
    print('public ok: load, compile, open, text turns, acknowledgement, one guarded booking')


if __name__ == '__main__':
    asyncio.run(main())
