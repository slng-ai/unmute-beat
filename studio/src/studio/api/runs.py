"""The runs' routes: runners linking in, runs started and followed, a text turn, a voice offer, and going back."""

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request, WebSocket
from pydantic import BaseModel

from core.snapshot import TurnSnapshot
from studio.runs.hub import RunnerInfo, StartRequest
from studio.runs.store import RunRow

if TYPE_CHECKING:
    from studio.api.app import Studio


class TurnText(BaseModel):
    """A text run's next caller turn."""

    text: str


class RunRoutes:
    """Everything under `/api/runners` and `/api/runs`."""

    def __init__(self, studio: Studio) -> None:
        self.studio = studio

    def router(self) -> APIRouter:
        """The routes, bound to this studio."""
        router = APIRouter(prefix='/api')
        router.add_api_websocket_route('/runners/link', self.link)
        router.add_api_route('/runners', self.runners, methods=['GET'])
        router.add_api_route('/runs', self.start, methods=['POST'])
        router.add_api_route('/runs', self.runs, methods=['GET'])
        router.add_api_route('/runs/{run}', self.one, methods=['GET'])
        router.add_api_route('/runs/{run}/events', self.events, methods=['GET'])
        router.add_api_websocket_route('/runs/{run}/live', self.live)
        router.add_api_route('/runs/{run}/offer', self.offer, methods=['POST'])
        router.add_api_route('/runs/{run}/offer', self.candidates, methods=['PATCH'])
        router.add_api_route('/runs/{run}/turns', self.turn, methods=['POST'])
        router.add_api_route('/runs/{run}', self.hang_up, methods=['DELETE'])
        router.add_api_route('/runs/{run}/turns/{turn}', self.go_back, methods=['GET'])
        return router

    async def link(self, socket: WebSocket) -> None:
        """One runner's link."""
        await self.studio.hub.link(socket)

    def runners(self) -> list[RunnerInfo]:
        """Every runner the studio has heard from."""
        return self.studio.hub.listed()

    async def start(self, ask: StartRequest) -> dict[str, Any]:
        """A new run, placed on a runner."""
        return await self.studio.hub.start(ask)

    async def runs(self, agent: str = '') -> list[RunRow]:
        """Every run, newest first, for one agent or all."""
        return await self.studio.hub.runs(agent)

    async def row(self, run: str) -> RunRow:
        """A run's row, live or stored, or a 404."""
        if (live := self.studio.hub.live.get(run)) is not None:
            return live.row
        if (row := await self.studio.store.run(run)) is None:
            raise HTTPException(404, f'no run {run!r}')
        return row

    async def one(self, run: str) -> dict[str, Any]:
        """A run's row, whether it is still live, and the turns it has snapshots of."""
        row = await self.row(run)
        live = self.studio.hub.live.get(run)
        return {
            'run': row,
            'live': live is not None and row.outcome is None and bool(live.runner_url),
            'turns': await self.studio.store.snapshot_turns(run),
        }

    async def events(self, run: str) -> list[dict[str, Any]]:
        """Every event of a run, in order."""
        await self.row(run)
        return await self.studio.store.events(run)

    async def live(self, run: str, socket: WebSocket) -> None:
        """A page following one run."""
        await self.studio.hub.watch(run, socket)

    async def offer(self, run: str, request: Request) -> Any:
        """The browser's WebRTC offer."""
        return await self.studio.hub.offer(run, 'POST', await request.json())

    async def candidates(self, run: str, request: Request) -> Any:
        """The browser's ICE candidates."""
        return await self.studio.hub.offer(run, 'PATCH', await request.json())

    async def turn(self, run: str, words: TurnText) -> dict[str, Any]:
        """A text run's next caller turn."""
        return await self.studio.hub.turn(run, words.text)

    async def hang_up(self, run: str) -> dict[str, Any]:
        """End a run."""
        return await self.studio.hub.hang_up(run)

    async def go_back(self, run: str, turn: int) -> dict[str, Any]:
        """What going back to the end of this turn means: the state, what was said, and what stays done after it."""
        row = await self.row(run)
        found = await self.studio.store.snapshot(run, turn)
        if found is None:
            raise HTTPException(404, f'no snapshot of {run} at turn {turn}')
        saved = TurnSnapshot.model_validate(found)
        events = await self.studio.store.events(run)
        after = [
            {'turn': event['turn'], 'operation': event['operation'], 'receipt': event['receipt']}
            for event in events
            if event.get('type') == 'receipt' and event['turn'] > turn and event.get('succeeded')
        ]
        return {
            'run': row,
            'turn': turn,
            'step': saved.agent,
            'pending': saved.pending,
            'consulting': saved.consulting,
            'facts': saved.facts,
            'said': saved.said(),
            'intents': [intent.model_dump(mode='json') for intent in saved.intents],
            'receipts': [
                filed.receipt | {'operation': filed.operation} for filed in saved.evidence if filed.succeeded
            ],
            'done_after': after,
            'turns': await self.studio.store.snapshot_turns(run),
        }
