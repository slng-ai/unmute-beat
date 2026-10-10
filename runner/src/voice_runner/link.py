"""The runner's one link to the studio: a websocket that says who this runner is, how full it is, and every event.

The studio is the only writer of Redis and Postgres; a runner only sends. On connect it says hello
(its id, its URL, its capacity, its agents), then a stats line every `STATS_EVERY` seconds and each
call's events as they come. The link reconnects on its own; while it is down, events wait in a
bounded buffer, and past it the oldest are dropped and counted, never a turn slowed.

    runner -> studio   {"hello": {...}}  {"stats": {...}}  {"event": {...}}

A runner with no studio (`--studio ''`) sends nothing and carries calls all the same.
"""

import asyncio
import contextlib
import json
from collections import deque
from collections.abc import Callable
from typing import Any

import websockets
from loguru import logger

from core.events import _Event


class Link:
    """One websocket to the studio, kept up on its own, with events buffered while it is down.

    `BUFFER` events are kept while the studio is unreachable; past it the oldest go first and are counted.
    """

    BUFFER = 20_000
    STATS_EVERY = 2.0
    RETRY_SECONDS = 1.0

    def __init__(
        self, url: str, hello: Callable[[], dict[str, Any]], stats: Callable[[], dict[str, Any]]
    ) -> None:
        self.url = url
        self.hello = hello
        self.stats = stats
        self.dropped = 0
        self.connected = False
        self._buffer: deque[str] = deque()
        self._ready = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start linking, when there is a studio to link to."""
        if self.url and self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self, timeout: float = 3.0) -> None:
        """Send what is buffered, within `timeout`, then close."""
        if self._task is None:
            return
        deadline = asyncio.get_running_loop().time() + timeout
        while self._buffer and self.connected and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    def send(self, event: _Event) -> None:
        """Queue one event for the studio. Never waits."""
        if not self.url:
            return
        if len(self._buffer) >= self.BUFFER:
            self._buffer.popleft()
            self.dropped += 1
        self._buffer.append(json.dumps({'event': event.model_dump(mode='json')}))
        self._ready.set()

    async def _run(self) -> None:
        """Link, say hello, pump; on a drop, say so once and try again."""
        said_down = False
        while True:
            try:
                async with websockets.connect(self.url, max_size=None, ping_interval=10) as socket:
                    self.connected, said_down = True, False
                    logger.info('linked to the studio at {}', self.url)
                    await socket.send(json.dumps({'hello': self.hello()}))
                    await self._pump(socket)
            except (OSError, websockets.WebSocketException) as error:
                if not said_down:
                    logger.warning('no studio at {} ({}): events wait, and the link retries', self.url, error)
                    said_down = True
            finally:
                self.connected = False
            await asyncio.sleep(self.RETRY_SECONDS)

    async def _pump(self, socket: Any) -> None:
        """Stats every `STATS_EVERY` seconds, and every buffered event as soon as it comes."""
        loop = asyncio.get_running_loop()
        next_stats = 0.0
        while True:
            if loop.time() >= next_stats:
                await socket.send(json.dumps({'stats': self.stats() | {'dropped': self.dropped}}))
                next_stats = loop.time() + self.STATS_EVERY
            while self._buffer:
                await socket.send(self._buffer[0])
                self._buffer.popleft()
            self._ready.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._ready.wait(), timeout=max(0.05, next_stats - loop.time()))
