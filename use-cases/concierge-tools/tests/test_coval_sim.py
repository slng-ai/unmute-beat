"""Run: uv run python -m tests.test_coval_sim

The Coval release check, with no Coval: which one job a set of arguments names
or why it is refused, what the run log remembers between invocations, and
which call results fail a run. The CLI, the tunnel and the bot are not run,
and nothing here starts the coval binary: every method that would is replaced.

Asserts, no pytest, no fixtures, same as the rest.
"""

import tempfile
from pathlib import Path
from typing import Any

from coval_sim.__main__ import (
    BOT_ENV,
    GRAPH_BUILD,
    GRAPH_ENV,
    MAX_CALL_SECONDS,
    PlanError,
    ReleaseCheck,
    Repository,
    RunLog,
)
from coval_sim.coval import CovalClient, CovalError
from coval_sim.models import (
    FALLBACKS,
    Agent,
    Message,
    MetricResult,
    MetricValue,
    Persona,
    Run,
    RunResults,
    Simulation,
)


class FakeCoval(CovalClient):
    """A CovalClient that never runs coval. Any method not replaced here raises, so a test cannot reach the network."""

    def __init__(self, agents: list[Agent], found: str | None = None, status: str = 'COMPLETED') -> None:
        super().__init__(api_key='test', binary='/nonexistent/coval')
        self._agents = agents
        self.found = found
        self.status = status
        self.launches = 0

    def _call(self, *args: str):
        raise AssertionError(f'a test reached the coval binary: {args}')

    def agents(self) -> list[Agent]:
        return self._agents

    def personas(self) -> list[Persona]:
        return [
            Persona(id='calm', name='Standard Customer'),
            Persona(id='p2', name='Twin'),
            Persona(id='p3', name='Twin'),
        ]

    def test_case_ids(self, test_set_id: str) -> list[str]:
        return {'ONE': ['c1'], 'THREE': ['c1', 'c2', 'c3'], 'EMPTY': []}.get(test_set_id, ['c1'])

    def find_run(self, agent_id: str, name: str) -> str | None:
        return self.found

    def launch(self, *args, **kwargs) -> str:
        self.launches += 1
        raise CovalError('coval runs launch: connection reset')

    def wait(self, run_id: str, *, timeout_s: float, poll_s: float = 15) -> Run:
        return Run(run_id=run_id, status=self.status)


AGENTS = [
    Agent(
        id='S', display_name='voice-harness-salon-pipecat', test_set_ids=['SALON', 'ONE', 'THREE', 'EMPTY']
    ),
    Agent(id='G', display_name='voice-harness-graph-pipecat', test_set_ids=['SALON', 'ONE']),
    Agent(id='P', display_name='voice-harness-salon-prompt-pipecat', test_set_ids=['ONE']),
    Agent(id='F', display_name='voice-harness-faro-language-learning-pipecat', test_set_ids=['ONE']),
    Agent(id='U', display_name='unmute-salon-concierge-pipecat', test_set_ids=['SALON']),
]


def checker(coval: FakeCoval | None = None, log: Path | None = None) -> ReleaseCheck:
    log = log or Path(tempfile.mkdtemp()) / 'runs.jsonl'
    return ReleaseCheck(Repository(Path('.')), coval or FakeCoval(AGENTS), Path('.'), RunLog(log))


def refused(
    check: ReleaseCheck,
    *,
    agent_id: str = 'S',
    test_set_id: str = 'ONE',
    persona: str = 'calm',
    iterations: int = 1,
) -> list[str]:
    """The problems `plan` gives for these arguments; fails the test if it accepts them."""
    try:
        check.plan(
            'salon',
            agent_id,
            test_set_id,
            persona,
            iterations=iterations,
            max_simulations=1,
            label='session-1',
        )
    except PlanError as error:
        return error.problems
    raise AssertionError(f'accepted: {agent_id} {test_set_id} {persona} {iterations}')


def latest(check: ReleaseCheck, key: str) -> dict[str, Any]:
    """The run log's newest line for a job, which the test expects to be there."""
    line = check.runs.latest(key)
    assert line is not None, key
    return line


def test_plan() -> None:
    """One exact job, or every reason it is refused. Nothing means nothing, not everything."""
    check = checker()
    job = check.plan('salon', 'S', 'ONE', 'calm', iterations=1, max_simulations=1, label='session-1')
    assert (job.agent.id, job.case_ids, job.persona.id, job.simulations) == ('S', ('c1',), 'calm', 1)
    assert 'simulations  1 = 1 case(s) x 1 iteration(s) x 1' in job.describe()
    assert f'{MAX_CALL_SECONDS}s' in job.describe()

    assert refused(check, agent_id='G') == [
        "agent G is 'voice-harness-graph-pipecat', which calls graph, not salon"
    ], 'the test set is shared with the graph baseline, which must not be called by accident'
    assert refused(check, agent_id='P')[0].endswith('calls salon-prompt, not salon'), (
        'the one-prompt baseline'
    )
    assert refused(check, agent_id='F')[0].endswith('calls faro-language-learning, not salon'), 'Faro'
    assert refused(check, agent_id='U')[0].endswith('calls no use case of this repository, not salon')
    assert refused(check, agent_id='nope') == ['no Coval agent has the ID nope']
    assert refused(check, test_set_id='') == [
        'agency, agent, test set, persona and label are all required; nothing means all'
    ]
    assert refused(check, test_set_id='OTHER') == ['test set OTHER is not attached to agent S']
    assert refused(check, test_set_id='EMPTY') == ['test set EMPTY has no test cases']
    assert refused(check, persona='Twin') == ["2 personas match 'Twin'; name exactly one, by ID"]
    assert refused(check, test_set_id='THREE') == ['3 simulations planned, allowance is 1'], 'oversized'
    assert refused(check, iterations=2) == ['2 simulations planned, allowance is 1'], (
        'one iteration is not one call'
    )

    twins = [*AGENTS, Agent(id='S2', display_name='voice-harness-salon-pipecat', test_set_ids=['ONE'])]
    assert refused(checker(FakeCoval(twins))) == [
        "2 agents are named 'voice-harness-salon-pipecat': S, S2"
    ], 'an ambiguous agent name'

    assert Agent(id='x', display_name='voice-harness-salon-livekit').agency is None
    assert Agent.name_for('salon') == 'voice-harness-salon-pipecat'
    assert Agent(id='x', display_name='voice-harness-salon-prompt-pipecat').agency == 'salon-prompt'


def test_run_log() -> None:
    """A job is launched once per label, and a launch whose outcome is unknown is never retried by this tool."""
    log = Path(tempfile.mkdtemp()) / 'runs.jsonl'
    coval = FakeCoval(AGENTS)
    check = checker(coval, log=log)
    job = check.plan('salon', 'S', 'ONE', 'calm', iterations=1, max_simulations=1, label='session-1')

    check.runs.write(job.key, name='n1', status='finished', run_id='R1')
    assert refused(check) == ["this job already ran under label 'session-1' as run R1; use a new label"]
    assert check.plan('salon', 'S', 'ONE', 'calm', iterations=1, max_simulations=1, label='session-2')

    row = check._uncertain(job, 'n2', CovalError('reset'), log)
    assert coval.launches == 0, 'an uncertain launch is looked up, never sent again'
    assert row.problems and 'Not retried' in row.problems[0], row.problems
    assert latest(check, job.key)['status'] == 'uncertain'
    assert 'has no known outcome' in refused(check)[0], 'the next invocation refuses it too'

    crashed = checker(FakeCoval(AGENTS, found='R9'), log=Path(tempfile.mkdtemp()) / 'runs.jsonl')
    crashed.runs.write(job.key, name='n3', status='intent')
    assert refused(crashed) == ["this job already ran under label 'session-1' as run R9; use a new label"], (
        'a launch that crashed before recording its answer is found by name'
    )
    assert latest(crashed, job.key) | {'at': ''} == {
        'key': job.key,
        'at': '',
        'name': 'n3',
        'status': 'launched',
        'run_id': 'R9',
    }
    lost = checker(log=Path(tempfile.mkdtemp()) / 'runs.jsonl')
    lost.runs.write(job.key, name='n4', status='intent')
    assert 'has no known outcome' in refused(lost)[0]
    assert latest(lost, job.key)['status'] == 'uncertain'


def test_still_running() -> None:
    """A local timeout is neither a pass nor a failure, and cancels nothing at Coval."""
    check = checker(FakeCoval(AGENTS, status='IN PROGRESS'))
    job = check.plan('salon', 'S', 'ONE', 'calm', iterations=1, max_simulations=1, label='session-1')
    row = check._result(job, 'R1', 'n1', Path('bot.log'))
    assert row.still_running and 'not failed' in row.problems[0], row
    assert row.render().startswith('WAIT'), row.render()
    assert latest(check, job.key)['remote'] == 'IN PROGRESS'
    assert BOT_ENV['MAX_CALL_SECONDS'] == str(MAX_CALL_SECONDS)


def test_use_case_folders() -> None:
    """`salon` is salon-graph; any other agency is its own folder, and a folder with no bot.py is refused."""
    repo = Repository.find(Path(__file__).resolve().parent)
    assert repo.use_case('salon') == repo.root / 'use-cases' / 'salon-graph'
    assert repo.use_case('salon-prompt') == repo.root / 'use-cases' / 'salon-prompt'
    try:
        repo.use_case('concierge-tools')
    except ValueError:
        pass
    else:
        raise AssertionError('a folder with no bot.py must be refused')


def test_graph_build() -> None:
    """`graph` is the unmute graph build's own folder, run with its placeholders; nothing else gets them."""
    assert Agent(id='g', display_name='voice-harness-graph-pipecat').agency == 'graph'
    assert GRAPH_ENV['MANAGER_PHONE_NUMBER'] and not set(GRAPH_ENV) & set(BOT_ENV), GRAPH_ENV
    if (GRAPH_BUILD / 'bot.py').exists():
        assert Repository.find(Path(__file__).resolve().parent).use_case('graph') == GRAPH_BUILD


def spoke(sim_id: str) -> Simulation:
    return Simulation(
        simulation_id=sim_id, status='COMPLETED', transcript=[Message(role='assistant', content='Hi')]
    )


def run(**values: tuple[str, str, object]) -> Run:
    """A finished run with one answer per metric: metric ID -> (name, call, value)."""
    metrics = {
        mid: MetricResult(metric_name=name, values=[MetricValue(simulation_output_id=call, value=value)])
        for mid, (name, call, value) in values.items()
    }
    return Run(run_id='r', status='COMPLETED', results=RunResults(metrics=metrics))


def test_problems() -> None:
    clean = run(C=('adherence', 'a', 1.0), B=('consent', 'a', 'YES'), T=('turns', 'a', 12.0))
    assert clean.problems([spoke('a')], composite={'C'}) == []

    assert run(B=('consent', 'a', 'NO')).problems([spoke('a')]) == ['call a: consent answered NO']
    assert run(C=('adherence', 'a', 0.6)).problems([spoke('a')], composite={'C'}) == [
        'call a: adherence scored 0.60'
    ]
    assert run(T=('turns', 'a', 0.5)).problems([spoke('a')], composite={'C'}) == [], (
        'a plain number is not judged'
    )

    silent = Simulation(
        simulation_id='s', status='COMPLETED', transcript=[Message(role='user', content='Hello?')]
    )
    assert run().problems([silent]) == ['call s: the agent never spoke']
    failed = Simulation(simulation_id='f', status='FAILED', error_message='no audio')
    assert run().problems([failed]) == ['call f FAILED: no audio']
    assert run().problems([]) == ['the run has no simulated calls']


def test_fallbacks() -> None:
    """Each fragment is still in its fixed line, and a call that said one fails, however Coval heard it."""
    from core import turn

    lines = [turn.STUMBLE, turn.EXHAUSTED, turn.UNBACKED, turn.WITHHELD]
    assert all(fragment in line.lower() for fragment, line in zip(FALLBACKS.values(), lines, strict=True))

    heard = [
        'Sorry.',
        'Had a problem there.',
        'Sorry. Me correct myself.',
        "I haven't actually made that change.",
    ]
    sim = Simulation(
        simulation_id='c',
        status='COMPLETED',
        transcript=[Message(role='assistant', content=c) for c in heard],
    )
    assert run().problems([sim]) == [
        'call c: the harness said its STUMBLE (a turn failed) line',
        'call c: the harness said its UNBACKED (claimed a change nothing made) line',
    ]
    assert spoke('a').fallbacks == []


if __name__ == '__main__':
    test_plan()
    test_run_log()
    test_still_running()
    test_use_case_folders()
    test_graph_build()
    test_problems()
    test_fallbacks()
    print('ok')
