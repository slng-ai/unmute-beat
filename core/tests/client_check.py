"""The browser client's own JavaScript, in a real Chrome: `core/client.html` reports playback the server accepts.

Outside the suite, like `tests.defects`: it needs Google Chrome on this
machine, and it drives it headless over the DevTools protocol (the
`websockets` package Pipecat already brings). No network: Chrome and the
server talk over loopback, with a fake microphone.

The server is what `serve_client` and the runner's `/api/offer` give a bot:
core's client page at `/`, and Pipecat's `SmallWebRTCRequestHandler` for the
offer. Each offer runs the support desk's flow on the real adapter
(`tests.test_webrtc.Line`, no salon). Checked, in Chrome:

* C1: the page says, on `client-ready`, that it reports playback;
* C2: a read-back is heard only by the page's playout report, which
  arrives after the segment's audio went out, as the transport's mark;
* C3: through the flow, a heard read-back and a yes resolve the ticket once.

What headless Chrome cannot show: that a speaker played it. Chrome updates
the playout position as its mixer pulls audio for output; with no audio
device that may be on receipt instead (see `core.processor.WebRTCPlayout`).
The live manual check in the session report covers a real speaker.

Run: `uv run python -m tests.client_check` from `core/`. Exit 0 on success.
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

import uvicorn
import websockets
from fastapi import FastAPI
from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequest, SmallWebRTCRequestHandler

from core.processor import reports_playback, serve_client
from tests.test_webrtc import READBACK, Line
from tests.ticket_flow.worker import TO_READ_BACK

CHROME = os.getenv('CHROME', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class DevTools:
    """One Chrome page over the DevTools protocol: just `evaluate`."""

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


async def main() -> int:
    if not Path(CHROME).exists():
        print(f'client check skipped: no Chrome at {CHROME}')
        return 0
    offered: asyncio.Queue[Any] = asyncio.Queue()
    latency = 0.0
    app = FastAPI()
    serve_client(app)
    handler = SmallWebRTCRequestHandler()

    @app.post('/api/offer')
    async def offer(request: SmallWebRTCRequest) -> Any:
        async def connected(connection: Any) -> None:
            offered.put_nowait(connection)

        return await handler.handle_web_request(request=request, webrtc_connection_callback=connected)

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    serving = asyncio.create_task(server.serve())
    profile = Path(tempfile.mkdtemp())
    debug = free_port()
    chrome = subprocess.Popen(
        [
            CHROME,
            '--headless=new',
            f'--remote-debugging-port={debug}',
            f'--user-data-dir={profile}',
            '--use-fake-ui-for-media-stream',
            '--use-fake-device-for-media-stream',
            '--autoplay-policy=no-user-gesture-required',
            '--no-first-run',
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
        async with DevTools(pages[0]['webSocketDebuggerUrl']) as page:
            await page.call('Page.navigate', url=f'http://127.0.0.1:{port}/')
            await asyncio.sleep(1.0)
            assert await page.evaluate('document.title') == 'Voice call', 'core client page not served at /'
            await page.evaluate("document.getElementById('start').click()")
            connection = await asyncio.wait_for(offered.get(), 15)
            with tempfile.TemporaryDirectory() as folder:
                async with Line(Path(folder), connection=connection) as line:
                    assert await reports_playback(line.wiring), 'C1: the page did not say it reports playback'
                    readback = await line.say(READBACK)
                    await line.until(lambda: readback.delivered == 'delivered', 15)
                    assert readback.heard_by == 'mark', readback
                    [sent] = [
                        e['at']
                        for e in line.call._playback_log
                        if e['kind'] == 'sent' and e['mark'] == readback.mark
                    ]
                    latency = (readback.played_at or 0) - sent
                    reported = await page.evaluate("document.getElementById('heard').textContent")
                    assert 'Playback reported' in reported, ('C2: the page reported nothing', reported)
                    line.script.moves = list(TO_READ_BACK)
                    await line.node.serve('Resuelve el ticket T-101, cuenta ACC-1234.')
                    [copy] = [s for s in line.call.all_spoken if s.text == READBACK and s is not readback]
                    await line.until(lambda: copy.delivered == 'delivered', 15)
                    await asyncio.sleep(0.5)
                    await line.onset()
                    line.script.moves = [('call', 'resolve_ticket', {})]
                    await line.node.serve('Sí.')
                    assert line.desk.sent() == 1, (
                        'C3: the yes did not resolve the ticket once',
                        line.desk.sent(),
                    )
            await page.evaluate("document.getElementById('stop').click()")
    finally:
        chrome.terminate()
        chrome.wait(10)
        shutil.rmtree(profile, ignore_errors=True)
        server.should_exit = True
        await serving
    print(
        'client check ok: C1-C3, core/client.html in headless Chrome against the real adapter; '
        f"playout reported {latency * 1000:.0f} ms after the read-back's last audio left the server"
    )
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
