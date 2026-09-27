"""The documented browser entry point, offline: `uv run bot.py`, then a call from the page it serves.

* BR1: `bot.py` run as a script (the runner's `main` stopped at the door)
  registers core's client at `/` before the runner's own routes, so the page
  a developer opens reports playback; the runner's prebuilt client stays at
  `/client/`;
* BR2: `run_bot` over Pipecat's SmallWebRTC transport, with a peer that
  plays the audio and reports playback as `core/client.html` does: the
  greeting goes out, and it is heard by the page's report, not by an
  estimate, with no simulation flag;
* BR3: a peer that reports no playback (the prebuilt client's
  `client-ready`) hears, before anything else, that this connection can
  make no changes, and the call ends: nobody is asked for details that
  could never be confirmed.

The speech services and the model are the offline doubles (`evals.doubles`);
no provider is called. Every wait is bounded.
"""

import asyncio
import json
import runpy
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import patch

from aiortc import RTCPeerConnection, RTCSessionDescription
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pipecat.runner import run as runner
from pipecat.runner.types import SmallWebRTCRunnerArguments
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

import bot
from core.processor import PLAYOUT
from evals import doubles

HERE = Path(__file__).resolve().parent.parent


class Page:
    """The far end of a browser call, as `core/client.html` behaves, or as a client that reports nothing does."""

    def __init__(self, *, reports: bool) -> None:
        self.reports = reports
        self.pc = RTCPeerConnection()
        self.pc.addTransceiver('audio', direction='sendrecv')
        self.pc.addTransceiver('video', direction='recvonly')
        self.channel = self.pc.createDataChannel('chat', ordered=True)
        self.channel.on('open', self._ready)
        self.channel.on('message', self._received)
        self.pc.on('track', self._track)
        self.pending: dict[str, tuple[int, int]] = {}
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
            self.pending[report['mark']] = (report['source'], report['rtp'])

    def _track(self, track: Any) -> None:
        if track.kind == 'audio':
            self._tasks.append(asyncio.ensure_future(self._play(track)))

    async def _play(self, track: Any) -> None:
        """Take each frame for playout; report each mark its RTP timestamp passed (aiortc strips the origin: put back)."""
        receiver = self.pc.getReceivers()[0]
        while True:
            frame = await track.recv()
            sources = receiver.getSynchronizationSources()
            origin = receiver._RTCRtpReceiver__timestamp_mapper._origin
            if not self.reports or not sources or origin is None:
                continue
            rtp = (origin + frame.pts) % 2**32
            for mark, (source, bound) in list(self.pending.items()):
                if source == sources[0].source and (rtp - bound) % 2**32 < 2**31:
                    self._send(
                        'client-message', {'t': 'playout', 'd': {'mark': mark, 'source': source, 'rtp': rtp}}
                    )
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
    page = client.get('/', follow_redirects=False)
    assert page.status_code == 200 and 'getSynchronizationSources' in page.text, (
        page.status_code,
        page.text[:200],
    )
    assert PLAYOUT in page.text, 'the page served at / does not announce playback reports'
    assert client.get('/client/').status_code == 200, 'the prebuilt client is gone'


async def a_browser_call(reports: bool) -> tuple[Any, asyncio.Task]:
    """`run_bot` over SmallWebRTC with the offline doubles; returns the call once it is open, and the bot's task."""
    opened: list[Any] = []
    flow = doubles.flow()
    first_open = flow.open

    def recorded(*args: Any, **kwargs: Any) -> Any:
        call = first_open(*args, **kwargs)
        opened.append(call)
        return call

    flow.open = recorded  # ty: ignore[invalid-assignment]
    page = Page(reports=reports)
    connection = await page.connect()
    transport = SmallWebRTCTransport(webrtc_connection=connection, params=bot.transport_params['webrtc']())
    arguments = SmallWebRTCRunnerArguments(webrtc_connection=connection, session_id=f'browser-{reports}')
    task = asyncio.create_task(
        bot.run_bot(transport, arguments, flow=flow, speech=doubles.speech, warm=False, trace=False)
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


async def a_reporting_page_hears_the_greeting():
    """BR2."""
    call, task = await a_browser_call(reports=True)
    try:
        await until(lambda: any(s.text == bot.GREETING for s in call.all_spoken))
        [greeting] = [s for s in call.all_spoken if s.text == bot.GREETING]
        await until(lambda: greeting.delivered == 'delivered')
        assert greeting.heard_by == 'mark' and not call.simulated_playout, greeting
        assert call.transport == 'webrtc' and bot.DELIVERY == 'marks', (call.transport, bot.DELIVERY)
    finally:
        task.cancel()
        with suppress(BaseException):
            await asyncio.wait_for(task, 10)


async def a_page_without_reports_is_told_at_once():
    """BR3."""
    call, task = await a_browser_call(reports=False)
    try:
        await until(lambda: bool(call.all_spoken))
        said = [s.text for s in call.all_spoken]
        assert said[0] == call.locale.unconfirmable, said
        await asyncio.wait_for(asyncio.shield(task), 15)
        assert not any(s.text == bot.GREETING for s in call.all_spoken), 'the greeting went out anyway'
        assert 'unconfirmable' in [e['kind'] for e in call.endings], call.endings
    finally:
        task.cancel()
        with suppress(BaseException):
            await asyncio.wait_for(task, 10)


async def main() -> None:
    the_script_serves_the_reporting_client()
    await a_reporting_page_hears_the_greeting()
    await a_page_without_reports_is_told_at_once()
    print(
        'browser ok: BR1-BR3, the documented entry point serves a reporting client and refuses one that is not'
    )


if __name__ == '__main__':
    asyncio.run(main())
