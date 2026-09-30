"""The scripted model every core test drives the real flow with: each request takes the next move.

A move is what one model response streams: `Reply` (a structured step's
answer, with the caller's update), `Record` (an update code carries out or
reads back), `Call` (any other tool or output, an operation with its
`update`, `hand_over`, `done`, `goodbye`), `Calls` (several in one
response, in order), `Say` (plain text, which no structured step accepts)
and `Stall` (a call started and never finished). Every request is kept as a
`Request`: which step asked, what it was sent and offered.

Scripted models prove runtime properties, never language understanding. A
non-streamed request would be a separate reading of the reply, which core
never sends, so it fails the test.
"""

import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

import core
from core.history import Payload

type Streamed = str | dict[int, DeltaToolCall]


class Move(ABC):
    """One model response, as the stream yields it."""

    @abstractmethod
    def stream(self) -> AsyncIterator[Streamed]: ...


@dataclass(init=False)
class Call(Move):
    """One tool or output call: `Call('book', update={'slot': '1000'})`."""

    tool: str
    args: dict[str, Any]

    def __init__(self, tool: str, **args: Any) -> None:
        self.tool, self.args = tool, args

    def delta(self, index: int = 0) -> dict[int, DeltaToolCall]:
        return {index: DeltaToolCall(name=self.tool, json_args=json.dumps(self.args))}

    async def stream(self) -> AsyncIterator[Streamed]:
        yield self.delta()


class Reply(Call):
    """A structured step's answer: words, the caller's update when there is one, `decline` for a no."""

    def __init__(self, say: str, *, decline: bool = False, **update: Any) -> None:
        args: dict[str, Any] = {'say': say}
        if update:
            args['update'] = update
        if decline:
            args['decline'] = True
        super().__init__('reply', **args)


class Record(Call):
    """An update where code carries out, reads back or offers what follows, and the model says nothing."""

    def __init__(self, **update: Any) -> None:
        super().__init__('record', update=update)


class Calls(Move):
    """Several calls in one response, in this order."""

    def __init__(self, *calls: Call) -> None:
        self.calls = calls

    async def stream(self) -> AsyncIterator[Streamed]:
        deltas: dict[int, DeltaToolCall] = {}
        for index, call in enumerate(self.calls):
            deltas |= call.delta(index)
        yield deltas


@dataclass
class Say(Move):
    """Plain text: a model that answers outside every output, which a structured step refuses."""

    words: str

    async def stream(self) -> AsyncIterator[Streamed]:
        yield self.words


class Stall(Move):
    """The start of a call, streamed, and then nothing: the provider hangs mid-output."""

    def __init__(self, tool: str, partial: str) -> None:
        self.tool, self.partial = tool, partial
        self.started = asyncio.Event()

    async def stream(self) -> AsyncIterator[Streamed]:
        yield {0: DeltaToolCall(name=self.tool, json_args=self.partial)}
        self.started.set()
        await asyncio.Event().wait()


@dataclass(frozen=True)
class Request:
    """One request a step's agent sent: who asked, and what it was sent and offered."""

    step: str
    messages: list[Any]
    instructions: str
    offered: frozenset[str]
    payload: Payload

    @property
    def told(self) -> str:
        """Everything the request was sent, as text, for checks on what a step saw."""
        return f'{self.instructions}\n{self.messages!r}'


class Script:
    """The step agents: each request takes the next move, or answers that it did not catch that.

    `call` names the call whose step is recorded with each request; `delay`
    holds every response back that long, for timing checks.
    """

    FALLBACK = 'Sorry, could you say that again?'

    def __init__(self, *moves: Move) -> None:
        self.moves: list[Move] = list(moves)
        self.requests: list[Request] = []
        self.call: core.FlowCall | None = None
        self.delay = 0.0
        self.model = FunctionModel(self.reading, stream_function=self.agent)

    @property
    def steps(self) -> list[str]:
        return [r.step for r in self.requests]

    @property
    def payloads(self) -> list[Payload]:
        return [r.payload for r in self.requests]

    @property
    def told(self) -> list[str]:
        return [r.told for r in self.requests]

    def reading(self, messages: Any, info: AgentInfo) -> ModelResponse:
        raise AssertionError('a separate reading was requested; only step agents may run')

    def next_move(self, messages: list[Any], info: AgentInfo) -> Move:
        """The move this request takes: the next one scripted, or the fallback when none is left."""
        if self.moves:
            return self.moves.pop(0)
        if any(tool.name == 'reply' for tool in info.output_tools):
            return Reply(self.FALLBACK)
        return Say(self.FALLBACK)

    async def agent(self, messages: list[Any], info: AgentInfo) -> AsyncIterator[Streamed]:
        self.requests.append(
            Request(
                step=self.call.state.agent if self.call is not None else '?',
                messages=list(messages),
                instructions=getattr(info, 'instructions', '') or '',
                offered=frozenset(t.name for t in (*info.function_tools, *info.output_tools)),
                payload=Payload.of(messages, info),
            )
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        async for streamed in self.next_move(messages, info).stream():
            yield streamed


async def turn(
    flow: core.StepFlow, call: core.FlowCall, script: Script, words: str, *moves: Move, heard: bool = True
) -> list[str]:
    """One caller turn with these moves: what was said to the caller, acknowledged unless `heard=False`."""
    script.moves = list(moves)
    reply = await core.text_turn(flow, call, words)
    if heard:
        call.acknowledge(*(segment.id for segment in reply.segments))
    return [segment.text for segment in reply.segments]
