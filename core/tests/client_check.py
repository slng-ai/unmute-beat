"""Core's browser page in a real Chrome: Pipecat's own web client, with core's playback reports, against the real adapter.

Outside the suite, like `tests.defects`: it needs Google Chrome on this
machine, and it drives it headless over the DevTools protocol (the
`websockets` package Pipecat already brings). No network: Chrome and the
server talk over loopback, with a fake microphone.

The server is what a bot's launcher gives the page: `serve_client` (core's
page at `/voice/`, `/` sent there) and the runner's `/api/offer` routes
(Pipecat's `SmallWebRTCRequestHandler`, POST for the offer, PATCH for ICE
candidates). The page is `core/client/index.html`: `PipecatClient` and
`SmallWebRTCTransport` from the official packages (`client/sdk.js`) do the
connection and the microphone, and `client/playout.js` reports playback.
Each call runs the support desk's flow on the real adapter
(`tests.test_webrtc.Line`, no salon). Checked, in Chrome:

* C1: the native client says, in its own `client-ready`, that it reports playback;
* C2: a read-back is heard only by the page's playout report, which arrives
  after the segment's audio went out, as the transport's mark;
* C3: through the flow, a heard read-back and a yes resolve the ticket once;
* C4: the audio element paused before the read-back: nothing is heard, and a
  yes resolves nothing;
* C5: paused in the middle of a read-back and played again: that read-back
  is never heard, and a yes resolves nothing; the next one is heard;
* C6: the output detached (its stream taken away, or the element taken out
  of the page): nothing is heard;
* C7: autoplay blocked (Chrome's strict policy needs a user gesture for every
  play, and the call is started without one): nothing is heard;
* C8: an interruption in the middle of a read-back: never heard, read `cut`.

What headless Chrome cannot show: that a speaker played it. Its audio output
is a fake device. The live manual check in the session report covers a
real speaker.

Run: `uv run python -m tests.client_check` from `core/`. Exit 0 on success.
"""

import asyncio
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
import websockets
from fastapi import FastAPI
from pipecat.frames.frames import InterruptionFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

from core.processor import reports_playback, serve_client
from tests.script import Call
from tests.test_webrtc import READBACK, Line
from tests.ticket_flow.worker import TO_READ_BACK

CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
BOT = "document.getElementById('bot')"
RUN_IN = 0.5
"""How long the page plays before a check speaks: a segment counts only if its output's unbroken run began before it."""
QUIET = 1.5
"""How long a read-back that must not be heard is given: several times the time a heard one takes to report."""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class DevTools:
    """One Chrome page over the DevTools protocol: just `call` and `evaluate`."""

    def __init__(self, socket_url: str) -> None:
        self._url, self._id = socket_url, 0

    async def __aenter__(self) -> DevTools:
        self._ws = await websockets.connect(self._url, max_size=None)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._ws.close()

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        self._id += 1
        await self._ws.send(json.dumps({'id': self._id, 'method': method, 'params': params}))
        while True:
            reply = json.loads(await self._ws.recv())
            if reply.get('id') == self._id:
                return reply

    async def evaluate(self, expression: str) -> Any:
        reply = await self.call(
            'Runtime.evaluate', expression=expression, returnByValue=True, awaitPromise=True
        )
        return reply.get('result', {}).get('result', {}).get('value')

    async def until(self, expression: str, limit: float = 10.0) -> None:
        deadline = time.perf_counter() + limit
        while not await self.evaluate(expression):
            if time.perf_counter() > deadline:
                raise TimeoutError(f'the page never got to: {expression}')
            await asyncio.sleep(0.05)


@asynccontextmanager
async def chrome(*flags: str):
    """A headless Chrome with a fake microphone and speaker; yields its first page's DevTools socket URL."""
    profile = Path(tempfile.mkdtemp())
    debug = free_port()
    process = subprocess.Popen(
        [
            CHROME,
            '--headless=new',
            f'--remote-debugging-port={debug}',
            f'--user-data-dir={profile}',
            '--use-fake-ui-for-media-stream',
            '--use-fake-device-for-media-stream',
            '--no-first-run',
            *flags,
            'about:blank',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.perf_counter() + 15
        pages: list[dict[str, Any]] = []
        while not pages:
            if time.perf_counter() > deadline:
                raise TimeoutError('Chrome never opened its DevTools port')
            await asyncio.sleep(0.2)
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{debug}/json/list', timeout=1) as listing:
                    pages = [p for p in json.load(listing) if p.get('type') == 'page']
            except OSError:
                continue
        yield pages[0]['webSocketDebuggerUrl']
    finally:
        process.terminate()
        process.wait(10)
        shutil.rmtree(profile, ignore_errors=True)


def sent(line: Line, segment: Any) -> bool:
    """Whether the segment's mark went out after its last audio: from then on, a report could come."""
    return any(e['kind'] == 'sent' and e['mark'] == segment.mark for e in line.call._playback_log)


class Checks:
    """The server, and one call per check: each starts from the page's own Start button and ends with Hang up."""

    def __init__(self, port: int, offered: asyncio.Queue[Any], folder: Path) -> None:
        self.port, self.offered, self.folder = port, offered, folder
        self.calls = 0
        self.latency = 0.0

    @asynccontextmanager
    async def call(self, page: DevTools, *, playing: bool = True, long: tuple[str, ...] = ()):
        """One call from the page. `playing`: wait until the page's audio element is playing the bot."""
        self.calls += 1
        await page.until(f"!document.getElementById('start').disabled && {BOT} !== null")
        await page.evaluate("document.getElementById('start').click()")
        connection = await asyncio.wait_for(self.offered.get(), 15)
        folder = self.folder / str(self.calls)
        folder.mkdir()
        async with Line(folder, connection=connection, long=long) as line:
            await line.until(lambda: line.adapter._sender() is not None, 15)
            if playing:
                await page.until(f'{BOT}.srcObject !== null && !{BOT}.paused', 15)
                await asyncio.sleep(RUN_IN)
            yield line
            await page.evaluate("document.getElementById('stop').click()")

    async def confirmed(self, page: DevTools) -> None:
        """C1-C3."""
        async with self.call(page) as line:
            assert await reports_playback(line.wiring, line.call), (
                'C1: the page did not say it reports playback'
            )
            readback = await line.say(READBACK)
            await line.until(lambda: readback.delivered == 'delivered', 15)
            assert readback.heard_by == 'mark', readback
            [sent] = [
                e['at'] for e in line.call._playback_log if e['kind'] == 'sent' and e['mark'] == readback.mark
            ]
            self.latency = (readback.played_at or 0) - sent
            reported = await page.evaluate("document.getElementById('heard').textContent")
            assert 'Playback reported' in reported, ('C2: the page reported nothing', reported)
            await self.resolves(line, 1)

    async def resolves(self, line: Line, times: int, meanwhile: Any = None) -> None:
        """The desk flow: resolve read back, the caller's yes after it; the ticket is resolved `times` times.

        `meanwhile`: done to the page while the read-back plays.
        """
        line.script.moves = list(TO_READ_BACK)
        await line.node.serve('Resuelve el ticket T-101, cuenta ACC-1234.')
        copy = [s for s in line.call.all_spoken if s.text == READBACK][-1]
        if meanwhile is not None:
            await meanwhile()
        if times:
            await line.until(lambda: copy.delivered == 'delivered', 15)
        else:
            await line.until(lambda: sent(line, copy), 15)
            await asyncio.sleep(QUIET)
            assert copy.delivered != 'delivered', ('heard with no output playing', copy)
        await asyncio.sleep(0.5)
        await line.onset()
        line.script.moves = [Call('resolve_ticket')]
        await line.node.serve('Sí.')
        assert line.desk.sent() == times, (
            'the yes resolved the ticket',
            line.desk.sent(),
            'times, not',
            times,
        )

    async def paused(self, page: DevTools) -> None:
        """C4."""
        async with self.call(page) as line:
            await page.evaluate(f'{BOT}.pause()')
            assert await page.evaluate(f'{BOT}.paused')
            await self.resolves(line, 0)

    async def paused_midway(self, page: DevTools) -> None:
        """C5: the flow's own read-back lasts 3 s here; the output stops 1 s in, for 0.4 s."""

        async def pause_and_play() -> None:
            await asyncio.sleep(1.0)
            await page.evaluate(f'{BOT}.pause()')
            await asyncio.sleep(0.4)
            await page.evaluate(f'{BOT}.play()')
            assert await page.evaluate(f'!{BOT}.paused'), 'the page did not play again'

        async with self.call(page, long=(READBACK,)) as line:
            await self.resolves(line, 0, meanwhile=pause_and_play)
            after = await line.say(READBACK)
            await line.until(lambda: after.delivered == 'delivered', 15)

    async def detached(self, page: DevTools) -> None:
        """C6."""
        for detach in (f'{BOT}.srcObject = null', f'{BOT}.remove()'):
            async with self.call(page) as line:
                await page.evaluate(detach)
                readback = await line.say(READBACK)
                await line.until(lambda readback=readback: sent(line, readback), 10)
                await asyncio.sleep(QUIET)
                assert readback.delivered != 'delivered', (detach, readback)
            await page.call('Page.reload')
            await page.until("document.readyState === 'complete'")

    async def interrupted(self, page: DevTools) -> None:
        """C8."""
        async with self.call(page) as line:
            cut = await line.say('[long] ' + READBACK)
            await asyncio.sleep(1.0)
            await line.worker.queue_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
            await line.until(lambda: cut.cleared, 10)
            await asyncio.sleep(QUIET)
            assert cut.delivered != 'delivered' and line.call.hearing(cut) == 'cut', cut


async def blocked(checks: Checks, url: str) -> None:
    """C7, in a Chrome whose autoplay policy needs a user gesture for every play: the call is started with none.

    Chrome's default policy lets a page that holds the microphone autoplay its
    stream, so the strict `user-gesture-required` policy stands for a browser
    that blocks it.
    """
    policy = '--autoplay-policy=user-gesture-required'
    async with chrome(policy) as socket_url, DevTools(socket_url) as page:
        await page.call('Page.navigate', url=url)
        await page.until("document.readyState === 'complete'")
        async with checks.call(page, playing=False) as line:
            await page.until(f'{BOT}.srcObject !== null', 15)
            readback = await line.say(READBACK)
            await line.until(lambda: sent(line, readback), 10)
            await asyncio.sleep(QUIET)
            assert await page.evaluate(f'{BOT}.paused'), 'C7: this Chrome did not block autoplay'
            assert readback.delivered != 'delivered', ('heard while autoplay was blocked', readback)


async def main() -> int:
    if not Path(CHROME).exists():
        print(f'client check skipped: no Chrome at {CHROME}')
        return 0
    offered: asyncio.Queue[Any] = asyncio.Queue()
    app = FastAPI()
    serve_client(app)
    handler = SmallWebRTCRequestHandler()

    @app.post('/api/offer')
    async def offer(request: SmallWebRTCRequest) -> Any:
        async def connected(connection: Any) -> None:
            offered.put_nowait(connection)

        return await handler.handle_web_request(request=request, webrtc_connection_callback=connected)

    @app.patch('/api/offer')
    async def candidate(request: SmallWebRTCPatchRequest) -> Any:
        await handler.handle_patch_request(request)
        return {'status': 'success'}

    port = free_port()
    url = f'http://127.0.0.1:{port}/'
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    serving = asyncio.create_task(server.serve())
    try:
        with tempfile.TemporaryDirectory() as folder:
            checks = Checks(port, offered, Path(folder))
            policy = '--autoplay-policy=no-user-gesture-required'
            async with chrome(policy) as socket_url, DevTools(socket_url) as page:
                await page.call('Page.navigate', url=url)
                await page.until("document.readyState === 'complete'")
                where = await page.evaluate('[document.title, location.pathname]')
                assert where == ['Voice call', '/voice/'], ('core page not served from /', where)
                await checks.confirmed(page)
                await checks.paused(page)
                await checks.paused_midway(page)
                await checks.detached(page)
                await checks.interrupted(page)
            await blocked(checks, url)
    finally:
        server.should_exit = True
        await serving
    print(
        'client check ok: C1-C8; Pipecat client-js 1.13.1 and small-webrtc-transport 1.10.8 with '
        f"core's playout.js in headless Chrome, against the real adapter; playout reported "
        f"{checks.latency * 1000:.0f} ms after the read-back's last audio left the server"
    )
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
