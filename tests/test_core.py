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


def scripted(**by_agent):
    stack = ExitStack()
    for name, fn in by_agent.items():
        stack.enter_context(AGENTS[name].agent.override(model=FunctionModel(stream_function=fn)))
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
        call.commit(customer_verified='existing')
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
        if 'look_up_salon_info' in str(messages):
            yield 'We close at six.'
            return
        yield 'Let me see. '
        yield {0: DeltaToolCall(name='look_up_salon_info', json_args=json.dumps({'query': 'opening hours'}))}

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
        yield {0: DeltaToolCall(name='look_up_salon_info', json_args=json.dumps({'query': f'q{len(calls)}'}))}

    return stream


def _stalls():
    async def stream(messages, info: AgentInfo):
        await asyncio.sleep(5)
        yield 'too late'

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
    the_core_imports_no_runtime()
    the_processor_no_longer_runs_the_turn()
    print('ok')


asyncio.run(main())
