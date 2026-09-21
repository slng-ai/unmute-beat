"""Run: uv run --env-file .env python -m tests.test_budget

One caller turn, one allowance. Model requests, tool calls, step runs and a
deadline, all of them shared across every agent run in the turn and none of
them refillable by handing the caller on.

The failure this is written against: the turn loop counted hops, ran out, and
then made one more model request anyway. A budget that buys an extra request
when it is spent is not a budget, and the extra request was itself unbounded
-- it could call a tool, take as long as it liked, and there was no number
anywhere that said how many requests a turn could make.

Scripted models, no audio, no network.

Asserts, no pytest, no fixtures, same as the rest.
"""

import asyncio
import json
from contextlib import ExitStack

from pydantic_ai import ModelRequest, ModelResponse, RetryPromptPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from pipecat.frames.frames import LLMFullResponseStartFrame, LLMTextFrame, TTSSpeakFrame

from salon import diary
from salon.agents import AGENTS
from salon.state import open_call
from voice.harness import TurnBudget
from voice.processor import EXHAUSTED, PydanticAgentProcessor

PHONE = '+34600111222'


class Probe(PydanticAgentProcessor):
    def __init__(self, call, **kwargs):
        super().__init__(call, AGENTS, **kwargs)
        self.frames = []

    async def push_frame(self, frame, direction=None):
        self.frames.append(frame)

    def spoken(self):
        return ''.join(f.text for f in self.frames if isinstance(f, LLMTextFrame))

    def fixed(self):
        return [f.text for f in self.frames if isinstance(f, TTSSpeakFrame)]

    def runs(self):
        return sum(isinstance(f, LLMFullResponseStartFrame) for f in self.frames)


def scripted(**by_agent):
    stack = ExitStack()
    for name, fn in by_agent.items():
        stack.enter_context(AGENTS[name].agent.override(model=FunctionModel(stream_function=fn)))
    return stack


def says(text, chunk=8):
    async def stream(messages, info: AgentInfo):
        for start in range(0, len(text), chunk):
            yield text[start:start + chunk]
    return stream


def hands_to(name, because='loop'):
    async def stream(messages, info: AgentInfo):
        if not any(t.name == 'hand_over' for t in info.output_tools):
            yield 'Sorry, what was that?'
            return
        yield {0: DeltaToolCall(name='hand_over',
                                json_args=json.dumps({'to': name, 'because': because}))}
    return stream


def _refusals(call) -> list[str]:
    return [
        str(part.content)
        for message in call.messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, RetryPromptPart)
    ]


# --- the hop budget bounds the turn, and nothing follows it -----------------


async def exhaustion_makes_no_further_request():
    """Alternating handoffs until the budget is gone.

    Exactly the runs it paid for, then a line this code speaks. The caller is
    never left in silence, and the model is never asked again.
    """
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing')
    probe = Probe(call, budget=TurnBudget(step_runs=3))

    with scripted(concierge=hands_to('complaints'), complaints=hands_to('concierge')):
        await probe._turn('hello?')

    assert probe.runs() == 3, probe.runs()
    assert EXHAUSTED in probe.fixed(), probe.fixed()
    assert not probe.spoken(), 'the fallback is fixed speech, not another model run'


async def the_fallback_does_no_business_work():
    """It speaks and stops. No tool, no state change, no booking.

    The run this replaced was narrowed to `str`, which stopped it handing off
    -- but it still held every ordinary tool, so an exhausted turn could book
    an appointment on its way out.
    """
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing')
    before = call.state
    probe = Probe(call, budget=TurnBudget(step_runs=2))

    with scripted(concierge=hands_to('complaints'), complaints=hands_to('concierge')):
        await probe._turn('hello?')

    assert EXHAUSTED in probe.fixed()
    assert call.state.appointment is None and call.state.booking_day == ''
    assert call.state.customer_verified == before.customer_verified


async def the_budget_is_not_refilled_by_exhausting_it():
    """A second turn gets its own allowance; the first one's is simply gone.

    Which is the difference between a per-turn budget and a per-run one: the
    point is that the *turn* stops, not that each run gets a fresh start.
    """
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing')
    probe = Probe(call, budget=TurnBudget(step_runs=2))

    with scripted(concierge=hands_to('complaints'), complaints=hands_to('concierge')):
        await probe._turn('hello?')
    first = probe.runs()

    probe.frames.clear()
    with scripted(concierge=says('We are open until six.'),
                  complaints=says('We are open until six.')):
        await probe._turn('when do you close?')

    assert first == 2, first
    assert probe.runs() == 1, 'the new turn started from a full allowance'
    assert probe.spoken() == 'We are open until six.'


# --- one tally across every run in the turn ---------------------------------


async def model_requests_are_counted_across_agents():
    """Four agents in one turn spend one allowance between them.

    Counted per run, a turn could hand the caller round the houses and each
    agent would start again from zero.
    """
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing')
    probe = Probe(call, budget=TurnBudget(step_runs=4))

    with scripted(concierge=hands_to('complaints'), complaints=hands_to('concierge')):
        await probe._turn('hello?')

    # Read off the turn's declared outcome, not a private tally: the count
    # the core reports is the one a runtime and a trace both see.
    assert probe._finished.requests == 4, probe._finished
    assert probe.runs() == 4
    assert probe._finished.status == 'exhausted', probe._finished


async def the_request_limit_stops_a_turn_that_never_hops():
    """One agent, many requests, no handoffs at all.

    The hop budget does not see this: every request is inside one run. The
    shared request limit is what bounds it, and it is a separate number for
    exactly that reason.
    """
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing')
    probe = Probe(call, budget=TurnBudget(step_runs=4, model_requests=3))
    asked = []

    async def never_settles(messages, info: AgentInfo):
        asked.append(1)
        yield {0: DeltaToolCall(name='look_up_salon_info',
                                json_args=json.dumps({'query': f'question {len(asked)}'}))}

    with scripted(concierge=never_settles):
        await probe._turn('tell me everything')

    assert len(asked) == 3, asked
    assert EXHAUSTED in probe.fixed(), probe.fixed()


async def the_tool_limit_is_its_own_number():
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing')
    probe = Probe(call, budget=TurnBudget(step_runs=4, model_requests=20, tool_calls=2))
    ran = []

    async def keep_looking(messages, info: AgentInfo):
        ran.append(1)
        yield {0: DeltaToolCall(name='look_up_salon_info',
                                json_args=json.dumps({'query': f'q{len(ran)}'}))}

    with scripted(concierge=keep_looking):
        await probe._turn('tell me everything')

    assert len(ran) <= 3, ran
    assert EXHAUSTED in probe.fixed(), probe.fixed()


# --- the deadline covers the whole turn -------------------------------------


async def a_slow_tool_hits_the_turn_deadline():
    """The deadline is over the turn, not over one request.

    A tool that never returns used to hold the line open for as long as it
    liked, because nothing above it was counting.
    """
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing')
    probe = Probe(call, budget=TurnBudget(seconds=0.25))

    async def stall(messages, info: AgentInfo):
        await asyncio.sleep(5)
        yield 'too late'

    started = asyncio.get_running_loop().time()
    with scripted(concierge=stall):
        await probe._turn('hello?')
    took = asyncio.get_running_loop().time() - started

    assert took < 2, f'the deadline did not fire: {took:.2f}s'
    assert EXHAUSTED in probe.fixed(), probe.fixed()


# --- repair is allowed, and also counted ------------------------------------


async def a_permitted_repair_succeeds():
    """A tool refuses, the model corrects itself, the turn completes.

    Repair is the thing the budget must not forbid: this is the case where
    the second attempt is the right one.
    """
    diary.reset()
    call = open_call(PHONE)
    call.commit(agent='verify')
    probe = Probe(call, budget=TurnBudget(repair_retries=2))
    attempts = []

    async def wrong_then_right(messages, info: AgentInfo):
        attempts.append(1)
        # First a number that is not a Spanish one; then the real one.
        phone = '+348340464' if len(attempts) == 1 else PHONE
        yield {0: DeltaToolCall(name='confirm_customer', json_args=json.dumps({'phone': phone}))}

    with scripted(verify=wrong_then_right, concierge=says('What can I do for you?')):
        await probe._turn('it is plus three four six zero zero one one one two two two')

    assert len(attempts) == 2, attempts
    assert _refusals(call), 'the model was told what was wrong'
    assert call.state.customer_verified == 'existing', call.state
    assert EXHAUSTED not in probe.fixed(), 'a legitimate repair must not exhaust the turn'


async def repair_retries_are_bounded():
    """A model that never gets it right is stopped, and stopped politely."""
    diary.reset()
    call = open_call(PHONE)
    call.commit(agent='verify')
    probe = Probe(call, budget=TurnBudget(step_runs=1, repair_retries=1))
    attempts = []

    async def always_wrong(messages, info: AgentInfo):
        attempts.append(1)
        yield {0: DeltaToolCall(name='confirm_customer',
                                json_args=json.dumps({'phone': '+348340464'}))}

    with scripted(verify=always_wrong):
        await probe._turn('my number is eight three four oh four six four')

    assert len(attempts) <= 3, attempts
    assert call.state.customer_verified == '', 'nothing was written by a failed repair'
    assert probe.fixed(), 'the caller hears something rather than silence'


async def main():
    await exhaustion_makes_no_further_request()
    await the_fallback_does_no_business_work()
    await the_budget_is_not_refilled_by_exhausting_it()
    await model_requests_are_counted_across_agents()
    await the_request_limit_stops_a_turn_that_never_hops()
    await the_tool_limit_is_its_own_number()
    await a_slow_tool_hits_the_turn_deadline()
    await a_permitted_repair_succeeds()
    await repair_retries_are_bounded()
    print('ok')


asyncio.run(main())
