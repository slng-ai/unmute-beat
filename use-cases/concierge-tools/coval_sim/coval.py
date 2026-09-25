"""The one place this tool talks to Coval, through the `coval` CLI."""

import json
import os
import subprocess
import time
from typing import Any

from pydantic import TypeAdapter

from coval_sim.models import Agent, Persona, Run, Simulation


class CovalError(RuntimeError):
    """A coval command failed."""


class CovalClient:
    """Reads and changes Coval resources with `coval --format json`.

    Args:
        api_key: The Coval API key every command runs with.
        binary: The coval executable.
    """

    def __init__(self, api_key: str, binary: str = 'coval') -> None:
        if not api_key:
            raise ValueError('a Coval API key is required')
        self._env = {**os.environ, 'COVAL_API_KEY': api_key}
        self._binary = binary

    def _call(self, *args: str) -> Any:
        """Run one coval command. A read that hits a network error is tried again, up to three times.

        Only reads: a launch or an update sent twice could start two runs.
        A 25-minute run used to be lost to one dropped poll (the baseline's
        first Coval call, 2026-09-24), though the call itself had finished.
        Coval's rate limit (HTTP 429) is a read tried again too, with a longer
        wait: session 4's run GYZdoiie lost its bot mid-call when one poll was
        refused, because the runner took the refusal as the run failing.
        """
        for attempt in range(6):
            out = subprocess.run(
                [self._binary, '--format', 'json', *args],
                capture_output=True,
                text=True,
                env=self._env,
                check=False,
            )
            limited = '429' in out.stderr or 'Too Many Requests' in out.stderr
            retry = (
                out.returncode != 0
                and ('Network error' in out.stderr or limited)
                and args[1:2] in (('get',), ('list',))
            )
            if not retry or attempt == 5:
                break
            time.sleep((15 if limited else 5) * (attempt + 1))
        if out.returncode != 0:
            raise CovalError(f'coval {" ".join(args[:2])}: {out.stderr.strip()}')
        return json.loads(out.stdout) if out.stdout.strip() else None

    def agents(self) -> list[Agent]:
        """Every agent in the organization."""
        return TypeAdapter(list[Agent]).validate_python(self._call('agents', 'list') or [])

    def create_agent(self, name: str, kind: str, metadata: dict[str, Any]) -> Agent:
        """Create an agent of a Coval type (websocket) with its connection."""
        return Agent.model_validate(
            self._call('agents', 'create', '--name', name, '--type', kind, '--metadata', json.dumps(metadata))
        )

    def update_connection(self, agent: Agent, metadata: dict[str, Any]) -> Agent:
        """Replace an agent's connection settings. Test sets and metrics stay."""
        return Agent.model_validate(
            self._call('agents', 'update', agent.id, '--metadata', json.dumps(metadata))
        )

    def personas(self) -> list[Persona]:
        """Every persona in the organization."""
        return TypeAdapter(list[Persona]).validate_python(self._call('personas', 'list') or [])

    def test_case_ids(self, test_set_id: str) -> list[str]:
        """The test cases in a test set: one simulation each, per iteration.

        ponytail: one page of 100. A set that large is over any allowance
        this tool is given, and `plan` refuses it on the count.
        """
        cases = self._call('test-cases', 'list', '--test-set-id', test_set_id, '--page-size', '100') or []
        return [case['id'] for case in cases]

    def find_run(self, agent_id: str, name: str) -> str | None:
        """The run with this display name among the agent's latest, or None: how an uncertain launch is settled."""
        runs = self._call(
            'runs', 'list', '--filter', f'agent_id="{agent_id}"', '--page-size', '50', '--order-by=-create_time'
        )  # fmt: skip
        return next((run['run_id'] for run in runs or [] if run.get('display_name') == name), None)

    def composite_ids(self, metric_ids: list[str]) -> set[str]:
        """Which of these metrics are composites, whose score is a fraction of criteria met.

        A run's results say only `float` for both a composite and a turn count,
        so the metric itself is asked.
        """
        return {
            m
            for m in metric_ids
            if self._call('metrics', 'get', m)['metric_type'] == 'METRIC_COMPOSITE_EVALUATION'
        }

    def launch(
        self,
        agent: Agent,
        persona: Persona,
        test_set_id: str,
        *,
        iterations: int,
        concurrency: int = 1,
        name: str,
    ) -> str:
        """Start a run of one test set against one agent, `concurrency` calls at once, with the agent's metrics named.

        Calls at once are the one speed-up on this side. Most of a run is
        Coval's: it dials in 13 to 120 s after launch, and scores a call 30 s
        (2026-09-24) to 8 minutes (2026-09-25) after it ends, with the same
        metrics and judge. One call at a time pays both for every call.

        The metrics are passed rather than left to Coval's default, so the
        ones `plan` printed are the ones that score. No mutation is passed,
        so every call is the base agent.

        Returns:
            The new run's ID.
        """
        run = self._call(
            'runs', 'launch',
            '--agent-id', agent.id,
            '--persona-id', persona.id,
            '--test-set-id', test_set_id,
            '--metric-ids', ','.join(agent.metric_ids),
            '--iterations', str(iterations),
            '--concurrency', str(concurrency),
            '--name', name,
            '--tags', 'release-check',
        )  # fmt: skip
        return run['run_id']

    def wait(self, run_id: str, *, timeout_s: float, poll_s: float = 5) -> Run:
        """Poll a run until Coval stops working on it or the timeout passes.

        Every 30 s it prints the run's status and this machine's load average:
        a load test runs every call's bot here, so a saturated machine is part
        of what its latency measures.
        """
        deadline = time.monotonic() + timeout_s
        run = Run.model_validate(self._call('runs', 'get', run_id))
        polls = 0
        while not run.finished and time.monotonic() < deadline:
            time.sleep(poll_s)
            run = Run.model_validate(self._call('runs', 'get', run_id))
            polls += 1
            if polls % max(1, round(30 / poll_s)) == 0:
                load = ' '.join(f'{x:.1f}' for x in os.getloadavg())
                print(f'run {run_id} {run.status}, load average {load}', flush=True)
        return run

    def simulations(self, run_id: str) -> list[Simulation]:
        """Every call in a run, each with its transcript.

        The list endpoint leaves the transcript out, so each call is read again.
        """
        listed = self._call('simulations', 'list', '--run-id', run_id) or []
        return [
            Simulation.model_validate(self._call('simulations', 'get', item['simulation_id']))
            for item in listed
        ]
