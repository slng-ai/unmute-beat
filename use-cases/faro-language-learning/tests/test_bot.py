"""Faro's documented browser entry point, offline: `uv run bot.py`, and a page that reports playback or does not.

* B1: `bot.py` run as a script (the runner's `main` stopped at the door)
  serves core's page at `/voice/`, with its playback reports, and sends `/`
  there;
* B2: `run_bot` over Pipecat's SmallWebRTC transport, with a page that
  declares playback reports: the greeting is said, with the carrier's number
  read back as its question;
* B3: a page that reports no playback hears, before anything else, that the
  line cannot confirm anything, and the call ends: no greeting, no number
  read back, nothing looked up.

No model and no speech provider runs: the step agents are a script that is
never asked, and speech is a pass-through.
"""

import asyncio
import json
import os
import runpy
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import patch

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import AudioStreamTrack
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.runner import run as runner
from pipecat.runner.types import SmallWebRTCRunnerArguments
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pydantic_ai.models.function import FunctionModel

import faro
from core.processor import PLAYOUT
from faro import curriculum

HERE = Path(__file__).resolve().parent.parent
EMMA = '+34600555666'


class PassThrough(FrameProcessor):
    """Speech with no provider: frames go straight through."""

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


def speech() -> tuple[FrameProcessor, FrameProcessor]:
    return PassThrough(), PassThrough()


async def never(messages, info):
    raise AssertionError('no step agent runs before the caller speaks')
    yield ''


class Page:
    """The far end of a browser call: says `client-ready`, declaring playback reports or not."""

    def __init__(self, *, reports: bool) -> None:
        self.reports = reports
        self.pc = RTCPeerConnection()
        self.pc.addTransceiver(AudioStreamTrack(), direction='sendrecv')
        self.pc.addTransceiver('video', direction='recvonly')
        self.channel = self.pc.createDataChannel('chat', ordered=True)
        self.channel.on('open', self._ready)

    def _ready(self) -> None:
        about: dict[str, Any] = {'library': 'test-page'}
        if self.reports:
            about['platform_details'] = {'playout': PLAYOUT}
        message = {
            'label': 'rtvi-ai',
            'type': 'client-ready',
            'id': 'x',
            'data': {'version': '2.1.0', 'about': about},
        }
        self.channel.send(json.dumps(message))

    async def connect(self) -> SmallWebRTCConnection:
        await self.pc.setLocalDescription(await self.pc.createOffer())
        server = SmallWebRTCConnection(ice_servers=[])
        await server.initialize(sdp=self.pc.localDescription.sdp, type='offer')
        answer = server.get_answer()
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'], type=answer['type']))
        return server


def the_script_serves_the_reporting_client():
    """B1."""
    served: list[FastAPI] = []
    with patch.object(runner, 'main', lambda: served.append(runner.app)):
        runpy.run_path(str(HERE / 'bot.py'), run_name='__main__')
    [app] = served
    client = TestClient(app)
    home = client.get('/', follow_redirects=False)
    assert home.status_code in (302, 307) and home.headers['location'] == '/voice/', home.status_code
    assert PLAYOUT in client.get('/voice/playout.js').text, 'the page does not announce playback reports'


async def a_browser_call(page: Page) -> tuple[Any, asyncio.Task]:
    """`run_bot` over SmallWebRTC with no provider; returns the call once it is open, and the bot's task."""
    import bot

    opened: list[Any] = []
    flow = faro.SPEC.compile(model=FunctionModel(stream_function=never))
    first_open = flow.open

    def recorded(*args: Any, **kwargs: Any) -> Any:
        call = first_open(*args, **kwargs)
        opened.append(call)
        return call

    flow.open = recorded  # ty: ignore[invalid-assignment]
    connection = await page.connect()
    transport = SmallWebRTCTransport(webrtc_connection=connection, params=bot.transport_params['webrtc']())
    arguments = SmallWebRTCRunnerArguments(webrtc_connection=connection, session_id=f'faro-{id(page)}')
    task = asyncio.create_task(
        bot.run_bot(transport, arguments, flow=flow, speech=speech, warm=False, trace=False)
    )
    task.add_done_callback(lambda _: asyncio.ensure_future(page.pc.close()))
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


async def a_reporting_page_is_greeted_with_the_read_back():
    """B2."""
    call, task = await a_browser_call(Page(reports=True))
    try:
        await until(lambda: bool(call.all_spoken))
        first = call.all_spoken[0]
        assert first.text.startswith(curriculum.GREETING) and curriculum.spaced(EMMA) in first.text, (
            first.text
        )
        assert call.open_question() is not None and call.transport == 'webrtc'
    finally:
        await ended(task)


async def a_page_without_reports_is_told_at_once():
    """B3."""
    call, task = await a_browser_call(Page(reports=False))
    try:
        await until(lambda: bool(call.all_spoken))
        said = [s.text for s in call.all_spoken]
        assert said[0] == call.locale.unconfirmable, said
        await asyncio.wait_for(asyncio.shield(task), 15)
        assert not any(curriculum.GREETING in text for text in said), 'the greeting went out anyway'
        assert 'unconfirmable' in [e['kind'] for e in call.endings], call.endings
        assert not call.state.facts.learner_id and not call.evidence, 'nothing was looked up'
    finally:
        await ended(task)


async def main() -> None:
    the_script_serves_the_reporting_client()
    import bot  # noqa: F401  (loads the repository's .env first, so nothing below can be overridden by it)

    os.environ['CALLER_ID'] = EMMA
    os.environ['LEARNING_DB_PATH'] = str(Path(tempfile.mkdtemp()) / 'faro.sqlite3')
    await a_reporting_page_is_greeted_with_the_read_back()
    await a_page_without_reports_is_told_at_once()
    print('bot ok: B1-B3, core page served, a reporting page greeted, one that reports nothing refused first')


if __name__ == '__main__':
    asyncio.run(main())
