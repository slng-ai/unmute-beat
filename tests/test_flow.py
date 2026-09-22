"""Run: uv run --env-file .env python -m tests.test_flow

Flow enforced where it actually has to hold: at execution, against the state
as it is at that moment, not in the schema that was built a request ago.

A narrowed schema is a convenience for the model. It is not enforcement, for
three reasons this module pins one at a time:

* a run makes several model requests, and a tool in the first can close an
  edge the second is still being offered;
* a provider can ignore an enum;
* a tool that was *called* is not a tool that *succeeded*.

Scripted models, no audio, no network.

Asserts, no pytest, no fixtures, same as the rest.
"""

import asyncio
import json
from contextlib import ExitStack

from pipecat.frames.frames import LLMFullResponseStartFrame, LLMTextFrame, TTSSpeakFrame
from pydantic_ai import ModelResponse, RetryPromptPart, ToolCallPart
from pydantic_ai.messages import ModelRequest
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from salon import diary
from salon.agents import AGENTS
from salon.state import SalonCall, SalonState, open_call
from voice.harness import moved_by
from voice.processor import EXHAUSTED, PydanticAgentProcessor

PHONE = '+34600111222'
# The same number, named for what it means where a state is built by hand:
# a verification status with no identity attached is cleared on construction.
VERIFIED = PHONE


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
    stack = ExitStack()
    for name, fn in by_agent.items():
        stack.enter_context(
            AGENTS[name].agent.override(
                model=FunctionModel(stream_function=fn),
                # `probe` alongside the agent's real tools, never instead of
                # them: `override(toolsets=...)` adds to what the agent was
                # built with.
                toolsets=[FunctionToolset([probe])],  # ty: ignore[invalid-argument-type]
            )
        )
    return stack


def _retries(call) -> list[str]:
    """Every refusal the model was handed back this turn."""
    return [
        part.content if isinstance(part.content, str) else str(part.content)
        for message in call.messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, RetryPromptPart)
    ]


def _calls(call) -> list[str]:
    return [
        part.tool_name
        for message in call.messages
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart)
    ]


async def _offered(node, state) -> dict[str, list]:
    """The tools and destinations really sent to the model, per request.

    Read off what the model receives, never recomputed from the table: a test
    that reimplements the filter passes when the filter is not wired up, which
    is exactly the failure that shipped -- the output-tool hook was matching a
    name pydantic-ai had already renamed, so every destination was offered
    whatever the guards said, and nothing noticed.
    """
    seen: dict[str, list] = {'requests': []}

    async def probe(messages, info: AgentInfo):
        to = next(
            (
                t.parameters_json_schema.get('properties', {}).get('to')
                for t in info.output_tools
                if t.name == 'hand_over'
            ),
            None,
        )
        seen['requests'].append(
            {
                'tools': sorted(t.name for t in info.function_tools),
                'outputs': sorted(t.name for t in info.output_tools),
                'to': (to or {}).get('enum'),
            }
        )
        if info.allow_text_output:
            yield 'ok'
        else:
            yield {0: DeltaToolCall(name='verification_guidance', json_args='{"topic":"number"}')}

    with node.agent.override(model=FunctionModel(stream_function=probe)):
        async with node.agent.run_stream_events(
            'hello', deps=SalonCall(state=state), output_type=node.outputs(state)
        ) as events:
            async for _ in events:
                pass
    return seen


# --- S04: a guard that closes must close the schema and the execution -------


async def a_shut_guard_is_not_offered():
    unverified = await _offered(AGENTS['concierge'], SalonState())
    verified = await _offered(
        AGENTS['concierge'],
        SalonState(customer_phone=VERIFIED, customer_verified='existing', verified_phone=VERIFIED),
    )
    assert unverified['requests'][0]['to'] == ['booking', 'complaints'], unverified
    assert verified['requests'][0]['to'] == ['booking', 'complaints', 'verify'], verified
    # The borrowed tool moves with its edge.
    assert 'read_diary' not in unverified['requests'][0]['tools']
    assert 'read_diary' in verified['requests'][0]['tools']


async def a_step_with_no_open_exit_keeps_its_own_name():
    """`verify` before anyone is identified has nowhere to hand over, and still asks.

    `hand_over` is dropped entirely rather than offered with an empty enum,
    and the finishers that are left keep the names that describe them.
    Pydantic AI renames a lone output function `final_result`, which would
    leave the model one tool and no word for what it does, so `build` names
    every one of them explicitly.
    """
    offered = await _offered(AGENTS['verify'], SalonState(agent='verify'))
    # `ask_number` beside them: putting the readback to the caller ends the run
    # the same way finishing the step does, so it is an output and not a tool.
    assert offered['requests'][0]['outputs'] == [
        'ask_number',
        'confirm_customer',
        'stop_verifying',
        'verification_guidance',
    ], offered
    assert offered['requests'][0]['to'] is None


async def a_stale_destination_is_refused_at_execution():
    """The model sends a destination whose guard has since closed.

    Straight at the tool, past the schema, which is what a provider ignoring
    an enum amounts to. It must not move the caller.
    """
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing', verified_phone=PHONE)  # `verify` is open as an exit
    probe = Probe(call)

    def hands_to_verify(messages, info: AgentInfo):
        async def stream(messages, info):
            if any(
                isinstance(p, RetryPromptPart)
                for m in messages
                if isinstance(m, ModelRequest)
                for p in m.parts
            ):
                yield 'Sorry, what was that?'
                return
            # Close the edge behind the model's back, exactly as a tool
            # running earlier in the same reply would have.
            call.commit(customer_verified='')
            yield {
                0: DeltaToolCall(name='hand_over', json_args=json.dumps({'to': 'verify', 'because': 'stale'}))
            }

        return stream(messages, info)

    with scripted(concierge=hands_to_verify):
        await probe._turn('hello?')

    assert any('not somewhere this caller can go' in r for r in _retries(call)), _retries(call)
    assert call.state.agent == 'concierge', 'the closed edge did not move anybody'


async def the_schema_is_rebuilt_between_requests_in_one_run():
    """Not once per turn. A run makes several requests and state moves under it.

    The first request calls a tool that identifies the caller; the second
    request in the *same run* must already see `read_diary` and the `verify`
    destination. Narrowed once per turn, it would not until the next turn.
    """
    diary.reset()
    call = open_call(PHONE)
    seen: list[dict] = []

    async def identify_then_answer(messages, info: AgentInfo):
        to = next(
            (
                t.parameters_json_schema.get('properties', {}).get('to')
                for t in info.output_tools
                if t.name == 'hand_over'
            ),
            None,
        )
        seen.append({'tools': sorted(t.name for t in info.function_tools), 'to': (to or {}).get('enum')})
        if len(seen) == 1:
            # Something in this run changes the state the guards read.
            call.commit(customer_verified='existing', verified_phone=PHONE)
            yield {0: DeltaToolCall(name='probe', json_args=json.dumps({'question': 'opening hours'}))}
        else:
            yield 'We are open until six.'

    probe = Probe(call)
    with scripted(concierge=identify_then_answer):
        await probe._turn('when do you close?')

    assert len(seen) == 2, seen
    assert seen[0]['to'] == ['booking', 'complaints'], seen[0]
    assert seen[1]['to'] == ['booking', 'complaints', 'verify'], 'the second request saw the new state'
    assert 'read_diary' not in seen[0]['tools'] and 'read_diary' in seen[1]['tools'], seen


# --- S05: one flow-changing action per reply, refused before the service ----


async def a_second_action_is_refused_before_it_runs():
    """Two actions in one reply is the model proposing two futures.

    The winner is chosen from the complete response before anything executes,
    by the order the model emitted the calls -- never by iterating a set and
    taking whatever comes out first. The loser is refused on its first line,
    so its service is never called.

    `read_diary` first, `hand_over` second. The refusal of an *output* tool is
    the half the SDK cannot do: `before_tool_execute` never fires for one
    (checked against 2.46.0), so `hand_over` refuses itself on entry.
    """
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    probe = Probe(call)

    async def read_and_leave(messages, info: AgentInfo):
        if any(
            isinstance(p, RetryPromptPart) for m in messages if isinstance(m, ModelRequest) for p in m.parts
        ):
            yield 'Sorry, one thing at a time.'
            return
        yield {
            0: DeltaToolCall(
                name='read_diary', json_args=json.dumps({'when': 'tomorrow', 'service': 'haircut'})
            ),
            1: DeltaToolCall(
                name='hand_over', json_args=json.dumps({'to': 'complaints', 'because': 'also this'})
            ),
        }

    with scripted(concierge=read_and_leave, booking=_says('Anything else?')):
        await probe._turn('anything tomorrow, and I want to complain')

    refusals = [r for r in _retries(call) if 'one of these may run per reply' in r]
    assert refusals, _retries(call)
    # The first by emission order did its work.
    assert call.state.booking_day, 'the claim holder ran'
    # The second wrote nothing and moved nobody.
    assert call.state.agent != 'complaints', call.state.agent


async def an_ordinary_tool_is_refused_when_it_is_the_second():
    """The other half: the loser is a function tool, refused by the hook.

    `hand_over` takes the claim, `read_diary` is refused before the diary is
    touched -- which is the point. Rejecting a transition after its service
    already ran is what the whole policy exists to avoid.
    """
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    probe = Probe(call)
    before = call.state.booking_day

    async def leave_and_read(messages, info: AgentInfo):
        if any(
            isinstance(p, RetryPromptPart) for m in messages if isinstance(m, ModelRequest) for p in m.parts
        ):
            yield 'Sorry, one thing at a time.'
            return
        yield {
            0: DeltaToolCall(
                name='hand_over', json_args=json.dumps({'to': 'complaints', 'because': 'first'})
            ),
            1: DeltaToolCall(
                name='read_diary', json_args=json.dumps({'when': 'tomorrow', 'service': 'haircut'})
            ),
        }

    with scripted(concierge=leave_and_read, complaints=_says('Tell me what happened.')):
        await probe._turn('I want to complain, and what is free tomorrow')

    assert any('one of these may run per reply' in r for r in _retries(call)), _retries(call)
    assert call.state.booking_day == before, 'the refused tool never reached the diary'


async def the_winner_is_the_same_every_time():
    """Emission order, so two runs of one reply reach one answer.

    A set would not: iteration order is not arbitration, and the contract
    forbids picking a winner that way.
    """
    outcomes = set()
    for _ in range(5):
        diary.reset()
        call = open_call(PHONE)
        call.commit(customer_verified='existing', verified_phone=PHONE)
        probe = Probe(call)

        async def both(messages, info: AgentInfo):
            if any(
                isinstance(p, RetryPromptPart)
                for m in messages
                if isinstance(m, ModelRequest)
                for p in m.parts
            ):
                yield 'ok'
                return
            yield {
                0: DeltaToolCall(
                    name='read_diary', json_args=json.dumps({'when': 'tomorrow', 'service': 'haircut'})
                ),
                1: DeltaToolCall(
                    name='hand_over', json_args=json.dumps({'to': 'complaints', 'because': 'x'})
                ),
            }

        with scripted(concierge=both, booking=_says('Anything else?')):
            await probe._turn('hello?')
        outcomes.add((call.state.agent, bool(call.state.booking_day)))
    assert len(outcomes) == 1, outcomes


# --- S06: a partial stream never acts ---------------------------------------


async def a_split_destination_name_never_moves_anybody():
    """`complaints` is split after `co`, which is not a destination.

    The nastier form is a prefix that *is* one: the enum here holds `booking`
    and `complaints`, and a stream cut mid-name must not act on the prefix. No
    tool runs until the arguments validate whole.
    """
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    probe = Probe(call)

    async def dribble(messages, info: AgentInfo):
        # The destination arrives in pieces, as a real provider streams it.
        yield {0: DeltaToolCall(name='hand_over', json_args='{"to": "co')}
        yield {0: DeltaToolCall(json_args='mplaints", "because": "a refund"}')}

    with scripted(concierge=dribble, complaints=_says('Tell me what happened.')):
        await probe._turn('I want a refund')

    # One action, from the complete arguments, and it is the whole name.
    assert _calls(call) == ['hand_over'], _calls(call)
    assert call.state.agent == 'complaints', call.state.agent


async def a_name_that_prefixes_another_is_not_acted_on_early():
    """A stream cut exactly where the prefix is itself a legal value.

    Built on a throwaway enum rather than the salon's, which has no such pair:
    the claim is about partial arguments, and it should be provable without
    waiting for two step names where one prefixes the other.
    """
    from pydantic_ai import Agent
    from pydantic_ai.output import ToolOutput

    acted: list[str] = []

    async def go(to: str) -> str:
        acted.append(to)
        return to

    async def split(messages, info: AgentInfo):
        yield {0: DeltaToolCall(name='go', json_args='{"to": "book')}
        assert not acted, 'acted on a prefix before the arguments were complete'
        yield {0: DeltaToolCall(json_args='ings"}')}

    agent = Agent(FunctionModel(stream_function=split), output_type=[ToolOutput(go, name='go')])
    async with agent.run_stream_events('x') as events:
        async for _ in events:
            pass
    assert acted == ['bookings'], acted


# --- S07: speech before a tool must not end the run --------------------------


async def text_before_a_tool_streams_and_the_tool_still_runs():
    """The model says something, then calls a tool in the same reply.

    Under pydantic-ai's `early` end strategy the text would finish the run and
    the tool would never execute. `graceful` is the v2 default; asserted here
    rather than assumed, because it decides whether a caller hears a sentence
    and then gets nothing done.
    """
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    probe = Probe(call)

    async def talk_then_call(messages, info: AgentInfo):
        already = any(
            isinstance(p, ToolCallPart) and p.tool_name == 'probe'
            for m in messages
            if isinstance(m, ModelResponse)
            for p in m.parts
        )
        if already:
            yield 'We close at six.'
            return
        yield 'Let me see. '
        yield {0: DeltaToolCall(name='probe', json_args=json.dumps({'question': 'opening hours'}))}

    with scripted(concierge=talk_then_call):
        await probe._turn('when do you close?')

    assert 'probe' in _calls(call), _calls(call)
    assert probe.spoken().startswith('Let me see.'), probe.spoken()
    assert 'We close at six.' in probe.spoken(), probe.spoken()


# --- a run answers before it runs out of budget -----------------------------


async def a_run_speaks_by_its_third_request():
    """The bound in front of the turn budget: the third request has no tools left.

    Two tool-bearing requests is the longest legitimate chain a step here
    needs. From the third the model is offered speech only, so a run that
    would otherwise keep calling tools has to say something instead -- which
    is what the caller is waiting for.

    Its **output** tools are untouched: a run that has read the diary can
    still hand over or save. Only what buys another request goes.
    """
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    probe_run = Probe(call)
    offered = []

    async def keep_asking(messages, info: AgentInfo):
        offered.append(
            {
                'tools': sorted(t.name for t in info.function_tools),
                'outputs': sorted(t.name for t in info.output_tools),
            }
        )
        if any(t.name == 'probe' for t in info.function_tools):
            yield {0: DeltaToolCall(name='probe', json_args=json.dumps({'question': f'q{len(offered)}'}))}
        else:
            yield 'We are open until six.'

    with scripted(concierge=keep_asking):
        await probe_run._turn('tell me everything')

    assert len(offered) == 3, offered
    assert offered[0]['tools'] and offered[1]['tools'], offered
    assert offered[2]['tools'] == [], offered[2]
    # The run can still finish itself; it just cannot keep going.
    assert 'hand_over' in offered[2]['outputs'], offered[2]
    assert probe_run.spoken() == 'We are open until six.', probe_run.spoken()
    assert EXHAUSTED not in probe_run.fixed(), probe_run.fixed()


async def the_loop_from_the_call_of_2026_09_21():
    """The trace, replayed. A caller moving a booking, and a model that keeps looking things up.

    On that call the model read the diary -- which returned everything it
    needed -- and then asked the knowledge base seven consecutive questions,
    rewording after the first came back empty. The turn's whole request budget
    went, and the caller heard "I'm going round in circles" 5.3 seconds later.

    What this pins is not the deleted tool: it is that *any* tool loop now
    ends in an answer. The diary read still moves the caller, and what it
    returned still reaches their ears.
    """
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    probe_run = Probe(call)
    requests = []

    async def read_then_fish(messages, info: AgentInfo):
        requests.append(1)
        if len(requests) == 1:
            yield {
                0: DeltaToolCall(
                    name='read_diary', json_args=json.dumps({'when': 'Saturday', 'service': 'haircut'})
                )
            }
        elif any(t.name == 'probe' for t in info.function_tools):
            yield {0: DeltaToolCall(name='probe', json_args=json.dumps({'question': 'cancellation policy'}))}
        else:
            yield 'I can do 9:00 AM on Saturday. Does that work?'

    with scripted(concierge=read_then_fish):
        await probe_run._turn("let's move it to Saturday, same time")

    assert len(requests) == 3, requests
    # The diary read is the free edge, and it still moved them.
    assert call.state.agent == 'booking', call.state.agent
    assert '9:00 AM' in probe_run.spoken(), probe_run.spoken()
    assert EXHAUSTED not in probe_run.fixed(), probe_run.fixed()


# --- a tool that did not succeed is not a move -------------------------------


def every_tool_that_writes_is_an_action():
    """A tool that commits state must be in `ACTIONS`, or the policy skips it.

    The since-deleted `get_a_manager` was not, for exactly the reason this
    check exists: it read like a lookup and was not one -- it wrote a state
    field. Nothing caught that, because the conflict tests only ever
    exercised the tools somebody had remembered to list.

    Source inspection rather than a registry, because the registry is the
    thing being checked. A tool added tomorrow is covered without anybody
    remembering this test exists.
    """
    import ast
    import inspect
    import pathlib

    from salon import agents

    source = pathlib.Path(inspect.getfile(agents)).read_text()
    writes = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            call = getattr(inner, 'func', None)
            if isinstance(inner, ast.Call) and isinstance(call, ast.Attribute) and call.attr == 'commit':
                writes.add(node.name)

    assert writes, 'found no tools that commit, so this check is not checking anything'

    # A private helper is not a tool. The model cannot call `_settle`, and the
    # claim for what it writes is made by the action that calls it -- so it is
    # exempt, but only while every one of its callers is itself an action.
    # Otherwise a leading underscore becomes the way to write to state
    # without a claim.
    called_by: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            name = getattr(getattr(inner, 'func', None), 'id', None)
            if isinstance(inner, ast.Call) and name and name.startswith('_'):
                called_by.setdefault(name, set()).add(node.name)

    def reaches(helper: str, seen: set[str]) -> set[str]:
        """Every named tool that can reach this helper, through however many private hops.

        Transitive, because one hop is not the rule. `_unknown` commits and is
        called by `_answered`, which is private too: a check that stopped at
        the first caller reported a private helper as the claimant and would
        have kept reporting one however long the chain got.
        """
        out: set[str] = set()
        for caller in called_by.get(helper, set()):
            if caller in seen:
                continue
            seen.add(caller)
            out |= reaches(caller, seen) if caller.startswith('_') else {caller}
        return out

    helpers = {name for name in writes if name.startswith('_')}
    for helper in helpers:
        callers = reaches(helper, {helper})
        assert callers and callers <= agents.ACTIONS | {'advance'}, (
            f'{helper} writes to state and is reachable from {sorted(callers) or "nowhere"}, '
            'which is not an action that can claim the write'
        )

    missing = (writes - helpers) - agents.ACTIONS
    assert not missing, f'these write to state and are not in ACTIONS: {sorted(missing)}'


def a_refused_tool_does_not_move_the_caller():
    """`moved_by` is given what *succeeded*, and re-derives everything else.

    The processor used to collect `FunctionToolCallEvent`, which fires when
    the model asks for a tool -- before it runs, and whatever it then does. A
    `read_diary` that refused an unidentified caller still moved them into
    `booking`.
    """
    known = SalonState(customer_phone=VERIFIED, customer_verified='existing', verified_phone=VERIFIED)
    assert moved_by(known, AGENTS['concierge'], AGENTS, {'read_diary'}) == 'booking'
    # Nothing succeeded, so nobody moved.
    assert moved_by(known, AGENTS['concierge'], AGENTS, set()) == 'concierge'
    # A tool that is not an edge is not a move however well it went.
    assert moved_by(known, AGENTS['concierge'], AGENTS, {'probe'}) == 'concierge'


def a_borrowed_tool_cannot_skip_a_prerequisite():
    """The back door into a step whose prerequisites are unmet.

    `booking` requires `customer_verified`, and the lending edge is guarded on
    the same fact -- so in this agency the two agree. The check is still made
    separately, because an agency where they disagree is one where a borrowed
    tool walks a caller past the interposition `route` exists to do.
    """
    fresh = SalonState()
    assert moved_by(fresh, AGENTS['concierge'], AGENTS, {'read_diary'}) == 'concierge'


async def a_cancelled_tool_is_not_a_move():
    """Barge-in during a borrowed tool leaves the caller where they were."""
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    probe = Probe(call)

    async def read_then_cancel(messages, info: AgentInfo):
        probe.interrupt()  # the caller speaks over the tool
        yield {
            0: DeltaToolCall(
                name='read_diary', json_args=json.dumps({'when': 'tomorrow', 'service': 'haircut'})
            )
        }

    with scripted(concierge=read_then_cancel):
        await probe._turn('anything tomorrow?')

    assert call.state.agent == 'concierge', 'a cancelled run moved the caller'


def _says(text, chunk=8):
    async def stream(messages, info: AgentInfo):
        for start in range(0, len(text), chunk):
            yield text[start : start + chunk]

    return stream


async def main():
    await a_shut_guard_is_not_offered()
    await a_step_with_no_open_exit_keeps_its_own_name()
    await a_stale_destination_is_refused_at_execution()
    await the_schema_is_rebuilt_between_requests_in_one_run()
    await a_second_action_is_refused_before_it_runs()
    await an_ordinary_tool_is_refused_when_it_is_the_second()
    await the_winner_is_the_same_every_time()
    await a_split_destination_name_never_moves_anybody()
    await a_name_that_prefixes_another_is_not_acted_on_early()
    await text_before_a_tool_streams_and_the_tool_still_runs()
    await a_run_speaks_by_its_third_request()
    await the_loop_from_the_call_of_2026_09_21()
    every_tool_that_writes_is_an_action()
    a_refused_tool_does_not_move_the_caller()
    a_borrowed_tool_cannot_skip_a_prerequisite()
    await a_cancelled_tool_is_not_a_move()
    print('ok')


if __name__ == '__main__':
    # `tests/__main__.py` runs each module with `run_name='__main__'`, so this
    # still runs there -- and a debugging session can import one module
    # without running every check in it.
    asyncio.run(main())
