"""Run: python -m tests.test_optimization — classified actions retain the same guards."""

import asyncio
import json
from contextlib import suppress
from dataclasses import replace
from datetime import datetime

from pipecat.frames.frames import LLMTextFrame, TTSSpeakFrame
from pydantic import ValidationError
from pydantic_ai import Agent, ModelResponse, ModelRetry, RunContext, TextPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from core.graph import Node, model_clients
from core.policies import TurnBudget
from core.processor import PydanticAgentProcessor
from core.turn import Barge, RunStarted, run_turn
from salon import agents, diary
from salon.diary import Diary
from salon.models.state import open_call
from salon.tools import booking
from tests.classifier_model import classifier_model

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
        assert probe._finished.status == 'answered' and probe.requests() == 1
        assert len(books.bookings_for(PHONE, CLOCK.date())) == 1 and call.state.writes == 1
        assert len(call.requests) == 1 and call.requests[0].kind == 'classifier' and len(call.spoken) == 1
        assert call.turn in call._retained_turns
        assert sum(isinstance(p, UserPromptPart) for m in call._protocol for p in m.parts) == 1
        receipt = [
            m for m in call._protocol if (m.metadata or {}).get('application_action') == 'save_booking'
        ]
        assert len(receipt) == 1 and call.state.appointment.booking_id in str(receipt)
        call.turn_started(answer)
        assert (await agents.advance(call)) is None, 'the same approval cannot write twice'

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
        assert (await agents.advance(call)) is None, words
        assert not call.state.writes

    call = await offered()
    call.commit(customer_phone='+34934567890', verified_phone='+34934567890', customer_verified='existing')
    call.turn_started('Yes.')
    assert (await agents.advance(call)) is None, 'approval belonged to the old identity'
    call = await offered()
    call.turn_started('One moment.')
    call.turn_started('Yes.')
    assert (await agents.advance(call)) is None, 'stale approval'
    try:
        await booking.write_booking(call, 'invented', '', '')  # ty: ignore[invalid-argument-type]
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
    assert (await agents.advance(call)) is None
    assert len(call.requests) == 1 and call.requests[0].kind == 'classifier'


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
    call._entered = 'handoff'
    assert (await agents.advance(call)) is None, 'nothing was asked, so nothing is answered'
    action = agents.AGENTS['verify'].opening(call)
    assert action and action.name == 'ask_number'
    (await action.run()).put(call)
    call.turn_started('Yes, that is my number.')
    action = await agents.advance(call)
    assert action and action.name == 'confirm_customer'
    result = await action.run()
    assert result.to == 'booking' and call.state.verified_phone == PHONE
    call = open_call(PHONE, diary=books)
    call.commit(agent='verify')
    (await agents.ask_number(context(call), PHONE)).put(call)
    call.commit(customer_phone='+34934567890')
    call.turn_started('Yes.')
    assert (await agents.advance(call)) is None, 'phone approval survived an identity change'
    for words in ('My number ends in two two.', 'Use another number.', '+34 600 111', 'Actually, wait.'):
        call = open_call(PHONE, diary=books)
        call.commit(agent='verify', pending='booking')
        call.turn_started(words)
        call._entered = 'handoff'
        assert agents.AGENTS['verify'].opening(call) is None, words
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


def scripted(*calls: tuple[str, dict]):
    """A model that answers each request with the next tool call, and fails the test if asked once too often."""
    replies = list(calls)
    seen: list[str] = []

    async def stream(messages, info):
        assert replies, 'a model request was made that the fast path should have saved'
        seen.append(messages[-1].instructions or '')
        name, args = replies.pop(0)
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args))}

    return FunctionModel(stream_function=stream), seen


async def openings_skip_the_model():
    """One booking from a caller with no caller ID: every turn costs at most one request.

    The trace of 2026-09-23 spent two on each of three turns: verify was run
    only to ask for a number, booking only to offer times for a request the
    caller had already made, and again only to find the id of a time it had
    just offered. Each is now an opening or a rendered fact, and each is
    counted here.
    """
    books.reset()
    books.set_clock(CLOCK)
    call = open_call('', diary=books)
    probe = Counter(call, agents.AGENTS)
    never, _ = scripted()
    concierge, _ = scripted(
        (
            'hand_over',
            {'to': 'booking', 'because': 'a haircut', 'booking': {'service': 'haircut', 'when': 'Friday'}},
        )
    )
    with (
        agents.AGENTS['concierge'].agent.override(model=concierge),
        agents.AGENTS['verify'].agent.override(model=never),
    ):
        await probe._turn('I would like to book a haircut for Friday.')
    assert probe.requests() == 1, probe.requests()
    assert call.state.agent == 'verify' and call.state.pending == 'booking', call.state
    assert call.state.wanted and call.state.wanted.when == 'Friday', call.state.wanted
    assert 'What phone number should I use' in call.spoken[-1].text, call.spoken

    verify, _ = scripted(('ask_number', {'phone': PHONE}))
    with agents.AGENTS['verify'].agent.override(model=verify):
        await probe._turn('It is plus three four, six hundred, one one one, two two two.')
    assert probe.requests() == 1 and call.pending('phone') is None, 'a readback went out'

    with agents.AGENTS['booking'].agent.override(model=never):
        await probe._turn("Yes, that's right.")
    assert probe.requests() == 1 and call.requests[0].kind == 'classifier', call.requests
    assert call.state.agent == 'booking' and call.state.customer_verified, call.state
    assert call.state.wanted is None and len(call.state.offered) == 3, call.state
    assert call.spoken[-1].text.startswith('For a haircut on Friday, I have 9:00 AM'), call.spoken

    nine = call.state.offered[0].slot_id
    booking_model, prompts = scripted(('save_booking', {'action': 'book', 'slot_id': nine, 'booking_id': ''}))
    with agents.AGENTS['booking'].agent.override(model=booking_model):
        await probe._turn('9:00 AM, please.')
    assert f'`{nine}`' in prompts[0], 'the offered ids are in the prompt, so no diary read is needed'
    assert probe.requests() == 1 and call.state.writes == 1, (probe.requests(), call.state)
    assert call.state.offered == [], 'a save takes the offer off the table'


async def openings_decline():
    """Each opening falls back to the model when what it knows is not enough."""
    call = open_call('', diary=books)
    call.commit(agent='verify', pending='booking')
    call.turn_started('My number is six hundred, one one one.')
    call._entered = 'handoff'
    assert agents.AGENTS['verify'].opening(call) is None, 'digits said: the model combines them'
    call.turn_started('Book me in.')
    assert agents.AGENTS['verify'].opening(call) is None, 'a new turn has entered nothing'

    call = fresh()
    call.turn_started('Yes.')
    call._entered = 'completed'
    assert agents.AGENTS['booking'].opening(call) is None, 'no request was carried'
    call.commit(wanted={'service': 'haircut', 'when': 'Friday'})
    assert agents.AGENTS['booking'].opening(call) is not None
    call._entered = 'handoff'
    assert agents.AGENTS['booking'].opening(call) is None, 'only a return from verification'
    call._entered = 'completed'
    call.commit(customer_phone='+34934567890')
    assert agents.AGENTS['booking'].opening(call) is None, 'the identity changed, so nobody is verified'

    call = fresh()
    call.commit(wanted={'service': 'haircut', 'when': 'next Friday'})
    call.turn_started('Friday, please.')
    call._entered = 'completed'
    action = agents.AGENTS['booking'].opening(call)
    assert action is not None
    try:
        await action.run()
    except ModelRetry:
        pass
    else:
        raise AssertionError('a carried day the caller never said was looked up')
    assert call.state.wanted is not None and not call.state.offered, 'a refused read changes nothing'


async def warm_up():
    """One single-token request per distinct client as the session opens, never raised if it fails."""
    sent = []

    async def answer(messages, info):
        sent.append(info.model_settings)
        return ModelResponse(parts=[TextPart('ok')])

    node = Node(Agent(FunctionModel(answer)))
    async with model_clients({'a': node, 'b': replace(node)}, warm=True):
        await asyncio.sleep(0.05)
    assert len(sent) == 1 and sent[0]['max_tokens'] == 1, sent

    async def broken(messages, info):
        raise RuntimeError('provider down')

    async with model_clients({'a': Node(Agent(FunctionModel(broken)))}, warm=True):
        await asyncio.sleep(0.05)


async def main():
    await approvals_and_receipts()
    await cancellation_keeps_committed_receipt()
    await barge_before_execution()
    await unsupported_booking_speech()
    await phone_and_lifecycle()
    await openings_skip_the_model()
    await openings_decline()
    await warm_up()
    print(
        'optimization ok: single-request approvals, openings, validation, retention, fencing, lifecycle, warm-up'
    )


if __name__ == '__main__':
    with classifier_model():
        asyncio.run(main())
