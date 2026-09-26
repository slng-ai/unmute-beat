"""Stage B3: a read-back is delivered only by its own Twilio mark, through the real frames, serializer and transport.

The pipeline is Pipecat's own from the processor down: `PydanticAgentProcessor`,
a TTS service on Pipecat's base class (audio made up, contexts and ordering
real), `FastAPIWebsocketTransport` with Pipecat's `TwilioFrameSerializer`
wrapped by `TwilioMarks`, over a fake WebSocket. On the other end a fake
phone does what Twilio's Media Streams do: it plays media in order, sends a
mark back when playout reaches it, and on a `clear` drops the audio and
sends every pending mark back at once. Checked:

* M1: each mark goes out after its segment's final media chunk, and a
  read-back is delivered once its own mark comes back;
* M2: a holding line's mark does not deliver the read-back queued behind it;
* M3: a segment with no audio, and one whose synthesis failed, never deliver;
* M4: a lost mark leaves the read-back unknown; duplicate and unrelated
  marks change nothing;
* M5: a clear sent while a mark is pending makes the mark that comes back
  for it count for nothing, and a mark from before the clear is stale; audio
  proven played before the clear stays played;
* M6: the caller's voice detector onset (less its own window) decides
  whether the reply began after the read-back played: before it, no answer.

Every wait is bounded.
"""

import asyncio
import json
import time
from contextlib import suppress
from typing import Any

from pipecat.frames.frames import ErrorFrame, InterruptionFrame, TTSAudioRawFrame, VADUserStartedSpeakingFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.tts_service import TTSService
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.workers.runner import WorkerRunner
from starlette.websockets import WebSocketState

from core.processor import PydanticAgentProcessor, TwilioMarks
from core.proposals import BaseProposal
from salon.app import FLOW, open_call
from salon.diary import Diary

READBACK = 'Shall I book a haircut, Friday at two?'


class Voice(TTSService):
    """A TTS service on Pipecat's own base: real contexts and ordering, made-up audio.

    Text starting `[silent]` makes no audio; `[fail]` makes one chunk, then an error.
    """

    def __init__(self) -> None:
        super().__init__(push_start_frame=True, push_stop_frames=True, sample_rate=8000)

    def can_generate_metrics(self) -> bool:
        return False

    async def run_tts(self, text: str, context_id: str):
        if text.startswith('[silent]'):
            return
        for index in range(3 if not text.startswith('[fail]') else 1):
            await asyncio.sleep(0.005)
            yield TTSAudioRawFrame(
                audio=bytes([index + 1]) * 320, sample_rate=8000, num_channels=1, context_id=context_id
            )
        if text.startswith('[fail]'):
            yield ErrorFrame(error='synthesis failed')


class Phone:
    """The far end of the media stream, as Twilio behaves: play in order, return marks, answer a clear."""

    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED
        self.inbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.wire: list[dict[str, Any]] = []
        self.buffer: list[dict[str, Any]] = []
        self.lose: set[str] = set()
        self.hold = asyncio.Event()
        self.hold.set()
        self.returned: list[str] = []

    async def receive(self) -> dict[str, Any]:
        return await self.inbound.get()

    async def send_bytes(self, data: bytes) -> None:
        raise AssertionError('Twilio media streams are text')

    async def send_text(self, data: str) -> None:
        message = json.loads(data)
        self.wire.append(message)
        if message['event'] == 'clear':
            for pending in [m for m in self.buffer if m['event'] == 'mark']:
                self.echo(pending['mark']['name'])
            self.buffer.clear()
        else:
            self.buffer.append(message)

    async def close(self) -> None:
        self.client_state = WebSocketState.DISCONNECTED

    def echo(self, name: str) -> None:
        self.returned.append(name)
        self.inbound.put_nowait(
            {
                'type': 'websocket.receive',
                'text': json.dumps({'event': 'mark', 'streamSid': 'MZ1', 'mark': {'name': name}}),
            }
        )

    async def play(self) -> None:
        """Play the buffer out: media first, a mark when playout reaches it. Stops while `hold` is clear."""
        while True:
            await self.hold.wait()
            if self.buffer:
                message = self.buffer.pop(0)
                if message['event'] == 'mark' and message['mark']['name'] not in self.lose:
                    self.echo(message['mark']['name'])
            await asyncio.sleep(0.001)


class Wire:
    """One call on the real adapter, transport and serializer. Use as `async with Wire() as wire:`."""

    def __init__(self) -> None:
        self.call = open_call(FLOW, '', Diary())
        self.call.turn_started('I want a haircut on Friday at two')
        self.node = PydanticAgentProcessor(self.call, FLOW.nodes)
        self.phone = Phone()
        twilio = TwilioFrameSerializer(
            stream_sid='MZ1', params=TwilioFrameSerializer.InputParams(auto_hang_up=False)
        )
        self.serializer = TwilioMarks(twilio, self.call)
        transport = FastAPIWebsocketTransport(
            websocket=self.phone,  # ty: ignore[invalid-argument-type]
            params=FastAPIWebsocketParams(
                audio_in_enabled=True, audio_out_enabled=True, serializer=self.serializer
            ),
        )
        pipeline = Pipeline([transport.input(), self.node, Voice(), transport.output()])
        self.worker = PipelineWorker(
            pipeline,
            params=PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000),
            cancel_on_idle_timeout=False,
        )
        self.runner = WorkerRunner(handle_sigint=False)
        self._tasks: list[asyncio.Task] = []

    async def __aenter__(self) -> Wire:
        started = asyncio.Event()

        @self.worker.event_handler('on_pipeline_started')
        async def _started(worker, frame) -> None:
            started.set()

        await self.runner.add_workers(self.worker)
        self._tasks = [asyncio.create_task(self.runner.run()), asyncio.create_task(self.phone.play())]
        await asyncio.wait_for(started.wait(), 5)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.worker.cancel()
        for task in self._tasks:
            task.cancel()
            with suppress(BaseException):
                await asyncio.wait_for(task, 5)

    async def say(self, text: str, *, question: bool = False) -> Any:
        """One fixed line through the adapter, as the engine speaks it: recorded, then handed to `say`."""
        if question:
            self.call.ask(BaseProposal(about='book_appointment', spoken=text, value='slot-1'))
        self.call.note_said('fixed', text, origin='ask' if question else 'tool_filler')
        segment = self.call.spoken[-1]
        await self.node.say(text)
        return segment

    async def until(self, done, limit: float = 5.0) -> None:
        deadline = time.perf_counter() + limit
        while not done():
            if time.perf_counter() > deadline:
                raise TimeoutError('the wire never got there')
            await asyncio.sleep(0.005)

    def marked(self, segment) -> bool:
        return any(m['event'] == 'mark' and m['mark']['name'] == segment.mark for m in self.phone.wire)


def order_of(wire: list[dict[str, Any]], name: str) -> tuple[int, int]:
    """The index of the mark and of the last media message before it."""
    at = next(i for i, m in enumerate(wire) if m['event'] == 'mark' and m['mark']['name'] == name)
    last_media = max(i for i, m in enumerate(wire[:at]) if m['event'] == 'media')
    return at, last_media


async def final_media_then_mark():
    """M1."""
    async with Wire() as wire:
        segment = await wire.say(READBACK, question=True)
        await wire.until(lambda: segment.delivered == 'delivered')
        at, last_media = order_of(wire.phone.wire, segment.mark)
        assert at == last_media + 1, ('the mark did not follow its final media chunk', at, last_media)
        assert segment.heard_by == 'mark' and segment.played_at is not None


async def a_holding_line_does_not_deliver_the_read_back():
    """M2."""
    async with Wire() as wire:
        holding = await wire.say('One moment.')
        await wire.until(lambda: wire.marked(holding))
        wire.phone.hold.clear()
        readback = await wire.say(READBACK, question=True)
        await wire.until(lambda: wire.marked(readback))
        wire.phone.hold.set()
        await wire.until(lambda: holding.delivered == 'delivered')
        wire.phone.hold.clear()
        await asyncio.sleep(0.05)
        assert readback.delivered == 'unknown', "the holding line's mark delivered the read-back behind it"
        wire.phone.hold.set()
        await wire.until(lambda: readback.delivered == 'delivered')


async def no_audio_or_failed_audio_never_delivers():
    """M3."""
    async with Wire() as wire:
        silent = await wire.say('[silent] Shall I book it?', question=True)
        await wire.until(lambda: silent.mark in wire.phone.returned)
        assert silent.delivered == 'unknown' and silent.cleared, (
            'a segment with no audio was delivered by its mark'
        )
    async with Wire() as wire:
        wire.phone.hold.clear()
        failed = await wire.say('[fail] Shall I book it?', question=True)
        await wire.until(lambda: failed.cleared)
        wire.phone.hold.set()
        await wire.until(lambda: failed.mark in wire.phone.returned)
        assert failed.delivered == 'unknown', 'a segment whose synthesis failed was delivered'


async def lost_duplicate_and_unrelated_marks():
    """M4."""
    async with Wire() as wire:
        readback = await wire.say(READBACK, question=True)
        wire.phone.lose.add(readback.mark)
        await wire.until(lambda: wire.marked(readback))
        await asyncio.sleep(0.1)
        assert readback.delivered == 'unknown', 'a read-back with a lost mark was delivered'
        wire.phone.echo('someone-else:1:1~0')
        await asyncio.sleep(0.05)
        assert readback.delivered == 'unknown'
        wire.phone.echo(readback.mark)
        await wire.until(lambda: readback.delivered == 'delivered')
        played_at = readback.played_at
        wire.phone.echo(readback.mark)
        await asyncio.sleep(0.05)
        assert readback.played_at == played_at, 'a duplicate mark moved the playout time'


async def a_clear_voids_pending_marks_only():
    """M5."""
    async with Wire() as wire:
        first = await wire.say('One moment.')
        await wire.until(lambda: first.delivered == 'delivered')
        wire.phone.hold.clear()
        readback = await wire.say(READBACK, question=True)
        await wire.until(lambda: wire.marked(readback))
        await wire.worker.queue_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
        await wire.until(lambda: readback.mark in wire.phone.returned)
        await asyncio.sleep(0.05)
        assert readback.cleared, 'the serializer did not void the mark pending at the clear'
        assert readback.delivered != 'delivered', 'a mark returned by a clear counted as playback'
        assert first.delivered == 'delivered', 'a clear of later audio unplayed earlier audio'
        stale = readback.mark
        wire.phone.hold.set()
        again = await wire.say(READBACK, question=True)
        assert again.mark != stale and again.mark.endswith('~1'), again.mark
        wire.phone.echo(stale)
        await wire.until(lambda: again.delivered == 'delivered')


async def reply_heard(late: bool) -> bool:
    """One read-back, and a caller who starts speaking before (`late`) or after its mark comes back."""
    async with Wire() as wire:
        readback = await wire.say(READBACK, question=True)
        if late:
            wire.phone.hold.clear()
            await wire.until(lambda: wire.marked(readback))
            await asyncio.sleep(0.3)
            await wire.node.queue_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
            await asyncio.sleep(0.05)
            wire.phone.hold.set()
        await wire.until(lambda: readback.delivered == 'delivered')
        if not late:
            await asyncio.sleep(0.25)
            await wire.node.queue_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await wire.until(lambda: bool(wire.call._onsets))
        wire.call.turn_started('yes')
        return wire.call.pending_question() is not None


async def the_reply_must_begin_after_the_mark():
    """M6: a voice onset less its window before the mark came back is not an answer to the read-back."""
    assert await reply_heard(late=False), 'a reply that began after the mark could not answer'
    assert not await reply_heard(late=True), 'a reply that began before the mark answered the read-back'


async def main() -> None:
    await final_media_then_mark()
    await a_holding_line_does_not_deliver_the_read_back()
    await no_audio_or_failed_audio_never_delivers()
    await lost_duplicate_and_unrelated_marks()
    await a_clear_voids_pending_marks_only()
    await the_reply_must_begin_after_the_mark()
    print('marks ok: M1-M6 on the real adapter, transport and Twilio serializer')


if __name__ == '__main__':
    asyncio.run(main())
