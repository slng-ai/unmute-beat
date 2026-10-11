"""The runner service's HTTP face: one process that loads every agent once and carries their calls, by voice or by text.

    uv run slng-runner --port 7810 --studio ws://127.0.0.1:7800/api/runners/link

    GET    /agents                 each agent it carries: id, name, app version, whether it needs a heard yes
    GET    /agents/{id}/schema     its state type as JSON schema: what the go-back page's form is built from
    POST   /calls                  admit a call or answer 429: {call_id, offer_url, ice_servers} for voice,
                                   {call_id, livekit: {url, room, token}} for voice on LiveKit,
                                   and the greeting for text
    POST   /calls/{id}/offer       the browser's WebRTC offer, handed to Pipecat's SmallWebRTCRequestHandler
    PATCH  /calls/{id}/offer       its ICE candidates
    POST   /calls/{id}/turns       a text call's next caller turn
    DELETE /calls/{id}             hang up: the same path as the line dropping
    GET    /ready, /stats          503 while warming, stopping or full; calls, capacity, loop lag

Voice is Pipecat's SmallWebRTC: the browser's audio comes straight to this process, and the
connection lives in its memory, so the browser sends its offer and ICE to the `offer_url` it was given,
which names this process. With no STUN or TURN server set (`PIPECAT_ICE_SERVERS`), calls work on this
machine and its local network only. Voice on LiveKit (`framework: livekit`) needs no offer: the browser
and this process both join a room of the call's own on the LiveKit server (`LIVEKIT_URL`), and the
audio goes through it.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
)
from pydantic import ValidationError

from voice_runner.agents import Found
from voice_runner.calls import Carried
from voice_runner.runner import GracefulStop, Runner
from voice_runner.settings import CallRequest, Settings, TurnRequest


class RunnerApp:
    """One runner and the FastAPI app over it. `signals=False` leaves SIGTERM alone (several runners in one test)."""

    def __init__(self, settings: Settings, found: list[Found], *, signals: bool = True) -> None:
        self.settings = settings
        self.runner = Runner(settings, found)
        self.signals = signals

    @asynccontextmanager
    async def lifespan(self, _: FastAPI) -> AsyncIterator[None]:
        """Link to the studio, sample the loop, warm the agents; on the way out, stop everything."""
        runner = self.runner
        if self.signals:
            GracefulStop(runner).install()
        sampler = asyncio.create_task(runner.sample_lag())
        runner.link.start()
        async with AsyncExitStack() as stack:
            warming = asyncio.create_task(runner.warm(stack))
            try:
                yield
            finally:
                if not warming.done():
                    warming.cancel()
                await runner.stop()
                sampler.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sampler

    def app(self) -> FastAPI:
        """The FastAPI app, its runner on `app.state.runner`."""
        app = FastAPI(title='slng runner', lifespan=self.lifespan)
        app.state.runner = self.runner
        routes = [
            ('/agents', self.agents, 'GET'),
            ('/agents/{agent}/schema', self.schema, 'GET'),
            ('/calls', self.start, 'POST'),
            ('/calls/{call_id}/offer', self.offer, 'POST'),
            ('/calls/{call_id}/offer', self.candidates, 'PATCH'),
            ('/calls/{call_id}/turns', self.turn, 'POST'),
            ('/calls/{call_id}', self.hang_up, 'DELETE'),
            ('/calls/{call_id}', self.one, 'GET'),
            ('/ready', self.ready, 'GET'),
            ('/stats', self.stats, 'GET'),
        ]
        for path, handler, method in routes:
            app.add_api_route(path, handler, methods=[method])
        return app

    def carried(self, call_id: str) -> Carried:
        """A call on this runner, or a 404."""
        if (carried := self.runner.calls.get(call_id)) is None:
            raise HTTPException(404, f'no call {call_id!r} on this runner')
        return carried

    def agents(self) -> list[dict[str, Any]]:
        """Each agent it carries, and each use case it could not load."""
        runner = self.runner
        loaded = [
            {
                'id': agent,
                'name': entry.name,
                'app_version': runner.flows[agent].version if agent in runner.flows else '',
                'confirms': runner.flows[agent].confirms if agent in runner.flows else None,
                'ready': agent in runner.flows,
                'error': runner.broken.get(agent, ''),
            }
            for agent, entry in sorted(runner.entries.items())
        ]
        broken = [
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
        return loaded + broken

    def schema(self, agent: str) -> dict[str, Any]:
        """The agent's state type as JSON schema: what the go-back page's form is built from."""
        if agent not in self.runner.flows:
            raise HTTPException(404, f'no agent {agent!r} here')
        flow = self.runner.flows[agent]
        return {'state': flow.state_type.model_json_schema(), 'steps': list(flow.by_id), 'start': flow.start}

    async def start(self, ask: CallRequest) -> dict[str, Any]:
        """Admit a call: a voice call gets where to send its offer, a text call opens and greets.

        A state the application refuses is a 422 that lists each refusal by its path, and nothing is opened.
        """
        carried = self.runner.admit(ask)
        found = {'call_id': carried.id, 'runner': self.settings.runner_id, 'channel': ask.channel}
        voice = {
            'offer_url': f'{self.settings.url}/calls/{carried.id}/offer',
            'ice_servers': list(self.settings.ice_servers),
        }
        try:
            if ask.channel == 'voice':
                self.runner.check_resume(ask)
                if ask.framework == 'livekit':
                    return found | self.runner.open_livekit(carried)
                return found | voice
            return found | await self.runner.open_text(carried)
        except ValidationError as error:
            carried.status = 'ended'
            raise HTTPException(
                422, {'refused': error.errors(include_url=False, include_context=False)}
            ) from error
        except Exception:
            carried.status = 'ended'
            raise

    async def offer(self, call_id: str, request: Request) -> dict[str, Any] | None:
        """The browser's WebRTC offer; once connected, the call runs to its end."""
        carried = self.carried(call_id)
        if carried.channel != 'voice' or carried.status == 'ended' or carried.request.framework != 'pipecat':
            raise HTTPException(
                409, f'the call is a {carried.channel} call on {carried.request.framework}, {carried.status}'
            )
        body = await request.json()
        sdp = SmallWebRTCRequest(
            sdp=body['sdp'],
            type=body['type'],
            pc_id=body.get('pc_id'),
            restart_pc=body.get('restart_pc'),
            request_data=body.get('request_data') or body.get('requestData'),
        )

        async def connected(connection: SmallWebRTCConnection) -> None:
            """Run the call once, when its connection is up."""
            if carried.task is None:
                carried.task = asyncio.create_task(self.runner.voice(carried, connection))

        return await self.runner.webrtc.handle_web_request(request=sdp, webrtc_connection_callback=connected)

    async def candidates(self, call_id: str, request: Request) -> dict[str, str]:
        """The browser's ICE candidates."""
        self.carried(call_id)
        body = await request.json()
        candidates = [IceCandidate(**candidate) for candidate in body.get('candidates', [])]
        await self.runner.webrtc.handle_patch_request(
            SmallWebRTCPatchRequest(pc_id=body['pc_id'], candidates=candidates)
        )
        return {'status': 'success'}

    async def turn(self, call_id: str, words: TurnRequest) -> dict[str, Any]:
        """A text call's next caller turn."""
        carried = self.carried(call_id)
        if carried.channel != 'text':
            raise HTTPException(409, 'a voice call takes its turns by voice')
        return await self.runner.text_turn(carried, words.text)

    async def hang_up(self, call_id: str) -> dict[str, str]:
        """End a call."""
        carried = self.carried(call_id)
        await self.runner.hang_up(carried)
        return {'status': carried.status}

    def one(self, call_id: str) -> dict[str, Any]:
        """One call: its agent, channel, status, step and outcome."""
        carried = self.carried(call_id)
        return carried.summary() | {'outcome': carried.outcome}

    def ready(self) -> JSONResponse:
        """200 when it takes calls, 503 when warming, stopping or full."""
        found = self.runner.stats()
        return JSONResponse(found, status_code=200 if found['ready'] else 503)

    def stats(self) -> dict[str, Any]:
        """How it stands, with its warm-up requests and the use cases it could not load."""
        return self.runner.stats() | {'warmed': len(self.runner.warmed), 'broken': self.runner.broken}
