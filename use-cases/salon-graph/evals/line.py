"""A harness call on the real pipeline, with scripted models at the provider boundary: the matrix's harness arm.

The same pipeline as `use-cases/salon-pipecat-flows/evals/line.py`, with the
harness where the native build's LLM and filters stand: the user aggregator,
`PydanticAgentProcessor` over the real salon `AGENTS`, a `Playout` sink and
the assistant aggregator. The fakes are Pydantic AI `FunctionModel`s, one for
every step's agent and one for every reading's classifier, each answering
after the same fixed delay as the native arm's scripted Gemini.

`Playout` stands for the transport. When `playback` is `full` it reports the
audio of each answer as started and run out (`BotStartedSpeakingFrame`,
`BotStoppedSpeakingFrame`); `none` reports nothing, which is a read-back handed
to TTS whose audio never played. The harness reads those signals; the native
build does not.

ponytail: the readings' classifiers are module globals, patched per line.
Two lines open at once share whichever patch came last, and restore them
out of order, so a patch can outlive both. The matrix runs every check in a
process of its own; route readings per line (a context variable) if one
process ever needs several.
"""

import asyncio
import json
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any, Literal
from unittest.mock import patch

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMMessagesAppendFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from core.processor import PydanticAgentProcessor
from salon.agents import AGENTS, GREETING
from salon.diary import Diary
from salon.models.state import SalonCall, open_call
from tests.classifier_model import ALL, answered, default_script, reading_asked


@dataclass
class Reply:
    """One scripted step-model response: text, or one or more tool calls."""

    text: str = ''
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


class Scripted:
    """Every agent and every reading on one fake provider, each request after `delay` seconds."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.replies: list[Reply] = []
        self.labels: dict[str, str] = {}
        """Reading name to label for this turn; anything not named reads by `default_script`."""
        self.requests: list[tuple[str, float]] = []

    async def agent(self, messages: Any, info: AgentInfo):
        self.requests.append(('agent', time.perf_counter()))
        await asyncio.sleep(self.delay)
        reply = self.replies.pop(0) if self.replies else Reply(text='Sorry, could you say that again?')
        if reply.text or not reply.calls:
            yield reply.text
        for index, (name, args) in enumerate(reply.calls):
            yield {index: DeltaToolCall(name=name, json_args=json.dumps(args))}

    async def reading(self, messages: Any, info: AgentInfo):
        self.requests.append(('classifier', time.perf_counter()))
        await asyncio.sleep(self.delay)
        reading, data = reading_asked(messages, info)
        label = self.labels.get(reading.name, default_script(reading.name, data))
        return answered(reading, label)


class Playout(FrameProcessor):
    """The transport's place: everything bound for TTS, timed, and, when `playback` says so, played."""

    def __init__(self) -> None:
        super().__init__()
        self.heard: list[tuple[float, str]] = []
        self.ends: list[float] = []
        self.playback: Literal['full', 'none'] = 'full'

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if direction is not FrameDirection.DOWNSTREAM:
            return
        if isinstance(frame, LLMTextFrame | TTSSpeakFrame):
            self.heard.append((time.perf_counter(), frame.text))
            if self.playback == 'full':
                await self.push_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        elif isinstance(frame, LLMFullResponseEndFrame):
            self.ends.append(time.perf_counter())


class Line:
    """One harness call on its real pipeline. Use as `async with Line() as line:`."""

    def __init__(self, caller: str = '', delay: float = 0.05, diary: Diary | None = None) -> None:
        self.diary = diary or Diary()
        self.call: SalonCall = open_call(caller, diary=self.diary)
        self.processor = PydanticAgentProcessor(self.call, AGENTS)
        self.context = LLMContext()
        self.pair = LLMContextAggregatorPair(self.context)
        self.playout = Playout()
        self.model = Scripted(delay)
        pipeline = Pipeline([self.pair.user(), self.processor, self.playout, self.pair.assistant()])
        self.worker = PipelineWorker(pipeline, cancel_on_idle_timeout=False)
        self.runner = WorkerRunner(handle_sigint=False)
        self._stack = ExitStack()
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> Line:
        agent_model = FunctionModel(stream_function=self.model.agent)
        reading_model = FunctionModel(function=self.model.reading)
        for node in AGENTS.values():
            self._stack.enter_context(node.agent.override(model=agent_model))
        for reading in ALL:
            self._stack.enter_context(patch.object(reading.classifier, 'model', reading_model))
        started = asyncio.Event()

        @self.worker.event_handler('on_pipeline_started')
        async def _started(worker, frame) -> None:
            started.set()

        await self.runner.add_workers(self.worker)
        self._task = asyncio.create_task(self.runner.run())
        await asyncio.wait_for(started.wait(), 5)
        await self.processor.greet(GREETING)
        await self.until(lambda: bool(self.playout.heard))
        await self.played()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.worker.cancel()
        if self._task is not None:
            await asyncio.wait_for(asyncio.gather(self._task, return_exceptions=True), 5)
        self._stack.close()

    @property
    def node(self) -> str:
        return self.call.state.agent

    async def played(self) -> None:
        """The audio of everything said so far ran out, as the transport reports it."""
        if self.playout.playback == 'full':
            await self.worker.queue_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
            await self.until(lambda: all(s.playback == 'played' for s in self.call.spoken if s.text))

    async def until(self, done, limit: float = 5.0) -> None:
        """Wait, bounded, until `done()` holds. Raises `TimeoutError` rather than waiting forever."""
        deadline = time.perf_counter() + limit
        while not done():
            if time.perf_counter() > deadline:
                raise TimeoutError('the harness line never got there')
            await asyncio.sleep(0.01)

    async def settle(self, since: float, quiet: float = 0.3, limit: float = 5.0) -> None:
        """Wait until the turn has answered and then gone quiet, and its turn has finished. Bounded."""
        await self.until(lambda: any(t > since for t, _ in self.playout.heard), limit)
        await self.until(lambda: self.processor._idle.is_set(), limit)
        deadline = time.perf_counter() + limit
        while time.perf_counter() < deadline:
            last = max(
                [t for t, _ in self.playout.heard] + self.playout.ends + [t for _, t in self.model.requests]
            )
            if time.perf_counter() - last >= quiet:
                return
            await asyncio.sleep(0.01)
        raise TimeoutError('the harness line never went quiet')

    async def turn(self, words: str, *replies: Reply, **labels: str) -> dict[str, Any]:
        """One caller turn: the step models' replies and the readings' labels, then everything that came out, timed."""
        self.model.replies, self.model.labels = list(replies), labels
        before_heard, before_requests = len(self.playout.heard), len(self.model.requests)
        sent = time.perf_counter()
        await self.worker.queue_frame(
            LLMMessagesAppendFrame([{'role': 'user', 'content': words}], run_llm=True)
        )
        await self.settle(sent)
        await self.played()
        heard = self.playout.heard[before_heard:]
        kinds = [kind for kind, _ in self.model.requests[before_requests:]]
        return {
            'words': words,
            'heard': ''.join(text for _, text in heard),
            'first_output': heard[0][0] - sent if heard else None,
            'last_output': heard[-1][0] - sent if heard else None,
            'requests': len(kinds),
            'agent_requests': kinds.count('agent'),
            'classifier_requests': kinds.count('classifier'),
            'node': self.node,
            'unused_replies': len(self.model.replies),
        }
