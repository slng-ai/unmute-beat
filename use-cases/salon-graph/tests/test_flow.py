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

`tests/__main__.py` runs each module with `run_name='__main__'`, so this still runs there -- and a
debugging session can import one module without running every check in it.
"""

import asyncio
import json
from contextlib import ExitStack

from pipecat.frames.frames import LLMFullResponseStartFrame, LLMTextFrame, TTSSpeakFrame
from pydantic_ai import ModelResponse, RetryPromptPart, ToolCallPart
from pydantic_ai.messages import ModelRequest
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from core.graph import moved_by
from core.processor import EXHAUSTED, PydanticAgentProcessor
from core.turn import take_turn
from salon.agents import AGENTS
from salon.diary import Diary
from salon.models.state import SalonCall, SalonState, open_call

PHONE = '+34600111222'
VERIFIED = PHONE
"""The same number, named for what it means where a state is built by hand: a verification status with
no identity attached is cleared on construction.
"""


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


async def a_shut_guard_is_not_offered():
    """The borrowed tool moves with its edge."""
    unverified = await _offered(AGENTS['concierge'], SalonState())
    verified = await _offered(
        AGENTS['concierge'],
        SalonState(customer_phone=VERIFIED, customer_verified='existing', verified_phone=VERIFIED),
    )
    assert unverified['requests'][0]['to'] == ['booking', 'complaints'], unverified
    assert verified['requests'][0]['to'] == ['booking', 'complaints', 'verify'], verified
    assert 'read_diary' not in unverified['requests'][0]['tools']
    assert 'read_diary' in verified['requests'][0]['tools']


async def a_step_with_no_open_exit_keeps_its_own_name():
    """`verify` before anyone is identified has nowhere to hand over, and still asks.

    `hand_over` is dropped entirely rather than offered with an empty enum,
    and the finishers that are left keep the names that describe them.
    Pydantic AI renames a lone output function `final_result`, which would
    leave the model one tool and no word for what it does, so `build` names
    every one of them explicitly.

    `ask_number` beside them: putting the readback to the caller ends the run the same way finishing
    the step does, so it is an output and not a tool.
    """
    offered = await _offered(AGENTS['verify'], SalonState(agent='verify'))
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

    `verify` is open as an exit
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    probe = Probe(call)

    def hands_to_verify(messages, info: AgentInfo):
        async def stream(messages, info):
            """Close the edge behind the model's back, exactly as a tool running earlier in the same
            reply would have.
            """
            if any(
                isinstance(p, RetryPromptPart)
                for m in messages
                if isinstance(m, ModelRequest)
                for p in m.parts
            ):
                yield 'Sorry, what was that?'
                return
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
    books.reset()
    call = open_call(PHONE, diary=books)
    seen: list[dict] = []

    async def identify_then_answer(messages, info: AgentInfo):
        """Something in this run changes the state the guards read."""
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


async def a_second_action_is_refused_before_it_runs():
    """Two actions in one reply is the model proposing two futures.

    The winner is chosen from the complete response before anything executes,
    by the order the model emitted the calls -- never by iterating a set and
    taking whatever comes out first. The loser is refused on its first line,
    so its service is never called.

    `read_diary` first, `hand_over` second. The refusal of an *output* tool is
    the half the SDK cannot do: `before_tool_execute` never fires for one
    (checked against 2.46.0), so `hand_over` refuses itself on entry.

    The first by emission order did its work.

    The second wrote nothing and moved nobody.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
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
    assert call.state.booking_day, 'the claim holder ran'
    assert call.state.agent != 'complaints', call.state.agent


async def an_ordinary_tool_is_refused_when_it_is_the_second():
    """The other half: the loser is a function tool, refused by the hook.

    `hand_over` takes the claim, `read_diary` is refused before the diary is
    touched -- which is the point. Rejecting a transition after its service
    already ran is what the whole policy exists to avoid.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
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
        books.reset()
        call = open_call(PHONE, diary=books)
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


async def a_failed_run_is_retained_once():
    """A run that fails after a handoff keeps what it added, once, and before the turn is over.

    When a run fails, the SDK can report the tail of the history it was given
    as new, and the engine used to abandon the failed stream to the garbage
    collector: the handoff landed in the history a second time, at some later
    and arbitrary moment. The next model request would have seen it twice.
    """
    import gc

    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE)

    async def hands(messages, info: AgentInfo):
        yield {0: DeltaToolCall(name='hand_over', json_args='{"to": "complaints", "because": "a refund"}')}

    with scripted(concierge=hands, complaints=_says('Words where only a tool is allowed.')):
        _, finished = await take_turn(call, AGENTS, 'I want a refund')
    assert finished.status == 'failed', finished
    held = len(call.messages)
    assert _calls(call) == ['hand_over'], _calls(call)
    gc.collect()
    await asyncio.sleep(0)
    assert _calls(call) == ['hand_over'] and len(call.messages) == held, (
        'the failed run was retained again, later'
    )


async def a_split_destination_name_never_moves_anybody():
    """`complaints` is split after `co`, which is not a destination.

    The nastier form is a prefix that *is* one: the enum here holds `booking`
    and `complaints`, and a stream cut mid-name must not act on the prefix. No
    tool runs until the arguments validate whole.

    One action, from the complete arguments, and it is the whole name.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    probe = Probe(call)

    async def dribble(messages, info: AgentInfo):
        """The destination arrives in pieces, as a real provider streams it."""
        yield {0: DeltaToolCall(name='hand_over', json_args='{"to": "co')}
        yield {0: DeltaToolCall(json_args='mplaints", "because": "a refund"}')}

    with scripted(concierge=dribble, complaints=_says('Tell me what happened.')):
        await probe._turn('I want a refund')

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


async def text_before_a_tool_streams_and_the_tool_still_runs():
    """The model says something, then calls a tool in the same reply.

    Under pydantic-ai's `early` end strategy the text would finish the run and
    the tool would never execute. `graceful` is the v2 default; asserted here
    rather than assumed, because it decides whether a caller hears a sentence
    and then gets nothing done.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
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


async def a_run_speaks_by_its_third_request():
    """The bound in front of the turn budget: the third request has no tools left.

    Two tool-bearing requests is the longest legitimate chain a step here
    needs. From the third the model is offered speech only, so a run that
    would otherwise keep calling tools has to say something instead -- which
    is what the caller is waiting for.

    Its **output** tools are untouched: a run that has read the diary can
    still hand over or save. Only what buys another request goes.

    The run can still finish itself; it just cannot keep going.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
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

    The diary read is the free edge, and it still moved them.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
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
    assert call.state.agent == 'booking', call.state.agent
    assert '9:00 AM' in probe_run.spoken(), probe_run.spoken()
    assert EXHAUSTED not in probe_run.fixed(), probe_run.fixed()


def every_tool_that_writes_is_an_action():
    """A tool that commits state must be in `ACTIONS`, or the conflict policy skips it.

    Source inspection rather than a registry, because the registry is the
    thing being checked: a tool added tomorrow is covered without anybody
    remembering this test exists. Every module the salon's tools and steps
    live in is scanned.

    A model-callable tool is a public function whose first parameter is
    `ctx`. Anything else that commits is an executor or a helper, and is
    exempt only while every path to it starts at an action, at `advance`, or
    at a step's opening, the three ways the turn engine runs one:
    otherwise a helper becomes the way to write to state without a claim.
    Transitively, because `_unknown` commits and is reached through
    `_answered`, which is not a tool either.
    """
    import ast
    import pathlib

    from salon import agents
    from salon.tools.policy import ACTIONS

    root = pathlib.Path(agents.__file__).parent
    functions: dict[str, ast.AST] = {}
    operations: dict[str, list[str]] = {}
    """An `Operation` subclass's methods, by qualified name: they are reached through its `run`."""
    instances: dict[str, str] = {}
    """A module-level operation instance, and its class, so `LOOKUP.run(...)` counts as calling its methods."""
    for path in [
        root / 'agents.py',
        root / 'nodes.py',
        *sorted((root / 'tools').glob('*.py')),
        *sorted((root / 'steps').glob('*.py')),
    ]:
        tree = ast.parse(path.read_text())
        methods = set()
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and any(
                'Operation' in ast.unparse(base) for base in node.bases
            ):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                        functions[f'{node.name}.{item.name}'] = item
                        operations.setdefault(node.name, []).append(f'{node.name}.{item.name}')
                        methods.add(id(item))
            elif (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id in operations
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        instances[target.id] = node.value.func.id
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and id(node) not in methods:
                functions.setdefault(node.name, node)

    def commits(node: ast.AST) -> bool:
        """A direct commit in this function's own body. A nested function is scanned as itself, not as its factory."""
        body = list(ast.iter_child_nodes(node))
        while body:
            inner = body.pop()
            if isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == 'commit'
            ):
                return True
            body.extend(ast.iter_child_nodes(inner))
        return False

    writes = {name for name, node in functions.items() if commits(node)}
    assert writes, 'found no tools that commit, so this check is not checking anything'

    tools = {
        name
        for name, node in functions.items()
        if not name.startswith('_') and node.args.args and node.args.args[0].arg == 'ctx'
    }
    called_by: dict[str, set[str]] = {}
    for name, node in functions.items():
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            callee = getattr(inner.func, 'id', None)
            if callee in functions:
                called_by.setdefault(callee, set()).add(name)
            owner = getattr(getattr(inner.func, 'value', None), 'id', None)
            if isinstance(inner.func, ast.Attribute) and inner.func.attr == 'run' and owner in instances:
                for method in operations[instances[owner]]:
                    called_by.setdefault(method, set()).add(name)
    openings = {o.run.__name__ for node in agents.AGENTS.values() for o in node.openings}
    terminal = tools | {'advance'} | openings

    def reaches(helper: str, seen: set[str]) -> set[str]:
        out: set[str] = set()
        for caller in called_by.get(helper, set()):
            if caller in seen:
                continue
            seen.add(caller)
            out |= {caller} if caller in terminal else reaches(caller, seen)
        return out

    for helper in writes - tools:
        callers = reaches(helper, {helper})
        assert callers and callers <= ACTIONS | {'advance'} | openings, (
            f'{helper} writes to state and is reachable from {sorted(callers) or "nowhere"}, '
            'which is not an action that can claim the write'
        )

    missing = (writes & tools) - ACTIONS
    assert not missing, f'these write to state and are not in ACTIONS: {sorted(missing)}'
    assert any('.' in name for name in writes), (
        'no Operation method was scanned, so the class path is unchecked'
    )


def a_refused_tool_does_not_move_the_caller():
    """`moved_by` is given what *succeeded*, and re-derives everything else.

    The processor used to collect `FunctionToolCallEvent`, which fires when
    the model asks for a tool -- before it runs, and whatever it then does. A
    `read_diary` that refused an unidentified caller still moved them into
    `booking`.

    Nothing succeeded, so nobody moved.

    A tool that is not an edge is not a move however well it went.
    """
    known = SalonState(customer_phone=VERIFIED, customer_verified='existing', verified_phone=VERIFIED)
    assert moved_by(known, AGENTS['concierge'], AGENTS, {'read_diary'}) == 'booking'
    assert moved_by(known, AGENTS['concierge'], AGENTS, set()) == 'concierge'
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
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    probe = Probe(call)

    async def read_then_cancel(messages, info: AgentInfo):
        """The caller speaks over the tool"""
        probe.interrupt()
        yield {
            0: DeltaToolCall(
                name='read_diary', json_args=json.dumps({'when': 'tomorrow', 'service': 'haircut'})
            )
        }

    with scripted(concierge=read_then_cancel):
        await probe._turn('anything tomorrow?')

    assert call.state.agent == 'concierge', 'a cancelled run moved the caller'


def no_salon_class_overrides_a_final_method():
    """No salon `Operation` or `Interpreter` overrides a `@final` method of its core base.

    Core's own copy of this check (`core/tests/test_classes.py`) cannot load the
    salon, so this is where the salon's subclasses are held to it.
    """
    import inspect

    from core.interpreters import Interpreter
    from core.operations import Operation

    def subclasses(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from subclasses(sub)

    for base in (Operation, Interpreter):
        final = {name for name, member in vars(base).items() if getattr(member, '__final__', False)}
        assert final, base
        found = [sub for sub in subclasses(base) if sub.__module__.startswith('salon.')]
        assert found, base
        for sub in found:
            overridden = final & set(vars(sub))
            assert not overridden, f'{sub.__name__} overrides {overridden}'
            assert not inspect.isabstract(sub), sub


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
    await a_failed_run_is_retained_once()
    await a_split_destination_name_never_moves_anybody()
    await a_name_that_prefixes_another_is_not_acted_on_early()
    await text_before_a_tool_streams_and_the_tool_still_runs()
    await a_run_speaks_by_its_third_request()
    await the_loop_from_the_call_of_2026_09_21()
    every_tool_that_writes_is_an_action()
    a_refused_tool_does_not_move_the_caller()
    a_borrowed_tool_cannot_skip_a_prerequisite()
    await a_cancelled_tool_is_not_a_move()
    no_salon_class_overrides_a_final_method()
    print('ok')


if __name__ == '__main__':
    asyncio.run(main())
