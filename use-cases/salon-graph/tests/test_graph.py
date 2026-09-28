"""The salon's graph and what each agent node is offered, from the compiled manifest.

* The legal moves: the four nodes, their declared exits, verification
  interposed before booking and complaints, and a return to whoever waited.
  A move the table does not declare matches no branch and the run fails.
* Interposition costs no request (P2): the caller asking to book is read
  their number back by code on the same turn, with no routing model.
* Each node is offered only its own tools and exits (P3), and `hand_over`
  names only its declared destinations.
* Every node's first request at four representative states is pinned in
  `golden/agents.json`: the prompt, the application state core renders, the
  tools and the outputs. Tool docstrings are prompt text, so any change is a
  diff to review. `BLESS_AGENTS=1` rewrites it; the parent's pins are kept as
  `agents.parent.json` in the research archive (https://github.com/slng-ai/slng-voice-harness/tree/ff13f14/docs/research/reusable-core/session-4).
"""

import asyncio
import json
import os
from pathlib import Path

from core.flow import Flow
from core.graph import route
from salon.app import FLOW
from tests.script import PHONE, Script, opened, turn

GOLDEN = Path(__file__).parent / 'golden' / 'agents.json'


def the_legal_moves_are_the_manifests():
    moves = Flow.of(FLOW.nodes).moves
    assert moves['concierge'] == {'booking', 'complaints', 'verify'}, moves['concierge']
    assert moves['booking'] == {'complaints', 'concierge', 'verify'}, moves['booking']
    assert moves['complaints'] == {'booking', 'concierge', 'verify'}, moves['complaints']
    assert {'booking', 'complaints', 'concierge'} <= moves['verify'], moves['verify']
    assert (
        FLOW.nodes['booking'].requires == ('verified_phone',)
        and FLOW.nodes['booking'].satisfied_by == 'verify'
    )


def verification_is_interposed_and_returned_from():
    """`route` decides and writes nothing: an unverified caller asking to book enters verify, with booking waiting."""
    _, _, call, _ = opened(phone='')
    assert route(call.state, FLOW.nodes, 'booking') == ('verify', 'booking')
    call.update(verified_phone=PHONE, customer_phone=PHONE)
    assert route(call.state, FLOW.nodes, 'booking') == ('booking', None), 'no interposition once verified'


async def an_undeclared_move_fails():
    """The graph refuses a move the table does not declare, whatever asked for it."""
    graph = Flow.of(FLOW.nodes)

    class Rogue:
        async def hop(self, step, prompt):
            from core.flow import Moved

            return Moved('nowhere', None)

    try:
        await graph.run(Rogue(), 'concierge', None)
    except Exception:
        return
    raise AssertionError('an undeclared move was followed')


async def each_node_is_offered_its_own_tools():
    """P3: tools and exits per node, read from what the model is actually sent."""
    script, flow, call, _ = opened()
    await turn(flow, call, script, 'Hello.', ('say', 'Hi!'))
    call.commit(agent='booking')
    await turn(flow, call, script, 'Hi.', ('say', 'Hi!'))
    concierge, booking = script.requests
    assert concierge.tools == ['collect', 'goodbye', 'hand_over'], concierge.tools
    assert set(booking.tools) == {
        'book_appointment', 'cancel_appointment', 'collect', 'decline', 'goodbye', 'hand_over',
        'move_appointment', 'say_again', 'wait',
    }, booking.tools  # fmt: skip
    assert 'lookup_customer' not in booking.tools and 'record_complaint' not in booking.tools


def pinned(script: Script) -> dict:
    return {
        request.step: {'instructions': request.instructions, 'tools': request.tools}
        for request in script.requests
    }


async def first_requests_are_pinned():
    """Each node's first request at a representative state. The diff is the review of any prompt or schema change."""
    requests: dict[str, dict] = {}
    script, flow, call, _ = opened(phone='')
    await turn(flow, call, script, 'What time do you open?', ('say', 'We open at nine.'))
    requests['concierge, nobody identified'] = pinned(script)['concierge']
    script, flow, call, _ = opened(phone='')
    call.commit(agent='verify')
    await turn(flow, call, script, 'It is plus 34 600 111 222.', ('say', 'Thanks.'))
    requests['verify, no number yet'] = pinned(script)['verify']
    script, flow, call, _ = opened(
        verified_phone=PHONE,
        customer_status='existing',
        service='haircut',
        booking_day='Friday',
        booking_time='14:00',
    )
    call.commit(agent='booking')
    call._offers.put('slot', {'2026-10-02T1400|haircut': 'haircut Friday the 2nd at 2:00 PM'})
    await turn(flow, call, script, 'Two works.', ('say', 'Great.'))
    requests['booking, verified, a time on offer'] = pinned(script)['booking']
    script, flow, call, _ = opened(verified_phone=PHONE, customer_status='existing')
    call.commit(agent='complaints')
    await turn(flow, call, script, 'The colour was patchy.', ('say', "I'm sorry."))
    requests['complaints, verified'] = pinned(script)['complaints']
    text = json.dumps(requests, indent=2, sort_keys=True, ensure_ascii=False) + '\n'
    if os.environ.get('BLESS_AGENTS'):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(text)
    assert GOLDEN.exists() and GOLDEN.read_text() == text, (
        'a node request changed: review it, then BLESS_AGENTS=1'
    )


async def main() -> None:
    the_legal_moves_are_the_manifests()
    verification_is_interposed_and_returned_from()
    await an_undeclared_move_fails()
    await each_node_is_offered_its_own_tools()
    await first_requests_are_pinned()
    print('graph ok: legal moves, interposition, refused moves, tools per node, pinned requests')


if __name__ == '__main__':
    asyncio.run(main())
