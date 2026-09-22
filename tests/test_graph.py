"""Run: uv run --env-file .env python -m tests.test_graph

The graph, proved without audio, without network and -- for everything but the
schema read -- without a model. `route` is a pure function over typed state and
a node table, so the only way it can be wrong is arithmetic, and arithmetic is
testable in a millisecond.

Asserts, no pytest, no fixtures, same as the rest.

`route` interposes one prerequisite and does not loop, which is only safe while no satisfier has
prerequisites of its own. Held here rather than by a loop in `route`: a graph that cannot chain
needs no cycle detection, and `pending` can never hold more than one step.

`tests/__main__.py` runs each module with `run_name='__main__'`, so this still runs there -- and a
debugging session can import one module without running every check in it.
"""

import asyncio
from contextlib import ExitStack

from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from core.graph import moved_by, route
from salon.agents import AGENTS
from salon.models.state import SalonCall, SalonState

STEPS = set(AGENTS)
VERIFIED = '+34600111222'
"""A number a lookup really ran against. A verification status with no identity attached is cleared by
`SalonState`'s own invariant, so a test that wants a verified caller has to say who.
"""
_ALL_TOOLS = {t.name for node in AGENTS.values() for t in node.agent._function_toolset.tools.values()}
"""Every tool any step holds, borrowed ones included. Read off the agents rather than listed here, so a
tool that stops existing fails the lending check.
"""


def _guidance(info):
    if any(tool.name == 'booking_guidance' for tool in info.output_tools):
        return {0: DeltaToolCall(name='booking_guidance', json_args='{"topic":"day"}')}
    if any(tool.name == 'verification_guidance' for tool in info.output_tools):
        return {0: DeltaToolCall(name='verification_guidance', json_args='{"topic":"number"}')}
    return {0: DeltaToolCall(name='complaint_guidance', json_args='{"topic":"details"}')}


async def _reachable_tools(node, state) -> set[str]:
    """The tool names this step really sends the model, in this state.

    Read off what the model is handed, not recomputed from the exits: a test
    that reimplements the filter passes when the filter is not wired up at all,
    which is the one failure that matters -- a borrowed tool left in an
    unguarded schema is called, refused, and retried, which costs the round trip
    the lending exists to save.
    """
    seen: set[str] = set()

    async def probe(messages, info: AgentInfo):
        seen.update(tool.name for tool in info.function_tools)
        if info.allow_text_output:
            yield 'What can I help you with?'
        else:
            yield _guidance(info)

    with node.agent.override(model=FunctionModel(stream_function=probe)):
        async with node.agent.run_stream_events(
            'hello', deps=SalonCall(state=state), output_type=node.outputs(state)
        ) as events:
            async for _ in events:
                pass
    return seen


for name, node in AGENTS.items():
    for exit in node.exits:
        assert exit.to in STEPS, f'{name} exits to {exit.to!r}, which is not a step'
        assert exit.to != name, f'{name} exits to itself; a hop to yourself says nothing'
    if node.satisfied_by:
        assert node.satisfied_by in STEPS, f'{name} is satisfied by {node.satisfied_by!r}, not a step'
        assert node.requires, f'{name} names a satisfier but requires nothing of it'
    if node.requires:
        assert node.satisfied_by, f'{name} requires {node.requires} and nothing is said to fill them'
        for field in node.requires:
            assert field in SalonState.model_fields, f'{name} requires {field!r}, not a state field'

reached, edge = {'concierge'}, True
"""Every step is somewhere a caller can actually end up."""
while edge:
    edge = False
    for name in list(reached):
        for to in [e.to for e in AGENTS[name].exits] + [AGENTS[name].satisfied_by]:
            if to and to not in reached:
                reached.add(to)
                edge = True
assert reached == STEPS, f'unreachable from the front desk: {STEPS - reached}'

for name, node in AGENTS.items():
    if node.satisfied_by:
        assert not AGENTS[node.satisfied_by].requires, (
            f'{node.satisfied_by} satisfies {name} and has prerequisites of its own; '
            'route interposes once, so this would leave the first one unmet'
        )


def prerequisite_interposed():
    """`route` decides and writes nothing: the step to enter and what is left waiting come back
    together, so its caller commits them as one.
    """
    state = SalonState()
    for destination in ('booking', 'complaints'):
        assert route(state, AGENTS, destination) == ('verify', destination), (
            'nobody is identified, so verify runs first'
        )
    assert state.pending is None, 'and route itself changed nothing'


def no_interpose_when_met():
    """And the model was never asked: the front desk said "booking" and the graph, not a request,
    worked out that verification comes first.
    """
    state = SalonState(customer_phone=VERIFIED, customer_verified='existing', verified_phone=VERIFIED)
    assert route(state, AGENTS, 'booking') == ('booking', None), 'a met prerequisite leaves nothing waiting'
    assert route(state, AGENTS, 'complaints') == ('complaints', None)
    assert route(SalonState(), AGENTS, 'concierge') == ('concierge', None)


def the_pending_slot_clears():
    """Nothing waiting: whoever finished says where the fallback is."""
    call = SalonCall(state=SalonState(pending='booking'))
    assert call.resume('concierge') == 'booking'
    assert call.state.pending is None, 'cleared, so the next finisher does not go there again'
    assert call.resume('concierge') == 'concierge'
    assert call.resume('complaints') == 'complaints'


def never_resumes_into_itself():
    """A push nobody popped must not become a hop to the agent you are already with.

    Found on the first benchmark run: `booking -> booking (the appointment was
    booked)`. A step is pushed on the way in and popped only by a finisher, so a
    caller who changes the subject leaves the push behind, and the next finisher
    to look found its own name.
    """
    call = SalonCall(state=SalonState(agent='booking', pending='booking'))
    assert call.resume('concierge') == 'concierge'
    assert call.state.pending is None, 'and the stale record is gone, not left for the next one'


def sent_to_a_satisfier_comes_back():
    """Handed straight to `verify` mid-complaint, they return to the complaint.

    Also found on a real run: `complaints -> verify -> concierge` left a caller
    who was part-way through a complaint at the front desk, with the whole thing
    to say again. The graph knows where they came from; nobody needed to ask.
    """
    call = SalonCall(state=SalonState(agent='complaints'))
    going_to, waiting = route(call.state, AGENTS, 'verify')
    assert (going_to, waiting) == ('verify', 'complaints')
    call.commit(
        agent=going_to,
        pending=waiting,
        customer_phone=VERIFIED,
        customer_verified='existing',
        verified_phone=VERIFIED,
    )
    assert call.resume('concierge') == 'complaints'


def one_waiting_step_at_a_time():
    """A second interposition replaces the first; it does not pile up.

    Now unrepresentable rather than merely untrue: `pending` is one optional
    step name, so there is no list for a second push to grow into.
    """
    call = SalonCall()
    for _ in range(2):
        going_to, waiting = route(call.state, AGENTS, 'booking')
        call.commit(agent=going_to, pending=waiting)
    assert call.state.pending == 'booking', call.state.pending
    assert SalonState.model_fields['pending'].annotation is not list


def round_trip():
    """The whole point, in five lines: out through verify and back into booking.

    What `confirm_customer` does, identity included: a status without the number it was established
    for is cleared by the state's own invariant.

    Two lines, and they have to be: `resume` commits, which replaces the state object, so reading
    `call.state` in the same expression would hand `route` the state from before the pending slot
    was cleared. Written as one line this test failed, which is the whole reason the context holds a
    replaceable state instead of tools holding a state directly.
    """
    call = SalonCall()
    going_to, waiting = route(call.state, AGENTS, 'booking')
    assert going_to == 'verify'
    call.commit(
        agent=going_to,
        pending=waiting,
        customer_phone=VERIFIED,
        customer_verified='existing',
        verified_phone=VERIFIED,
    )
    back_to = call.resume('concierge')
    assert route(call.state, AGENTS, back_to) == ('booking', None)
    assert call.state.pending is None


def no_model_request_on_kind_a():
    """The invariant. A graph move that costs a round trip is not a graph move."""

    async def explode(messages, info: AgentInfo):
        """unreachable, and here only to make this an async generator"""
        raise AssertionError('route ran a model; a deterministic hop must cost nothing')
        yield

    with ExitStack() as stack:
        for node in AGENTS.values():
            stack.enter_context(node.agent.override(model=FunctionModel(stream_function=explode)))
        state = SalonState()
        route(state, AGENTS, 'booking')
        route(state, AGENTS, 'complaints')
        SalonCall(state=SalonState(pending='booking')).resume('concierge')


async def offered_exits(node, state) -> tuple[str, ...]:
    """The `to` enum in this step's `hand_over`, read off the emitted tool schema.

    Off the schema and never off the source: the claim is about what the model
    receives, and a `Literal` built from a value is exactly the thing that could
    be right in the table and wrong on the wire.

    a finisher: it has no destination to narrow

    Pydantic writes a one-value `Literal` as `const` and a longer one as `enum`. Both are the same
    claim about what the model may send.
    """
    seen: dict = {}

    async def probe(messages, info: AgentInfo):
        seen['tools'] = [tool.parameters_json_schema for tool in info.output_tools]
        if info.allow_text_output:
            yield 'What can I help you with?'
        else:
            yield _guidance(info)

    with node.agent.override(model=FunctionModel(stream_function=probe)):
        async with node.agent.run_stream_events(
            'hello', deps=SalonCall(state=state), output_type=node.outputs(state)
        ) as events:
            async for _ in events:
                pass

    for schema in seen['tools']:
        to = schema.get('properties', {}).get('to')
        if to is None:
            continue
        return tuple(to['enum']) if 'enum' in to else (to['const'],)
    return ()


async def no_illegal_edge_offered():
    """`dict.fromkeys`, matching `build`: two exits can name one step -- one a `hand_over` the model
    may choose, one lending that step's entry tool -- and the model should be offered it once.

    Named, so the two guards are asserted and not merely computed twice.
    """
    for state in (
        SalonState(),
        SalonState(customer_phone=VERIFIED, customer_verified='existing', verified_phone=VERIFIED),
    ):
        for name, node in AGENTS.items():
            want = tuple(dict.fromkeys(exit.to for exit in node.exits if exit.guard(state)))
            got = await offered_exits(node, state)
            assert got == want, f'{name} with verified={state.customer_verified!r}: {got} != {want}'
            assert name not in got, f'{name} was offered a hop to itself'

    fresh, known = (
        SalonState(),
        SalonState(customer_phone=VERIFIED, customer_verified='existing', verified_phone=VERIFIED),
    )
    assert 'verify' not in await offered_exits(AGENTS['concierge'], fresh), (
        'before anyone is identified the graph interposes verify; offering it as well '
        'is a route the model can only get wrong'
    )
    assert 'verify' in await offered_exits(AGENTS['concierge'], known), (
        'once identified, verify is how a caller corrects their number'
    )
    assert 'verify' not in await offered_exits(AGENTS['complaints'], fresh), (
        'verification is a prerequisite, not a complaint-agent decision'
    )
    assert 'verify' not in await offered_exits(AGENTS['complaints'], known), (
        'already identified, so there is nothing for complaints to send them there for'
    )


def a_borrowed_tool_is_the_move():
    """Calling a lent tool puts the caller in the step that owns it.

    This is the edge that costs nothing. The request that read the diary is the
    request that moved them, so the routing round trip -- 0.85s of silence,
    three times in one live call -- does not happen.

    A tool nobody lends moves nobody.
    """
    known = SalonState(customer_phone=VERIFIED, customer_verified='existing', verified_phone=VERIFIED)
    assert moved_by(known, AGENTS['concierge'], AGENTS, {'read_diary'}) == 'booking'
    assert moved_by(known, AGENTS['concierge'], AGENTS, {'probe'}) == 'concierge'
    assert moved_by(known, AGENTS['concierge'], AGENTS, set()) == 'concierge'


async def a_shut_edge_lends_nothing():
    """An unidentified caller's front desk is not holding the diary.

    Both halves matter: the guard keeps the tool out of the schema the model is
    sent, and it keeps `moved_by` from honouring a call that somehow happened
    anyway.

    The step that owns it always has it, guard or no guard.
    """
    fresh = SalonState()
    assert moved_by(fresh, AGENTS['concierge'], AGENTS, {'read_diary'}) == 'concierge'
    assert 'read_diary' not in await _reachable_tools(AGENTS['concierge'], fresh)
    assert 'read_diary' in await _reachable_tools(
        AGENTS['concierge'],
        SalonState(customer_phone=VERIFIED, customer_verified='existing', verified_phone=VERIFIED),
    )
    assert 'read_diary' in await _reachable_tools(AGENTS['booking'], fresh)


def an_edge_with_no_via_still_needs_hand_over():
    """Only a tool that really means "the caller moved" is allowed to move them.

    `complaints` opens by listening, so there is no tool whose call could stand
    for the move, and the model still has to say so.
    """
    for name, node in AGENTS.items():
        for exit in node.exits:
            for tool in exit.via:
                assert tool.__name__ in _ALL_TOOLS, f'{name} lends {tool.__name__}, which no step owns'
    assert not any(exit.via for exit in AGENTS['concierge'].exits if exit.to == 'complaints')


async def a_finisher_keeps_its_name():
    """No step ever offers the model a tool called `final_result`.

    Pydantic AI names a lone output function `final_result` and only uses the
    real name once there are several, so a step whose exits are all shut --
    `verify` before anyone is identified -- silently loses the one word telling
    the model what the tool does. Caught only because guarding verify's exit
    dropped it to a single output.
    """
    for state in (
        SalonState(),
        SalonState(customer_phone=VERIFIED, customer_verified='existing', verified_phone=VERIFIED),
    ):
        for name, node in AGENTS.items():
            offered = set()

            async def probe(messages, info: AgentInfo, _seen=offered):
                _seen.update(tool.name for tool in info.output_tools)
                if info.allow_text_output:
                    yield 'What can I help you with?'
                else:
                    yield _guidance(info)

            with node.agent.override(model=FunctionModel(stream_function=probe)):
                async with node.agent.run_stream_events(
                    'hello', deps=SalonCall(state=state), output_type=node.outputs(state)
                ) as events:
                    async for _ in events:
                        pass
            assert 'final_result' not in offered, f'{name} offers an unnamed output tool: {offered}'


async def main():
    prerequisite_interposed()
    no_interpose_when_met()
    the_pending_slot_clears()
    never_resumes_into_itself()
    sent_to_a_satisfier_comes_back()
    one_waiting_step_at_a_time()
    round_trip()
    no_model_request_on_kind_a()
    a_borrowed_tool_is_the_move()
    await a_shut_edge_lends_nothing()
    an_edge_with_no_via_still_needs_hand_over()
    await no_illegal_edge_offered()
    await a_finisher_keeps_its_name()
    print('ok')


if __name__ == '__main__':
    asyncio.run(main())
