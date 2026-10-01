"""The scripted model every Nosh test drives the real flow with, and fake place tools with no network.

A move is `('call', tool, args)`: the decision the model makes on that
request. Every request records which step asked and what it was sent.
Scripted models prove runtime properties, never language understanding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

import core
from nosh.app import SPEC, greeting
from nosh.app import open_call as open_on
from nosh.operations import TZ
from nosh.tools import NotFoundError

CLOCK = datetime(2026, 10, 1, 20, 0, tzinfo=TZ)
"""Thursday 1 October 2026, 8 PM in Barcelona."""

THURSDAY = 'Thursday: 1:00\u202fPM \u2013 12:00\u202fAM'

GOTHIC = {'latitude': 41.3833, 'longitude': 2.1777, 'formatted_address': 'Barri Gòtic, Barcelona, Spain'}
GRACIA = {'latitude': 41.4036, 'longitude': 2.1561, 'formatted_address': 'Gràcia, Barcelona, Spain'}
STREET = {
    'latitude': 41.3946,
    'longitude': 2.1745,
    'formatted_address': 'Passeig de Sant Joan, 55, 08009 Barcelona, Spain',
}


def place(gid: str, name: str, rating: float, count: int, kind: str) -> dict[str, Any]:
    return {
        'place_id': gid,
        'name': name,
        'address': f'{name} street 1, 08002 Barcelona, Spain',
        'latitude': 41.38,
        'longitude': 2.17,
        'rating': rating,
        'user_rating_count': count,
        'types': ['restaurant'],
        'primary_type': 'restaurant',
        'primary_type_display_name': kind,
    }


TAPAS = [
    place('g-canete', 'Bar Cañete', 4.5, 3200, 'Tapas Restaurant'),
    place('g-gone', 'El Xampanyet', 4.4, 800, 'Bar'),
    place('g-palma', 'Bodega La Palma', 4.0, 1500, 'Spanish Restaurant'),
    place('g-paixano', 'Can Paixano', 4.2, 9000, 'Bar'),
]
NEARBY = [place('g-snack', 'Bar Snack 55', 4.0, 600, 'Bar')]
DETAILS = {
    'g-canete': {
        'place_id': 'g-canete',
        'name': 'Bar Cañete',
        'address': 'Carrer de la Unió, 17, Ciutat Vella, 08001 Barcelona, Spain',
        'rating': 4.5,
        'price_level': 'PRICE_LEVEL_MODERATE',
        'opening_hours': ['Wednesday: Closed', THURSDAY],
        'phone': '+34 932 70 34 58',
        'website': 'https://barcanete.com',
        'types': ['restaurant'],
        'primary_type': 'restaurant',
        'primary_type_display_name': 'Tapas Restaurant',
    },
    'g-palma': {
        'place_id': 'g-palma',
        'name': 'Bodega La Palma',
        'address': 'Carrer de la Palma de Sant Just, 7, 08002 Barcelona, Spain',
        'rating': 4.0,
        'price_level': None,
        'opening_hours': None,
        'phone': None,
        'website': None,
    },
}


@dataclass
class FakeTools:
    """The four place routes, from fixed answers, with every request kept. 404 where Google would have nothing."""

    sent: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def post(self, route: str, body: dict[str, Any]) -> Any:
        self.sent.append((route, body))
        if route == 'resolve-location':
            found = {'gothic quarter, barcelona': GOTHIC, 'gràcia, barcelona': GRACIA}
            found |= {'passeig de sant joan 55, barcelona': STREET}
            if (answer := found.get(body['query'].lower())) is None:
                raise NotFoundError(route)
            return answer
        if route == 'search-text':
            return [] if 'sushi' in body['query'] else TAPAS
        if route == 'search-nearby':
            return NEARBY
        if route == 'place-details':
            if (answer := DETAILS.get(body['place_id'])) is None:
                raise NotFoundError(route)
            return answer
        raise AssertionError(f'Nosh never calls {route}')

    def routes(self, name: str) -> list[dict[str, Any]]:
        return [body for route, body in self.sent if route == name]


@dataclass
class Request:
    step: str
    messages: list[ModelMessage]
    instructions: str
    tools: list[str]


@dataclass
class Script:
    moves: list[tuple[Any, ...]] = field(default_factory=list)
    requests: list[Request] = field(default_factory=list)
    call: core.FlowCall | None = None

    def __post_init__(self) -> None:
        self.model = FunctionModel(self.reading, stream_function=self.agent)

    def reading(self, messages: Any, info: AgentInfo) -> ModelResponse:
        raise AssertionError('a separate reading was requested; only step agents may run')

    async def agent(self, messages: list[ModelMessage], info: AgentInfo):
        step = self.call.state.agent if self.call is not None else '?'
        tools = sorted(t.name for t in [*info.function_tools, *info.output_tools])
        self.requests.append(Request(step, list(messages), info.instructions or '', tools))
        if not self.moves:
            yield {
                0: DeltaToolCall(
                    name='reply', json_args=json.dumps({'say': 'Sorry, could you say that again?'})
                )
            }
            return
        _, name, *args = self.moves.pop(0)
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args[0] if args else {}))}


def opened(**facts: Any) -> tuple[Script, core.StepFlow, core.FlowCall, FakeTools, str]:
    """A fresh call on Nosh's real flow, with the fake tools on the fixed clock, greeted and heard."""
    script = Script()
    flow = SPEC.compile(model=script.model)
    tools = FakeTools()
    call = open_on(flow, tools, clock=lambda: CLOCK, **facts)  # ty: ignore[invalid-argument-type]
    script.call = call
    said = core.greet(call, greeting(flow, call))
    call.acknowledge(call.spoken[-1].id)
    return script, flow, call, tools, said


async def turn(flow, call, script: Script, words: str, *moves, heard: bool = True) -> list[str]:
    """One caller turn. Every segment of the reply reaches the caller unless `heard` is False."""
    script.moves = list(moves)
    reply = await core.text_turn(flow, call, words)
    if heard:
        call.acknowledge(*(segment.id for segment in reply.segments))
    return [segment.text for segment in reply.segments]
