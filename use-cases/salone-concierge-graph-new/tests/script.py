"""The scripted provider every salon test drives the real flow with: each agent request takes the next move.

A move is `('say', words)`, `('call', tool, args)`, `('calls', [(tool, args), ...])`
for one response carrying several tool calls, or `('collect', values)`. Every
request records which step asked and exactly what it was sent (messages and
instructions), so a test can inspect the provider input. A non-streamed
request would be a separate reading of the reply, which the node-agent core
never sends: it fails the test (P1).

Every step of this salon is prose. `('answer', words)` is the stylist's
reply and its `done` in one response: what a consultation that answers at
once sends.

Scripted models prove runtime properties, never language understanding.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

import core
from salon.app import FLOW, SPEC
from salon.app import open_call as open_on
from salon.diary import TZ, Diary

CLOCK = datetime(2026, 9, 28, 8, 30, tzinfo=TZ)
"""Monday 28 September 2026, before opening: Thursday is 1 October, Friday the 2nd, Saturday the 3rd."""
PHONE = '+34600111222'
"""The diary's seed customer."""


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
    delay: float = 0.0
    flow: core.StepFlow | None = None

    def __post_init__(self) -> None:
        self.model = FunctionModel(self.reading, stream_function=self.agent)

    def reading(self, messages: Any, info: AgentInfo) -> ModelResponse:
        raise AssertionError('a separate reading was requested; only step agents may run')

    async def agent(self, messages: list[ModelMessage], info: AgentInfo):
        step = self.call.state.agent if self.call is not None else '?'
        tools = sorted(t.name for t in [*info.function_tools, *info.output_tools])
        self.requests.append(Request(step, list(messages), info.instructions or '', tools))
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self.moves:
            yield 'Sorry, could you say that again?'
            return
        kind, *rest = self.moves.pop(0)
        if kind in ('say', 'text'):
            yield rest[0]
        elif kind == 'answer':
            yield rest[0]
            yield {1: DeltaToolCall(name='done', json_args='{}')}
        elif kind == 'collect':
            yield {0: DeltaToolCall(name='collect', json_args=json.dumps({'values': rest[0]}))}
        elif kind == 'call':
            yield {0: DeltaToolCall(name=rest[0], json_args=json.dumps(rest[1] if len(rest) > 1 else {}))}
        else:
            yield {i: DeltaToolCall(name=n, json_args=json.dumps(a)) for i, (n, a) in enumerate(rest[0])}

    @property
    def steps(self) -> list[str]:
        return [r.step for r in self.requests]


def flow_for(script: Script) -> core.StepFlow:
    """The salon compiled once per script, with its model: the same manifest the bot compiles."""
    if script.flow is None:
        script.flow = SPEC.compile(model=script.model)
    return script.flow


def opened(script: Script | None = None, *, phone: str = PHONE, diary: Diary | None = None, **facts: Any):
    """A fresh call on the salon's real flow, with its own diary on the fixed clock. `phone` is the caller ID."""
    script = script or Script()
    flow = flow_for(script)
    diary = diary if diary is not None else Diary(clock=CLOCK)
    call = open_on(flow, phone, diary, **facts)
    script.call = call
    return script, flow, call, diary


async def turn(flow, call, script: Script, words: str, *moves, heard: bool = True) -> list[str]:
    """One caller turn. Every segment of the reply reaches the caller unless `heard` is False."""
    script.moves = list(moves)
    reply = await core.text_turn(flow, call, words)
    if heard:
        call.acknowledge(*(segment.id for segment in reply.segments))
    return [segment.text for segment in reply.segments]


def slot(day: str, at: str, service: str = 'haircut') -> str:
    """The diary's id for a slot, as the diary issues it. Tests name ids; only the diary makes them."""
    return f'{day}T{at.replace(":", "")}|{service}'


def text_of(messages: list[ModelMessage]) -> str:
    """Everything a request's messages carried, as one string to search."""
    return str(messages)


def open_call(phone: str = '', diary: Diary | None = None, **facts: Any) -> core.FlowCall:
    """A call on the production flow (`salon.app.FLOW`), for tests that override its agents' models directly."""
    return open_on(FLOW, phone, diary if diary is not None else Diary(clock=CLOCK), **facts)
