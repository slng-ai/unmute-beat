"""Run: uv run python -m tests.test_discovery

Every agent.yaml is read with `core.load` and drawn as its declared moves, nothing inferred.
The desk fixture (`tests/fixtures/desk`) has one of each edge kind a conversation step makes;
the repository's own use cases must all compile, as a launcher would open them.
"""

import shutil
import tempfile
from pathlib import Path

from studio import discovery
from studio.settings import Settings

HERE = Path(__file__).parent
DESK = HERE / 'fixtures' / 'desk'


def the_desk_is_drawn_as_declared():
    agent = discovery.read(DESK / 'agent.yaml', HERE)
    assert agent.status == 'compiled', agent.error
    assert (agent.id, agent.entry, agent.steps, agent.path) == (
        'desk',
        'front',
        4,
        'fixtures/desk/agent.yaml',
    )
    graph = agent.graph
    assert graph is not None
    nodes = {node.id: node for node in graph.nodes}
    assert list(nodes) == ['front', 'verify', 'account', 'expert', discovery.END], list(nodes)
    assert (
        nodes['front'].roles == ['entry']
        and nodes['verify'].roles == ['gate']
        and nodes['expert'].roles == ['consulted']
    )
    assert nodes[discovery.END].virtual and nodes[discovery.END].kind == 'end'
    assert nodes['front'].facts == ['topic'] and nodes['account'].facts is None, 'None is the flow default'
    edges = {(edge.source, edge.target, edge.kind) for edge in graph.edges}
    assert ('front', 'account', 'exit') in edges
    assert ('front', 'expert', 'consult') in edges
    assert ('account', 'verify', 'prerequisite') in edges
    assert ('verify', 'front', 'exit') in edges
    assert ('front', 'verify', 'prerequisite') in edges, (
        'an exit whose target requires a caller interposes verify'
    )
    assert ('verify', 'account', 'return') in edges, 'the satisfier returns to the step that was waiting'
    assert ('expert', 'front', 'handback') in edges, 'the consulted step hands the caller back'
    assert not graph.groups, 'no three steps hand over to one another'
    goodbyes = {source for source, target, kind in edges if kind == 'goodbye'}
    assert goodbyes == {'front', 'verify', 'account'}, 'a consulted step hands back; it does not say goodbye'
    purpose = next(edge.purpose for edge in graph.edges if edge.kind == 'prerequisite')
    assert purpose == 'requires caller', purpose


def a_broken_manifest_carries_the_loaders_error():
    """The studio shows core's own FlowSpecError, naming the step, and keeps the manifest's id."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / 'desk'
        shutil.copytree(DESK, folder)
        manifest = folder / 'agent.yaml'
        manifest.write_text(manifest.read_text().replace('{to: account,', '{to: acount,'))
        agent = discovery.read(manifest, Path(tmp))
    assert agent.status == 'error' and agent.graph is None
    assert agent.id == 'desk', 'the id is read from YAML even when the flow does not compile'
    assert 'front' in agent.error and 'acount' in agent.error, agent.error


def one_broken_file_hides_no_other():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shutil.copytree(DESK, root / 'a' / 'desk')
        (root / 'b' / 'mess').mkdir(parents=True)
        (root / 'b' / 'mess' / 'agent.yaml').write_text('steps: [unclosed')
        found = discovery.find(root, '*/*/agent.yaml')
    assert [(agent.id, agent.status) for agent in found] == [('desk', 'compiled'), ('mess', 'error')], found


def every_use_case_in_the_repository_compiles():
    settings = Settings.from_env()
    found = {agent.id: agent for agent in discovery.find(settings.root, settings.agents)}
    assert {'sage-and-stone', 'faro', 'nosh', 'wasla', 'kestrel'} <= set(found), sorted(found)
    broken = {agent.id: agent.error for agent in found.values() if agent.status == 'error'}
    assert not broken, broken
    salon = found['sage-and-stone'].graph
    assert salon is not None and salon.entry == 'concierge'
    stylist = next(node for node in salon.nodes if node.id == 'stylist')
    assert stylist.roles == ['consulted'], stylist.roles
    moves = {(edge.source, edge.target, edge.kind) for edge in salon.edges}
    assert ('verify', 'booking', 'return') in moves and ('stylist', 'booking', 'handback') in moves, (
        "the moves back are the engine's, not only what the YAML declares"
    )
    assert [group.steps for group in salon.groups] == [['booking', 'cancelling', 'complaints', 'moving']]
    faro = found['faro'].graph
    assert faro is not None and any(edge.kind == 'route' for edge in faro.edges), (
        'receipts that move the learner'
    )


if __name__ == '__main__':
    the_desk_is_drawn_as_declared()
    a_broken_manifest_carries_the_loaders_error()
    one_broken_file_hides_no_other()
    every_use_case_in_the_repository_compiles()
    print('discovery ok: declared moves, loader errors, one file at a time, every use case')
