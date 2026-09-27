"""The documented browser entry point, offline: `uv run bot.py`, then calls from a page that behaves as core's does.

* BR1: `bot.py` run as a script (the runner's `main` stopped at the door)
  registers core's page before the runner's own routes: `/` goes to
  `/voice/`, Pipecat's own web client (`sdk.js`, built from the official
  packages) with core's playback reports (`playout.js`); the runner's
  prebuilt client stays at `/client/`;
* BR2: `run_bot` over Pipecat's SmallWebRTC transport, with a peer that
  plays the audio and reports playback as core's page does: the greeting is
  heard by the page's report, not by an estimate, with no simulation flag;
* BR3: a peer that reports no playback (the prebuilt client's
  `client-ready`) hears, before anything else, that this connection can
  make no changes, and the call ends: nobody is asked for details that
  could never be confirmed;
* BR4: a whole call, the caller speaking (macOS `say` audio into the
  page's microphone): the number read back and confirmed, the booking read
  back and confirmed; one booking, each read-back heard by the page's report;
* BR5: the same call with the page's output stopped while the number is read
  back, then playing again: that read-back is never heard, the yes to it
  confirms nothing, the number is read back again, and the yes to the copy
  that played whole confirms it; still one lookup and one booking.

The model is the offline brain (`evals.doubles`), and so are STT and TTS;
no provider is called. The Chrome check of the page itself is core's
`tests.client_check`. Every wait is bounded.
"""

import asyncio
import json
import runpy
import subprocess
import tempfile
import time
import wave
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import AudioStreamTrack
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pipecat.runner import run as runner
from pipecat.runner.types import SmallWebRTCRunnerArguments
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

import bot
from core.processor import PLAYOUT
from evals import doubles
from salon.diary import Diary

HERE = Path(__file__).resolve().parent.parent
PHONE = '+34699000001'
NUMBER, YES, BYE = "Yes, that's my number.", 'Yes please.', "No, that's all. Bye."


class Microphone(AudioStreamTrack):
    """The page's microphone: silence, or the words it was given, as 8 kHz audio from macOS `say`."""

    def __init__(self, cache: Path) -> None:
        super().__init__()
        self._cache = cache
        self._queued = np.zeros(0, dtype=np.int16)

    def say(self, words: str) -> None:
        path = self._cache / f'{abs(hash(words))}.wav'
        if not path.exists():
            spoken = ''.join(c for c in words if c.isalnum() or c.isspace())
            subprocess.run(['say', '-o', str(path), '--data-format=LEI16@8000', spoken], check=True)
        with wave.open(str(path)) as audio:
            pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16)
        self._queued = np.concatenate([self._queued, pcm])

    async def recv(self) -> Any:
        frame = await super().recv()
        if self._queued.size:
            chunk, self._queued = self._queued[: frame.samples], self._queued[frame.samples :]
            frame.planes[0].update(np.pad(chunk, (0, frame.samples - chunk.size)).tobytes())
        return frame


class Page:
    """The far end of a browser call, as core's page behaves, or as a client that reports nothing does.

    `playing` is the page's audio output: cleared, it is stopped while the
    receiver goes on. A mark is reported once the output played without a
    break from before its `start` until the playout position passed its bound.
    """

    def __init__(self, *, reports: bool, cache: Path | None = None) -> None:
        self.reports = reports
        self.microphone = Microphone(cache or Path(tempfile.mkdtemp()))
        self.pc = RTCPeerConnection()
        self.pc.addTransceiver(self.microphone, direction='sendrecv')
        self.pc.addTransceiver('video', direction='recvonly')
        self.channel = self.pc.createDataChannel('chat', ordered=True)
        self.channel.on('open', self._ready)
        self.channel.on('message', self._received)
        self.pc.on('track', self._track)
        self.pending: dict[str, tuple[int, int, int]] = {}
        self.playing = asyncio.Event()
        self.playing.set()
        self.since: int | None = None
        self._tasks: list[asyncio.Task] = []

    def _send(self, type_: str, data: Any) -> None:
        self.channel.send(json.dumps({'label': 'rtvi-ai', 'type': type_, 'id': 'x', 'data': data}))

    def _ready(self) -> None:
        about: dict[str, Any] = {'library': 'test-page'}
        if self.reports:
            about['platform_details'] = {'playout': PLAYOUT}
        self._send('client-ready', {'version': '2.1.0', 'about': about})

    def _received(self, raw: str) -> None:
        message = json.loads(raw)
        if message.get('type') == 'server-message' and message['data'].get('t') == 'playout-mark':
            report = message['data']['d']
            self.pending[report['mark']] = (report['source'], report['start'], report['rtp'])

    def _track(self, track: Any) -> None:
        if track.kind == 'audio':
            self._tasks.append(asyncio.ensure_future(self._play(track)))

    async def _play(self, track: Any) -> None:
        """Take each frame for playout; report each mark that played whole (aiortc strips the origin: put back)."""
        receiver = self.pc.getReceivers()[0]
        while True:
            frame = await track.recv()
            sources = receiver.getSynchronizationSources()
            origin = receiver._RTCRtpReceiver__timestamp_mapper._origin
            if not self.reports or not sources or origin is None:
                continue
            rtp = (origin + frame.pts) % 2**32
            if not self.playing.is_set():
                self.since = None
                continue
            if self.since is None:
                self.since = rtp
            for mark, (source, start, bound) in list(self.pending.items()):
                if source != sources[0].source:
                    continue
                if (start - self.since) % 2**32 >= 2**31:
                    del self.pending[mark]
                elif (rtp - bound) % 2**32 < 2**31:
                    report = {'mark': mark, 'source': source, 'rtp': rtp, 'since': self.since}
                    self._send('client-message', {'t': 'playout', 'd': report})
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


def the_script_serves_the_reporting_client():
    """BR1."""
    served: list[FastAPI] = []

    def stopped_main() -> None:
        served.append(runner.app)

    with patch.object(runner, 'main', stopped_main):
        runpy.run_path(str(HERE / 'bot.py'), run_name='__main__')
    [app] = served
    runner._setup_frontend_routes(app)
    client = TestClient(app)
    home = client.get('/', follow_redirects=False)
    assert home.status_code in (302, 307) and home.headers['location'] == '/voice/', home.status_code
    page = client.get('/voice/')
    assert page.status_code == 200 and 'reportPlayout' in page.text, (page.status_code, page.text[:200])
    playout = client.get('/voice/playout.js')
    assert playout.status_code == 200 and PLAYOUT in playout.text, (
        'the page does not announce playback reports'
    )
    sdk = client.get('/voice/sdk.js')
    assert sdk.status_code == 200 and 'javascript' in sdk.headers['content-type'], sdk.headers
    assert '@pipecat-ai/client-js 1.13.1' in sdk.text and 'SmallWebRTCTransport' in sdk.text, (
        'not the official client'
    )
    assert client.get('/client/').status_code == 200, 'the prebuilt client is gone'


async def a_browser_call(page: Page, **run: Any) -> tuple[Any, asyncio.Task]:
    """`run_bot` over SmallWebRTC with the offline doubles; returns the call once it is open, and the bot's task."""
    opened: list[Any] = []
    flow = doubles.flow()
    first_open = flow.open

    def recorded(*args: Any, **kwargs: Any) -> Any:
        call = first_open(*args, **kwargs)
        opened.append(call)
        return call

    flow.open = recorded  # ty: ignore[invalid-assignment]
    connection = await page.connect()
    transport = SmallWebRTCTransport(webrtc_connection=connection, params=bot.transport_params['webrtc']())
    arguments = SmallWebRTCRunnerArguments(webrtc_connection=connection, session_id=f'browser-{id(page)}')
    task = asyncio.create_task(
        bot.run_bot(transport, arguments, flow=flow, speech=doubles.speech, warm=False, trace=False, **run)
    )
    task.add_done_callback(lambda _: asyncio.ensure_future(page.close()))
    deadline = time.perf_counter() + 10
    while not opened:
        assert time.perf_counter() < deadline, 'the call never opened'
        await asyncio.sleep(0.01)
    return opened[0], task


async def until(done, limit: float = 10.0) -> None:
    deadline = time.perf_counter() + limit
    while not done():
        if time.perf_counter() > deadline:
            raise TimeoutError('the browser call never got there')
        await asyncio.sleep(0.01)


async def ended(task: asyncio.Task) -> None:
    task.cancel()
    with suppress(BaseException):
        await asyncio.wait_for(task, 10)


async def a_reporting_page_hears_the_greeting():
    """BR2."""
    call, task = await a_browser_call(Page(reports=True))
    try:
        await until(lambda: any(s.text == bot.GREETING for s in call.all_spoken))
        [greeting] = [s for s in call.all_spoken if s.text == bot.GREETING]
        await until(lambda: greeting.delivered == 'delivered')
        assert greeting.heard_by == 'mark' and not call.simulated_playout, greeting
        assert call.transport == 'webrtc' and bot.DELIVERY == 'marks', (call.transport, bot.DELIVERY)
    finally:
        await ended(task)


async def a_page_without_reports_is_told_at_once():
    """BR3."""
    call, task = await a_browser_call(Page(reports=False))
    try:
        await until(lambda: bool(call.all_spoken))
        said = [s.text for s in call.all_spoken]
        assert said[0] == call.locale.unconfirmable, said
        await asyncio.wait_for(asyncio.shield(task), 15)
        assert not any(s.text == bot.GREETING for s in call.all_spoken), 'the greeting went out anyway'
        assert 'unconfirmable' in [e['kind'] for e in call.endings], call.endings
    finally:
        await ended(task)


def sent(call: Any, segment: Any) -> bool:
    """Whether the segment's mark went out after its last audio."""
    return any(e['kind'] == 'sent' and e['mark'] == segment.mark for e in call._playback_log)


async def read_back(call: Any, seen: int) -> Any:
    """Wait for a read-back copy newer than the first `seen` segments, and for its audio to have gone out."""
    await until(lambda: any(s.proposal for s in call.all_spoken[seen:]), 20)
    copy = next(s for s in call.all_spoken[seen:] if s.proposal)
    await until(lambda: sent(call, copy), 20)
    return copy


async def a_booking_call(folder: Path, *, stop_during_number: bool) -> None:
    """BR4 and BR5: one call, the caller speaking after each read-back has gone out."""
    script = (NUMBER, NUMBER, YES, BYE) if stop_during_number else (NUMBER, YES, BYE)
    caller = doubles.booking(
        f'browser-{stop_during_number}',
        PHONE,
        'Tuesday',
        '10:00',
        script=tuple(
            doubles.Utterance((words,)) for words in ("Hi, I'd like a haircut on Tuesday at 10 am.", *script)
        ),
    )
    doubles.register(caller)
    doubles.begin(f'browser-{stop_during_number}', caller.name)
    diary = Diary(path=folder / 'salon.db', clock=doubles.CLOCK)
    page = Page(reports=True, cache=folder)
    with patch.object(bot, 'DIARY', diary):
        call, task = await a_browser_call(page, from_number=PHONE)
        try:
            await until(
                lambda: any(s.text == bot.GREETING and s.delivered == 'delivered' for s in call.all_spoken),
                15,
            )
            seen = len(call.all_spoken)
            page.microphone.say(caller.script[0].pieces[0])
            if stop_during_number:
                page.playing.clear()
            number = await read_back(call, seen)
            await asyncio.sleep(0.6)
            if stop_during_number:
                page.playing.set()
                await asyncio.sleep(0.6)
                assert number.delivered != 'delivered', ('heard with the output stopped', number)
                assert call.hearing(number) == 'unconfirmed', call.hearing(number)
                seen = len(call.all_spoken)
                page.microphone.say(NUMBER)
                again = await read_back(call, seen)
                assert call.hearing(number) == 'unconfirmed', ('missing evidence recorded as a cut', number)
                assert again.proposal == number.proposal or again.text == number.text, (again, number)
                assert not [
                    i for i in call.intents if i.operation == 'lookup_customer' and i.status != 'prepared'
                ]
                await until(lambda: again.delivered == 'delivered', 15)
                await asyncio.sleep(0.6)
            else:
                await until(lambda: number.delivered == 'delivered', 15)
            seen = len(call.all_spoken)
            page.microphone.say(NUMBER)
            booking = await read_back(call, seen)
            await until(lambda: booking.delivered == 'delivered', 15)
            await asyncio.sleep(0.6)
            seen = len(call.all_spoken)
            page.microphone.say(YES)
            await until(lambda: len(diary.bookings_for(PHONE, doubles.CLOCK.date())) == 1, 20)
            lookups = [i for i in call.intents if i.operation == 'lookup_customer']
            assert [i.status for i in lookups] == ['succeeded'], lookups
            assert booking.heard_by == 'mark' and not call.simulated_playout, booking
            await asyncio.sleep(1.0)
            assert len(diary.bookings_for(PHONE, doubles.CLOCK.date())) == 1, 'booked twice'
        finally:
            await ended(task)


async def main() -> None:
    the_script_serves_the_reporting_client()
    await a_reporting_page_hears_the_greeting()
    await a_page_without_reports_is_told_at_once()
    with tempfile.TemporaryDirectory() as folder:
        for stop in (False, True):
            (Path(folder) / str(stop)).mkdir()
            await a_booking_call(Path(folder) / str(stop), stop_during_number=stop)
    print(
        "browser ok: BR1-BR5, the documented entry point serves Pipecat's client with playback reports, refuses "
        "one without, and confirms by the page's reports only"
    )


if __name__ == '__main__':
    asyncio.run(main())
