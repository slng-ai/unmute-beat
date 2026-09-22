"""Run: uv run --env-file .env python -m tests.test_core

The extraction, checked from both sides. `core.turn` runs a caller turn and
knows nothing about how it reaches anybody; `core.processor` turns its events
into Pipecat frames. The claim Phase 02 rests on is that those are two views
of one behaviour, so the same scenarios run both ways here and are compared
outcome by outcome.

Also the place the runtime-neutrality claim is actually enforced, rather than
asserted in a docstring: the core's imports are read and checked.

Asserts, no pytest, no fixtures, same as the rest.

`tests/__main__.py` runs each module with `run_name='__main__'`, so this still runs there -- and a
debugging session can import one module without running every check in it.
"""

import asyncio
import contextlib
import json
import pathlib
from contextlib import ExitStack
from dataclasses import replace

from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pydantic_ai import RunContext
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from core import turn as core
from core.processor import PydanticAgentProcessor
from core.state import turn_generation
from core.turn import EXHAUSTED, UNBACKED, TurnBudget, take_turn
from salon.agents import AGENTS
from salon.diary import Diary
from salon.models.state import Appointment, SalonCall, open_call

PHONE = '+34600111222'


books = Diary()
"""This module's salon, emptied by each case that writes to it."""


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


async def speaking_probe(ctx: RunContext[SalonCall], line: str) -> str:
    """A tool that says a line of its own while it runs, which is what `record_complaint` does.

    Needed because the interleaving only exists when something other than the
    model speaks in the middle of a run. Borrowing a real tool for it would
    tie this check to the agency's mind about that tool.
    """
    await ctx.deps.speak_now(line)
    return 'said'


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
    """`probe` alongside the agent's real tools, never instead of them: `override(toolsets=...)` adds
    to what the agent was built with.
    """
    stack = ExitStack()
    for name, fn in by_agent.items():
        stack.enter_context(
            AGENTS[name].agent.override(
                model=FunctionModel(stream_function=fn),
                toolsets=[FunctionToolset([probe, slow_probe, speaking_probe])],  # ty: ignore[invalid-argument-type]
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
"""The scenarios both sides are driven with. One entry per shape of turn the extraction could plausibly
break: a plain answer, a handoff, a borrowed tool that moves the caller, and a turn that runs out of
budget.
"""


def _fresh(verified: bool):
    books.reset()
    call = open_call(PHONE, diary=books)
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
        direct_call = _fresh(case['verified'])  # ty: ignore[invalid-argument-type]
        with scripted(**case['scripts']()):
            said, finished = await take_turn(direct_call, AGENTS, case['said'], case['budget'])  # ty: ignore[invalid-argument-type]

        framed_call = _fresh(case['verified'])  # ty: ignore[invalid-argument-type]
        probe = Probe(framed_call, budget=case['budget'])
        with scripted(**case['scripts']()):
            await probe._turn(case['said'])  # ty: ignore[invalid-argument-type]

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


async def the_ledger_keeps_the_order_the_caller_heard_it_in():
    """Model text, a fixed line from a tool, then more model text -- in that order.

    Reproduced by the second recheck. Model speech was accumulated for the
    whole run and appended once in `finally`, while a fixed line was appended
    the moment it went out: the caller heard `Before. Fixed middle. After.`
    and the ledger read `fixed: Fixed middle.` then `model: Before. After.`.
    Both model sentences ended up on one side of a line spoken between them,
    and no timestamp can put back a boundary that was never kept -- sorting
    two records cannot split one of them in half.

    Asserted against the frames rather than against an expected string alone:
    what the ledger says was said has to be what went to the wire, in order.
    """
    call = _fresh(True)
    probe = Probe(call)

    async def talk_speak_talk(messages, info: AgentInfo):
        if 'speaking_probe' in str(messages):
            yield 'After.'
            return
        yield 'Before. '
        yield {0: DeltaToolCall(name='speaking_probe', json_args=json.dumps({'line': 'Fixed middle. '}))}

    with scripted(concierge=talk_speak_talk):
        await probe._turn('when do you close?')

    assert probe.heard() == 'Before. Fixed middle. After.', probe.heard()
    assert [(s.kind, s.text) for s in call.spoken] == [
        ('model', 'Before. '),
        ('fixed', 'Fixed middle. '),
        ('model', 'After.'),
    ], call.spoken
    assert ''.join(s.text for s in call.spoken) == probe.heard(), call.spoken


MOVED = Appointment(
    booking_id='BK0001',
    service='haircut',
    date='2026-09-26',
    time='14:30',
    spoken='Saturday at 2:30 PM',
    action='move',
)


async def a_claim_the_salon_cannot_bear_out_is_corrected_out_loud():
    """The model says it cancelled something, and nothing was written.

    Reproduced off the live call of 2026-09-21 12:23:22 -- *"I've cancelled
    your appointment, and I have you down for a redo on Saturday at 2:30
    PM."* The last write in that call was a move, three turns earlier; no
    `save_booking` ran in that turn or after it. The appointment was still
    there, there was no redo, and the next turn promised the redo would be
    free.

    It cannot be unsaid: the words were at TTS before anything could know.
    What it can do is not leave the caller with them.
    """
    call = _fresh(True)
    call.commit(appointment=MOVED)
    probe = Probe(call)
    with scripted(
        concierge=says("I've cancelled your appointment, and I have you down for a redo on Saturday.")
    ):
        await probe._turn('can you cancel it and do the redo instead?')

    assert UNBACKED in probe.heard(), probe.heard()
    kinds = [(s.kind, s.text) for s in call.spoken]
    assert kinds[-1] == ('fixed', UNBACKED), kinds
    assert call.state.appointment == MOVED, 'and nothing was invented on the state either'


async def recounting_a_booking_already_made_is_not_a_claim():
    """The guard has to let ordinary speech through, which is most speech.

    An agent answering "what have I got" says the same words in the same
    tense. What separates them is whether the salon agrees: a booking on the
    state is the corroboration, and only a *cancellation* nobody recorded,
    or a booking claimed by a call that holds none, trips it.

    And a cancellation the salon *did* record reads as true.
    """
    held = _fresh(True)
    held.commit(appointment=MOVED)
    probe = Probe(held)
    with scripted(concierge=says("I've booked you in for Saturday at 2:30 PM.")):
        await probe._turn('what have I got?')
    assert UNBACKED not in probe.heard(), probe.heard()

    gone = _fresh(True)
    gone.commit(appointment=MOVED.model_copy(update={'action': 'cancel'}))
    probe = Probe(gone)
    with scripted(concierge=says("I've cancelled that for you.")):
        await probe._turn('cancel it please')
    assert UNBACKED not in probe.heard(), probe.heard()


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


def tool_call(name, **args):
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

    A turn that speaks nothing of its own: it spends its budget calling a tool and the caller hears
    only the fixed line the core writes.
    """
    call = _fresh(True)
    with scripted(concierge=says('We are open until six.')):
        await take_turn(call, AGENTS, 'when do you close?')
    answered = len(call.messages)

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

    The tool slept between them, and neither request paid for it.
    """
    call = _fresh(True)
    script = then(calls('slow_probe', question='anything'), says('Nothing to report.'))
    with scripted(concierge=script):
        _, finished = await take_turn(call, AGENTS, 'anything tomorrow?')

    assert finished.requests == 2, finished
    assert len(call.requests) == 2, call.requests
    assert [r.kind for r in call.requests] == ['tool', 'text'], call.requests
    assert all(r.ttft is not None for r in call.requests)
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

    a runtime, so the filler has a line to go out on
    """
    call = _fresh(True)
    probe = Probe(call)
    script = then(calls('speaking_probe', line='Checking.'), says('Nine is free.'))
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
    engine = pathlib.Path(__file__).parent.parent / 'core'
    for module in ('state', 'graph', 'policies', 'history', 'turn'):
        source = (engine / f'{module}.py').read_text()
        for banned in ('pipecat', 'livekit', 'SlngSTT', 'SlngTTS', 'TTSSpeakFrame', 'LLMTextFrame'):
            assert banned not in source, f'{banned!r} reached the runtime-neutral core ({module}.py)'


def the_processor_no_longer_runs_the_turn():
    """The loop moved; it did not get copied.

    Two turn loops that drift apart is worse than one in the wrong place.
    """
    source = (pathlib.Path(__file__).parent.parent / 'core' / 'processor.py').read_text()
    for moved in ('run_stream_events', 'RunCancelled', 'moved_by', 'route('):
        assert moved not in source, f'{moved!r} is still in the processor'


async def a_turn_that_never_finished_says_so():
    """A turn cancelled from outside still records how it ended.

    An absent outcome is not a good one. The live call of 2026-09-21 12:57 was
    scored `answered` on a turn where the agency hung up on a caller
    mid-question, and a benchmark that reads a missing row as a pass will
    score a dropped transport the same way.

    And the transport dropping is told apart from the caller ringing off.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    probe_it = Probe(call)

    async def never_returns(messages, info: AgentInfo):
        await asyncio.sleep(10)
        yield 'too late'

    with scripted(concierge=never_returns):
        turn = asyncio.create_task(probe_it._turn('hello?'))
        await asyncio.sleep(0.05)
        turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn

    assert probe_it._finished is not None, 'the turn ended with no outcome at all'
    assert probe_it._finished.status in ('failed', 'interrupted'), probe_it._finished
    assert 'never finished' in probe_it._finished.detail, probe_it._finished
    assert call.ended == 'transport', call.ended


async def a_late_commit_cannot_move_a_caller_who_moved_on():
    """A tool that outlives its turn keeps what it did and loses where it was going.

    Two different things, and the old fence only covered one of them: `_hops`
    checked the generation before its own two transitions, and every other
    write went through unexamined. A write that really happened is a fact
    whichever turn made it, and refusing to record it leaves the business
    holding something the call has no memory of -- so completed effects land
    and the routing that was going to follow them does not.

    The caller speaks again, and the old turn's tool is still running.

    A commit with nothing but flow in it changes nothing at all.

    And the current turn writes freely.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    first = call.begin_turn()
    turn_generation.set((call.id, first))
    call.commit(agent='concierge')

    call.begin_turn()

    call.commit(agent='booking', customer_verified='existing', verified_phone=PHONE)
    assert call.state.agent == 'concierge', 'the move was fenced'
    assert call.state.customer_verified == 'existing', 'the fact it established was not'

    call.commit(agent='complaints')
    assert call.state.agent == 'concierge', call.state.agent

    turn_generation.set((call.id, call._generation))
    call.commit(agent='booking')
    assert call.state.agent == 'booking'
    turn_generation.set(None)


async def a_reset_step_is_sent_none_of_the_conversation():
    """C3's two history modes, and the difference between them.

    `messages` is the delivered conversation so far. `reset` is a step that
    gets its own instructions and the values projected into them, and nothing
    the caller said to anybody else -- so what it does not need, it cannot
    leak. Both are declared on the step, not decided per request.

    An ordinary step: the conversation so far arrives with it.

    The same step, declared `reset`.

    Its own question, and nothing anybody said to anybody else.

    What it is told instead is projected into its own instructions.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    seen: dict = {}

    async def remember(messages, info: AgentInfo):
        seen.setdefault('sent', []).append(list(messages))
        async for chunk in says('Right.')(messages, info):
            yield chunk

    probe_it = Probe(call)
    with scripted(concierge=remember):
        await probe_it._turn('hello there')
        await probe_it._turn('and my last cut was uneven')
    assert len(seen['sent'][1]) > len(seen['sent'][0]), 'the second turn saw the first'

    seen['sent'] = []
    quiet = replace(AGENTS['concierge'], history='reset')
    table = AGENTS | {'concierge': quiet}
    reset_probe = Probe(call)
    reset_probe._agents = table
    with scripted(concierge=remember):
        await reset_probe._turn('and about that colour')
    only = seen['sent'][0]
    assert len(only) == 1, [type(m).__name__ for m in only]
    assert 'and about that colour' in str(only[0].parts[0].content), only[0].parts
    assert 'and my last cut was uneven' not in str(only), 'the earlier turns leaked into a reset step'
    assert 'and about that colour' in (only[0].instructions or '')


async def the_transcript_is_the_call_and_not_the_protocol():
    """Two different records, and merging them is how a turn reads as silent.

    `messages` is the model and tool protocol that executed. The transcript is
    the conversation the caller had: their own words and every segment that
    went out, fixed lines included -- and those never enter the model's
    history at all, so a record built from it reports a turn that said three
    things as a turn that said one.

    The ledger itself is still this turn's alone.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    probe_it = Probe(call)
    with scripted(concierge=says('Hello, how can I help?')):
        await probe_it._turn('hi there')
    with scripted(concierge=says('Of course.')):
        await probe_it._turn('can I book something')

    said = call.transcript()
    assert [line['said'] for line in said] == ['caller', 'model', 'caller', 'model'], said
    assert said[0]['text'] == 'hi there' and said[1]['text'] == 'Hello, how can I help?'
    assert said[2]['text'] == 'can I book something', said
    assert len(call.spoken) == 1, call.spoken


async def every_segment_says_where_it_came_from():
    """A tracked readback carries its origin and segment id into the TTS frame."""
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(agent='verify')
    probe_it = Probe(call)

    with scripted(verify=calls('ask_number', phone=PHONE)):
        await probe_it._turn('my number is ' + PHONE)

    assert len(call.spoken) == 1, call.spoken
    readback = call.spoken[0]
    assert (readback.origin, readback.kind) == ('ask', 'fixed'), readback
    assert f':{call.turn}:' in readback.id, readback
    spoken_frames = [f for f in probe_it.frames if isinstance(f, TTSSpeakFrame)]
    assert len(spoken_frames) == 1, probe_it.kinds()
    carried = spoken_frames[0].metadata.get('speech') or {}
    assert carried.get('segment') == readback.id, carried
    assert carried.get('origin') == 'ask' and carried.get('submitted_at'), carried


async def the_processor_reports_no_metrics_of_its_own():
    """Its timings go on its span. Pipecat's metrics belong to the services.

    The installed TTS tracing decorator takes the first `TTFBMetricsData`
    that arrives under the playing audio context without checking which
    processor emitted it, so this processor's time to first token was
    recorded as the TTS service's time to first audio. Eight of the 47 TTS
    spans on the live calls of 2026-09-21 reported a TTFB longer than the
    span's own duration -- N1's goodbye, 0.233s long against 1.678s to first
    byte, which is the enclosing agent attempt's number.

    And nothing is lost: the numbers are on the span, per request.
    """
    from pipecat.frames.frames import MetricsFrame

    books.reset()
    call = open_call(PHONE, diary=books)
    probe_it = Probe(call)
    assert probe_it.can_generate_metrics() is False
    with scripted(concierge=says('Hello there.')):
        await probe_it._turn('hi')
    assert not [f for f in probe_it.frames if isinstance(f, MetricsFrame)], probe_it.kinds()
    assert [r.ttft for r in call.requests if r.ttft is not None], call.requests


async def main():
    await the_same_scenarios_both_ways()
    await the_processor_still_speaks_pipecat()
    await a_fixed_line_is_still_a_tts_frame()
    await speech_before_a_tool_survives_the_seam()
    await every_response_frame_is_closed()
    await the_turn_says_how_it_ended()
    await a_barge_in_is_reported_as_one()
    await a_turn_that_never_finished_says_so()
    await a_late_commit_cannot_move_a_caller_who_moved_on()
    await a_reset_step_is_sent_none_of_the_conversation()
    await the_transcript_is_the_call_and_not_the_protocol()
    await every_segment_says_where_it_came_from()
    await the_processor_reports_no_metrics_of_its_own()
    await a_barge_in_after_a_tool_still_moves_nobody()
    await an_interrupted_fixed_line_leaves_an_earlier_answer_alone()
    await time_to_first_token_is_timed_per_request()
    await a_new_turn_starts_a_new_clock()
    await the_ledger_holds_both_halves_of_what_was_said()
    await the_ledger_keeps_the_order_the_caller_heard_it_in()
    await a_claim_the_salon_cannot_bear_out_is_corrected_out_loud()
    await recounting_a_booking_already_made_is_not_a_claim()
    await the_cores_own_fixed_lines_are_in_the_ledger_too()
    await the_ledger_is_cut_down_to_what_was_heard()
    await a_segment_is_timed_from_when_it_started_going_out()
    the_core_imports_no_runtime()
    the_processor_no_longer_runs_the_turn()
    print('ok')


if __name__ == '__main__':
    asyncio.run(main())
