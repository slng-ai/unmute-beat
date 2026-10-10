"""The runner itself: its agents compiled and warmed once, the calls it carries, and how it stops.

Each process compiles every flow once and warms its model clients once, then carries up to
`max_calls` calls at a time on one asyncio loop. One that is warming, full or stopping says so on
`/ready` and is given no new calls. It never writes a store: each call's events go to the studio over
the runner's one link (`voice_runner.link`).

Text is `core.text_turn`: each reply's segments are acknowledged once the response carrying them is
sent, which is the text runtime's simulated delivery. Voice is Pipecat's SmallWebRTC, run to its end
by `core.launch.run_call`. Stopping is the runner's own: it stops admitting, lets its calls run to
their end for at most `drain_seconds`, and only then lets uvicorn stop. A second SIGTERM stops at once.
"""

import asyncio
import hashlib
import os
import signal
import statistics
import time
from collections import deque
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import HTTPException
from loguru import logger
from opentelemetry import trace
from pipecat.runner.types import SmallWebRTCRunnerArguments
from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequestHandler
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

import core
from core.events import RunEnded
from core.graph import model_clients
from core.launch import run_call, transport_params
from voice_runner.agents import Found
from voice_runner.calls import Carried, Forward
from voice_runner.link import Link
from voice_runner.settings import CallRequest, Settings


class ConfigFiles:
    """An agent's config files on disk, as the studio versions them: a digest that changes with any save."""

    TEXT = frozenset({'.yaml', '.yml', '.md', '.json', '.txt', '.hbs'})

    def __init__(self, manifest: Path) -> None:
        self.manifest = manifest

    def digest(self) -> str:
        """A digest of the manifest and the text files beside it, as they are now."""
        folder = self.manifest.parent
        digest = hashlib.sha256()
        files = (
            path
            for path in sorted(folder.rglob('*'))
            if path.is_file() and path.suffix in self.TEXT and '__pycache__' not in path.parts
        )
        for path in files:
            digest.update(str(path.relative_to(folder)).encode() + b'\0' + path.read_bytes() + b'\0')
        return digest.hexdigest()


class Runner:
    """The process's calls, agents and readiness.

    `ADMIT_SECONDS` is how long an admitted voice call may wait for its browser's offer before its place
    is given back. `loaded` is the config version each agent's flow was last asked for, as the studio
    named it; `compiled` what each agent's config files held when its flow was compiled, so a save since
    means compiling again.
    """

    ADMIT_SECONDS = 30.0
    LAG_EVERY = 0.05
    FORGET_SECONDS = 3600.0

    def __init__(self, settings: Settings, found: list[Found]) -> None:
        self.settings = settings
        self.found = found
        self.entries = {each.entry.id: each.entry for each in found if each.entry is not None}
        self.flows: dict[str, core.StepFlow] = {}
        self.broken: dict[str, str] = {each.package: each.error for each in found if each.entry is None}
        self.calls: dict[str, Carried] = {}
        self.ready = False
        self.stopping = False
        self.warmed: list[dict[str, Any]] = []
        self.lag: deque[float] = deque(maxlen=1200)
        self.started = time.time()
        self.loaded: dict[str, str] = {}
        self.compiled = {
            agent: ConfigFiles(entry.manifest).digest()
            for agent, entry in self.entries.items()
            if entry.manifest
        }
        self.stack: AsyncExitStack | None = None
        self.link = Link(settings.studio, self.hello, self.stats)
        ice = [IceServer(**server) for server in settings.ice_servers]
        self.webrtc = SmallWebRTCRequestHandler(ice_servers=ice or None)
        self.tracer = trace.get_tracer('slng.runner')

    @property
    def active(self) -> list[Carried]:
        """The calls not over yet."""
        return [carried for carried in self.calls.values() if carried.status != 'ended']

    def full(self) -> bool:
        """Whether it carries as many calls as it may."""
        return len(self.active) >= self.settings.max_calls

    def hello(self) -> dict[str, Any]:
        """What it says when it links: who it is, where, how many calls, which agents."""
        return {
            'runner': self.settings.runner_id,
            'url': self.settings.url,
            'pid': os.getpid(),
            'max_calls': self.settings.max_calls,
            'agents': sorted(self.entries),
            'frameworks': ['pipecat'],
        }

    def stats(self) -> dict[str, Any]:
        """How it stands: ready or not, its calls, its loop lag."""
        samples = sorted(self.lag)
        return {
            'runner': self.settings.runner_id,
            'url': self.settings.url,
            'ready': self.ready and not self.stopping and not self.full(),
            'warming': not self.ready,
            'stopping': self.stopping,
            'active': len(self.active),
            'max_calls': self.settings.max_calls,
            'calls': [carried.summary() for carried in self.active],
            'lag_p50_ms': round(1000 * statistics.median(samples), 2) if samples else None,
            'lag_max_ms': round(1000 * samples[-1], 2) if samples else None,
            'uptime_s': round(time.time() - self.started, 1),
        }

    async def flow_for(self, agent: str, config: str) -> core.StepFlow:
        """The agent's flow for a call on this config version: compiled again when the studio saved a new one.

        Whether the config changed is read from the files themselves, not from the version a call
        names: a save between this runner's start and its first call is a new config too, though it
        is the first version the runner hears of. A changed one is compiled from the manifest on
        disk; calls already running keep the flow they started on. Only config is read again, never
        the application's code.

        ponytail: each recompiled flow's model clients stay open for the process's life. Close the
        old flow's clients once its last call ends if editors ever save hundreds of times a day.
        """
        entry = self.entries[agent]
        if config and self.loaded.get(agent) != config and entry.manifest is not None:
            now = ConfigFiles(entry.manifest).digest()
            if now != self.compiled.get(agent):
                flow = await asyncio.to_thread(entry.recompile)
                if self.stack is not None:
                    await self.stack.enter_async_context(model_clients(flow.nodes))
                self.flows[agent], self.compiled[agent] = flow, now
                logger.info('agent {} compiled again for config {}', agent, config)
            self.loaded[agent] = config
        return self.flows[agent]

    async def warm(self, stack: AsyncExitStack) -> None:
        """Compile every flow and, when asked, open and warm its model clients once, for the process's life."""
        self.stack = stack
        for agent, entry in self.entries.items():
            try:
                flow = await asyncio.to_thread(entry.flow)
                self.warmed += await stack.enter_async_context(
                    model_clients(flow.nodes, warm=self.settings.warm)
                )
            except Exception as error:
                logger.exception('agent {} did not compile', agent)
                self.broken[agent] = f'{type(error).__name__}: {error}'
                continue
            self.flows[agent] = flow
        self.ready = True
        logger.info(
            'runner {} ready: {} agent(s), {} warm-up request(s)',
            self.settings.runner_id,
            len(self.flows),
            len(self.warmed),
        )

    def admit(self, ask: CallRequest) -> Carried:
        """Take a call, or say why not: warming, stopping, no such agent, or full."""
        if not self.ready:
            raise HTTPException(503, 'warming: the agents are still being compiled')
        if self.stopping:
            raise HTTPException(429, 'stopping: this runner takes no new calls')
        if ask.agent not in self.flows:
            raise HTTPException(
                404, f'no agent {ask.agent!r} here: {", ".join(sorted(self.flows)) or "none"}'
            )
        self.reap()
        if self.full():
            raise HTTPException(429, f'busy: {self.settings.max_calls} calls already')
        carried = Carried(
            id=ask.run or f'run_{uuid4().hex[:10]}', agent=ask.agent, channel=ask.channel, request=ask
        )
        self.calls[carried.id] = carried
        return carried

    def reap(self) -> None:
        """Give back the place of a voice call whose browser never sent its offer, and forget calls long over."""
        now = time.monotonic()
        for carried in [
            c for c in self.calls.values() if c.status == 'waiting' and now - c.created > self.ADMIT_SECONDS
        ]:
            carried.status, carried.outcome = 'ended', 'stopped'
            self.ended(carried, 'stopped', f'the browser sent no offer in {self.ADMIT_SECONDS:.0f} s')
        self.calls = {
            key: carried
            for key, carried in self.calls.items()
            if not (carried.status == 'ended' and now - carried.created > self.FORGET_SECONDS)
        }

    def ended(self, carried: Carried, outcome: Literal['stopped', 'failed'], detail: str) -> None:
        """Tell the studio a call is over when the call itself never could: it never opened, or failed before its end."""
        if carried.ended_sent:
            return
        carried.ended_sent = True
        turn = carried.call.turn if carried.call is not None else 0
        self.link.send(
            RunEnded(
                run=carried.id, seq=carried.last_seq + 1, turn=turn, t_ms=0, outcome=outcome, detail=detail
            )
        )

    def check_resume(self, ask: CallRequest) -> None:
        """Open and resume a call that is then dropped: an edit the application refuses is refused now, not after the offer."""
        entry, flow = self.entries[ask.agent], self.flows[ask.agent]
        if ask.resume is not None:
            flow.resume(entry.open(flow, core.Opening(caller_id=ask.caller_id)), ask.resume, ask.facts)

    async def open_text(self, carried: Carried) -> dict[str, Any]:
        """Open a text call: its state, its listener, and what it says first (nothing, when it carries on)."""
        entry, ask = self.entries[carried.agent], carried.request
        flow = carried.flow = await self.flow_for(carried.agent, ask.config)
        self._trace(carried)
        call = entry.open(flow, core.Opening(caller_id=ask.caller_id, key=carried.id))
        events = call.listen(Forward(self.link, carried), run=carried.id)
        if ask.resume is not None:
            flow.resume(call, ask.resume, ask.facts)
        events.started(
            agent=carried.agent,
            channel='text',
            entry=call.state.agent,
            state=call.state.facts.model_dump(mode='json'),
            config=ask.config,
            forked_from=ask.resume.run_id if ask.resume is not None else None,
            forked_turn=ask.resume.turn if ask.resume is not None else None,
        )
        carried.call, carried.status, carried.step = call, 'active', call.state.agent
        carried.conversation = self.tracer.start_span(
            'conversation', attributes={'conversation.type': 'text'}
        )
        said = [] if ask.resume is not None else [core.greet(call, entry.greeting(flow, call))]
        if said:
            call.acknowledge(*(segment.id for segment in call.spoken if segment.text))
        return {'said': said, 'step': call.state.agent, 'turn': call.turn}

    def _trace(self, carried: Carried) -> None:
        """Name the spans that follow after the call, its agent and its run."""
        from core.observability import setup

        setup(
            session=carried.id,
            trace_name=self.entries[carried.agent].name,
            agent=carried.agent,
            run=carried.id,
        )

    async def text_turn(self, carried: Carried, words: str) -> dict[str, Any]:
        """One caller turn of a text call: what was said back, where the call stands, and whether it ended."""
        flow, call = carried.flow, carried.call
        async with carried.lock:
            if carried.status != 'active':
                raise HTTPException(409, f'the call is {carried.status}')
            self._trace(carried)
            with (
                trace.use_span(carried.conversation, end_on_exit=False),
                self.tracer.start_as_current_span('turn', attributes={'turn.number': call.turn + 1}),
            ):
                reply = await core.text_turn(flow, call, words)
            call.acknowledge(*(segment.id for segment in reply.segments))
            call.turn_snapshot()
            ended = bool(call.ended)
            if ended:
                await self.end_text(carried, 'goodbye')
            return {
                'said': [segment.text for segment in reply.segments],
                'status': reply.finished.status,
                'step': call.state.agent,
                'turn': call.turn,
                'requests': reply.finished.requests,
                'ended': ended,
            }

    async def end_text(self, carried: Carried, outcome: Literal['goodbye', 'hangup', 'stopped']) -> None:
        """End a text call: its last event, its span, and the use case's close."""
        if carried.status == 'ended':
            return
        carried.status, carried.outcome = 'ended', outcome
        call = carried.call
        if call is not None and call.events is not None:
            call.events.ended(call.turn, outcome)
            await call.events.close()
        if carried.conversation is not None:
            carried.conversation.end()
        entry = self.entries[carried.agent]
        if call is not None and entry.close is not None:
            await entry.close(call)

    async def voice(self, carried: Carried, connection: SmallWebRTCConnection) -> None:
        """Run a browser call to its end on this process (`core.launch.run_call`)."""
        ask = carried.request
        carried.connection, carried.status = connection, 'active'
        transport = SmallWebRTCTransport(webrtc_connection=connection, params=transport_params['webrtc']())
        arguments = SmallWebRTCRunnerArguments(webrtc_connection=connection, session_id=carried.id)

        def opened(call: Any) -> None:
            """Keep the call once it is open."""
            carried.call, carried.step = call, call.state.agent

        try:
            carried.flow = await self.flow_for(carried.agent, ask.config)
            await run_call(
                self.entries[carried.agent],
                transport,
                arguments,
                flow=carried.flow,
                warm=False,
                caller_id=ask.caller_id,
                listener=Forward(self.link, carried),
                run=carried.id,
                config=ask.config,
                resume=ask.resume,
                facts=ask.facts,
                simulate=ask.delivery == 'simulated',
                opened=opened,
            )
        except Exception as error:
            logger.exception('call {} failed; the runner goes on', carried.id)
            carried.outcome = carried.outcome or 'failed'
            self.ended(carried, 'failed', f'{type(error).__name__}: {error}')
        finally:
            carried.status = 'ended'

    async def hang_up(self, carried: Carried) -> None:
        """End a call as the line dropping would."""
        if carried.channel == 'text':
            await self.end_text(carried, 'hangup')
        elif carried.connection is not None:
            await carried.connection.disconnect()
        else:
            carried.status, carried.outcome = 'ended', 'stopped'

    async def sample_lag(self) -> None:
        """How late the loop wakes, every `LAG_EVERY`; and every few seconds, the calls that never connected."""
        ticks = 0
        while True:
            started = time.perf_counter()
            await asyncio.sleep(self.LAG_EVERY)
            self.lag.append(time.perf_counter() - started - self.LAG_EVERY)
            ticks += 1
            if ticks % 100 == 0:
                self.reap()

    async def drain(self) -> None:
        """Take no new calls, and wait for these to end, at most `drain_seconds`."""
        self.stopping = True
        started = time.monotonic()
        logger.warning('runner {} stopping: {} call(s) running', self.settings.runner_id, len(self.active))
        while self.active and time.monotonic() - started < self.settings.drain_seconds:
            self.reap()
            await asyncio.sleep(0.25)
        logger.warning(
            'runner {} drained in {:.1f}s; {} call(s) left',
            self.settings.runner_id,
            time.monotonic() - started,
            len(self.active),
        )

    async def stop(self) -> None:
        """Stop now: end every text call, drop every voice call, close the WebRTC handler and the link."""
        self.stopping = True
        for carried in self.active:
            if carried.channel == 'text':
                await self.end_text(carried, 'stopped')
            elif carried.connection is not None:
                await carried.connection.disconnect()
        await self.webrtc.close()
        await self.link.stop()


class GracefulStop:
    """uvicorn's SIGTERM handler wrapped: the runner's calls finish first, then uvicorn stops; a second SIGTERM stops at once."""

    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    def install(self) -> None:
        """Take over SIGTERM on the running loop."""
        loop = asyncio.get_running_loop()
        stop = signal.getsignal(signal.SIGTERM)

        async def drain() -> None:
            """Drain, then hand over to uvicorn's own handler."""
            await self.runner.drain()
            if callable(stop):
                stop(signal.SIGTERM, None)

        def handler(signum: int, frame: Any) -> None:
            """The first SIGTERM drains; one while stopping stops at once."""
            if self.runner.stopping and callable(stop):
                stop(signum, frame)
                return
            loop.call_soon_threadsafe(lambda: loop.create_task(drain()))

        signal.signal(signal.SIGTERM, handler)
