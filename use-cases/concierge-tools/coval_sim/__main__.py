"""`python -m coval_sim`: let Coval call a use case's `bot.py`, running on this laptop.

    cd use-cases/concierge-tools
    uv run python -m coval_sim run                   # every test set attached to a voice-harness-* agent
    uv run python -m coval_sim run Ab12Cd34          # only this test set
    uv run python -m coval_sim run Ab12Cd34 --iterations 3 --persona "Impatient Customer"
    uv run python -m coval_sim add salon-prompt      # create the baseline's Coval agent

Everything about what to test lives in Coval. A test set runs against every
agent named voice-harness-<agency>-pipecat it is attached to, with that agent's
metrics. The agency names the use case folder that answers: `salon` is
salon-graph, anything else is the folder of that name. So to cover a flow,
attach a test set to its agent in Coval.
"""

import argparse
import os
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from coval_sim.coval import CovalClient, CovalError
from coval_sim.models import FOLDERS, Agent, Persona
from coval_sim.runtime import NotReadyError, Process, Tunnel, answers_http

PORT = 7860
RUN_TIMEOUT_S = 45 * 60
CALLER = '+15005550006'
"""Twilio's own magic test number, which Coval sends as the caller and the callee.
`bot.py` never reads it: a simulated call reaches the agent with no caller ID."""
BOT_ENV = {
    'CALLER_ID': '',
    'DIARY_PER_CALL': '1',
    'TWILIO_ACCOUNT_SID': 'AC' + '0' * 32,
    'TWILIO_AUTH_TOKEN': 'coval-sim',
}
"""What the bot gets on top of `.env` for a simulated call.

No caller ID, so the agent has to ask for the number. A fresh diary per call,
so calls running side by side cannot see each other's bookings. Placeholder
Twilio credentials, because Pipecat's Twilio serializer refuses to start
without them; its hang-up request then fails with a logged 401, which is harmless.

`bot.py` loads `.env` with `override=True`, so do not put any of these names in `.env`.
"""


def connection(host: str) -> dict[str, Any]:
    """The Coval agent's connection settings: Coval plays Twilio against the runner's route.

    It posts to the TwiML webhook, reads the <Stream> URL out of the reply,
    then sends `connected` and `start` before any audio, which is the handshake
    the runner's websocket route waits for.
    """
    return {
        'connection_mode': 'twiml_webhook',
        'voice_url': f'https://{host}/',
        'voice_http_method': 'POST',
        'twilio_account_sid': BOT_ENV['TWILIO_ACCOUNT_SID'],
        'twilio_from_number': CALLER,
        'twilio_to_number': CALLER,
        'send_sample_rate_hertz': 8000,
        'receive_sample_rate_hertz': 8000,
        'receive_audio_channels': 1,
        'audio_encoding': 'ulaw',
        'message_type_path': 'event',
        'audio_message_type_value': 'media',
        'audio_data_path': 'media.payload',
        'send_audio_template': '{"event":"media","streamSid":"{{stream_sid}}","media":{"payload":"{{audio_data}}"}}',
    }


@dataclass(frozen=True)
class Job:
    """One run to launch: a test set against one agent."""

    agent: Agent
    test_set_id: str


@dataclass
class Row:
    """One line of the report: what ran, and why it failed if it did."""

    agency: str
    test_set_id: str = '-'
    run_id: str = '-'
    problems: list[str] = field(default_factory=list)
    log: Path | None = None

    def render(self) -> str:
        """The row, then each problem indented under it."""
        status = 'FAIL' if self.problems else 'ok  '
        lines = [f'{status}  {self.agency}  test set {self.test_set_id}  run {self.run_id}']
        lines += [f'      {problem}' for problem in self.problems]
        if self.problems and self.log:
            lines.append(f'      log: {self.log}')
        return '\n'.join(lines)


class Repository:
    """The checkout this tool runs in: the directory holding `use-cases/`, `pyproject.toml` and `.env`."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def find(cls, start: Path) -> Repository:
        """The nearest checkout at or above start.

        Raises:
            FileNotFoundError: If no parent holds use-cases/ and pyproject.toml.
        """
        for directory in (start, *start.parents):
            if (directory / 'use-cases').is_dir() and (directory / 'pyproject.toml').exists():
                return cls(directory)
        raise FileNotFoundError(f'no voice harness checkout at or above {start}')

    def env(self) -> dict[str, str]:
        """The checkout's .env, with the real environment on top."""
        values = {k: v for k, v in dotenv_values(self.root / '.env').items() if v is not None}
        return {**values, **os.environ}

    def use_case(self, agency: str) -> Path:
        """The folder whose `bot.py` answers an agency's calls.

        Raises:
            ValueError: If that folder has no bot.py.
        """
        folder = self.root / 'use-cases' / FOLDERS.get(agency, agency)
        if not (folder / 'bot.py').exists():
            raise ValueError(f'agency {agency}: no bot.py in {folder}')
        return folder

    def bot(self, agency: str, env: dict[str, str], host: str, log: Path) -> Process:
        """Start the agency's `bot.py`, from its own folder, on the runner's Twilio route; return once it answers HTTP."""
        bot = Process(
            'Pipecat bot',
            ['uv', 'run', 'python', 'bot.py', '-t', 'twilio', '-x', host, '--port', str(PORT)],
            self.use_case(agency),
            {**env, **BOT_ENV},
            log,
        )
        bot.wait_until(lambda: answers_http(f'http://127.0.0.1:{PORT}/'), 600)
        return bot


class ReleaseCheck:
    """Plans and runs Coval simulations against the bot.

    Args:
        repo: The checkout holding bot.py.
        coval: The Coval client.
        workdir: Where logs go.
    """

    def __init__(self, repo: Repository, coval: CovalClient, workdir: Path) -> None:
        self.repo = repo
        self.coval = coval
        self.workdir = workdir

    def plan(self, test_set_ids: list[str]) -> tuple[dict[str, list[Job]], list[Row]]:
        """Which runs to launch, grouped by agency, from what Coval holds.

        Args:
            test_set_ids: The test sets to run. Empty means every test set
                attached to one of our agents.

        Returns:
            The jobs per agency, and a failed row for each test set nothing runs.
        """
        jobs: dict[str, list[Job]] = defaultdict(list)
        for agent in self.coval.agents():
            if agent.agency is None:
                continue
            wanted = [t for t in agent.test_set_ids if not test_set_ids or t in test_set_ids]
            jobs[agent.agency] += [Job(agent, test_set_id) for test_set_id in wanted]
        covered = {job.test_set_id for agency_jobs in jobs.values() for job in agency_jobs}
        missing = [
            Row(
                '-',
                test_set_id,
                problems=['no voice-harness-<agency>-pipecat agent has this test set attached'],
            )
            for test_set_id in test_set_ids
            if test_set_id not in covered
        ]
        return {agency: j for agency, j in jobs.items() if j}, missing

    def run(
        self, plan: dict[str, list[Job]], persona: Persona, iterations: int, concurrency: int
    ) -> list[Row]:
        """Open one tunnel, then per agency start the bot, run its jobs and stop it."""
        env = self.repo.env()
        rows = []
        with Tunnel(PORT, self.workdir) as tunnel:
            for agency, jobs in sorted(plan.items()):
                try:
                    rows += self._check(agency, jobs, env, tunnel.host, persona, iterations, concurrency)
                except (NotReadyError, RuntimeError, ValueError) as error:
                    rows.append(Row(agency, problems=[str(error)]))
        return rows

    def _check(
        self,
        agency: str,
        jobs: list[Job],
        env: dict[str, str],
        host: str,
        persona: Persona,
        iterations: int,
        concurrency: int,
    ) -> list[Row]:
        log = self.workdir / f'{agency}-bot.log'
        launched: list[tuple[Job, str]] = []
        rows = []
        with self.repo.bot(agency, env, host, log):
            for agent in {job.agent.id: job.agent for job in jobs}.values():
                self.coval.update_connection(agent, connection(host))
                for job in (j for j in jobs if j.agent.id == agent.id):
                    run_id = self.coval.launch(
                        agent,
                        persona,
                        job.test_set_id,
                        iterations=iterations,
                        concurrency=concurrency,
                        label=f'release-check {agency}',
                    )
                    launched.append((job, run_id))
            for job, run_id in launched:
                run = self.coval.wait(run_id, timeout_s=RUN_TIMEOUT_S)
                problems = (
                    run.problems(
                        self.coval.simulations(run_id), self.coval.composite_ids(job.agent.metric_ids)
                    )
                    if run.finished
                    else [f'run still {run.status} after {RUN_TIMEOUT_S}s']
                )
                rows.append(Row(agency, job.test_set_id, run_id, problems, log))
        return rows

    def add(self, agency: str) -> str:
        """Create the Coval agent for a flow, with a placeholder connection.

        The connection is replaced on every run, because each run gets a new
        tunnel host, so the placeholder never reaches a call.
        """
        name = Agent.name_for(agency)
        if name in {agent.display_name for agent in self.coval.agents()}:
            return f'exists  {name}'
        self.coval.create_agent(name, 'websocket', connection('example.invalid'))
        return f'created {name}'


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m coval_sim',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='run test sets against the bot')
    run.add_argument('test_set_ids', nargs='*', metavar='TEST_SET', help='Coval test set IDs; default is all')
    run.add_argument(
        '--persona', default='Standard Customer', help='the Coval persona that places every call'
    )
    run.add_argument('--iterations', type=int, default=1, help='times to run each test case')
    run.add_argument('--concurrency', type=int, default=3, help='calls in flight per run')
    add = commands.add_parser('add', help="create a flow's Coval agent")
    add.add_argument('agency', help='a use case folder under use-cases/, or salon for salon-graph')
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command line. Returns the exit code: 0 passed, 1 failed."""
    args = _parser().parse_args(argv)
    try:
        repo = Repository.find(Path.cwd())
    except FileNotFoundError as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    env = repo.env()
    if not env.get('COVAL_API_KEY'):
        print('error: set COVAL_API_KEY in the environment or .env', file=sys.stderr)
        return 1
    check = ReleaseCheck(repo, CovalClient(env['COVAL_API_KEY']), Path(tempfile.mkdtemp(prefix='coval-sim-')))
    try:
        if args.command == 'add':
            line = check.add(args.agency)
            print(line)
            if line.startswith('created'):
                print('next: attach a test set and metrics to this agent in Coval, then run it')
            return 0
        plan, rows = check.plan(args.test_set_ids)
        if not plan and not rows:
            print('error: no voice-harness-<agency>-pipecat agent has a test set attached', file=sys.stderr)
            return 1
        rows += check.run(plan, check.coval.persona(args.persona), args.iterations, args.concurrency)
    except (CovalError, ValueError, RuntimeError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    for row in rows:
        print(row.render())
    return 1 if any(row.problems for row in rows) else 0


if __name__ == '__main__':
    sys.exit(main())
