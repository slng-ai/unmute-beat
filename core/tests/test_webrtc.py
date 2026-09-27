"""A read-back is heard in the browser only when the browser has played past it: the real WebRTC path, offline.

Pipecat's own `SmallWebRTCTransport` and `SmallWebRTCConnection` on the
server, with `core.processor.playout` wiring `WebRTCPlayout` after the output,
and `PydanticAgentProcessor` running the support desk's flow (no salon). On
the far side, a real aiortc peer over loopback plays the part of
`core/client.html`: it opens the `chat` channel, says `client-ready` with
the playout capability, plays the audio track, and reports a mark only once
its played RTP timestamp passes the mark's bound. aiortc's decoded frames
carry their RTP timestamp, which is what a browser's
`getSynchronizationSources().rtpTimestamp` gives for the frame last
delivered for playout. The client page's JavaScript itself runs only in a
browser: `core/tests/client_check.py`, outside this suite. Checked:

* W1: the mark reaching the browser is not evidence; its playout report is.
  A read-back is delivered once the browser played past its bound, recorded
  as the transport's mark, and written as playback evidence;
* W2: a browser that is not playing never reports, so nothing is heard;
* W3: a report for another source, short of its bound, for a mark never
  bounded, from another call, or said twice counts for nothing;
* W4: an interruption voids every mark still pending: a report that comes
  afterwards for one of them counts for nothing, and the copy reads `cut`;
* W5: a reply that began before the playout report is not an answer to it;
* W6: through the flow, a heard read-back and a yes resolve the ticket once;
* W7: a client that says nothing about playout is known on `client-ready`;
* W8: a segment with no audio is never heard.

Every wait is bounded.
"""

import asyncio
import json
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from aiortc import RTCPeerConnection, RTCSessionDescription
from pipecat.frames.frames import InterruptionFrame, TTSAudioRawFrame, VADUserStartedSpeakingFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.tts_service import TTSService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner

from core.processor import PLAYOUT, PydanticAgentProcessor, WebRTCPlayout, playout, reports_playback
from core.proposals import BaseProposal
from tests.test_tickets import desk_in, opened
from tests.ticket_flow.worker import TO_READ_BACK

RATE = 16000
READBACK = '¿Marco el ticket T-101 como resuelto: la impresora ya imprime?'


class Voice(TTSService):
    """A TTS service on Pipecat's own base: real contexts and ordering, made-up audio (0.2 s a line, `[long]` 3 s, `[silent]` none)."""

    def __init__(self) -> None:
        super().__init__(push_start_frame=True, push_stop_frames=True, sample_rate=RATE)

    def can_generate_metrics(self) -> bool:
        return False

    async def run_tts(self, text: str, context_id: str):
        if text.startswith('[silent]'):
            return
        for _ in range(150 if text.startswith('[long]') else 10):
            yield TTSAudioRawFrame(audio=bytes(640), sample_rate=RATE, num_channels=1, context_id=context_id)


def passed(rtp: int, bound: int) -> bool:
    """Serial comparison (RFC 1982), as `client.html` makes it: RTP timestamps wrap at 2^32."""
    return (rtp - bound) % 2**32 < 2**31


class Browser:
    """The far end, as `core/client.html` behaves: plays the audio, reports each mark once its playout passes the bound."""

    def __init__(self, *, capable: bool = True) -> None:
        self.capable = capable
        self.pc = RTCPeerConnection()
        self.pc.addTransceiver('audio', direction='sendrecv')
        self.pc.addTransceiver('video', direction='recvonly')
        self.channel = self.pc.createDataChannel('chat', ordered=True)
        self.channel.on('open', self._ready)
        self.channel.on('message', self._received)
        self.pc.on('track', self._track)
        self.pending: dict[str, tuple[int, int]] = {}
        self.arrived: dict[str, float] = {}
        """When each mark's message reached this side, on the segments' clock."""
        self.reported: dict[str, int] = {}
        self.position: tuple[int, int] | None = None
        """The source and RTP timestamp of the frame last delivered for playout."""
        self.playing = asyncio.Event()
        self.playing.set()
        self._tasks: list[asyncio.Task] = []

    def send(self, type_: str, data: Any) -> None:
        self.channel.send(json.dumps({'label': 'rtvi-ai', 'type': type_, 'id': 'x', 'data': data}))

    def _ready(self) -> None:
        about: dict[str, Any] = {'library': 'test-browser'}
        if self.capable:
            about['platform_details'] = {'playout': PLAYOUT}
        self.send('client-ready', {'version': '2.1.0', 'about': about})

    def _received(self, raw: str) -> None:
        message = json.loads(raw)
        if message.get('type') == 'server-message' and message['data'].get('t') == 'playout-mark':
            report = message['data']['d']
            self.pending[report['mark']] = (report['source'], report['rtp'])
            self.arrived[report['mark']] = time.perf_counter()

    def _track(self, track: Any) -> None:
        if track.kind == 'audio':
            self._tasks.append(asyncio.ensure_future(self._play(track)))

    async def _play(self, track: Any) -> None:
        """Take each decoded frame for playout, and report the marks its RTP timestamp has passed.

        aiortc hands frames out with the stream's first RTP timestamp taken
        off (its receiver's `TimestampMapper`); a browser reports the RTP
        timestamp itself, so it is put back here.
        """
        receiver = self.pc.getReceivers()[0]
        while True:
            await self.playing.wait()
            frame = await track.recv()
            sources = receiver.getSynchronizationSources()
            origin = receiver._RTCRtpReceiver__timestamp_mapper._origin
            if not sources or origin is None:
                continue
            rtp = (origin + frame.pts) % 2**32
            self.position = (sources[0].source, rtp)
            for mark, (source, bound) in list(self.pending.items()):
                if source == self.position[0] and passed(rtp, bound):
                    self.send(
                        'client-message', {'t': 'playout', 'd': {'mark': mark, 'source': source, 'rtp': rtp}}
                    )
                    self.reported[mark] = rtp
                    del self.pending[mark]

    async def connect(self) -> SmallWebRTCConnection:
        await self.pc.setLocalDescription(await self.pc.createOffer())
        server = SmallWebRTCConnection(ice_servers=[])
        await server.initialize(sdp=self.pc.localDescription.sdp, type='offer')
        answer = server.get_answer()
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'], type=answer['type']))
        return server

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self.pc.close()


class Line:
    """One desk call on the real adapter, SmallWebRTC transport and a real peer. Use as `async with Line(folder):`."""

    def __init__(self, folder: Path, *, capable: bool = True, connection: Any = None) -> None:
        """`connection` is a server connection another peer already offered (a real browser); else an aiortc one."""
        self.desk = desk_in(folder)
        self.script, self.flow, self.call = opened(self.desk)
        self.node = PydanticAgentProcessor(self.call, self.flow.nodes)
        self.browser = Browser(capable=capable) if connection is None else None
        self._connection = connection
        self._tasks: list[asyncio.Task] = []

    async def __aenter__(self) -> Line:
        connection = self._connection or await self.browser.connect()
        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        self.wiring = playout(transport, self.call)
        [self.adapter] = self.wiring
        pipeline = Pipeline([transport.input(), self.node, Voice(), transport.output(), *self.wiring])
        self.worker = PipelineWorker(
            pipeline,
            params=PipelineParams(audio_in_sample_rate=RATE, audio_out_sample_rate=RATE),
            cancel_on_idle_timeout=False,
        )
        self.runner = WorkerRunner(handle_sigint=False)
        await self.runner.add_workers(self.worker)
        self._tasks = [asyncio.create_task(self.runner.run())]
        if self.browser is not None:
            await self.until(lambda: self.browser.position is not None, 10)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.worker.cancel()
        if self.browser is not None:
            await self.browser.close()
        for task in self._tasks:
            task.cancel()
            with suppress(BaseException):
                await asyncio.wait_for(task, 5)

    async def say(self, text: str) -> Any:
        """One read-back through the adapter, as the engine speaks a fixed question: recorded, then said."""
        self.call.ask(BaseProposal(about='resolve_ticket', spoken=text, value='T-101'))
        self.call.note_said('fixed', text, origin='ask')
        segment = self.call.spoken[-1]
        await self.node.say(text)
        return segment

    async def until(self, done, limit: float = 5.0) -> None:
        deadline = time.perf_counter() + limit
        while not done():
            if time.perf_counter() > deadline:
                raise TimeoutError('the line never got there')
            await asyncio.sleep(0.005)

    async def onset(self) -> None:
        """The caller starts speaking now: the voice detector's frame, through the processor."""
        await self.node.queue_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        count = len(self.call._onsets)
        await self.until(lambda: len(self.call._onsets) > count)


async def the_mark_is_not_the_evidence(folder: Path):
    """W1."""
    async with Line(folder) as line:
        assert isinstance(line.adapter, WebRTCPlayout) and line.call.transport == 'webrtc', line.wiring
        readback = await line.say(READBACK)
        await line.until(lambda: readback.mark in line.browser.arrived)
        await line.until(lambda: readback.delivered == 'delivered')
        assert readback.heard_by == 'mark' and not readback.simulated, readback
        assert readback.mark in line.browser.reported and readback.mark not in line.adapter._bounds, readback
        assert readback.played_at is not None and readback.played_at > line.browser.arrived[readback.mark], (
            'heard before the browser played past it'
        )
        await asyncio.sleep(0.5)
        await line.onset()
        assert line.call.hearing(readback) == 'heard', line.call.hearing(readback)
        evidence = line.call.playback_for(readback.proposal)
        assert evidence is not None and [m['kind'] for m in evidence['marks']] == ['played'], evidence


async def a_browser_not_playing_hears_nothing(folder: Path):
    """W2."""
    async with Line(folder) as line:
        line.browser.playing.clear()
        readback = await line.say(READBACK)
        await line.until(lambda: readback.mark in line.browser.arrived)
        await asyncio.sleep(0.6)
        assert readback.delivered == 'unknown' and readback.heard_by in ('', 'signal'), readback
        line.browser.playing.set()
        await line.until(lambda: readback.delivered == 'delivered')


async def reports_that_prove_nothing(folder: Path):
    """W3."""
    async with Line(folder) as line:
        line.browser.playing.clear()
        readback = await line.say(READBACK)
        await line.until(lambda: readback.mark in line.browser.arrived)
        source, bound = line.browser.pending[readback.mark]
        other = open_other(folder)
        forged = [
            {'mark': readback.mark, 'source': source + 1, 'rtp': bound},
            {'mark': readback.mark, 'source': source, 'rtp': (bound - 960) % 2**32},
            {'mark': readback.mark, 'source': source, 'rtp': (bound + 2**31 + 1) % 2**32},
            {'mark': f'{other}:1:1~0', 'source': source, 'rtp': bound},
            {'mark': readback.mark, 'source': source, 'rtp': str(bound)},
        ]
        for report in forged:
            assert not line.adapter.played(report), report
        assert readback.delivered == 'unknown', readback
        assert line.adapter.played({'mark': readback.mark, 'source': source, 'rtp': bound})
        assert readback.delivered == 'delivered'
        assert not line.adapter.played({'mark': readback.mark, 'source': source, 'rtp': bound}), (
            'counted twice'
        )


def open_other(folder: Path) -> str:
    """Another call's id, for a mark name that is not this call's."""
    _, _, call = opened(desk_in(folder / 'other'))
    return call.id


async def an_interruption_voids_what_is_pending(folder: Path):
    """W4."""
    async with Line(folder) as line:
        line.browser.playing.clear()
        readback = await line.say(READBACK)
        await line.until(lambda: readback.mark in line.browser.arrived)
        await line.worker.queue_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
        await line.until(lambda: readback.cleared)
        line.browser.playing.set()
        await line.until(lambda: readback.mark in line.browser.reported)
        await asyncio.sleep(0.1)
        assert readback.delivered != 'delivered' and readback.lost == 'cleared', readback
        assert line.call.hearing(readback) == 'cut', line.call.hearing(readback)

        cut = await line.say('[long] ' + READBACK)
        await asyncio.sleep(0.3)
        await line.worker.queue_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
        await line.until(lambda: cut.cleared)
        await asyncio.sleep(0.3)
        assert cut.mark not in line.browser.arrived and cut.delivered != 'delivered', cut


async def a_reply_before_the_report_is_not_an_answer(folder: Path):
    """W5."""
    async with Line(folder) as line:
        line.browser.playing.clear()
        readback = await line.say(READBACK)
        await line.until(lambda: readback.mark in line.browser.arrived)
        await line.onset()
        line.browser.playing.set()
        await line.until(lambda: readback.delivered == 'delivered')
        assert line.call.hearing(readback) == 'early', line.call.hearing(readback)
        line.call.turn_started('Sí.')
        assert line.call.pending_question() is None, (
            'a reply that began before playout answered the read-back'
        )


async def a_heard_yes_resolves_once(folder: Path):
    """W6."""
    async with Line(folder) as line:
        line.script.moves = list(TO_READ_BACK)
        await line.node.serve('Resuelve el ticket T-101, cuenta ACC-1234.')
        [readback] = [s for s in line.call.all_spoken if s.text == READBACK]
        await line.until(lambda: readback.delivered == 'delivered')
        await asyncio.sleep(0.5)
        await line.onset()
        line.script.moves = [('call', 'resolve_ticket', {})]
        await line.node.serve('Sí.')
        assert line.desk.sent() == 1 and line.desk.ticket('T-101') == (2, 'resolved'), line.desk.sent()
        line.script.moves = [('call', 'resolve_ticket', {})]
        await line.node.serve('Sí, sí.')
        assert line.desk.sent() == 1, 'a second yes sent it again'


async def a_client_without_playout_is_known_at_once(folder: Path):
    """W7."""
    for capable in (True, False):
        async with Line(folder / str(capable), capable=capable) as line:
            started = time.perf_counter()
            assert await reports_playback(line.wiring) is capable, capable
            assert time.perf_counter() - started < 2, 'the capability was not known on client-ready'


async def a_segment_with_no_audio_is_never_heard(folder: Path):
    """W8: its mark passes the output with no audio before it, so it is never bounded, and no report can count."""
    async with Line(folder) as line:
        silent = await line.say('[silent] ' + READBACK)
        await line.until(lambda: silent.cleared)
        await asyncio.sleep(0.5)
        assert silent.lost == 'silent' and silent.mark not in line.browser.arrived, silent
        assert silent.delivered == 'unknown' and line.call.hearing(silent) == 'failed', silent


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        for index, check in enumerate(
            (
                the_mark_is_not_the_evidence,
                a_browser_not_playing_hears_nothing,
                reports_that_prove_nothing,
                an_interruption_voids_what_is_pending,
                a_reply_before_the_report_is_not_an_answer,
                a_heard_yes_resolves_once,
                a_client_without_playout_is_known_at_once,
                a_segment_with_no_audio_is_never_heard,
            )
        ):
            folder = Path(tmp) / str(index)
            for sub in ('', 'other', 'True', 'False'):
                (folder / sub).mkdir(parents=True, exist_ok=True)
            await check(folder)
    print('webrtc ok: W1-W8 on the real SmallWebRTC transport, an aiortc browser and the desk flow')


if __name__ == '__main__':
    asyncio.run(main())
