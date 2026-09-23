"""Run: uv run python -m tests.test_coval_sim

The Coval release check, with no Coval: which runs a test set turns into, and
which call results fail a run. The CLI, the tunnel and the bot are not run.

Asserts, no pytest, no fixtures, same as the rest.
"""

from pathlib import Path

from utils.coval_sim.__main__ import ReleaseCheck, Repository
from utils.coval_sim.coval import CovalClient
from utils.coval_sim.models import Agent, Message, MetricResult, MetricValue, Run, RunResults, Simulation


class FakeCoval(CovalClient):
    """A CovalClient that never runs coval: plan() only lists agents."""

    def __init__(self, agents: list[Agent]) -> None:
        super().__init__(api_key='test')
        self._agents = agents

    def agents(self) -> list[Agent]:
        return self._agents


AGENTS = [
    Agent(id='1', display_name='voice-harness-salon-pipecat', test_set_ids=['SALON', 'SMOKE']),
    Agent(id='2', display_name='voice-harness-access-pipecat', test_set_ids=['SMOKE']),
    Agent(id='3', display_name='unmute-salon-concierge-pipecat', test_set_ids=['SALON']),
]


def plan(test_set_ids: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    check = ReleaseCheck(Repository(Path('.')), FakeCoval(AGENTS), Path('.'))
    jobs, missing = check.plan(test_set_ids)
    return {agency: sorted(j.test_set_id for j in js) for agency, js in jobs.items()}, [
        row.test_set_id for row in missing
    ]


def test_plan() -> None:
    assert plan(['SALON']) == ({'salon': ['SALON']}, []), 'an unmute agent with the same test set is not ours'
    assert plan(['SMOKE']) == ({'salon': ['SMOKE'], 'access': ['SMOKE']}, [])
    assert plan([]) == ({'salon': ['SALON', 'SMOKE'], 'access': ['SMOKE']}, [])
    assert plan(['NOWHERE']) == ({}, ['NOWHERE'])
    assert Agent(id='x', display_name='voice-harness-salon-livekit').agency is None
    assert Agent.name_for('salon') == 'voice-harness-salon-pipecat'


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


if __name__ == '__main__':
    test_plan()
    test_problems()
    print('ok')
