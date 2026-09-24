"""An agency's steps as a Pydantic graph: which step may follow which, checked while the call runs.

`Flow.of(nodes)` builds one `pydantic_graph.Graph` per steps table. Each
step is a graph step; after it, a decision with a branch for every move that
step can legally make and one to the end. A move the table does not allow
matches no branch, and the graph refuses it, so an illegal transition is an
error rather than a surprise. `render()` draws the same graph as Mermaid.

What a step *does* is not here. A graph step asks its driver to run one hop
(`Hopper.hop`) and gets back where the caller is now. The turn engine is the
driver, because streaming speech, barge-in, the budget and the retained
history are the turn's business, not the graph's.

ponytail: one short graph run per caller turn, starting at the step the
caller is in. `pydantic_graph` 2.46 has no save-and-resume, and a call's
position already lives on its state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from pydantic_graph import Graph, GraphBuilder, StepContext
from pydantic_graph.step import StepFunction

from core.graph import Node


@dataclass(frozen=True)
class Moved:
    """The caller is now in `to`, and that step runs next in this turn with `prompt`."""

    to: str
    prompt: str | None = None


@dataclass(frozen=True)
class Answered:
    """The turn is over: answered, interrupted, failed or out of budget. The graph ends."""


class Hopper(Protocol):
    """What drives the graph: runs one step of a turn and says where the caller is afterwards."""

    async def hop(self, step: str, prompt: str | None) -> Moved | Answered: ...


def legal_moves(nodes: dict[str, Node[Any]]) -> dict[str, frozenset[str]]:
    """Every step each step can hand the caller to within one turn.

    A step's own exits; the step that satisfies the prerequisites of an exit,
    or of the step itself (a step handing the caller back to itself before
    they are identified is interposed too); and for a satisfier, every step
    that could be waiting on it plus the first step in the table, where a
    finished satisfier goes when nobody is.
    """
    home = next(iter(nodes))
    moves: dict[str, set[str]] = {}
    for name, node in nodes.items():
        reach = {exit.to for exit in node.exits} | {name}
        reach |= {nodes[to].satisfied_by for to in list(reach) if nodes[to].satisfied_by}
        moves[name] = reach
    for name in nodes:
        if any(other.satisfied_by == name for other in nodes.values()):
            waiting = {n for n, reach in moves.items() if name in reach and n != name}
            waiting |= {n for n, other in nodes.items() if other.satisfied_by == name}
            moves[name] |= waiting | {home}
    return {name: frozenset(reach - {name}) for name, reach in moves.items()}


class Flow:
    """One steps table, compiled once into a graph and shared by every call that uses it."""

    _built: ClassVar[dict[int, tuple[dict[str, Node[Any]], Flow]]] = {}
    """Every table compiled so far, with the table itself held so its id cannot be reused."""

    def __init__(self, nodes: dict[str, Node[Any]]) -> None:
        self.nodes = nodes
        self.moves = legal_moves(nodes)
        self.graph = self._build()

    @classmethod
    def of(cls, nodes: dict[str, Node[Any]]) -> Flow:
        """The flow for this table, built on first use. Keyed by identity: a table is a module-level constant."""
        cached = cls._built.get(id(nodes))
        if cached is None or cached[0] is not nodes:
            cached = cls._built[id(nodes)] = (nodes, cls(nodes))
        return cached[1]

    def _build(self) -> Graph[None, Hopper, Moved, Answered]:
        g = GraphBuilder[None, Hopper, Moved, Answered](
            state_type=type(None), deps_type=Hopper, input_type=Moved, output_type=Answered
        )
        steps = {name: g.step(_runner(name), node_id=name) for name in self.nodes}

        taken = set(self.nodes)

        def label(wanted: str) -> str:
            """A decision id for the diagram that no step already uses."""
            while wanted in taken:
                wanted += '_'
            taken.add(wanted)
            return wanted

        def branches(to: frozenset[str] | set[str], after: str):
            decision = g.decision(node_id=label(f'after_{after}'))
            for target in sorted(to):
                decision = decision.branch(
                    g.match(Moved, matches=lambda o, t=target: isinstance(o, Moved) and o.to == t).to(
                        steps[target]
                    )
                )
            return decision.branch(g.match(Answered).to(g.end_node))

        g.add(g.edge_from(g.start_node).to(branches(set(self.nodes), 'turn_start')))
        for name, step in steps.items():
            g.add(g.edge_from(step).to(branches(self.moves[name], name)))  # ty: ignore[invalid-argument-type]
        return g.build()

    async def run(self, driver: Hopper, start: str, prompt: str | None) -> Answered:
        """One caller turn through the graph, from the step the caller is in."""
        return await self.graph.run(state=None, deps=driver, inputs=Moved(start, prompt))

    def render(self) -> str:
        """The flow as a Mermaid state diagram."""
        return self.graph.render()


def _runner(name: str) -> StepFunction[None, Hopper, Moved, Moved | Answered]:
    """The graph step for one table step: ask the driver to run it."""

    async def run(ctx: StepContext[None, Hopper, Moved]) -> Moved | Answered:
        return await ctx.deps.hop(name, ctx.inputs.prompt)

    run.__name__ = run.__qualname__ = f'step_{name}'
    return run
