"""Every agent in the repository, read into the step graph the studio draws.

Each `agent.yaml` is read twice, both with core's own loader and no model. `core.load` reads its
shape (the graph comes from it); `FlowSpec.problems()` then checks every step, exit, field,
operation and prompt against the application's code, in a process of its own (`studio.check`).
So an agent the studio shows as compiled is one a launcher can open, and one it shows as broken
carries the loader's own words, naming the step. Each result is kept until a file in the
manifest's folder changes.

The graph is the manifest's declared moves, nothing inferred:

    exit           a conversation step's `exits` (the model's `hand_over`)
    consult        a step's `consults`; the specialist hands the caller back on `done`
    prerequisite   `satisfied_by`: the graph puts that step in front when `requires` is unmet
    then           an action step's next step; `error` and `unknown` its failure paths
    take_back      an end step's undo
    goodbye        every conversation step's `goodbye`, to the one end node the studio adds

ponytail: an operation's `route:` (a receipt naming the next step) is a Literal on the receipt
class, which only resolving the operation reads. It is left out until the engine reports it,
which needs the application's packages importable; upgrade by resolving through `FlowSpec`.
"""

import json
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

import core
from core.flowspec import ActionSpec, ContextSpec, ConversationSpec, EndSpec

END = '$end'
"""The end node the studio adds: where every `goodbye` goes. A manifest's own end steps keep their ids."""

type EdgeKind = Literal['exit', 'consult', 'prerequisite', 'then', 'error', 'unknown', 'take_back', 'goodbye']
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


class Graph(BaseModel):
    entry: str
    nodes: list[Node]
    edges: list[Edge]


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


_checked: dict[tuple[Path, float], list[str]] = {}
"""Each manifest's problems, by its folder's newest change: a save anywhere in it checks again."""


def read(path: Path, root: Path) -> Agent:
    shown = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    try:
        spec = core.load(path)
    except (core.FlowSpecError, yaml.YAMLError, OSError, ValueError) as error:
        return Agent(id=_id_or_folder(path), path=shown, status='error', error=str(error))
    found = problems(path)
    if found:
        return Agent(id=spec.id or path.parent.name, path=shown, status='error', error='\n'.join(found))
    return Agent(
        id=spec.id or path.parent.name,
        path=shown,
        status='compiled',
        app_version=spec.app_version,
        entry=spec.entry or spec.steps[0].id,
        model=spec.model or '',
        steps=len(spec.steps),
        operations=len(spec.operations),
        graph=graph(spec),
    )


def problems(path: Path) -> list[str]:
    """`FlowSpec.problems()` for this manifest, from `studio.check` in its own process; cached per folder state."""
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
            return ['the check did not finish in 60 s: an import in the application hangs']
        if done.returncode != 0:
            last = done.stderr.strip().splitlines()[-1:] or [f'exit {done.returncode}']
            return [f'the check crashed: {last[0]}']
        _checked[key] = json.loads(done.stdout)
    return _checked[key]


def _newest(folder: Path) -> float:
    return max((f.stat().st_mtime for f in folder.rglob('*') if '__pycache__' not in f.parts), default=0.0)


def graph(spec: core.FlowSpec) -> Graph:
    steps = spec.steps
    entry = spec.entry or steps[0].id
    edges = list(_edges(steps))
    gates = {edge.target for edge in edges if edge.kind == 'prerequisite'}
    consulted = {edge.target for edge in edges if edge.kind == 'consult'}
    nodes = [_node(step, entry, gates, consulted) for step in steps]
    if any(edge.target == END for edge in edges):
        nodes.append(Node(id=END, kind='end', virtual=True))
    return Graph(entry=entry, nodes=nodes, edges=edges)


def _edges(steps: Iterable[ConversationSpec | ActionSpec | EndSpec]) -> Iterable[Edge]:
    consulted = {c.to for step in steps if isinstance(step, ConversationSpec) for c in step.consults}
    for step in steps:
        if step.then:
            yield Edge(source=step.id, target=step.then, kind='then')
        match step:
            case ConversationSpec():
                for exit in step.exits:
                    yield Edge(source=step.id, target=exit.to, kind='exit', purpose=exit.purpose)
                for consult in step.consults:
                    yield Edge(source=step.id, target=consult.to, kind='consult', purpose=consult.purpose)
                if step.satisfied_by:
                    yield Edge(
                        source=step.id,
                        target=step.satisfied_by,
                        kind='prerequisite',
                        purpose='requires ' + ', '.join(step.requires),
                    )
                if step.id not in consulted:
                    yield Edge(source=step.id, target=END, kind='goodbye')
            case ActionSpec():
                if step.on_error:
                    yield Edge(source=step.id, target=step.on_error, kind='error')
                if step.on_unknown:
                    yield Edge(source=step.id, target=step.on_unknown, kind='unknown')
            case EndSpec():
                if step.take_back:
                    yield Edge(source=step.id, target=step.take_back, kind='take_back')


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
