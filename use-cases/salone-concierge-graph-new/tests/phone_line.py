"""A fake Twilio phone and a paced TTS, copied from salon-graph's `tests.test_marks` for the audit export check.

`Voice` turns text into timed silent audio; `Phone` is the far end of a real
FastAPI websocket speaking Twilio's protocol, and sends each mark back once
the audio before it has "played".
"""

import asyncio
import json
from typing import Any

from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame
from pipecat.services.tts_service import TTSService
from starlette.websockets import WebSocketState


class Voice(TTSService):
    """A TTS service on Pipecat's own base: real contexts and ordering, made-up audio.

    Text starting `[silent]` makes no audio; `[fail]` makes one chunk, then an error; `[long]` makes 4 s of audio.
    """

    def __init__(self) -> None:
        super().__init__(push_start_frame=True, push_stop_frames=True, sample_rate=8000)

    def can_generate_metrics(self) -> bool:
        return False

    async def run_tts(self, text: str, context_id: str):
        if text.startswith('[silent]'):
            return
        if text.startswith('[long]'):
            for _ in range(20):
                yield TTSAudioRawFrame(
                    audio=bytes([7]) * 3200, sample_rate=8000, num_channels=1, context_id=context_id
                )
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
            for pending in [m for m in self.buffer if m['event'] == 'mark' and '*' not in self.lose]:
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
                if (
                    message['event'] == 'mark'
                    and message['mark']['name'] not in self.lose
                    and '*' not in self.lose
                ):
                    self.echo(message['mark']['name'])
            await asyncio.sleep(0.001)
