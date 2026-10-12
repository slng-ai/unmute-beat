"""Every agent in the repository, read into the step graph the studio draws.

Each `agent.yaml` is read with core's own loader and no model key. `core.load` reads its shape
(the steps and what each declares); `studio.agents.check`, in a process of its own, runs
`FlowSpec.problems()` against the application's code and, when there are none, compiles the flow
on a test model and returns `Flow.moves`: every move the engine allows. So an agent the studio
shows as compiled is one a launcher can open, a broken one carries the loader's own words, and
the lines on the page are the engine's, never a reading of the YAML that could miss one. Each
result is kept until a file in the manifest's folder changes.

Each move is labelled by why the engine allows it, from the manifest:

    exit           a conversation step's `exits` (the model's `hand_over`)
    consult        a step's `consults`
    handback       a consulted step back to a step that consults it (`done`)
    prerequisite   to the step that satisfies a `requires`, interposed by the graph
    return         a satisfier back to a step that was waiting on it, or to the first step
    route          an operation's receipt naming the next step (`route:`)
    then           an action step's next step; `error` and `unknown` its failure paths
    take_back      an end step's undo
    goodbye        every conversation step's `goodbye`, to the one end node the studio adds

Steps that all hand over to one another (`exit` both ways, three or more, the entry aside) form a
group (`Group`): the map view draws the group once instead of every line inside it.
"""

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar, Literal

import yaml
from pydantic import BaseModel

import core
from core.flowspec import ActionSpec, ContextSpec, ConversationSpec, EndSpec

type EdgeKind = Literal[
    'exit',
    'consult',
    'handback',
    'prerequisite',
    'return',
    'route',
    'then',
    'error',
    'unknown',
    'take_back',
    'goodbye',
]
type StepKind = Literal['conversation', 'action', 'end']
type Role = Literal['entry', 'gate', 'consulted']
type StepSpec = ConversationSpec | ActionSpec | EndSpec


class Edge(BaseModel):
    """One move the engine allows, and what the manifest tells the model it is for, where it says."""

    source: str
    target: str
    kind: EdgeKind
    purpose: str = ''


class Node(BaseModel):
    """One step as the map draws it.

    `prompt` is relative to the manifest's folder; `facts` are the state fields its agent sees, None
    for the flow's default; `virtual` marks the one end node the studio adds.
    """

    id: str
    kind: StepKind
    roles: list[Role] = []
    agent: str = ''
    prompt: str = ''
    fields: list[str] = []
    facts: list[str] | None = None
    operations: list[str] = []
    choices: list[str] = []
    offers: list[str] = []
    requires: list[str] = []
    opens_with: str = ''
    capabilities: list[str] = []
    virtual: bool = False


class Group(BaseModel):
    """Steps that can all hand the caller to one another: drawn once, with the moves inside it implied."""

    id: str
    steps: list[str]


class Graph(BaseModel):
    """An agent's steps, its moves and its groups."""

    entry: str
    nodes: list[Node]
    edges: list[Edge]
    groups: list[Group] = []


class Agent(BaseModel):
    """One manifest, as the studio lists it: `path` is its agent.yaml, relative to the repository root."""

    id: str
    path: str
    status: Literal['compiled', 'error']
    error: str = ''
    app_version: str = ''
    entry: str = ''
    model: str = ''
    steps: int = 0
    operations: int = 0
    graph: Graph | None = None


class CheckResult(BaseModel):
    """What `studio.agents.check` found: the loader's problems, the engine's moves, each operation, and the facts."""

    problems: list[str]
    moves: dict[str, list[str]] = {}
    operations: list[dict[str, Any]] = []
    facts: dict[str, Any] = {}


class AgentCheckProcess:
    """`studio.agents.check` on one manifest, in a process of its own, kept until its folder changes.

    `keep=False` checks a copy that is about to be deleted (the editor's), which is never asked about again.
    """

    cache: ClassVar[dict[tuple[Path, float], CheckResult]] = {}

    def __init__(self, manifest: Path, timeout: float = 90, keep: bool = True) -> None:
        self.manifest = manifest
        self.timeout = timeout
        self.keep = keep

    def result(self) -> CheckResult:
        """The check, from the cache when nothing in the folder changed since. A hang or a crash is not kept."""
        key = (self.manifest.resolve(), self._newest())
        if key in self.cache:
            return self.cache[key]
        found, lasting = self._run()
        if lasting and self.keep:
            self.cache[key] = found
        return found

    def _run(self) -> tuple[CheckResult, bool]:
        """The check's answer, and whether it is worth keeping: a hang or a crash may pass on its own."""
        try:
            done = subprocess.run(
                [sys.executable, '-m', 'studio.agents.check', str(self.manifest)],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            hung = f'the check did not finish in {self.timeout:.0f} s: an import in the application hangs'
            return CheckResult(problems=[hung]), False
        if done.returncode != 0:
            last = done.stderr.strip().splitlines()[-1:] or [f'exit {done.returncode}']
            return CheckResult(problems=[f'the check crashed: {last[0]}']), False
        return CheckResult.model_validate(json.loads(done.stdout)), True

    def _newest(self) -> float:
        """When anything in the manifest's folder last changed."""
        files = (f for f in self.manifest.parent.rglob('*') if '__pycache__' not in f.parts)
        return max((f.stat().st_mtime for f in files), default=0.0)


class GraphBuilder:
    """An agent's map: its steps as nodes, the engine's moves as edges labelled by why, and its groups.

    `END` is the end node the studio adds, where every `goodbye` goes; a manifest's own end steps keep their ids.
    """

    END = '$end'

    def __init__(self, spec: core.FlowSpec, moves: dict[str, list[str]]) -> None:
        self.steps: dict[str, StepSpec] = {step.id: step for step in spec.steps}
        self.entry = spec.entry or spec.steps[0].id
        self.moves = moves
        self.consulted = {
            consult.to
            for step in self.steps.values()
            if isinstance(step, ConversationSpec)
            for consult in step.consults
        }

    def graph(self) -> Graph:
        """The whole map, with one end node added when any step can say goodbye."""
        edges = [self._edge(source, target) for source, targets in self.moves.items() for target in targets]
        edges += [
            Edge(source=step.id, target=self.END, kind='goodbye')
            for step in self.steps.values()
            if isinstance(step, ConversationSpec) and step.id not in self.consulted
        ]
        gates = {edge.target for edge in edges if edge.kind == 'prerequisite'}
        nodes = [self._node(step, gates) for step in self.steps.values()]
        if any(edge.target == self.END for edge in edges):
            nodes.append(Node(id=self.END, kind='end', virtual=True))
        groups = Cliques(edges, exclude={self.entry, *self.consulted}).groups()
        return Graph(entry=self.entry, nodes=nodes, edges=edges, groups=groups)

    def _edge(self, source: str, target: str) -> Edge:
        """One engine move, labelled with the manifest's reason for it, in the order the engine adds them."""
        kind, purpose = next(self._reasons(source, target), ('route', "an operation's receipt names it"))
        return Edge(source=source, target=target, kind=kind, purpose=purpose)

    def _reasons(self, source: str, target: str) -> Iterator[tuple[EdgeKind, str]]:
        """Every reason the manifest gives for this move, the engine's first first."""
        step, other = self.steps[source], self.steps.get(target)
        if isinstance(step, ConversationSpec):
            yield from (('exit', exit.purpose) for exit in step.exits if exit.to == target)
            yield from (('consult', consult.purpose) for consult in step.consults if consult.to == target)
            needed = [step, *(self.steps[exit.to] for exit in step.exits if exit.to in self.steps)]
            yield from (
                ('prerequisite', 'requires ' + ', '.join(waiting.requires))
                for waiting in needed
                if isinstance(waiting, ConversationSpec) and waiting.satisfied_by == target
            )
        if isinstance(other, ConversationSpec) and any(consult.to == source for consult in other.consults):
            yield 'handback', 'back to the step that consulted it'
        if isinstance(step, ActionSpec):
            routes: list[tuple[EdgeKind, str | None]] = [
                ('then', step.then),
                ('error', step.on_error),
                ('unknown', step.on_unknown),
            ]
            yield from ((kind, '') for kind, to in routes if to == target)
        if isinstance(step, EndSpec) and step.take_back == target:
            yield 'take_back', ''
        satisfier = any(getattr(each, 'satisfied_by', '') == source for each in self.steps.values())
        if satisfier or (isinstance(step, ActionSpec) and step.returns):
            yield 'return', 'back to the step that was waiting'

    def _node(self, step: StepSpec, gates: set[str]) -> Node:
        """One step as a node, with the roles the graph gives it."""
        held = (
            ('entry', step.id == self.entry),
            ('gate', step.id in gates),
            ('consulted', step.id in self.consulted),
        )
        roles: list[Role] = [role for role, holds in held if holds]
        match step:
            case ConversationSpec():
                context = step.context if isinstance(step.context, ContextSpec) else None
                facts = (
                    list(context.facts) if context is not None and isinstance(context.facts, list) else None
                )
                return Node(
                    id=step.id,
                    kind='conversation',
                    roles=roles,
                    agent=step.agent,
                    prompt=step.prompt,
                    fields=list(step.fields),
                    facts=facts,
                    operations=list(step.operations),
                    choices=list(step.choices),
                    offers=list(step.offers),
                    requires=list(step.requires),
                    opens_with=step.opens_with,
                    capabilities=list(step.capabilities),
                )
            case ActionSpec():
                return Node(id=step.id, kind='action', roles=roles, operations=[step.uses])
            case EndSpec():
                return Node(id=step.id, kind='end', roles=roles)


class Cliques:
    """Largest sets of three or more steps that all hand over to one another, none shared, biggest first.

    Bron-Kerbosch on the graph of two-way exits; flows have tens of steps, so the plain form is enough.
    """

    def __init__(self, edges: list[Edge], exclude: set[str]) -> None:
        exits = {(edge.source, edge.target) for edge in edges if edge.kind == 'exit'}
        self.near: dict[str, set[str]] = {}
        for a, b in exits:
            if (b, a) in exits and a not in exclude and b not in exclude:
                self.near.setdefault(a, set()).add(b)

    def groups(self) -> list[Group]:
        """The groups to draw, numbered biggest first."""
        taken: set[str] = set()
        chosen: list[set[str]] = []
        for clique in sorted(self._cliques(set(), set(self.near), set()), key=lambda c: (-len(c), sorted(c))):
            if len(clique) >= 3 and not clique & taken:
                taken |= clique
                chosen.append(clique)
        return [Group(id=f'group-{n}', steps=sorted(clique)) for n, clique in enumerate(chosen, 1)]

    def _cliques(self, r: set[str], p: set[str], x: set[str]) -> Iterator[set[str]]:
        """Every maximal clique growing from `r`, with `p` still possible and `x` already covered."""
        if not p and not x:
            yield r
        for v in sorted(p):
            yield from self._cliques(r | {v}, p & self.near[v], x & self.near[v])
            p, x = p - {v}, x | {v}


class Catalog:
    """Every manifest under the repository matching a glob, each read on its own: one broken file hides no other."""

    def __init__(self, root: Path, pattern: str = 'use-cases/*/*/agent.yaml') -> None:
        self.root = root
        self.pattern = pattern

    def agents(self) -> list[Agent]:
        """Every agent, in path order."""
        return [self.read(path) for path in sorted(self.root.glob(self.pattern))]

    def read(self, path: Path) -> Agent:
        """One manifest: compiled with its graph, or an error in the loader's own words."""
        shown = str(path.relative_to(self.root)) if path.is_relative_to(self.root) else str(path)
        try:
            spec = core.load(path)
        except (core.FlowSpecError, yaml.YAMLError, OSError, ValueError) as error:
            return Agent(id=self._name(path), path=shown, status='error', error=str(error))
        found = AgentCheckProcess(path).result()
        name = spec.id or path.parent.name
        if found.problems:
            return Agent(id=name, path=shown, status='error', error='\n'.join(found.problems))
        return Agent(
            id=name,
            path=shown,
            status='compiled',
            app_version=spec.app_version,
            entry=spec.entry or spec.steps[0].id,
            model=spec.model or '',
            steps=len(spec.steps),
            operations=len(spec.operations),
            graph=GraphBuilder(spec, found.moves).graph(),
        )

    @staticmethod
    def _name(path: Path) -> str:
        """A broken manifest still gets a name: its own `id:` if YAML can read it, else its folder's."""
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError, OSError:
            return path.parent.name
        found = data.get('id') if isinstance(data, dict) else None
        return found if isinstance(found, str) and found else path.parent.name
