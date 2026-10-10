"""Runs: the runners that carry them, where each new one goes, and every event on its way to the store and the page.

Runners link in over a websocket (`voice_runner.link`): a hello (id, URL, capacity, agents), a stats
line every two seconds (ready, warming, stopping, calls), and each call's events. The studio is the
only writer of the store: each event goes to Redis (and Postgres behind it) and to every page
watching that run, in the order it came.

Starting a run (`Hub.start`): the agent's config on disk is versioned and kept (`studio.agents.configs`),
the run is placed on the least busy runner that is ready and carries the agent, and the runner is
asked for the call. A run that carries on from a turn of an earlier one takes that turn's snapshot
from the store, with the state edited on the go-back page when it was. The browser's WebRTC offer and
a text run's turns pass through the studio to the runner that holds the call: the audio itself goes
straight from the browser to that runner.
"""

import asyncio
import contextlib
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

import httpx
from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from loguru import logger
from pydantic import BaseModel

from studio.agents.catalog import Agent
from studio.agents.configs import AgentConfig
from studio.runs.store import RunRow, Store
from studio.settings import Settings


class RunnerInfo(BaseModel):
    """One runner as the studio sees it: what it said in its hello, and its latest stats."""

    runner: str
    url: str
    pid: int = 0
    max_calls: int = 0
    agents: list[str] = []
    frameworks: list[str] = ['pipecat']
    ready: bool = False
    warming: bool = True
    stopping: bool = False
    active: int = 0
    calls: list[dict[str, Any]] = []
    lag_p50_ms: float | None = None
    lag_max_ms: float | None = None
    connected: bool = True
    seen: float = 0.0

    def heard_lately(self, now: float, gone: float) -> bool:
        """Whether it is linked and spoke within `gone` seconds."""
        return self.connected and now - self.seen < gone

    def why_not(self) -> str:
        """Why a linked runner takes no call for an agent, in a word."""
        if self.warming:
            return 'warming'
        if self.stopping:
            return 'stopping'
        return 'full' if self.active >= self.max_calls else 'without it'


class ResumeFrom(BaseModel):
    """The turn of an earlier run a new one carries on from."""

    run: str
    turn: int


class StartRequest(BaseModel):
    """What the Agents page asks for: an agent, a channel, where to start from, and the state edited on going back."""

    agent: str
    channel: Literal['voice', 'text'] = 'text'
    caller_id: str = ''
    resume_from: ResumeFrom | None = None
    facts: dict[str, Any] | None = None
    delivery: Literal['marks', 'simulated'] = 'marks'


class Live:
    """A run this studio placed or heard from, while it lasts: its row, its runner, and who is watching."""

    def __init__(self, row: RunRow, runner_url: str) -> None:
        self.row = row
        self.runner_url = runner_url
        self.offer_url = ''
        self.watchers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.last_seq = -1

    def note(self, event: dict[str, Any]) -> bool:
        """What an event changes on the run's row; True when the row must be saved again."""
        self.last_seq = max(self.last_seq, int(event.get('seq', -1)))
        row, kind, changed = self.row, event.get('type'), False
        if int(event.get('turn', 0)) > row.turns:
            row.turns, changed = int(event['turn']), True
        if kind == 'step.entered':
            row.step = str(event.get('step', ''))
        elif kind == 'run.started' and not row.step:
            row.step = str(event.get('entry', ''))
        elif kind == 'run.ended':
            row.ended_at, row.outcome, changed = datetime.now(UTC), str(event.get('outcome')), True
        return changed

    def tell(self, event: dict[str, Any]) -> None:
        """Pass an event to every page watching; one that fell too far behind is let go."""
        full = {queue for queue in self.watchers if queue.full()}
        self.watchers -= full
        for queue in self.watchers:
            queue.put_nowait(event)


class RunnerClient:
    """The studio's requests to runners: any answer but 200 becomes the same error, with the runner's own words."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5))

    async def ask(self, method: str, url: str, body: Any = None) -> Any:
        """One request; its JSON answer, or an `HTTPException` carrying the runner's status and detail."""
        answer = await self.http.request(method, url, json=body)
        if answer.status_code != 200:
            raise HTTPException(answer.status_code, self.detail(answer))
        return answer.json()

    @staticmethod
    def detail(answer: httpx.Response) -> Any:
        """What a runner said was wrong: its `detail`, or its whole text."""
        try:
            return answer.json().get('detail', answer.text)
        except ValueError:
            return answer.text

    async def close(self) -> None:
        """Close the connections."""
        await self.http.aclose()


class Hub:
    """The runners linked to this studio, the runs on them, and every event on its way to the store and the pages.

    `GONE_SECONDS` is how long a runner may go unheard before it is taken out of placement.
    """

    GONE_SECONDS = 10.0
    WATCH_QUEUE = 5000

    def __init__(
        self,
        settings: Settings,
        store: Store,
        agents: Callable[[], list[Agent]],
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.agents = agents
        self.client = RunnerClient(http)
        self.runners: dict[str, RunnerInfo] = {}
        self.live: dict[str, Live] = {}
        self.events_in = 0

    async def link(self, socket: WebSocket) -> None:
        """One runner's link, for as long as it stays: its hello, its stats, and its calls' events."""
        await socket.accept()
        name = ''
        try:
            while True:
                message = await socket.receive_json()
                if hello := message.get('hello'):
                    name = hello['runner']
                    self.runners[name] = RunnerInfo.model_validate(hello | {'seen': time.monotonic()})
                    logger.info('runner {} linked from {}', name, hello.get('url'))
                elif (stats := message.get('stats')) and name in self.runners:
                    self._stats(name, stats)
                elif event := message.get('event'):
                    await self.ingest(event)
        except WebSocketDisconnect:
            pass
        finally:
            if name in self.runners:
                self.runners[name] = self.runners[name].model_copy(
                    update={'connected': False, 'ready': False}
                )
                logger.warning('runner {} unlinked', name)

    def _stats(self, name: str, stats: dict[str, Any]) -> None:
        """A runner's latest stats over what it said before."""
        known = self.runners[name].model_dump()
        fields = {key: value for key, value in stats.items() if key in RunnerInfo.model_fields}
        self.runners[name] = RunnerInfo.model_validate(
            known | fields | {'seen': time.monotonic(), 'connected': True}
        )

    def listed(self) -> list[RunnerInfo]:
        """Every runner, by name, marked disconnected once unheard for too long."""
        now = time.monotonic()
        return [
            runner.model_copy(update={'connected': runner.heard_lately(now, self.GONE_SECONDS)})
            for runner in sorted(self.runners.values(), key=lambda runner: runner.runner)
        ]

    def place(self, agent: str) -> RunnerInfo:
        """The least busy runner that is ready and carries this agent, with the call counted against it at once.

        Runners report their load every 2 s, so calls started faster than that would otherwise all go
        to the one that looked idle. The next report replaces the count with the runner's own.
        """
        chosen = self.carrier(agent)
        chosen.active += 1
        return chosen

    def carrier(self, agent: str) -> RunnerInfo:
        """The least busy runner that is ready and carries this agent, or a 503 that says what to do."""
        now = time.monotonic()
        linked = [runner for runner in self.runners.values() if runner.heard_lately(now, self.GONE_SECONDS)]
        ready = [runner for runner in linked if runner.ready and agent in runner.agents]
        if not ready:
            why = (
                f'no runner is ready for {agent!r}: '
                + ', '.join(f'{r.runner} is {r.why_not()}' for r in linked)
                if linked
                else 'no runner is linked: `slng studio up` starts one, `slng studio runner` adds more'
            )
            raise HTTPException(503, why)
        return min(ready, key=lambda runner: (runner.active / max(runner.max_calls, 1), runner.runner))

    def _agent(self, agent: str) -> Agent:
        """A compiled agent by id, or the error that says why it cannot run."""
        found = next((each for each in self.agents() if each.id == agent), None)
        if found is None:
            raise HTTPException(404, f'no agent {agent!r}')
        if found.status != 'compiled':
            raise HTTPException(409, f'{agent} does not compile: {found.error}')
        return found

    async def start(self, ask: StartRequest) -> dict[str, Any]:
        """Place a new run, keep its config version, and ask its runner for the call."""
        agent = self._agent(ask.agent)
        config = AgentConfig(self.settings.root / agent.path).row(agent.id, self.settings.root)
        await self.store.save_config(config)
        snapshot = await self._snapshot(ask.resume_from)
        runner = self.place(agent.id)
        run = f'run_{uuid4().hex[:10]}'
        live = self.live[run] = Live(
            self._new_row(run, agent.id, config.version, ask, runner.runner), runner.url
        )
        body = {
            'agent': agent.id,
            'channel': ask.channel,
            'run': run,
            'config': config.version,
            'caller_id': ask.caller_id,
            'resume': snapshot,
            'facts': ask.facts,
            'delivery': ask.delivery,
        }
        try:
            found = await self.client.ask('POST', f'{runner.url}/calls', body)
        except httpx.HTTPError as error:
            del self.live[run]
            raise HTTPException(502, f'runner {runner.runner} did not answer: {error}') from error
        except HTTPException:
            del self.live[run]
            raise
        await self.store.save_run(live.row)
        live.offer_url = found.get('offer_url', '')
        reply = {
            'run': run,
            'agent': agent.id,
            'channel': ask.channel,
            'config': config.version,
            'runner': runner.runner,
            'said': found.get('said', []),
            'step': found.get('step', ''),
            'turn': found.get('turn', 0),
        }
        voice = {'offer': f'/api/runs/{run}/offer', 'ice_servers': found.get('ice_servers', [])}
        return reply | voice if ask.channel == 'voice' else reply

    async def _snapshot(self, resume: ResumeFrom | None) -> dict[str, Any] | None:
        """The snapshot a run carries on from, or a 404 when the store has none for that turn."""
        if resume is None:
            return None
        snapshot = await self.store.snapshot(resume.run, resume.turn)
        if snapshot is None:
            raise HTTPException(404, f'no snapshot of {resume.run} at turn {resume.turn}')
        return snapshot

    @staticmethod
    def _new_row(run: str, agent: str, config: str, ask: StartRequest, runner: str) -> RunRow:
        """The row of a run this studio is placing."""
        resume = ask.resume_from
        return RunRow(
            id=run,
            agent=agent,
            config=config,
            channel=ask.channel,
            started_at=datetime.now(UTC),
            forked_from=resume.run if resume else None,
            forked_turn=resume.turn if resume else None,
            runner=runner,
        )

    def _live(self, run: str) -> Live:
        """A run live on a runner this studio knows, or a 404."""
        if (live := self.live.get(run)) is None:
            raise HTTPException(404, f'run {run} is not live on any runner this studio knows')
        return live

    async def offer(self, run: str, method: Literal['POST', 'PATCH'], body: dict[str, Any]) -> Any:
        """The browser's WebRTC offer or ICE candidates, passed to the runner that holds the call."""
        live = self._live(run)
        if not live.offer_url:
            raise HTTPException(409, f'run {run} is not a voice call')
        return await self.client.ask(method, live.offer_url, body)

    async def turn(self, run: str, text: str) -> dict[str, Any]:
        """A text run's next caller turn."""
        live = self._live(run)
        return await self.client.ask('POST', f'{live.runner_url}/calls/{run}/turns', {'text': text})

    async def hang_up(self, run: str) -> dict[str, Any]:
        """End a run, as the line dropping would."""
        live = self._live(run)
        return await self.client.ask('DELETE', f'{live.runner_url}/calls/{run}')

    async def state_schema(self, agent: str) -> dict[str, Any]:
        """The agent's state type as JSON schema, from a runner that carries it (the studio imports no use case)."""
        runner = self.carrier(agent)
        return await self.client.ask('GET', f'{runner.url}/agents/{agent}/schema')

    async def ingest(self, event: dict[str, Any]) -> None:
        """One event from a runner: into the store, onto its run's row, and out to every page watching the run."""
        self.events_in += 1
        run = str(event.get('run', ''))
        live = self.live.get(run) or await self._revive(run, event)
        if event.get('type') == 'turn.snapshot':
            await self.store.save_snapshot(run, int(event['turn']), event['snapshot'])
            shown = {key: value for key, value in event.items() if key != 'snapshot'}
        else:
            await self.store.add_event(run, event)
            shown = event
        if live.note(event):
            await self.store.save_run(live.row)
        live.tell(shown)

    async def _revive(self, run: str, event: dict[str, Any]) -> Live:
        """A run this studio did not place, or placed before it restarted: its row from the store, or from the event."""
        known = await self.store.run(run)
        if known is not None and event.get('type') != 'run.ended':
            known = known.model_copy(update={'outcome': None, 'ended_at': None})
        live = self.live[run] = Live(known or self._row_from(event), '')
        await self.store.save_run(live.row)
        return live

    @staticmethod
    def _row_from(event: dict[str, Any]) -> RunRow:
        """A row for a run this studio did not place (it restarted while the run went on), from what the event says."""
        return RunRow(
            id=str(event.get('run', '')),
            agent=str(event.get('agent', '?')),
            config=str(event.get('config', '')),
            channel=str(event.get('channel', 'voice')),
            started_at=datetime.now(UTC),
            forked_from=event.get('forked_from'),
            forked_turn=event.get('forked_turn'),
        )

    async def watch(self, run: str, socket: WebSocket) -> None:
        """A page follows one run: every event so far, then each new one, until the run ends or the page goes."""
        await socket.accept()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.WATCH_QUEUE)
        live = self.live.get(run)
        if live is not None:
            live.watchers.add(queue)
        try:
            sent = await self._replay(run, socket)
            row = live.row if live is not None else await self.store.run(run)
            if row is None:
                await socket.send_json({'type': 'studio.missing', 'run': run})
                return
            if live is None or row.outcome is not None:
                await socket.send_json({'type': 'studio.replayed', 'run': run})
                if live is None:
                    return
            await self._follow(run, socket, queue, sent)
        except WebSocketDisconnect:
            pass
        finally:
            if live is not None:
                live.watchers.discard(queue)
            with contextlib.suppress(RuntimeError):
                await socket.close()

    async def _replay(self, run: str, socket: WebSocket) -> int:
        """Every stored event of the run, in order; the last sequence number sent."""
        events = await self.store.events(run)
        for event in events:
            await socket.send_json(event)
        return max((int(event.get('seq', -1)) for event in events), default=-1)

    @staticmethod
    async def _follow(run: str, socket: WebSocket, queue: asyncio.Queue[dict[str, Any]], sent: int) -> None:
        """Each new event, skipping what the replay already sent, until the page goes."""
        while True:
            event = await queue.get()
            if int(event.get('seq', -1)) <= sent and event.get('type') != 'turn.snapshot':
                continue
            await socket.send_json(event)
            sent = max(sent, int(event.get('seq', -1)))
            if event.get('type') == 'run.ended':
                await socket.send_json({'type': 'studio.replayed', 'run': run})

    async def runs(self, agent: str = '') -> list[RunRow]:
        """Every run, live ones as they stand now, newest first, for one agent or all."""
        stored = {row.id: row for row in await self.store.runs(agent)}
        live = {each.row.id: each.row for each in self.live.values() if not agent or each.row.agent == agent}
        return sorted((stored | live).values(), key=lambda row: row.started_at, reverse=True)

    async def close(self) -> None:
        """Close the connections to runners."""
        await self.client.close()
