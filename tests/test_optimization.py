"""Run: python -m tests.test_optimization — model-free actions retain the same guards."""

import asyncio
from contextlib import suppress
from dataclasses import replace
from datetime import datetime

from pipecat.frames.frames import LLMTextFrame, TTSSpeakFrame
from pydantic import ValidationError
from pydantic_ai import Agent, RunContext, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from salon import agents, diary
from salon.diary import Diary
from salon.state import open_call
from voice.core import Barge, RunStarted, run_turn
from voice.harness import Node, TurnBudget, model_clients
from voice.processor import PydanticAgentProcessor

PHONE = '+34600111222'
CLOCK = datetime(2026, 9, 21, 10, tzinfo=diary.TZ)


books = Diary()
"""This module's salon, emptied by each case that writes to it."""


class Counter(PydanticAgentProcessor):
    """The processor with its downstream replaced by a tally."""

    def __init__(self, call, agents):
        super().__init__(call, agents)
        self.ended = False
        call._end = self._hang_up
        self.reset()

    async def _hang_up(self):
        self.ended = True

    def reset(self):
        self.spoke = False
        self.fixed = 0

    async def push_frame(self, frame, direction=None):
        if isinstance(frame, LLMTextFrame):
            self.spoke = True
        elif isinstance(frame, TTSSpeakFrame):
            self.fixed += 1

    def requests(self) -> int:
        """Provider requests in the turn that just ran, off the turn's own shared `RunUsage` tally."""
        return self._finished.requests if self._finished else 0


def context(call):
    return RunContext(deps=call, model=None, usage=None)  # ty: ignore[invalid-argument-type]


def fresh(agent='booking'):
    books.reset()
    books.set_clock(CLOCK)
    call = open_call(PHONE, diary=books)
    call.commit(agent=agent, customer_verified='existing', verified_phone=PHONE)
    return call


async def offered():
    call = fresh()
    slot = books.find_slots(PHONE, 'Friday', 'haircut', CLOCK, offered_to=call.id)['slots'][0]
    (await agents.ask_booking(context(call), 'book', slot['slot_id'], '')).put(call)
    return call


async def approvals_and_receipts():
    for answer in ('Yes.', 'Go for it.', 'Sure, please.', 'Okay.'):
        call = await offered()
        probe = Counter(call, agents.AGENTS)
        await probe._turn(answer)
        assert probe._finished.status == 'answered' and probe.requests() == 0
        assert len(books.bookings_for(PHONE, CLOCK.date())) == 1 and call.state.writes == 1
        assert call.requests == [] and len(call.spoken) == 1
        assert call.turn in call._retained_turns
        assert sum(isinstance(p, UserPromptPart) for m in call._protocol for p in m.parts) == 1
        receipt = [
            m for m in call._protocol if (m.metadata or {}).get('application_action') == 'save_booking'
        ]
        assert len(receipt) == 1 and call.state.appointment.booking_id in str(receipt)
        call.turn_started(answer)
        assert agents.advance(call) is None, 'the same approval cannot write twice'

    for words in (
        'Yes, but Saturday.',
        'Go for it?',
        'Yes, how much?',
        'Maybe.',
        'No.',
        'Yes, if it is free.',
    ):
        call = await offered()
        call.turn_started(words)
        assert agents.advance(call) is None, words
        assert not call.state.writes

    call = await offered()
    call.commit(customer_phone='+34934567890', verified_phone='+34934567890', customer_verified='existing')
    call.turn_started('Yes.')
    assert agents.advance(call) is None, 'approval belonged to the old identity'
    call = await offered()
    call.turn_started('One moment.')
    call.turn_started('Yes.')
    assert agents.advance(call) is None, 'stale approval'
    try:
        await agents._save_booking(call, 'invented', '', '')  # ty: ignore[invalid-argument-type]
    except ValidationError:
        pass
    else:
        raise AssertionError('local execution bypassed input validation')


async def cancellation_keeps_committed_receipt():
    call = fresh('complaints')
    call.turn_started('My haircut was uneven. I want a refund.')
    (await agents.ask_complaint(context(call), 'My haircut was uneven', 'I want a refund')).put(call)
    entered = asyncio.Event()

    async def blocked_speech(text):
        entered.set()
        await asyncio.Event().wait()

    probe = Counter(call, agents.AGENTS)
    call._say = blocked_speech
    task = asyncio.create_task(probe._turn('Yes.'))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert len(books.complaints()) == 1 and call.state.writes == 1
    assert call.turn in call._retained_turns and not call._complaint
    receipt = [
        m for m in call._protocol if (m.metadata or {}).get('application_action') == 'record_complaint'
    ]
    assert len(receipt) == 1 and books.complaints()[0]['reference'] in str(receipt)
    call.turn_started('Yes.')
    assert agents.advance(call) is None
    assert not call.requests


async def barge_before_execution():
    for superseded in (False, True):
        call = await offered()
        barge = Barge()
        stream = run_turn(call, agents.AGENTS, 'Go for it.', TurnBudget(), barge)
        assert isinstance(await anext(stream), RunStarted)
        if superseded:
            call.begin_turn()
        else:
            barge.fire()
        async for _ in stream:
            pass
        assert call.state.writes == 0 and not books.bookings_for(PHONE, CLOCK.date())

    call = await offered()
    events = [event async for event in run_turn(call, agents.AGENTS, 'Yes.', TurnBudget(tool_calls=0))]
    assert events[-1].status == 'exhausted' and call.state.writes == 0


async def unsupported_booking_speech():
    call = fresh()
    probe = Counter(call, agents.AGENTS)
    requests = 0
    invented = 'Your appointment is complimentary now; you will not need to pay.'

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield invented
        else:
            yield {0: DeltaToolCall(name='booking_guidance', json_args='{"topic":"retry"}')}

    with agents.AGENTS['booking'].agent.override(model=FunctionModel(stream_function=stream)):
        await probe._turn('Could my appointment be free?')
    assert requests == 2 and probe._finished.status == 'answered'
    assert not probe.spoke and invented not in ' '.join(s.generated for s in call.all_spoken)
    assert not call.state.writes


async def phone_and_lifecycle():
    call = open_call(PHONE, diary=books)
    call.commit(agent='verify', pending='booking')
    call.turn_started('Book a haircut for Friday afternoon.')
    call._return_status = 'Application handoff: concierge -> verify.'
    action = agents.advance(call)
    assert action and action.name == 'ask_number'
    (await action.run()).put(call)
    call.turn_started('Yes, that is my number.')
    action = agents.advance(call)
    assert action and action.name == 'confirm_customer'
    result = await action.run()
    assert result.to == 'booking' and call.state.verified_phone == PHONE
    call = open_call(PHONE, diary=books)
    call.commit(agent='verify')
    (await agents.ask_number(context(call), PHONE)).put(call)
    call.commit(customer_phone='+34934567890')
    call.turn_started('Yes.')
    assert agents.advance(call) is None, 'phone approval survived an identity change'
    for words in ('My number ends in two two.', 'Use another number.', '+34 600 111', 'Actually, wait.'):
        call = open_call(PHONE, diary=books)
        call.commit(agent='verify', pending='booking')
        call.turn_started(words)
        call._return_status = 'Application handoff: concierge -> verify.'
        assert agents.advance(call) is None, words
    assert len({id(n.agent.model) for n in agents.AGENTS.values()}) == 1

    counts = []

    class Client(FunctionModel):
        async def __aenter__(self):
            counts.append('enter')
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            counts.append('exit')

    async def unused(messages, info):
        yield 'Unused.'

    model = Client(stream_function=unused)
    node = Node(Agent(model))
    try:
        async with model_clients({'a': node, 'b': replace(node)}):
            assert counts == ['enter']
            raise RuntimeError('close on failure')
    except RuntimeError:
        pass
    assert counts == ['enter', 'exit']


async def main():
    await approvals_and_receipts()
    await cancellation_keeps_committed_receipt()
    await barge_before_execution()
    await unsupported_booking_speech()
    await phone_and_lifecycle()
    print('optimization ok: zero-request approvals, validation, retention, fencing, lifecycle')


if __name__ == '__main__':
    asyncio.run(main())
