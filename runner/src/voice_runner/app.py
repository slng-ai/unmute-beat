"""The runner service: one process that loads every agent once and carries their calls, by voice or by text.

    uv run slng-runner --port 7810 --studio ws://127.0.0.1:7800/api/runners/link

Each process imports every use case's entry (`voice_runner.agents`), compiles each flow once and
warms its model clients once, then carries up to `max_calls` calls at a time on one asyncio loop.
Copies of it scale out behind the studio (or a load balancer): one that is warming, full or
stopping says so on `/ready` and is given no new calls. It never writes a store: each call's events
go to the studio over the runner's one link (`voice_runner.link`).

    GET    /agents                 each agent it carries: id, name, app version, whether it needs a heard yes
    POST   /calls                  admit a call or answer 429: {call_id, offer_url, ice_servers} for voice,
                                   and the greeting for text
    POST   /calls/{id}/offer       the browser's WebRTC offer, handed to Pipecat's SmallWebRTCRequestHandler
    PATCH  /calls/{id}/offer       its ICE candidates
    POST   /calls/{id}/turns       a text call's next caller turn
    DELETE /calls/{id}             hang up: the same path as the line dropping
    GET    /ready, /stats          503 while warming, stopping or full; calls, capacity, loop lag

Voice is Pipecat's SmallWebRTC: the browser's audio comes straight to this process, and the
connection lives in its memory, so the browser sends its offer and ICE to the `offer_url` it was given,
which names this process. With no STUN or TURN server set (`PIPECAT_ICE_SERVERS`), calls work on this
machine and its local network only. Text is `core.text_turn`: each reply's segments are acknowledged
once the response carrying them is sent, which is the text runtime's simulated delivery.

Stopping is the runner's own (the salon `server.py`'s drain): on SIGTERM it stops admitting, lets its
calls run to their end for at most `drain_seconds`, and only then lets uvicorn stop. A second SIGTERM
stops at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import statistics
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger
from opentelemetry import trace
from pipecat.runner.types import SmallWebRTCRunnerArguments
from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pydantic import BaseModel, ValidationError

import core
from core.events import Event, RunEnded
from core.graph import model_clients
from core.launch import run_call, transport_params
from core.snapshot import TurnSnapshot
from voice_runner.agents import Found
from voice_runner.link import Link

ADMIT_SECONDS = 30.0
"""How long an admitted voice call may wait for its browser's offer before its place is given back."""
LAG_EVERY = 0.05


@dataclass(frozen=True)
class Settings:
    port: int = 7810
    host: str = '127.0.0.1'
    public_url: str = ''
    """How the browser and the studio reach this process; default `http://{host}:{port}`."""
    studio: str = ''
    """The studio's link (`ws://127.0.0.1:7800/api/runners/link`); '' runs with no studio."""
    max_calls: int = 4
    warm: bool = True
    drain_seconds: float = 300.0
    runner_id: str = field(default_factory=lambda: f'runner-{os.getpid()}')
    ice_servers: tuple[dict[str, Any], ...] = ()

    @property
    def url(self) -> str:
        return self.public_url or f'http://{self.host}:{self.port}'


class CallRequest(BaseModel):
    """What the studio asks for: an agent, a channel, and optionally where to start from."""

    agent: str
    channel: Literal['voice', 'text'] = 'text'
    run: str = ''
    """The studio's run id; the runner makes one when it is not given."""
    config: str = ''
    """The config version the studio names the run by."""
    caller_id: str = ''
    resume: TurnSnapshot | None = None
    facts: dict[str, Any] | None = None
    """The state to carry on with, edited on the go-back page; the snapshot's own when absent."""
    delivery: Literal['marks', 'simulated'] = 'marks'
    """`simulated` lets estimated playback count as heard on a voice call (a developer's choice)."""


class TurnRequest(BaseModel):
    text: str


class Forward:
    """A call's listener: each event goes on the runner's link to the studio, and into the call's own record."""

    def __init__(self, link: Link, carried: Carried) -> None:
        self.link = link
        self.carried = carried

    async def event(self, event: Event) -> None:
        self.link.send(event)
        self.carried.note(event)


@dataclass
class Carried:
    """One call this process carries, from admission to its end."""

    id: str
    agent: str
    channel: Literal['voice', 'text']
    request: CallRequest
    created: float = field(default_factory=time.monotonic)
    status: Literal['waiting', 'active', 'ended'] = 'waiting'
    call: Any = None
    task: asyncio.Task[None] | None = None
    connection: SmallWebRTCConnection | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    conversation: Any = None
    """A text call's root span: its turns hang from it, so one run is one trace."""
    flow: Any = None
    """The flow this call runs on, kept for its whole life even if the agent is compiled again."""
    step: str = ''
    outcome: str = ''
    last_seq: int = -1
    ended_sent: bool = False

    def note(self, event: Event) -> None:
        self.last_seq = max(self.last_seq, event.seq)
        if event.type == 'step.entered':
            self.step = event.step
        elif event.type == 'run.ended':
            self.outcome, self.ended_sent = event.outcome, True


class Runner:
    """The process's calls, agents and readiness."""

    def __init__(self, settings: Settings, found: list[Found]) -> None:
        self.settings = settings
        self.found = found
        self.entries = {f.entry.id: f.entry for f in found if f.entry is not None}
        self.flows: dict[str, core.StepFlow] = {}
        self.broken: dict[str, str] = {f.package: f.error for f in found if f.entry is None}
        self.calls: dict[str, Carried] = {}
        self.ready = False
        self.stopping = False
        self.warmed: list[dict[str, Any]] = []
        self.lag: deque[float] = deque(maxlen=1200)
        self.started = time.time()
        self.loaded: dict[str, str] = {}
        """The config version each agent's flow was compiled from, as the studio named it."""
        self.stack: AsyncExitStack | None = None
        self.link = Link(settings.studio, self.hello, self.stats)
        ice = [IceServer(**server) for server in settings.ice_servers]
        self.webrtc = SmallWebRTCRequestHandler(ice_servers=ice or None)
        self.tracer = trace.get_tracer('slng.runner')

    @property
    def active(self) -> list[Carried]:
        return [c for c in self.calls.values() if c.status != 'ended']

    def full(self) -> bool:
        return len(self.active) >= self.settings.max_calls

    def hello(self) -> dict[str, Any]:
        return {
            'runner': self.settings.runner_id,
            'url': self.settings.url,
            'pid': os.getpid(),
            'max_calls': self.settings.max_calls,
            'agents': sorted(self.entries),
            'frameworks': ['pipecat'],
        }

    def stats(self) -> dict[str, Any]:
        samples = sorted(self.lag)
        return {
            'runner': self.settings.runner_id,
            'url': self.settings.url,
            'ready': self.ready and not self.stopping and not self.full(),
            'warming': not self.ready,
            'stopping': self.stopping,
            'active': len(self.active),
            'max_calls': self.settings.max_calls,
            'calls': [
                {'id': c.id, 'agent': c.agent, 'channel': c.channel, 'status': c.status, 'step': c.step}
                for c in self.active
            ],
            'lag_p50_ms': round(1000 * statistics.median(samples), 2) if samples else None,
            'lag_max_ms': round(1000 * samples[-1], 2) if samples else None,
            'uptime_s': round(time.time() - self.started, 1),
        }

    async def flow_for(self, agent: str, config: str) -> core.StepFlow:
        """The agent's flow for a call on this config version: compiled again when the studio saved a new one.

        The first version a runner hears of is the one it compiled at start (it read the files then).
        A later, different one is compiled from the manifest on disk; calls already running keep the
        flow they started on. Only config is read again, never the application's code.

        ponytail: each recompiled flow's model clients stay open for the process's life. Close the
        old flow's clients once its last call ends if editors ever save hundreds of times a day.
        """
        known = self.loaded.setdefault(agent, config)
        entry = self.entries[agent]
        if config and known != config and entry.manifest is not None:
            flow = await asyncio.to_thread(entry.recompile)
            if self.stack is not None:
                await self.stack.enter_async_context(model_clients(flow.nodes))
            self.flows[agent], self.loaded[agent] = flow, config
            logger.info('agent {} compiled again for config {}', agent, config)
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
        for carried in list(self.calls.values()):
            if carried.status == 'waiting' and now - carried.created > ADMIT_SECONDS:
                carried.status, carried.outcome = 'ended', 'stopped'
                self.ended(carried, 'stopped', f'the browser sent no offer in {ADMIT_SECONDS:.0f} s')
            if carried.status == 'ended' and now - carried.created > 3600:
                del self.calls[carried.id]

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

    def listener(self, carried: Carried) -> Forward:
        return Forward(self.link, carried)

    async def open_text(self, carried: Carried) -> dict[str, Any]:
        """Open a text call: its state, its listener, and what it says first (nothing, when it carries on)."""
        entry, ask = self.entries[carried.agent], carried.request
        flow = await self.flow_for(carried.agent, ask.config)
        carried.flow = flow
        self._trace(carried)
        call = entry.open(flow, core.Opening(caller_id=ask.caller_id, key=carried.id))
        events = call.listen(self.listener(carried), run=carried.id)
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
        said = []
        if ask.resume is None:
            said = [core.greet(call, entry.greeting(flow, call))]
            call.acknowledge(*(s.id for s in call.spoken if s.text))
        return {'said': said, 'step': call.state.agent, 'turn': call.turn}

    def _trace(self, carried: Carried) -> None:
        from core.observability import setup

        setup(
            session=carried.id,
            trace_name=self.entries[carried.agent].name,
            agent=carried.agent,
            run=carried.id,
        )

    async def text_turn(self, carried: Carried, words: str) -> dict[str, Any]:
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
            call.acknowledge(*(s.id for s in reply.segments))
            call.turn_snapshot()
            finished = reply.finished
            ended = bool(call.ended)
            if ended:
                await self.end_text(carried, 'goodbye')
            return {
                'said': [s.text for s in reply.segments],
                'status': finished.status,
                'step': call.state.agent,
                'turn': call.turn,
                'requests': finished.requests,
                'ended': ended,
            }

    async def end_text(self, carried: Carried, outcome: Literal['goodbye', 'hangup', 'stopped']) -> None:
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
                listener=self.listener(carried),
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

    async def sample_lag(self) -> None:
        """How late the loop wakes, every `LAG_EVERY`; and every few seconds, the calls that never connected."""
        ticks = 0
        while True:
            started = time.perf_counter()
            await asyncio.sleep(LAG_EVERY)
            self.lag.append(time.perf_counter() - started - LAG_EVERY)
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


def _drain_on_signal(runner: Runner) -> None:
    """Wrap uvicorn's SIGTERM handler: finish this runner's calls first, then let uvicorn stop."""
    loop = asyncio.get_running_loop()
    stop = signal.getsignal(signal.SIGTERM)

    async def drain() -> None:
        await runner.drain()
        if callable(stop):
            stop(signal.SIGTERM, None)

    def handler(signum: int, frame: Any) -> None:
        if runner.stopping and callable(stop):
            stop(signum, frame)
            return
        loop.call_soon_threadsafe(lambda: loop.create_task(drain()))

    signal.signal(signal.SIGTERM, handler)


def create_app(settings: Settings, found: list[Found], *, signals: bool = True) -> FastAPI:
    runner = Runner(settings, found)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if signals:
            _drain_on_signal(runner)
        sampler = asyncio.create_task(runner.sample_lag())
        runner.link.start()
        async with AsyncExitStack() as stack:
            warming = asyncio.create_task(runner.warm(stack))
            try:
                yield
            finally:
                runner.stopping = True
                if not warming.done():
                    warming.cancel()
                for carried in runner.active:
                    if carried.channel == 'text':
                        await runner.end_text(carried, 'stopped')
                    elif carried.connection is not None:
                        await carried.connection.disconnect()
                await runner.webrtc.close()
                await runner.link.stop()
                sampler.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sampler

    app = FastAPI(title='slng runner', lifespan=lifespan)
    app.state.runner = runner

    def carried_or_404(call_id: str) -> Carried:
        if (carried := runner.calls.get(call_id)) is None:
            raise HTTPException(404, f'no call {call_id!r} on this runner')
        return carried

    @app.get('/agents')
    def agents() -> list[dict[str, Any]]:
        return [
            {
                'id': agent,
                'name': entry.name,
                'app_version': runner.flows[agent].version if agent in runner.flows else '',
                'confirms': runner.flows[agent].confirms if agent in runner.flows else None,
                'ready': agent in runner.flows,
                'error': runner.broken.get(agent, ''),
            }
            for agent, entry in sorted(runner.entries.items())
        ] + [
            {
                'id': package,
                'name': package,
                'app_version': '',
                'confirms': None,
                'ready': False,
                'error': error,
            }
            for package, error in sorted(runner.broken.items())
            if package not in runner.entries
        ]

    @app.get('/agents/{agent}/schema')
    def schema(agent: str) -> dict[str, Any]:
        """The agent's state type as JSON schema: what the go-back page's form is built from."""
        if agent not in runner.flows:
            raise HTTPException(404, f'no agent {agent!r} here')
        flow = runner.flows[agent]
        return {'state': flow.state_type.model_json_schema(), 'steps': list(flow.by_id), 'start': flow.start}

    @app.post('/calls')
    async def start(ask: CallRequest) -> dict[str, Any]:
        carried = runner.admit(ask)
        found = {'call_id': carried.id, 'runner': settings.runner_id, 'channel': ask.channel}
        if ask.channel == 'voice':
            if ask.resume is not None:
                try:
                    runner.check_resume(ask)
                except ValidationError as error:
                    carried.status = 'ended'
                    raise HTTPException(
                        422, {'refused': error.errors(include_url=False, include_context=False)}
                    ) from error
            return found | {
                'offer_url': f'{settings.url}/calls/{carried.id}/offer',
                'ice_servers': list(settings.ice_servers),
            }
        try:
            return found | await runner.open_text(carried)
        except ValidationError as error:
            carried.status = 'ended'
            raise HTTPException(
                422, {'refused': error.errors(include_url=False, include_context=False)}
            ) from error
        except Exception:
            carried.status = 'ended'
            raise

    @app.post('/calls/{call_id}/offer')
    async def offer(call_id: str, request: Request) -> dict[str, Any] | None:
        carried = carried_or_404(call_id)
        if carried.channel != 'voice' or carried.status == 'ended':
            raise HTTPException(409, f'the call is a {carried.channel} call, {carried.status}')
        body = await request.json()
        sdp = SmallWebRTCRequest(
            sdp=body['sdp'],
            type=body['type'],
            pc_id=body.get('pc_id'),
            restart_pc=body.get('restart_pc'),
            request_data=body.get('request_data') or body.get('requestData'),
        )

        async def connected(connection: SmallWebRTCConnection) -> None:
            if carried.task is None:
                carried.task = asyncio.create_task(runner.voice(carried, connection))

        return await runner.webrtc.handle_web_request(request=sdp, webrtc_connection_callback=connected)

    @app.patch('/calls/{call_id}/offer')
    async def candidates(call_id: str, request: Request) -> dict[str, str]:
        carried_or_404(call_id)
        body = await request.json()
        await runner.webrtc.handle_patch_request(
            SmallWebRTCPatchRequest(
                pc_id=body['pc_id'], candidates=[IceCandidate(**c) for c in body.get('candidates', [])]
            )
        )
        return {'status': 'success'}

    @app.post('/calls/{call_id}/turns')
    async def turn(call_id: str, words: TurnRequest) -> dict[str, Any]:
        carried = carried_or_404(call_id)
        if carried.channel != 'text':
            raise HTTPException(409, 'a voice call takes its turns by voice')
        return await runner.text_turn(carried, words.text)

    @app.delete('/calls/{call_id}')
    async def hang_up(call_id: str) -> dict[str, str]:
        carried = carried_or_404(call_id)
        if carried.channel == 'text':
            await runner.end_text(carried, 'hangup')
        elif carried.connection is not None:
            await carried.connection.disconnect()
        else:
            carried.status, carried.outcome = 'ended', 'stopped'
        return {'status': carried.status}

    @app.get('/calls/{call_id}')
    def one(call_id: str) -> dict[str, Any]:
        carried = carried_or_404(call_id)
        return {
            'id': carried.id,
            'agent': carried.agent,
            'channel': carried.channel,
            'status': carried.status,
            'step': carried.step,
            'outcome': carried.outcome,
        }

    @app.get('/ready')
    def ready() -> JSONResponse:
        found = runner.stats()
        return JSONResponse(found, status_code=200 if found['ready'] else 503)

    @app.get('/stats')
    def stats() -> dict[str, Any]:
        return runner.stats() | {'warmed': len(runner.warmed), 'broken': runner.broken}

    return app


def data_defaults(root: Path) -> None:
    """Where the use cases keep their business data when the environment does not say: one folder, shared by runners."""
    folder = root / 'data' / 'studio'
    folder.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('DIARY_PATH', str(folder / 'salon-diary.sqlite3'))
    os.environ.setdefault('LEARNING_DB_PATH', str(folder / 'faro.sqlite3'))
