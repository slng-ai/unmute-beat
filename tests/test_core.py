"""Run: uv run --env-file .env python -m tests.test_core

The extraction, checked from both sides. `voice.core` runs a caller turn and
knows nothing about how it reaches anybody; `voice.processor` turns its events
into Pipecat frames. The claim Phase 02 rests on is that those are two views
of one behaviour, so the same scenarios run both ways here and are compared
outcome by outcome.

Also the place the runtime-neutrality claim is actually enforced, rather than
asserted in a docstring: the core's imports are read and checked.

Asserts, no pytest, no fixtures, same as the rest.
"""

import asyncio
import json
import pathlib
from contextlib import ExitStack

from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from salon import diary
from salon.agents import AGENTS
from salon.state import open_call
from voice import core
from voice.core import EXHAUSTED, TurnBudget, take_turn
from voice.processor import PydanticAgentProcessor

PHONE = '+34600111222'


class Probe(PydanticAgentProcessor):
    def __init__(self, call, **kwargs):
        super().__init__(call, AGENTS, **kwargs)
        self.frames = []

    async def push_frame(self, frame, direction=None):
        self.frames.append(frame)

    def heard(self) -> str:
        """Everything that reached the caller, model speech and fixed lines alike."""
        return ''.join(f.text for f in self.frames if isinstance(f, (LLMTextFrame, TTSSpeakFrame)))

    def runs(self) -> int:
        return sum(isinstance(f, LLMFullResponseStartFrame) for f in self.frames)

    def kinds(self) -> list[str]:
        return [type(f).__name__ for f in self.frames]


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


SLOW = 0.05


async def slow_probe(question: str) -> str:
    """A tool that takes long enough to tell two clocks apart.

    `request_ttft` used to be timed from the start of the *run*, so on any
    turn with a tool in it the number reported as time to first token
    contained the tool's whole round trip. Fifty milliseconds is nothing to a
    caller and plenty to a comparison: it is the difference between measuring
    the provider and measuring the provider plus whatever the agency did
    while waiting for it.
    """
    await asyncio.sleep(SLOW)
    return f'slowly, nothing useful about {question}'


def scripted(**by_agent):
    stack = ExitStack()
    for name, fn in by_agent.items():
        stack.enter_context(
            AGENTS[name].agent.override(
                model=FunctionModel(stream_function=fn),
                # `probe` alongside the agent's real tools, never instead of
                # them: `override(toolsets=...)` adds to what the agent was
                # built with.
                toolsets=[FunctionToolset([probe, slow_probe])],
            )
        )
    return stack


def says(text, chunk=8):
    async def stream(messages, info: AgentInfo):
        for start in range(0, len(text), chunk):
            yield text[start : start + chunk]

    return stream


def hands_to(name, because='test'):
    async def stream(messages, info: AgentInfo):
        if not any(t.name == 'hand_over' for t in info.output_tools):
            yield 'Sorry, what was that?'
            return
        yield {0: DeltaToolCall(name='hand_over', json_args=json.dumps({'to': name, 'because': because}))}

    return stream


def reads_then_answers(text):
    async def stream(messages, info: AgentInfo):
        if any(t.name == 'read_diary' for t in info.function_tools) and 'read' not in str(messages):
            yield {
                0: DeltaToolCall(
                    name='read_diary', json_args=json.dumps({'when': 'tomorrow', 'service': 'haircut'})
                )
            }
            return
        for start in range(0, len(text), 8):
            yield text[start : start + 8]

    return stream


# The scenarios both sides are driven with. One entry per shape of turn the
# extraction could plausibly break: a plain answer, a handoff, a borrowed tool
# that moves the caller, and a turn that runs out of budget.
SCENARIOS = {
    'plain answer': {
        'verified': True,
        'scripts': lambda: {'concierge': says('We are open until six.')},
        'said': 'when do you close?',
        'budget': TurnBudget(),
    },
    'handoff then answer': {
        'verified': True,
        'scripts': lambda: {
            'concierge': hands_to('complaints'),
            'complaints': says('Tell me what happened.'),
        },
        'said': 'I want to complain',
        'budget': TurnBudget(),
    },
    'borrowed tool moves the caller': {
        'verified': True,
        'scripts': lambda: {
            'concierge': reads_then_answers('Nine or eleven tomorrow.'),
            'booking': says('Nine or eleven tomorrow.'),
        },
        'said': 'anything tomorrow?',
        'budget': TurnBudget(),
    },
    'budget runs out': {
        'verified': True,
        'scripts': lambda: {'concierge': hands_to('complaints'), 'complaints': hands_to('concierge')},
        'said': 'hello?',
        'budget': TurnBudget(step_runs=2),
    },
}


def _fresh(verified: bool):
    diary.reset()
    call = open_call(PHONE)
    if verified:
        call.commit(customer_verified='existing', verified_phone=PHONE)
    return call


async def the_same_scenarios_both_ways():
    """Run each scenario against the core directly and through the processor.

    Compared on what a caller would notice -- the words, the step they end up
    in, how many model runs it took -- rather than on internals, because the
    two sides legitimately differ in everything else.
    """
    for name, case in SCENARIOS.items():
        direct_call = _fresh(case['verified'])
        with scripted(**case['scripts']()):
            said, finished = await take_turn(direct_call, AGENTS, case['said'], case['budget'])

        framed_call = _fresh(case['verified'])
        probe = Probe(framed_call, budget=case['budget'])
        with scripted(**case['scripts']()):
            await probe._turn(case['said'])

        assert said == probe.heard(), f'{name}: {said!r} != {probe.heard()!r}'
        assert finished.requests == probe._finished.requests, name
        assert finished.status == probe._finished.status, name
        assert finished.handoffs == probe._finished.handoffs, name
        assert direct_call.state.agent == framed_call.state.agent, name
        assert direct_call.state.model_dump() == framed_call.state.model_dump(), name


async def the_processor_still_speaks_pipecat():
    """Extraction must not change the frame contract the pipeline is built on.

    Start, text, end, in that order, with the fixed lines as `TTSSpeakFrame`
    -- which is what TTS, the aggregators and the interruption machinery all
    read.
    """
    call = _fresh(True)
    probe = Probe(call)
    with scripted(concierge=says('We are open until six.')):
        await probe._turn('when do you close?')

    kinds = probe.kinds()
    assert kinds[0] == 'LLMFullResponseStartFrame', kinds
    assert kinds[-1] == 'LLMFullResponseEndFrame', kinds
    assert 'LLMTextFrame' in kinds, kinds
    assert probe.heard() == 'We are open until six.'


async def a_fixed_line_is_still_a_tts_frame():
    call = _fresh(True)
    probe = Probe(call, budget=TurnBudget(step_runs=2))
    with scripted(concierge=hands_to('complaints'), complaints=hands_to('concierge')):
        await probe._turn('hello?')

    fixed = [f.text for f in probe.frames if isinstance(f, TTSSpeakFrame)]
    assert EXHAUSTED in fixed, fixed
    assert not any(isinstance(f, LLMTextFrame) for f in probe.frames), 'no model spoke'


async def speech_before_a_tool_survives_the_seam():
    """The ordering that makes a voice agent feel alive, checked after the move.

    Model text, then a tool, then more text -- and the words reach the wire in
    that order rather than being buffered until the run ends.
    """
    call = _fresh(True)
    probe = Probe(call)

    async def talk_then_call(messages, info: AgentInfo):
        if 'probe' in str(messages):
            yield 'We close at six.'
            return
        yield 'Let me see. '
        yield {0: DeltaToolCall(name='probe', json_args=json.dumps({'question': 'opening hours'}))}

    with scripted(concierge=talk_then_call):
        await probe._turn('when do you close?')

    text = [f.text for f in probe.frames if isinstance(f, LLMTextFrame)]
    assert text and text[0].startswith('Let me see'), text
    assert 'We close at six.' in ''.join(text), text


async def every_response_frame_is_closed():
    """Start and end always pair, on every way a turn can end.

    The core unwinds through an exception when a turn blows its request
    limit or hits the deadline, so the event that would have said the run
    ended is never yielded. An unclosed response wedges the aggregators, and
    pairing the frames belongs to the side that owns the frame contract.
    """
    cases = {
        'answered': (TurnBudget(), {'concierge': says('We are open until six.')}, 'when do you close?'),
        'exhausted by hops': (
            TurnBudget(step_runs=2),
            {'concierge': hands_to('complaints'), 'complaints': hands_to('concierge')},
            'hello?',
        ),
        'exhausted by requests': (
            TurnBudget(step_runs=4, model_requests=2),
            {'concierge': _never_settles()},
            'tell me everything',
        ),
        'deadline': (TurnBudget(seconds=0.25), {'concierge': _stalls()}, 'hello?'),
    }
    for name, (budget, scripts, said) in cases.items():
        call = _fresh(True)
        probe = Probe(call, budget=budget)
        with scripted(**scripts):
            await probe._turn(said)
        starts = sum(isinstance(f, LLMFullResponseStartFrame) for f in probe.frames)
        ends = sum(isinstance(f, LLMFullResponseEndFrame) for f in probe.frames)
        assert starts == ends, f'{name}: {starts} starts, {ends} ends -- {probe.kinds()}'
        assert starts >= 1, name


def _never_settles():
    calls = []

    async def stream(messages, info: AgentInfo):
        calls.append(1)
        yield {0: DeltaToolCall(name='probe', json_args=json.dumps({'question': f'q{len(calls)}'}))}

    return stream


def _stalls():
    async def stream(messages, info: AgentInfo):
        await asyncio.sleep(5)
        yield 'too late'

    return stream


def calls(name, **args):
    """One scripted tool call, whatever else the agent was offered."""

    async def stream(messages, info: AgentInfo):
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args))}

    return stream


def then(*steps):
    """A different scripted response for each request this agent receives."""
    seen = {'n': 0}

    async def stream(messages, info: AgentInfo):
        step = steps[min(seen['n'], len(steps) - 1)]
        seen['n'] += 1
        async for chunk in step(messages, info):
            yield chunk

    return stream


async def the_turn_says_how_it_ended():
    """Never implied. Four statuses, and an absent complaint is not success."""
    call = _fresh(True)
    with scripted(concierge=says('We are open until six.')):
        _, finished = await take_turn(call, AGENTS, 'when do you close?')
    assert finished.status == 'answered' and finished.requests == 1, finished

    call = _fresh(True)
    with scripted(concierge=hands_to('complaints'), complaints=hands_to('concierge')):
        _, finished = await take_turn(call, AGENTS, 'hello?', TurnBudget(step_runs=2))
    assert finished.status == 'exhausted', finished
    assert finished.detail, 'and it says what ran out'


async def a_barge_in_is_reported_as_one():
    call = _fresh(True)
    barge = core.Barge()

    async def interrupted(messages, info: AgentInfo):
        barge.fire()
        yield {
            0: DeltaToolCall(
                name='read_diary', json_args=json.dumps({'when': 'tomorrow', 'service': 'haircut'})
            )
        }

    with scripted(concierge=interrupted):
        _, finished = await take_turn(call, AGENTS, 'anything tomorrow?', barge=barge)

    assert finished.status == 'interrupted', finished
    assert call.state.agent == 'concierge', 'a cancelled run moved nobody'


async def a_barge_in_after_a_tool_still_moves_nobody():
    """The later window: the tool ran, and the caller talked over what came next.

    `read_diary` is borrowed by the front desk, and a *successful* borrowed
    tool is what takes the free edge into `booking`. That move was committed
    on a turn the caller had interrupted, so a run that came back
    `status='interrupted'` had silently moved them into a step whose answer
    they never heard. The existing barge test fires before the tool runs and
    cannot see it.

    The diary read itself stands: it wrote nothing, and the work that did
    happen is not pretended away.
    """
    call = _fresh(True)
    barge = core.Barge()

    async def cut_off(messages, info: AgentInfo):
        barge.fire()
        yield 'we have nine, ten thirty and'

    with scripted(concierge=then(calls('read_diary', when='tomorrow', service='haircut'), cut_off)):
        _, finished = await take_turn(call, AGENTS, 'anything tomorrow?', barge=barge)

    assert finished.status == 'interrupted', finished
    assert call.state.agent == 'concierge', 'an interrupted turn does not move the caller'
    assert call.state.booking_day, 'and the diary read it did do is not undone'


async def an_interrupted_fixed_line_leaves_an_earlier_answer_alone():
    """`note_spoken` cut down the newest answer with text in it, whoever said it.

    A turn whose only speech is written by this code -- a confirmation, a
    goodbye, the exhausted line -- has nothing in the history to cut down,
    because fixed speech never goes there. So the walk backwards found the
    *previous* turn's answer and replaced it with this turn's delivered
    prefix: an answer the caller heard in full, rewritten in the history as
    half a sentence they never heard at all.
    """
    call = _fresh(True)
    with scripted(concierge=says('We are open until six.')):
        await take_turn(call, AGENTS, 'when do you close?')
    answered = len(call.messages)

    # A turn that speaks nothing of its own: it spends its budget calling a
    # tool and the caller hears only the fixed line the core writes.
    with scripted(concierge=calls('probe', question='anything')):
        heard, finished = await take_turn(
            call, AGENTS, 'and parking?', TurnBudget(step_runs=4, model_requests=2)
        )
    assert finished.status == 'exhausted', finished
    assert EXHAUSTED in heard and 'until six' not in heard

    core.note_spoken(call, EXHAUSTED[:12], interrupted=True)
    said = [
        part.content
        for message in call.messages[:answered]
        for part in getattr(message, 'parts', [])
        if getattr(part, 'content', None) == 'We are open until six.'
    ]
    assert said == ['We are open until six.'], 'the earlier answer was rewritten'


async def time_to_first_token_is_timed_per_request():
    """Two requests in one run, and the tool between them belongs to neither.

    Timed from the start of the run, the second request's time to first token
    contained the whole tool call: the number compared against the native
    arm's per-request TTFB was measuring something else entirely.
    """
    call = _fresh(True)
    script = then(calls('slow_probe', question='anything'), says('Nothing to report.'))
    with scripted(concierge=script):
        _, finished = await take_turn(call, AGENTS, 'anything tomorrow?')

    assert finished.requests == 2, finished
    assert len(call.requests) == 2, call.requests
    assert [r.kind for r in call.requests] == ['tool', 'text'], call.requests
    assert all(r.ttft is not None for r in call.requests)
    # The tool slept between them, and neither request paid for it.
    assert call.requests[1].ttft < SLOW, call.requests[1]
    assert call.requests[0].started + SLOW < call.requests[1].started, 'the tool really did run between'


async def a_new_turn_starts_a_new_clock():
    """The list is the turn's, not the call's, so turn five is not fifteen requests."""
    call = _fresh(True)
    with scripted(concierge=says('We are open until six.')):
        await take_turn(call, AGENTS, 'when do you close?')
        assert len(call.requests) == 1
        await take_turn(call, AGENTS, 'and on Sunday?')
    assert len(call.requests) == 1, call.requests


async def the_ledger_holds_both_halves_of_what_was_said():
    """A turn where the model answered read back as a turn that said nothing.

    Only fixed lines routed through `say` were recorded, so `call.spoken` was
    empty on an ordinary answer and the declared `kind='model'` was never
    written by anything. A transcript with the model's half missing cannot
    show what was offered, whether consent was asked for, or whether the
    answer was any use -- which is most of what a benchmark row is for.
    """
    call = _fresh(True)
    with scripted(concierge=says('We close at six.')):
        heard, finished = await take_turn(call, AGENTS, 'when do you close?')

    assert finished.status == 'answered'
    assert [(s.kind, s.text) for s in call.spoken] == [('model', 'We close at six.')], call.spoken
    assert 'We close at six.' in heard


async def the_cores_own_fixed_lines_are_in_the_ledger_too():
    """The stumble and the exhausted line reach the caller and bypassed the ledger.

    They are yielded as events rather than spoken through the call, so the
    one turn whose transcript matters most -- the one that went wrong -- had
    no transcript at all.
    """
    call = _fresh(True)
    with scripted(concierge=calls('probe', question='anything')):
        _, finished = await take_turn(call, AGENTS, 'and parking?', TurnBudget(step_runs=4, model_requests=2))
    assert finished.status == 'exhausted'
    assert [s.kind for s in call.spoken] == ['fixed'], call.spoken
    assert EXHAUSTED in call.spoken[0].text


async def the_ledger_is_cut_down_to_what_was_heard():
    """Generated text is not heard text, and the ledger stops claiming the rest."""
    call = _fresh(True)
    with scripted(concierge=says('We have nine, ten thirty and two.')):
        await take_turn(call, AGENTS, 'anything tomorrow?')

    core.note_spoken(call, 'We have nine,', interrupted=True)
    delivered = [s for s in call.spoken if s.kind == 'model'][-1]
    assert delivered.text == 'We have nine,' and delivered.interrupted, delivered


async def a_segment_is_timed_from_when_it_started_going_out():
    """Not from when the run finished assembling it.

    A streamed answer is sent as it arrives, so its clock is its first token.
    Timed at the end instead, every model segment would look later than the
    filler that was covering it, and "first text out" would name the wrong
    one.
    """
    call = _fresh(True)
    probe = Probe(call)  # a runtime, so the filler has a line to go out on
    script = then(calls('read_diary', when='tomorrow', service='haircut'), says('Nine is free.'))
    with scripted(concierge=script):
        await probe._turn('anything tomorrow?')

    kinds = [s.kind for s in call.spoken]
    assert kinds == ['fixed', 'model'], call.spoken
    filler, answer = call.spoken
    assert filler.at < answer.at, 'the filler covered the gap, so it went out first'


def the_core_imports_no_runtime():
    """The property the second adapter depends on, enforced rather than promised.

    A Pipecat import in here would compile and pass every behavioural test in
    this file, and Phase 02 would find it.
    """
    source = (pathlib.Path(__file__).parent.parent / 'voice' / 'core.py').read_text()
    for banned in ('pipecat', 'livekit', 'SlngSTT', 'SlngTTS', 'TTSSpeakFrame', 'LLMTextFrame'):
        assert banned not in source, f'{banned!r} reached the runtime-neutral core'


def the_processor_no_longer_runs_the_turn():
    """The loop moved; it did not get copied.

    Two turn loops that drift apart is worse than one in the wrong place.
    """
    source = (pathlib.Path(__file__).parent.parent / 'voice' / 'processor.py').read_text()
    for moved in ('run_stream_events', 'RunCancelled', 'moved_by', 'route('):
        assert moved not in source, f'{moved!r} is still in the processor'


async def main():
    await the_same_scenarios_both_ways()
    await the_processor_still_speaks_pipecat()
    await a_fixed_line_is_still_a_tts_frame()
    await speech_before_a_tool_survives_the_seam()
    await every_response_frame_is_closed()
    await the_turn_says_how_it_ended()
    await a_barge_in_is_reported_as_one()
    await a_barge_in_after_a_tool_still_moves_nobody()
    await an_interrupted_fixed_line_leaves_an_earlier_answer_alone()
    await time_to_first_token_is_timed_per_request()
    await a_new_turn_starts_a_new_clock()
    await the_ledger_holds_both_halves_of_what_was_said()
    await the_cores_own_fixed_lines_are_in_the_ledger_too()
    await the_ledger_is_cut_down_to_what_was_heard()
    await a_segment_is_timed_from_when_it_started_going_out()
    the_core_imports_no_runtime()
    the_processor_no_longer_runs_the_turn()
    print('ok')


asyncio.run(main())
