"""The scripted model every Wasla test drives the real flow with, on a fresh fake ledger and a fixed clock.

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
from wasla.app import SPEC, greeting
from wasla.app import open_call as open_on
from wasla.ledger import Ledger
from wasla.operations import TZ

CLOCK = datetime(2026, 10, 1, 10, 0, tzinfo=TZ)
"""Thursday 1 October 2026, 10 AM in Dubai. The deadline is Sunday the 11th."""


@dataclass
class Request:
    step: str
    messages: list[ModelMessage]
    instructions: str
    tools: list[str]

    @property
    def sent(self) -> str:
        """Everything the model was given, as one string to search."""
        return f'{self.instructions}\n{self.messages}'


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


def opened(account_id: str = 'WA-1001') -> tuple[Script, core.StepFlow, core.FlowCall, Ledger, str]:
    """A fresh call about one seeded account, on its own ledger and the fixed clock, greeted and heard."""
    script = Script()
    flow = SPEC.compile(model=script.model)
    ledger = Ledger()
    call = open_on(flow, account_id, ledger, clock=lambda: CLOCK)
    script.call = call
    said = core.greet(call, greeting(flow, call))
    call.acknowledge(call.spoken[-1].id)
    return script, flow, call, ledger, said


async def turn(flow, call, script: Script, words: str, *moves, heard: bool = True) -> list[str]:
    """One caller turn. Every segment of the reply reaches the caller unless `heard` is False."""
    script.moves = list(moves)
    reply = await core.text_turn(flow, call, words)
    if heard:
        call.acknowledge(*(segment.id for segment in reply.segments))
    return [segment.text for segment in reply.segments]


async def verified(flow, call, script: Script) -> None:
    """Through identify: the caller is the holder, their date of birth read back and confirmed."""
    await turn(
        flow,
        call,
        script,
        'Yes, speaking.',
        ('call', 'reply', {'say': 'Thanks. What is your date of birth?'}),
    )
    await turn(
        flow, call, script, '14 March 1990.', ('call', 'record', {'update': {'birth_date': '1990-03-14'}})
    )
    await turn(flow, call, script, 'Yes.', ('call', 'verify_holder'))
    assert call.state.agent == 'account', call.state.agent
