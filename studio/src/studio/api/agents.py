"""The agents' routes: each agent and its graph, its state schema, and its config read, checked and saved."""

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from studio.agents.catalog import Agent
from studio.agents.editor import ConfigEditor
from studio.agents.models import Checked, Config, Edit, Saved
from studio.runs.store import ConfigRow

if TYPE_CHECKING:
    from studio.api.app import Studio


class AgentRoutes:
    """Everything under `/api/agents`."""

    RECEIPT_RUNS = 30

    def __init__(self, studio: Studio) -> None:
        self.studio = studio

    def router(self) -> APIRouter:
        """The routes, bound to this studio."""
        router = APIRouter(prefix='/api/agents')
        router.add_api_route('', self.listed, methods=['GET'], response_model_exclude={'__all__': {'graph'}})
        router.add_api_route('/{agent_id}', self.one, methods=['GET'])
        router.add_api_route('/{agent_id}/schema', self.state_schema, methods=['GET'])
        router.add_api_route('/{agent_id}/config', self.config, methods=['GET'])
        router.add_api_route('/{agent_id}/config/check', self.check, methods=['POST'])
        router.add_api_route('/{agent_id}/config', self.save, methods=['PUT'])
        router.add_api_route('/{agent_id}/receipts', self.receipts, methods=['GET'])
        router.add_api_route('/{agent_id}/configs', self.versions, methods=['GET'])
        return router

    def listed(self) -> list[Agent]:
        """Every agent, without its graph."""
        return self.studio.agents()

    def one(self, agent_id: str) -> Agent:
        """One agent, with its graph, or a 404."""
        found = next((agent for agent in self.studio.agents() if agent.id == agent_id), None)
        if found is None:
            raise HTTPException(404, f'no agent {agent_id!r} under {self.studio.settings.agents}')
        return found

    async def state_schema(self, agent_id: str) -> dict[str, Any]:
        """The agent's state type as JSON schema, from a runner that carries it: the go-back form is built from it."""
        return await self.studio.hub.state_schema(agent_id)

    def editor(self, agent_id: str) -> ConfigEditor:
        """The editor over one agent's config."""
        agent, root = self.one(agent_id), self.studio.settings.root
        return ConfigEditor(root / agent.path, agent.id, root)

    def config(self, agent_id: str) -> Config:
        """The config as the editor opens it."""
        return self.editor(agent_id).read()

    def check(self, agent_id: str, edit: Edit) -> Checked:
        """An edit checked on a copy, as a save would check it."""
        return self.editor(agent_id).check(edit)

    async def save(self, agent_id: str, edit: Edit) -> Saved:
        """A checked edit written, and its version kept."""
        saved = self.editor(agent_id).save(edit)
        if saved.ok and saved.config is not None:
            await self.studio.store.save_config(saved.config)
        return saved

    async def receipts(self, agent_id: str) -> dict[str, dict[str, dict[str, Any]]]:
        """Each operation's latest receipt of each result in recent runs: what the editor fills a line from.

        {"lookup_customer": {"done": {...}, "refused": {...}}}
        """
        found: dict[str, dict[str, dict[str, Any]]] = {}
        for row in (await self.studio.hub.runs(agent_id))[: self.RECEIPT_RUNS]:
            events = await self.studio.store.events(row.id)
            for event in reversed([event for event in events if event.get('type') == 'receipt']):
                result = 'done' if event.get('succeeded') else 'refused'
                found.setdefault(event['operation'], {}).setdefault(result, event['receipt'])
        return found

    async def versions(self, agent_id: str) -> list[ConfigRow]:
        """Every saved version of the agent's config, newest first."""
        return await self.studio.store.configs(agent_id)
