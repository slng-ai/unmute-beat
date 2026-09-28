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

`tests/__main__.py` runs each module with `run_name='__main__'`, so this still runs there -- and a
debugging session can import one module without running every check in it.
"""

import asyncio
import json
from contextlib import ExitStack

from pipecat.frames.frames import LLMFullResponseStartFrame, LLMTextFrame, TTSSpeakFrame
from pydantic_ai import ModelRequest, RetryPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from core.policies import TurnBudget
from core.processor import EXHAUSTED, PydanticAgentProcessor
from salon.app import FLOW
from salon.diary import Diary
from tests.script import open_call

AGENTS = FLOW.nodes

PHONE = '+34600111222'


books = Diary()
"""This module's salon, emptied by each case that writes to it."""


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


async def probe(question: str) -> str:
    """A tool that does nothing, for tests that need a tool call to be about something else.

    Every test here that wanted "the model calls a harmless tool" used to reach
    for `look_up_salon_info`. That tool is gone -- the salon's documents are in
    the prompts now -- and borrowing a real tool for its harmlessness was always
    the wrong dependency: the test breaks when the agency changes its mind about
    a tool, not when the thing under test breaks.

    Injected through `agent.override(toolsets=...)`, so it is offered exactly
    like a real tool and the agent's own `prepare_tools` capabilities narrow it
    the same way.
    """
    return f'nothing useful about {question}'


def scripted(**by_agent):
    """`probe` alongside the agent's real tools, never instead of them: `override(toolsets=...)` adds
    to what the agent was built with.
    """
    stack = ExitStack()
    for name, fn in by_agent.items():
        stack.enter_context(
            AGENTS[name].agent.override(
                model=FunctionModel(stream_function=fn),
                toolsets=[FunctionToolset([probe])],  # ty: ignore[invalid-argument-type]
            )
        )
    return stack


def says(text, chunk=8):
    async def stream(messages, info: AgentInfo):
        for start in range(0, len(text), chunk):
            yield text[start : start + chunk]

    return stream


def hands_to(name, because='loop'):
    async def stream(messages, info: AgentInfo):
        if not any(t.name == 'hand_over' for t in info.output_tools):
            yield 'Sorry, what was that?'
            return
        yield {0: DeltaToolCall(name='hand_over', json_args=json.dumps({'to': name, 'because': because}))}

    return stream


def _refusals(call) -> list[str]:
    return [
        str(part.content)
        for message in call.messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, RetryPromptPart)
    ]


async def exhaustion_makes_no_further_request():
    """Alternating handoffs until the budget is gone.

    Exactly the runs it paid for, then a line this code speaks. The caller is
    never left in silence, and the model is never asked again.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.update(customer_status='existing', verified_phone=PHONE)
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
    books.reset()
    call = open_call(PHONE, diary=books)
    call.update(customer_status='existing', verified_phone=PHONE)
    before = call.state
    probe = Probe(call, budget=TurnBudget(step_runs=2))

    with scripted(concierge=hands_to('complaints'), complaints=hands_to('concierge')):
        await probe._turn('hello?')

    assert EXHAUSTED in probe.fixed()
    assert call.state.facts == before.facts and not call.evidence


async def the_budget_is_not_refilled_by_exhausting_it():
    """A second turn gets its own allowance; the first one's is simply gone.

    Which is the difference between a per-turn budget and a per-run one: the
    point is that the *turn* stops, not that each run gets a fresh start.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.update(customer_status='existing', verified_phone=PHONE)
    probe = Probe(call, budget=TurnBudget(step_runs=2))

    with scripted(concierge=hands_to('complaints'), complaints=hands_to('concierge')):
        await probe._turn('hello?')
    first = probe.runs()

    probe.frames.clear()
    with scripted(concierge=says('We are open until six.'), complaints=says('We are open until six.')):
        await probe._turn('when do you close?')

    assert first == 2, first
    assert probe.runs() == 1, 'the new turn started from a full allowance'
    assert probe.spoken() == 'We are open until six.'


async def model_requests_are_counted_across_agents():
    """Four agents in one turn spend one allowance between them.

    Counted per run, a turn could hand the caller round the houses and each
    agent would start again from zero.

    Read off the turn's declared outcome, not a private tally: the count the core reports is the one
    a runtime and a trace both see.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.update(customer_status='existing', verified_phone=PHONE)
    probe = Probe(call, budget=TurnBudget(step_runs=4))

    with scripted(concierge=hands_to('complaints'), complaints=hands_to('concierge')):
        await probe._turn('hello?')

    assert probe._finished.requests == 4, probe._finished
    assert probe.runs() == 4
    assert probe._finished.status == 'exhausted', probe._finished


async def the_request_limit_stops_a_turn_that_never_hops():
    """One agent, many requests, no handoffs at all.

    The hop budget does not see this: every request is inside one run. The
    shared request limit is what bounds it, and it is a separate number for
    exactly that reason.

    A tool name nobody defined, because `speak_by` now takes the ordinary
    tools away at the third request and a model asking for a real one would
    be stopped by that instead. An unknown name is not a contrived case: it
    is what a provider hallucinating a tool looks like, it is answered with a
    retry prompt rather than a tool, and the request limit is the only thing
    that ends it.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.update(customer_status='existing', verified_phone=PHONE)
    probe = Probe(call, budget=TurnBudget(step_runs=4, model_requests=3, repair_retries=99))
    asked = []

    async def never_settles(messages, info: AgentInfo):
        asked.append(1)
        yield {0: DeltaToolCall(name='no_such_tool', json_args=json.dumps({'q': len(asked)}))}

    with scripted(concierge=never_settles):
        await probe._turn('tell me everything')

    assert len(asked) == 3, asked
    assert EXHAUSTED in probe.fixed(), probe.fixed()


async def the_tool_limit_is_its_own_number():
    """Two tools per reply, so the allowance goes before the request cap can matter.

    `speak_by` bounds requests, not calls: one reply may ask for several
    tools and a run that does it twice has spent four. Which is the whole
    reason `tool_calls` is a separate number.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.update(customer_status='existing', verified_phone=PHONE)
    probe = Probe(call, budget=TurnBudget(step_runs=4, model_requests=20, tool_calls=2))
    ran = []

    async def keep_looking(messages, info: AgentInfo):
        ran.append(1)
        yield {
            0: DeltaToolCall(name='probe', json_args=json.dumps({'question': f'a{len(ran)}'})),
            1: DeltaToolCall(name='probe', json_args=json.dumps({'question': f'b{len(ran)}'})),
        }

    with scripted(concierge=keep_looking):
        await probe._turn('tell me everything')

    assert len(ran) <= 2, ran
    assert EXHAUSTED in probe.fixed(), probe.fixed()


async def a_slow_tool_hits_the_turn_deadline():
    """The deadline is over the turn, not over one request.

    A tool that never returns used to hold the line open for as long as it
    liked, because nothing above it was counting.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.update(customer_status='existing', verified_phone=PHONE)
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


async def a_request_that_sends_nothing_is_tried_again():
    """The concierge's request sends nothing within `first_part`: it is cancelled, and the run is tried once more."""
    books.reset()
    call = open_call(PHONE, diary=books)
    call.update(customer_status='existing', verified_phone=PHONE)
    probe = Probe(call, budget=TurnBudget(first_part=0.2))
    tries: list[int] = []

    async def stalls_once(messages, info: AgentInfo):
        tries.append(1)
        if len(tries) == 1:
            await asyncio.sleep(10)
        yield 'We open at nine.'

    started = asyncio.get_running_loop().time()
    with scripted(concierge=stalls_once):
        await probe._turn('When do you open?')
    took = asyncio.get_running_loop().time() - started
    assert len(tries) == 2 and took < 1, (tries, took)
    assert EXHAUSTED not in probe.fixed() and call.spoken[-1].text == 'We open at nine.', call.spoken


async def a_permitted_repair_succeeds():
    """A tool refuses, the model corrects itself, the turn completes.

    Repair is the thing the budget must not forbid: this is the case where
    the second attempt is the right one. The verify agent first records a
    number one digit short; the state refuses it, saying why, and the second
    attempt is the right number. That reply only recorded the number, so code
    reads it back (`reads_back`): no third request.
    """
    books.reset()
    call = open_call('', diary=books)
    call.commit(agent='verify')
    probe = Probe(call, budget=TurnBudget(repair_retries=2))
    attempts = []

    async def wrong_then_right(messages, info: AgentInfo):
        attempts.append(1)
        if len(attempts) > 2:
            yield 'Let me read that back.'
            return
        phone = '+348340464' if len(attempts) == 1 else PHONE
        yield {0: DeltaToolCall(name='collect', json_args=json.dumps({'values': {'customer_phone': phone}}))}

    with scripted(verify=wrong_then_right):
        await probe._turn('my number is plus three four, six hundred, one one one, two two two')

    assert len(attempts) == 2, attempts
    assert call.spoken[-1].text.startswith('Is your number +34 600'), call.spoken
    assert any('too few' in r for r in _refusals(call)), 'the model was told what was wrong'
    assert call.state.facts.customer_phone == PHONE, call.state.facts
    assert EXHAUSTED not in probe.fixed(), 'a legitimate repair must not exhaust the turn'


async def repair_retries_are_bounded():
    """A model that never gets it right is stopped, and stopped politely."""
    books.reset()
    call = open_call('', diary=books)
    call.commit(agent='verify')
    probe = Probe(call, budget=TurnBudget(step_runs=1, repair_retries=1))
    attempts = []

    async def always_wrong(messages, info: AgentInfo):
        attempts.append(1)
        yield {
            0: DeltaToolCall(
                name='collect', json_args=json.dumps({'values': {'customer_phone': '+348340464'}})
            )
        }

    with scripted(verify=always_wrong):
        await probe._turn('my number is eight three four oh four six four')

    assert len(attempts) <= 3, attempts
    assert call.state.facts.customer_phone == '' and call.state.facts.verified_phone == '', (
        'nothing was written'
    )
    assert probe.fixed(), 'the caller hears something rather than silence'


async def a_superseded_turn_cannot_move_the_caller():
    """A newer caller turn started while this one was still unwinding.

    Where the caller *is* belongs to the turn they are actually in. A late
    result is about a question they have moved on from, so the move is
    fenced -- while whatever the tool actually did stands, to be reconciled
    by its own operation token rather than pretended away.

    The tool still ran and its effect stands: the diary was read for that day, and pretending
    otherwise would lose real work.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.update(customer_status='existing', verified_phone=PHONE)
    probe = Probe(call)

    read = []

    async def reads_diary_then_is_superseded(messages, info: AgentInfo):
        """The run answers, so the turn ends on a move rather than on an exhausted budget -- which is
        the path the fence sits in.

        The caller says something new while the tool is running: a newer turn is accepted, and this
        one is now stale.
        """
        if read:
            yield 'Nine or eleven tomorrow.'
            return
        read.append(1)
        call.begin_turn()
        yield {
            0: DeltaToolCall(
                name='collect',
                json_args=json.dumps({'values': {'booking_day': 'tomorrow', 'service': 'haircut'}}),
            )
        }

    with scripted(concierge=reads_diary_then_is_superseded):
        await probe._turn('anything tomorrow?')

    assert read, 'the tool ran'
    assert call.state.agent == 'concierge', f'a superseded turn moved the caller to {call.state.agent}'
    assert call.state.facts.booking_day == 'tomorrow', 'the work the tool did is not pretended away'


async def main():
    await a_superseded_turn_cannot_move_the_caller()
    await exhaustion_makes_no_further_request()
    await the_fallback_does_no_business_work()
    await the_budget_is_not_refilled_by_exhausting_it()
    await model_requests_are_counted_across_agents()
    await the_request_limit_stops_a_turn_that_never_hops()
    await the_tool_limit_is_its_own_number()
    await a_slow_tool_hits_the_turn_deadline()
    await a_request_that_sends_nothing_is_tried_again()
    await a_permitted_repair_succeeds()
    await repair_retries_are_bounded()
    print('ok')


if __name__ == '__main__':
    asyncio.run(main())
