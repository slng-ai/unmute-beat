"""A native call on the real pipeline, with a scripted model at the network boundary: the matrix's native arm.

`Line` builds what `bot.py` builds between the transports: the user
aggregator, a `GoogleLLMService`, the `ClaimFilter` and `EmptyReplyGuard`, the
assistant aggregator, and a real `FlowManager` over `flow.yaml`. The one fake
is `ScriptedGemini._stream_response`: it answers each request with the next
scripted reply as Gemini's own response chunks, after a fixed delay, so the
service's real code turns them into text frames, function calls and node
changes. No handler is called directly.

Caller turns enter as the user aggregator's appended message, the way a
transcript does. A `Playout` sink stands for the transport: it records every
frame that would reach TTS and, when told to, reports the audio as played.
The native build reads no playback, so that report changes nothing here.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from google.genai.types import Candidate, Content, FunctionCall, GenerateContentResponse, Part
from pipecat.frames.frames import (
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
from pipecat.services.google.llm import GoogleLLMService
from pipecat.workers.runner import WorkerRunner

from graph_salon import GraphSalon
from salon.diary import TZ, Diary

MONDAY = datetime(2026, 9, 21, 8, 0, tzinfo=TZ)
KNOWN = '+34600111222'


@dataclass
class Reply:
    """One scripted model response: text, or one or more function calls, or nothing at all."""

    text: str = ''
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


class ScriptedGemini(GoogleLLMService):
    """Pipecat's Gemini service, answering from a script instead of the network."""

    def __init__(self, delay: float = 0.05) -> None:
        super().__init__(api_key='offline', settings=GoogleLLMService.Settings(model='gemini-3.1-flash-lite'))
        self.replies: list[Reply] = []
        self.delay = delay
        self.requests = 0
        self.started: list[float] = []

    async def _stream_response(self, context: LLMContext) -> AsyncIterator[GenerateContentResponse]:
        self.requests += 1
        self.started.append(time.perf_counter())
        await asyncio.sleep(self.delay)
        reply = self.replies.pop(0) if self.replies else Reply(text='Sorry, could you say that again?')
        parts = [Part(text=reply.text)] if reply.text else []
        parts += [
            Part(function_call=FunctionCall(id=str(uuid.uuid4()), name=name, args=args))
            for name, args in reply.calls
        ]
        yield GenerateContentResponse(
            candidates=[Candidate(content=Content(role='model', parts=parts), finish_reason='STOP')]
        )


class Playout(FrameProcessor):
    """The transport's place: everything bound for TTS, with the time it arrived."""

    def __init__(self) -> None:
        super().__init__()
        self.heard: list[tuple[float, str]] = []
        self.ends: list[float] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction is FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMTextFrame | TTSSpeakFrame):
                self.heard.append((time.perf_counter(), frame.text))
            elif isinstance(frame, LLMFullResponseEndFrame):
                self.ends.append(time.perf_counter())
        await self.push_frame(frame, direction)


class Line:
    """One native call on its real pipeline. Use as `async with Line() as line:`."""

    def __init__(self, caller: str = '', delay: float = 0.05, diary: Diary | None = None) -> None:
        self.diary = diary or Diary(clock=MONDAY)
        self.llm = ScriptedGemini(delay)
        self.salon = GraphSalon(self.diary, 'call-1', self.llm, caller=caller)
        self.pair = LLMContextAggregatorPair(self.salon.context)
        self.playout = Playout()
        pipeline = Pipeline(
            [self.pair.user(), self.llm, *self.salon.processors, self.playout, self.pair.assistant()]
        )
        self.worker = PipelineWorker(pipeline, cancel_on_idle_timeout=False)
        self.salon.attach(self.worker, self.pair)
        self.runner = WorkerRunner(handle_sigint=False)
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> Line:
        started = asyncio.Event()

        @self.worker.event_handler('on_pipeline_started')
        async def _started(worker, frame) -> None:
            started.set()

        await self.runner.add_workers(self.worker)
        self._task = asyncio.create_task(self.runner.run())
        await asyncio.wait_for(started.wait(), 5)
        await asyncio.wait_for(self.salon.start(), 5)
        await self.until(lambda: bool(self.playout.heard))
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.worker.cancel()
        if self._task is not None:
            await asyncio.wait_for(asyncio.gather(self._task, return_exceptions=True), 5)

    @property
    def node(self) -> str:
        manager = self.salon._manager
        return (manager.current_node if manager else '') or ''

    @property
    def said(self) -> str:
        """What the node's `say` action last queued: the words code chose."""
        manager = self.salon._manager
        return manager.state.get('say', '') if manager else ''

    async def until(self, done, limit: float = 5.0) -> None:
        """Wait, bounded, until `done()` holds. Raises `TimeoutError` rather than waiting forever."""
        deadline = time.perf_counter() + limit
        while not done():
            if time.perf_counter() > deadline:
                raise TimeoutError('the native line never got there')
            await asyncio.sleep(0.01)

    async def settle(self, since: float, quiet: float = 0.3, limit: float = 5.0) -> None:
        """Wait until the turn has answered (something reached the playout after `since`) and then gone quiet. Bounded."""
        await self.until(lambda: any(t > since for t, _ in self.playout.heard), limit)
        deadline = time.perf_counter() + limit
        while time.perf_counter() < deadline:
            last = max([t for t, _ in self.playout.heard] + self.playout.ends + self.llm.started)
            if time.perf_counter() - last >= quiet:
                return
            await asyncio.sleep(0.01)
        raise TimeoutError('the native line never went quiet')

    async def turn(self, words: str, *replies: Reply) -> dict[str, Any]:
        """One caller turn: what the model will answer, the words, then everything that came out, timed."""
        self.llm.replies = list(replies)
        before_heard, before_requests = len(self.playout.heard), self.llm.requests
        sent = time.perf_counter()
        await self.worker.queue_frame(
            LLMMessagesAppendFrame([{'role': 'user', 'content': words}], run_llm=True)
        )
        await self.settle(sent)
        heard = self.playout.heard[before_heard:]
        return {
            'words': words,
            'heard': ''.join(text for _, text in heard),
            'first_output': heard[0][0] - sent if heard else None,
            'last_output': heard[-1][0] - sent if heard else None,
            'requests': self.llm.requests - before_requests,
            'node': self.node,
            'unused_replies': len(self.llm.replies),
        }
