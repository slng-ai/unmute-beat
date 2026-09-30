"""A read-back is heard in the browser only when the browser has played past it: the real WebRTC path, offline.

Pipecat's own `SmallWebRTCTransport` and `SmallWebRTCConnection` on the
server, with `core.processor.playout` wiring `WebRTCPlayout` after the output,
and `PydanticAgentProcessor` running the support desk's flow (no salon). On
the far side, a real aiortc peer over loopback plays the part of core's
page (`core/client/playout.js` on Pipecat's client): it opens the `chat`
channel, says `client-ready` with the playout capability, and reports a mark
only once its output played without a break from before the mark's `start`
and its played RTP timestamp passed the mark's bound. aiortc's decoded
frames carry their RTP timestamp, which is what a browser's
`getSynchronizationSources().rtpTimestamp` gives for the frame last
delivered for playout; the receiver keeps advancing while the output is
stopped, as a browser's may. The page itself runs only in a browser:
`core/tests/client_check.py`, outside this suite. Checked:

* W1: the mark reaching the browser is not evidence; its playout report is.
  A read-back is delivered once the browser played past its bound, recorded
  as the transport's mark, and written as playback evidence;
* W2: a browser that is not playing never reports, so nothing is heard, and
  playing again approves nothing that began before it; the next segment is heard;
* W3: a report for another source, short of its bound, from an output that
  started after the segment began, for a mark never bounded, from another
  call, or said twice counts for nothing;
* W4: an interruption cuts only audio still to be sent: a segment cut before
  its end is never heard and reads `cut`; a segment whose audio was all sent
  is not cut by it (a browser has no clear), reads `unconfirmed` until its
  report, and a report after the caller began to speak is `early`, no answer;
* W5: a reply that began before the playout report is not an answer to it;
* W6: through the flow, a heard read-back and a yes resolve the ticket once;
* W7: a client that says nothing about playout is known on `client-ready`;
* W8: a segment with no audio is never heard;
* W9: a `client-ready` whose `about` is null, a list, a string or has no
  usable details is a client that reports nothing, never an exception;
* W10: `reports_playback` is truthful: a Twilio stream with marks, or a call
  opened for simulation, can confirm; a transport with estimates only, or a
  Twilio far end with no marks, cannot.

Every wait is bounded.
"""

import asyncio
import json
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from aiortc import RTCPeerConnection, RTCSessionDescription
from pipecat.frames.frames import InterruptionFrame, TTSAudioRawFrame, VADUserStartedSpeakingFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.tts_service import TTSService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner

from core.processor import (
    PLAYOUT,
    LocalPlayout,
    PydanticAgentProcessor,
    WebRTCPlayout,
    playout,
    reports_playback,
)
from core.proposals import BaseProposal
from core.state import CUT_OFF_WINDOW
from tests.script import Call
from tests.test_tickets import desk_in, opened
from tests.ticket_flow.worker import TO_READ_BACK

RATE = 16000
READBACK = '¿Marco el ticket T-101 como resuelto: la impresora ya imprime?'


class Voice(TTSService):
    """A TTS service on Pipecat's own base: real contexts and ordering, made-up audio (0.2 s a line, `[long]` 3 s, `[silent]` none).

    `long` names lines said for 3 s too, so a flow's own read-back can be long.
    """

    def __init__(self, long: tuple[str, ...] = ()) -> None:
        super().__init__(push_start_frame=True, push_stop_frames=True, sample_rate=RATE)
        self._long = set(long)

    def can_generate_metrics(self) -> bool:
        return False

    async def run_tts(self, text: str, context_id: str):
        if text.startswith('[silent]'):
            return
        for _ in range(150 if text.startswith('[long]') or text.strip() in self._long else 10):
            yield TTSAudioRawFrame(audio=bytes(640), sample_rate=RATE, num_channels=1, context_id=context_id)


def passed(rtp: int, bound: int) -> bool:
    """Serial comparison (RFC 1982), as `playout.js` makes it: RTP timestamps wrap at 2^32."""
    return (rtp - bound) % 2**32 < 2**31


class Browser:
    """The far end, as core's page behaves: reports a mark once its output played from before `start` past the bound.

    `playing` is the page's audio output: cleared, the output is stopped (paused,
    blocked, detached) while the receiver goes on. `hold` keeps reports back
    while the output plays on, as a slow data channel would.
    """

    def __init__(self, *, capable: bool = True) -> None:
        self.capable = capable
        self.pc = RTCPeerConnection()
        self.pc.addTransceiver('audio', direction='sendrecv')
        self.pc.addTransceiver('video', direction='recvonly')
        self.channel = self.pc.createDataChannel('chat', ordered=True)
        self.channel.on('open', self._ready)
        self.channel.on('message', self._received)
        self.pc.on('track', self._track)
        self.pending: dict[str, tuple[int, int, int]] = {}
        self.arrived: dict[str, float] = {}
        """When each mark's message reached this side, on the segments' clock."""
        self.reported: dict[str, int] = {}
        self.position: tuple[int, int] | None = None
        """The source and RTP timestamp of the frame last delivered for playout."""
        self.playing = asyncio.Event()
        self.playing.set()
        self.since: int | None = None
        """The playout position when the output last started playing without a break."""
        self.held: list[dict[str, Any]] | None = None
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
            self.pending[report['mark']] = (report['source'], report['start'], report['rtp'])
            self.arrived[report['mark']] = time.perf_counter()

    def _track(self, track: Any) -> None:
        if track.kind == 'audio':
            self._tasks.append(asyncio.ensure_future(self._play(track)))

    def hold(self) -> None:
        self.held = []

    def release(self) -> None:
        held, self.held = self.held or [], None
        for report in held:
            self.send('client-message', {'t': 'playout', 'd': report})

    async def _play(self, track: Any) -> None:
        """Take each decoded frame for playout, and report the marks that played whole.

        aiortc hands frames out with the stream's first RTP timestamp taken
        off (its receiver's `TimestampMapper`); a browser reports the RTP
        timestamp itself, so it is put back here.
        """
        receiver = self.pc.getReceivers()[0]
        while True:
            frame = await track.recv()
            sources = receiver.getSynchronizationSources()
            origin = receiver._RTCRtpReceiver__timestamp_mapper._origin
            if not sources or origin is None:
                continue
            rtp = (origin + frame.pts) % 2**32
            self.position = (sources[0].source, rtp)
            if not self.playing.is_set():
                self.since = None
                continue
            if self.since is None:
                self.since = rtp
            for mark, (source, start, bound) in list(self.pending.items()):
                if source != self.position[0]:
                    continue
                if not passed(start, self.since):
                    del self.pending[mark]
                elif passed(rtp, bound):
                    report = {'mark': mark, 'source': source, 'rtp': rtp, 'since': self.since}
                    if self.held is None:
                        self.send('client-message', {'t': 'playout', 'd': report})
                    else:
                        self.held.append(report)
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

    def __init__(
        self, folder: Path, *, capable: bool = True, connection: Any = None, long: tuple[str, ...] = ()
    ) -> None:
        """`connection` is a server connection another peer already offered (a real browser); else an aiortc one.

        `long`: lines the voice says for 3 s (`Voice`).
        """
        self._long = long
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
        pipeline = Pipeline(
            [transport.input(), self.node, Voice(self._long), transport.output(), *self.wiring]
        )
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
        await asyncio.sleep(0.6)
        assert readback.delivered == 'unknown', ('playing again approved what began before it', readback)
        after = await line.say(READBACK)
        await line.until(lambda: after.delivered == 'delivered')


async def reports_that_prove_nothing(folder: Path):
    """W3."""
    async with Line(folder) as line:
        line.browser.playing.clear()
        readback = await line.say(READBACK)
        await line.until(lambda: readback.mark in line.browser.arrived)
        source, start, bound = line.browser.pending[readback.mark]
        other = open_other(folder)
        good = {'mark': readback.mark, 'source': source, 'rtp': bound, 'since': start}
        forged = [
            good | {'source': source + 1},
            good | {'rtp': (bound - 960) % 2**32},
            good | {'rtp': (bound + 2**31 + 1) % 2**32},
            good | {'since': (start + 960) % 2**32},
            good | {'mark': f'{other}:1:1~0'},
            good | {'rtp': str(bound)},
            {k: v for k, v in good.items() if k != 'since'},
        ]
        for report in forged:
            assert not line.adapter.played(report), report
        assert readback.delivered == 'unknown', readback
        assert line.adapter.played(good)
        assert readback.delivered == 'delivered'
        assert not line.adapter.played(good), 'counted twice'


def open_other(folder: Path) -> str:
    """Another call's id, for a mark name that is not this call's."""
    _, _, call = opened(desk_in(folder / 'other'))
    return call.id


async def an_interruption_cuts_only_what_was_still_to_send(folder: Path):
    """W4."""
    async with Line(folder) as line:
        cut = await line.say('[long] ' + READBACK)
        await asyncio.sleep(0.3)
        await line.onset()
        await line.worker.queue_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
        await line.until(lambda: cut.cleared)
        await asyncio.sleep(0.3)
        assert cut.mark not in line.browser.arrived and cut.delivered != 'delivered', cut
        assert cut.lost == 'cleared' and line.call.hearing(cut) == 'cut', (cut, line.call.hearing(cut))

        line.browser.hold()
        sent_whole = await line.say(READBACK)
        await line.until(lambda: sent_whole.mark in line.browser.reported)
        await asyncio.sleep(CUT_OFF_WINDOW + 0.1)
        await line.onset()
        await line.worker.queue_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(0.2)
        assert not sent_whole.cleared, (
            'a clear the browser never had voided audio it had all been sent',
            sent_whole,
        )
        assert line.call.hearing(sent_whole) == 'unconfirmed', line.call.hearing(sent_whole)
        line.browser.release()
        await line.until(lambda: sent_whole.delivered == 'delivered')
        assert line.call.hearing(sent_whole) == 'early', line.call.hearing(sent_whole)
        line.call.turn_started('Sí.')
        assert line.call.pending_question() is None, 'a report after the caller began to speak answered it'


async def a_reply_before_the_report_is_not_an_answer(folder: Path):
    """W5."""
    async with Line(folder) as line:
        line.browser.hold()
        readback = await line.say(READBACK)
        await line.until(lambda: readback.mark in line.browser.reported)
        await line.onset()
        line.browser.release()
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
        line.script.moves = [Call('resolve_ticket')]
        await line.node.serve('Sí.')
        assert line.desk.sent() == 1 and line.desk.ticket('T-101') == (2, 'resolved'), line.desk.sent()
        line.script.moves = [Call('resolve_ticket')]
        await line.node.serve('Sí, sí.')
        assert line.desk.sent() == 1, 'a second yes sent it again'


async def a_client_without_playout_is_known_at_once(folder: Path):
    """W7."""
    for capable in (True, False):
        async with Line(folder / str(capable), capable=capable) as line:
            started = time.perf_counter()
            assert await reports_playback(line.wiring, line.call) is capable, capable
            assert time.perf_counter() - started < 2, 'the capability was not known on client-ready'


async def a_segment_with_no_audio_is_never_heard(folder: Path):
    """W8: its mark passes the output with no audio before it, so it is never bounded, and no report can count."""
    async with Line(folder) as line:
        silent = await line.say('[silent] ' + READBACK)
        await line.until(lambda: silent.cleared)
        await asyncio.sleep(0.5)
        assert silent.lost == 'silent' and silent.mark not in line.browser.arrived, silent
        assert silent.delivered == 'unknown' and line.call.hearing(silent) == 'failed', silent


async def malformed_client_details_report_nothing(folder: Path):
    """W9: each `about` shape reaches the adapter's handler; none raises, none is a client that reports."""
    shapes = (None, [], 'playout', 7, {'platform_details': None}, {'platform_details': ['playout']})
    for index, about in enumerate(shapes):
        _, _, call = opened(desk_in(folder / str(index)))
        adapter = WebRTCPlayout(call, SimpleNamespace(add_event_handler=lambda *a: None))
        ready = {'label': 'rtvi-ai', 'type': 'client-ready', 'data': {'about': about}}
        await adapter._on_app_message(None, ready)
        assert adapter._decided.is_set() and await adapter.capability() is False, about
    _, _, call = opened(desk_in(folder / 'ok'))
    adapter = WebRTCPlayout(call, SimpleNamespace(add_event_handler=lambda *a: None))
    ready = {
        'label': 'rtvi-ai',
        'type': 'client-ready',
        'data': {'about': {'platform_details': {'playout': PLAYOUT}}},
    }
    await adapter._on_app_message(None, ready)
    assert await adapter.capability() is True


async def the_capability_is_truthful(folder: Path):
    """W10."""

    def twilio() -> Any:
        params = TwilioFrameSerializer.InputParams(auto_hang_up=False)
        serializer = TwilioFrameSerializer(stream_sid='MZ1', call_sid='CA1', params=params)
        return SimpleNamespace(_params=SimpleNamespace(serializer=serializer))

    def local() -> Any:
        return SimpleNamespace(_params=None)

    cases = [
        ('local', local, {}, False),
        ('local', local, {'simulate': True}, True),
        ('twilio', twilio, {}, True),
        ('twilio-no-marks', twilio, {'echoes_marks': False}, False),
        ('twilio-no-marks', twilio, {'echoes_marks': False, 'simulate': True}, True),
    ]
    for index, (name, transport, options, can) in enumerate(cases):
        _, _, call = opened(desk_in(folder / str(index)))
        wiring = playout(transport(), call, **options)
        assert call.transport == name, (call.transport, name)
        assert all(isinstance(p, LocalPlayout) for p in wiring), wiring
        assert await reports_playback(wiring, call) is can, (name, options)


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        for index, check in enumerate(
            (
                the_mark_is_not_the_evidence,
                a_browser_not_playing_hears_nothing,
                reports_that_prove_nothing,
                an_interruption_cuts_only_what_was_still_to_send,
                a_reply_before_the_report_is_not_an_answer,
                a_heard_yes_resolves_once,
                a_client_without_playout_is_known_at_once,
                a_segment_with_no_audio_is_never_heard,
                malformed_client_details_report_nothing,
                the_capability_is_truthful,
            )
        ):
            folder = Path(tmp) / str(index)
            for sub in ('', 'other', 'True', 'False', 'ok', *map(str, range(6))):
                (folder / sub).mkdir(parents=True, exist_ok=True)
            await check(folder)
    print('webrtc ok: W1-W10 on the real SmallWebRTC transport, an aiortc browser and the desk flow')


if __name__ == '__main__':
    asyncio.run(main())
