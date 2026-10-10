"""Every agent in the repository, read into the step graph the studio draws.

Each `agent.yaml` is read with core's own loader and no model key. `core.load` reads its shape
(the steps and what each declares); `studio.check`, in a process of its own, runs
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
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

import core
from core.flowspec import ActionSpec, ContextSpec, ConversationSpec, EndSpec

END = '$end'
"""The end node the studio adds: where every `goodbye` goes. A manifest's own end steps keep their ids."""

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


class Edge(BaseModel):
    source: str
    target: str
    kind: EdgeKind
    purpose: str = ''
    """What the manifest tells the model this move is for, where it says."""


class Node(BaseModel):
    id: str
    kind: StepKind
    roles: list[Role] = []
    agent: str = ''
    prompt: str = ''
    """The prompt file, relative to the manifest's folder."""
    fields: list[str] = []
    facts: list[str] | None = None
    """The state fields its agent sees; None is the flow's default."""
    operations: list[str] = []
    choices: list[str] = []
    offers: list[str] = []
    requires: list[str] = []
    opens_with: str = ''
    capabilities: list[str] = []
    virtual: bool = False
    """True only for the end node the studio adds."""


class Group(BaseModel):
    """Steps that can all hand the caller to one another: drawn once, with the moves inside it implied."""

    id: str
    steps: list[str]


class Graph(BaseModel):
    entry: str
    nodes: list[Node]
    edges: list[Edge]
    groups: list[Group] = []


class Agent(BaseModel):
    """One manifest, as the studio lists it."""

    id: str
    path: str
    """agent.yaml, relative to the repository root."""
    status: Literal['compiled', 'error']
    error: str = ''
    app_version: str = ''
    entry: str = ''
    model: str = ''
    steps: int = 0
    operations: int = 0
    graph: Graph | None = None


def find(root: Path, pattern: str = 'use-cases/*/*/agent.yaml') -> list[Agent]:
    """Every manifest under `root` matching `pattern`, each read on its own: one broken file hides no other."""
    return [read(path, root) for path in sorted(root.glob(pattern))]


class Checked(BaseModel):
    """What `studio.check` found: the loader's problems, and the engine's moves when there are none."""

    problems: list[str]
    moves: dict[str, list[str]]


_checked: dict[tuple[Path, float], Checked] = {}
"""Each manifest's check, by its folder's newest change: a save anywhere in it checks again."""


def read(path: Path, root: Path) -> Agent:
    shown = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    try:
        spec = core.load(path)
    except (core.FlowSpecError, yaml.YAMLError, OSError, ValueError) as error:
        return Agent(id=_id_or_folder(path), path=shown, status='error', error=str(error))
    found = checked(path)
    if found.problems:
        return Agent(
            id=spec.id or path.parent.name, path=shown, status='error', error='\n'.join(found.problems)
        )
    return Agent(
        id=spec.id or path.parent.name,
        path=shown,
        status='compiled',
        app_version=spec.app_version,
        entry=spec.entry or spec.steps[0].id,
        model=spec.model or '',
        steps=len(spec.steps),
        operations=len(spec.operations),
        graph=graph(spec, found.moves),
    )


def checked(path: Path) -> Checked:
    """`studio.check` on this manifest, in its own process; cached per folder state."""
    key = (path.resolve(), _newest(path.parent))
    if key not in _checked:
        try:
            done = subprocess.run(
                [sys.executable, '-m', 'studio.check', str(path)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Checked(
                problems=['the check did not finish in 60 s: an import in the application hangs'], moves={}
            )
        if done.returncode != 0:
            last = done.stderr.strip().splitlines()[-1:] or [f'exit {done.returncode}']
            return Checked(problems=[f'the check crashed: {last[0]}'], moves={})
        _checked[key] = Checked.model_validate(json.loads(done.stdout))
    return _checked[key]


def _newest(folder: Path) -> float:
    return max((f.stat().st_mtime for f in folder.rglob('*') if '__pycache__' not in f.parts), default=0.0)


def graph(spec: core.FlowSpec, moves: dict[str, list[str]]) -> Graph:
    steps = {step.id: step for step in spec.steps}
    entry = spec.entry or spec.steps[0].id
    edges = [_edge(steps, source, target) for source, targets in moves.items() for target in targets]
    consulted = {c.to for step in steps.values() if isinstance(step, ConversationSpec) for c in step.consults}
    edges += [
        Edge(source=step.id, target=END, kind='goodbye')
        for step in steps.values()
        if isinstance(step, ConversationSpec) and step.id not in consulted
    ]
    gates = {edge.target for edge in edges if edge.kind == 'prerequisite'}
    nodes = [_node(step, entry, gates, consulted) for step in steps.values()]
    if any(edge.target == END for edge in edges):
        nodes.append(Node(id=END, kind='end', virtual=True))
    return Graph(entry=entry, nodes=nodes, edges=edges, groups=groups(edges, exclude={entry, *consulted}))


def _edge(steps: dict[str, ConversationSpec | ActionSpec | EndSpec], source: str, target: str) -> Edge:
    """One engine move, labelled with the manifest's reason for it, checked in the order the engine adds them."""
    step = steps[source]

    def edge(kind: EdgeKind, purpose: str = '') -> Edge:
        return Edge(source=source, target=target, kind=kind, purpose=purpose)

    if isinstance(step, ConversationSpec):
        for exit in step.exits:
            if exit.to == target:
                return edge('exit', exit.purpose)
        for consult in step.consults:
            if consult.to == target:
                return edge('consult', consult.purpose)
        needed = [step, *(steps[exit.to] for exit in step.exits if exit.to in steps)]
        for other in needed:
            if isinstance(other, ConversationSpec) and other.satisfied_by == target:
                return edge('prerequisite', 'requires ' + ', '.join(other.requires))
    other = steps.get(target)
    if isinstance(other, ConversationSpec) and any(c.to == source for c in other.consults):
        return edge('handback', 'back to the step that consulted it')
    if isinstance(step, ActionSpec):
        for kind, to in (('then', step.then), ('error', step.on_error), ('unknown', step.on_unknown)):
            if to == target:
                return edge(kind)
    if isinstance(step, EndSpec) and step.take_back == target:
        return edge('take_back')
    satisfier = any(getattr(s, 'satisfied_by', '') == source for s in steps.values())
    if satisfier or (isinstance(step, ActionSpec) and step.returns):
        return edge('return', 'back to the step that was waiting')
    return edge('route', "an operation's receipt names it")


def groups(edges: list[Edge], exclude: set[str]) -> list[Group]:
    """Largest sets of three or more steps that all hand over to one another, none shared, biggest first.

    Bron-Kerbosch on the graph of two-way exits; flows have tens of steps, so the plain form is enough.
    """
    exits = {(e.source, e.target) for e in edges if e.kind == 'exit'}
    near: dict[str, set[str]] = {}
    for a, b in exits:
        if (b, a) in exits and a not in exclude and b not in exclude:
            near.setdefault(a, set()).add(b)
    cliques: list[set[str]] = []

    def grow(r: set[str], p: set[str], x: set[str]) -> None:
        if not p and not x:
            cliques.append(r)
        for v in sorted(p):
            grow(r | {v}, p & near[v], x & near[v])
            p = p - {v}
            x = x | {v}

    grow(set(), set(near), set())
    taken: set[str] = set()
    found: list[Group] = []
    for clique in sorted(cliques, key=lambda c: (-len(c), sorted(c))):
        if len(clique) >= 3 and not clique & taken:
            taken |= clique
            found.append(Group(id=f'group-{len(found) + 1}', steps=sorted(clique)))
    return found


def _node(
    step: ConversationSpec | ActionSpec | EndSpec, entry: str, gates: set[str], consulted: set[str]
) -> Node:
    roles: list[Role] = [
        role
        for role, holds in (
            ('entry', step.id == entry),
            ('gate', step.id in gates),
            ('consulted', step.id in consulted),
        )
        if holds
    ]
    match step:
        case ConversationSpec():
            context = step.context if isinstance(step.context, ContextSpec) else None
            facts = list(context.facts) if context is not None and isinstance(context.facts, list) else None
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


def _id_or_folder(path: Path) -> str:
    """A broken manifest still gets a name: its own `id:` if YAML can read it, else its folder's."""
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError, OSError:
        return path.parent.name
    found = data.get('id') if isinstance(data, dict) else None
    return found if isinstance(found, str) and found else path.parent.name
