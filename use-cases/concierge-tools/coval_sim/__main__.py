"""`python -m coval_sim`: let Coval call a use case's `bot.py`, running on this laptop.

    cd use-cases/concierge-tools
    uv run python -m coval_sim plan --agency salon --agent JxGgSPA55DcFcDcvbKAUQM
        --test-set Ab12Cd34 --persona MUFJdYAHdBHU6UbRRu4ykM --max-simulations 1 --label session-1
    uv run python -m coval_sim run  ...the same arguments...   # launches it, billable
    uv run python -m coval_sim add salon-prompt      # create the baseline's Coval agent
    uv run python -m coval_sim add graph             # the other baseline: the unmute graph build of the salon
    uv run python -m coval_sim serve --agency salon  # one bot and tunnel for several jobs, until stopped
    uv run python -m coval_sim run ... --host <the host serve printed>

One job per invocation: one agent, one test set, one persona. `plan` reads
Coval and prints what `run` would launch, and how many calls that is; it
launches nothing. `run` refuses anything `plan` refuses. Nothing is chosen for
you: no test set means no job, not every job.

A Coval agent named voice-harness-<agency>-pipecat calls the use case folder
of that agency: `salon` is salon-graph, anything else is the folder of that
name. The agent is named by its ID and must be the agency's, so a test set
shared with the baseline's agent can never launch against the baseline by
accident.

Every launch is written to a local run log (`--log`) before and after it
happens. A launch whose answer was lost is looked up by its unique name; if
Coval has no run by that name either, the job is marked uncertain and is never
launched again by this tool until the line is removed from the log by hand.
"""

import argparse
import contextlib
import json
import os
import signal
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from coval_sim.coval import CovalClient, CovalError
from coval_sim.models import FOLDERS, Agent, Persona, SpeechProfile
from coval_sim.runtime import NotReadyError, Process, Tunnel, answers_http, ceiling

PORT = 7860
RUN_TIMEOUT_S = 45 * 60
MAX_CALL_SECONDS = 180
"""The longest a simulated call may run. Coval cannot cap a call's length, so `bot.py` hangs up at it."""
FINISH_SECONDS = 8
"""How long salon-graph's bot lets a running turn finish after its limit; the runner allows the same."""
STILL_RUNNING = 3
"""The exit code when a run had not finished by the local timeout: neither passed nor failed, and not cancelled."""
CALLER = '+15005550006'
"""Twilio's own magic test number, which Coval sends as the caller and the callee.
`bot.py` never reads it: a simulated call reaches the agent with no caller ID."""
BOT_ENV = {
    'CALLER_ID': '',
    'DIARY_PER_CALL': '1',
    'MAX_CALL_SECONDS': str(MAX_CALL_SECONDS),
    'TWILIO_ACCOUNT_SID': 'AC' + '0' * 32,
    'TWILIO_AUTH_TOKEN': 'coval-sim',
}
"""What the bot gets on top of `.env` for a simulated call.

No caller ID, so the agent has to ask for the number. A fresh diary per call,
so calls running side by side cannot see each other's bookings. A ceiling on
the call's length. Placeholder Twilio credentials, because Pipecat's Twilio
serializer refuses to start without them; its hang-up request then fails with
a logged 401, which is harmless.

`bot.py` loads `.env` with `override=True`, so do not put any of these names in `.env`.
"""
GRAPH = 'graph'
"""The agency name of the baseline: the unmute graph build of the same salon, on Pipecat Flows.

Coval calls it through its own agent, voice-harness-graph-pipecat, on the same
test sets, so both builds answer the same cases.
"""
GRAPH_BUILD = Path(
    os.environ.get(
        'GRAPH_BUILD_DIR', Path.home() / 'Documents/GitHub/unmute_cli/examples/salon-concierge/build/pipecat'
    )
)
"""Where the graph build's `bot.py` lives. It runs in its own project, with its own lock file."""
GRAPH_ENV = {'MANAGER_PHONE_NUMBER': '+34600000000', 'TWILIO_PHONE_NUMBER': CALLER}
"""Placeholders the graph build refuses to start without on a phone route. No simulated call reaches them."""


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


class PlanError(ValueError):
    """What `plan` refused, every problem at once."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__('refused:\n' + '\n'.join(f'  - {p}' for p in problems))
        self.problems = problems


@dataclass(frozen=True)
class Job:
    """One run to launch: one test set against one agent, with one persona.

    Coval places cases x iterations x (1 + mutations) calls. This tool passes
    no mutation, so it is cases x iterations.
    """

    agency: str
    agent: Agent
    test_set_id: str
    case_ids: tuple[str, ...]
    persona: Persona
    iterations: int
    label: str
    limit: str = 'bot.py (MAX_CALL_SECONDS)'
    """Who ends a call at the limit: the bot itself, or this runner stopping a bot that has no limit."""
    concurrency: int = 1
    """How many of the job's calls Coval places at once. Not part of the job's key: the same calls, sooner."""
    profile: SpeechProfile | None = None
    """The speech settings the bot gets (`--profile`)."""
    call_seconds: int = MAX_CALL_SECONDS
    """The longest a call may run before the bot hangs up (`--call-seconds`)."""

    @property
    def simulations(self) -> int:
        """How many calls Coval will place for this job."""
        return len(self.case_ids) * self.iterations

    @property
    def key(self) -> str:
        """What makes two launches the same job: the label, the agent, the test set, the persona, the iterations."""
        return f'{self.label}|{self.agent.id}|{self.test_set_id}|{self.persona.id}|{self.iterations}'

    def describe(self) -> str:
        """The plan, as `plan` prints it."""
        return '\n'.join([
            f'agency       {self.agency} (use-cases/{FOLDERS.get(self.agency, self.agency)})',
            f'agent        {self.agent.id} {self.agent.display_name}',
            f'test set     {self.test_set_id}: {len(self.case_ids)} case(s) {", ".join(self.case_ids)}',
            f'persona      {self.persona.id} {self.persona.name}',
            f'metrics      {", ".join(self.agent.metric_ids) or "none"}',
            f'iterations   {self.iterations}',
            'mutations    none',
            f'simulations  {self.simulations} = {len(self.case_ids)} case(s) x {self.iterations} iteration(s) x 1',
            f'call limit   {self.call_seconds}s each, enforced by {self.limit}',
            f'speech       {self.profile.describe() if self.profile else "none named"}',
            f'concurrency  {self.concurrency}' + (
                ' at once: the calls share this machine and the speech providers, '
                'so their latency is not comparable to a run of one call at a time' if self.concurrency > 1 else ''
            ),
            f'label        {self.label}',
        ])  # fmt: skip


def load_profile(path: Path) -> SpeechProfile:
    """A speech profile env file, validated whole: every name present, no other name.

    Raises:
        OSError: If the file cannot be read.
        ValueError: A `ValidationError` listing what is missing or extra.
    """
    if not path.is_file():
        raise FileNotFoundError(f'no such file: {path}')
    return SpeechProfile.model_validate(dotenv_values(path))


class RunLog:
    """Every launch this tool attempted, one JSON line per change, newest last.

    The log is the only memory between invocations: a job whose latest line
    says it was launched, or might have been, is never launched again under
    the same label.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def latest(self, key: str) -> dict[str, Any] | None:
        """The newest line for this job, or None if it was never attempted."""
        if not self.path.exists():
            return None
        lines = [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]
        return next((line for line in reversed(lines) if line['key'] == key), None)

    def write(self, key: str, **entry: Any) -> None:
        """Append one line, flushed before this returns: a crash after it still leaves it."""
        line = {'key': key, 'at': datetime.now(UTC).isoformat(), **entry}
        with self.path.open('a') as out:
            out.write(json.dumps(line) + '\n')
            out.flush()
            os.fsync(out.fileno())


@dataclass
class Row:
    """One line of the report: what ran, and why it failed if it did."""

    agency: str
    test_set_id: str = '-'
    run_id: str = '-'
    problems: list[str] = field(default_factory=list)
    log: Path | None = None
    still_running: bool = False

    def render(self) -> str:
        """The row, then each problem indented under it."""
        status = 'WAIT' if self.still_running else 'FAIL' if self.problems else 'ok  '
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
        """The folder whose `bot.py` answers an agency's calls: the graph build's own for `graph`.

        Raises:
            ValueError: If that folder has no bot.py.
        """
        folder = GRAPH_BUILD if agency == GRAPH else self.root / 'use-cases' / FOLDERS.get(agency, agency)
        if not (folder / 'bot.py').exists():
            raise ValueError(f'agency {agency}: no bot.py in {folder}')
        return folder

    def limits_itself(self, agency: str) -> bool:
        """Whether the agency's `bot.py` ends its own calls at `MAX_CALL_SECONDS`."""
        return 'MAX_CALL_SECONDS' in (self.use_case(agency) / 'bot.py').read_text()

    def bot(
        self,
        agency: str,
        env: dict[str, str],
        host: str,
        log: Path,
        port: int = PORT,
        profile: SpeechProfile | None = None,
        call_seconds: int = MAX_CALL_SECONDS,
    ) -> Process:
        """Start the agency's `bot.py`, from its own folder, on the runner's Twilio route; return once it answers HTTP.

        A bot with no call limit of its own is stopped from outside once a
        call has run `MAX_CALL_SECONDS` plus the time a running turn may take
        to finish (`FINISH_SECONDS`), the same ceiling salon-graph's bot keeps.
        """
        bot = Process(
            'Pipecat bot',
            ['uv', 'run', 'python', 'bot.py', '-t', 'twilio', '-x', host, '--port', str(port)],
            self.use_case(agency),
            {
                **env,
                **BOT_ENV,
                'MAX_CALL_SECONDS': str(call_seconds),
                **(profile.model_dump() if profile else {}),
                **(GRAPH_ENV if agency == GRAPH else {}),
            },
            log,
        )
        bot.wait_until(lambda: answers_http(f'http://127.0.0.1:{port}/'), 600)
        if not self.limits_itself(agency):
            ceiling(bot, call_seconds + FINISH_SECONDS)
        return bot


class ReleaseCheck:
    """Plans and runs one Coval job against the bot.

    Args:
        repo: The checkout holding bot.py.
        coval: The Coval client.
        workdir: Where logs go.
        runs: The run log.
    """

    def __init__(
        self, repo: Repository, coval: CovalClient, workdir: Path, runs: RunLog, port: int = PORT
    ) -> None:
        self.port = port
        """The local port of this job's bot and tunnel. Jobs run side by side need one each."""
        self.repo = repo
        self.coval = coval
        self.workdir = workdir
        self.runs = runs

    def plan(
        self,
        agency: str,
        agent_id: str,
        test_set_id: str,
        persona: str,
        *,
        iterations: int,
        max_simulations: int,
        label: str,
        concurrency: int = 1,
        metrics: list[str] | None = None,
        profile: Path | None = None,
        call_seconds: int = MAX_CALL_SECONDS,
    ) -> Job:
        """The one job these arguments name, checked against what Coval holds. Reads only.

        Args:
            agency: The use case the agent must call.
            agent_id: The Coval agent's exact ID.
            test_set_id: The one test set to run.
            persona: The persona's ID, or a name exactly one persona has.
            iterations: Times each case runs.
            max_simulations: The most calls this job may place.
            label: What this launch belongs to (a session); the same job twice under one label is refused.
            concurrency: How many calls Coval places at once. A run of N calls one at a time
                waits N times for Coval's dial and its scoring after each call; at once, about once.
            metrics: The metric IDs to score instead of the agent's own. Coval refuses an
                empty list and scores the agent's own when none is named, so session 4's load
                test names Turn Count alone and judges the calls from Langfuse and the write
                records instead (`load_report.py`): no judge's noise, and no wait for scoring.

        Raises:
            PlanError: Listing every reason the job is refused.
        """
        problems = []
        if not (agency and agent_id and test_set_id and persona and label):
            raise PlanError(
                ['agency, agent, test set, persona and label are all required; nothing means all']
            )
        agents = self.coval.agents()
        matches = [a for a in agents if a.id == agent_id]
        if len(matches) != 1:
            raise PlanError([f'no Coval agent has the ID {agent_id}'])
        agent = matches[0] if metrics is None else matches[0].model_copy(update={'metric_ids': metrics})
        if agent.agency != agency:
            problems.append(
                f'agent {agent_id} is {agent.display_name!r}, which calls '
                f'{agent.agency or "no use case of this repository"}, not {agency}'
            )
        namesakes = [a.id for a in agents if a.display_name == agent.display_name]
        if len(namesakes) > 1:
            problems.append(
                f'{len(namesakes)} agents are named {agent.display_name!r}: {", ".join(namesakes)}'
            )
        if test_set_id not in agent.test_set_ids:
            problems.append(f'test set {test_set_id} is not attached to agent {agent_id}')
        case_ids = tuple(self.coval.test_case_ids(test_set_id))
        if not case_ids:
            problems.append(f'test set {test_set_id} has no test cases')
        people = [p for p in self.coval.personas() if persona in (p.id, p.name)]
        if len(people) != 1:
            problems.append(f'{len(people)} personas match {persona!r}; name exactly one, by ID')
        if iterations < 1:
            problems.append('iterations must be at least 1')
        speech = None
        if profile is not None:
            try:
                speech = load_profile(profile)
            except (OSError, ValueError) as error:
                problems.append(f'speech profile {profile}: {error}')
        if problems:
            raise PlanError(problems)
        limit = (
            'bot.py (MAX_CALL_SECONDS)'
            if self.repo.limits_itself(agency)
            else f'this runner: the bot has none, and is stopped {MAX_CALL_SECONDS + FINISH_SECONDS:.0f}s after the call connects'
        )
        job = Job(
            agency, agent, test_set_id, case_ids, people[0], iterations, label, limit, concurrency, speech, call_seconds
        )  # fmt: skip
        if job.simulations > max_simulations:
            problems.append(f'{job.simulations} simulations planned, allowance is {max_simulations}')
        if not 1 <= concurrency <= job.simulations:
            problems.append(f'concurrency must be from 1 to the {job.simulations} simulation(s) planned')
        if concurrency > 1 and not self.repo.limits_itself(agency):
            problems.append(
                f'{agency} has no call limit of its own, and this runner stops its whole bot one limit after '
                'the first call connects, which would cut later calls short: concurrency 1 only'
            )
        previous = self.runs.latest(job.key)
        if previous is not None:
            problems.append(self._duplicate(job, previous))
        if problems:
            raise PlanError(problems)
        return job

    def _duplicate(self, job: Job, previous: dict[str, Any]) -> str:
        """Why a job already in the log is refused. A launch that never recorded its outcome is looked up first."""
        if previous['status'] == 'intent':
            run_id = self.coval.find_run(job.agent.id, previous['name'])
            if run_id:
                self.runs.write(job.key, name=previous['name'], status='launched', run_id=run_id)
                previous = {**previous, 'status': 'launched', 'run_id': run_id}
            else:
                self.runs.write(job.key, name=previous['name'], status='uncertain')
                previous = {**previous, 'status': 'uncertain'}
        if previous['status'] == 'uncertain':
            return (
                f'an earlier launch of this job ({previous["name"]!r}) has no known outcome. Check Coval '
                f'for it, then remove its lines from {self.runs.path} to allow another'
            )
        return f'this job already ran under label {job.label!r} as run {previous.get("run_id", "?")}; use a new label'

    def run(self, job: Job, host: str | None = None) -> Row:
        """Open a tunnel, start the bot, launch the job, wait for it, stop both. Every step is logged first.

        With `host`, a bot is already answering there (`serve`), and this only
        points the agent at it, launches and waits. Two jobs on one Coval agent
        must share one host: each points the agent's connection at its own
        host, so a second tunnel would take the first job's later calls.
        """
        env = self.repo.env()
        log = self.workdir / f'{job.agency}-bot.log'
        name = f'{job.label} {job.agency} {job.test_set_id} {uuid.uuid4().hex[:8]}'
        try:
            with contextlib.ExitStack() as stack:
                if host is None:
                    tunnel = stack.enter_context(Tunnel(self.port, self.workdir))
                    stack.enter_context(
                        self.repo.bot(
                            job.agency, env, tunnel.host, log, self.port, job.profile, job.call_seconds
                        )
                    )
                    host = tunnel.host
                self.coval.update_connection(job.agent, connection(host))
                self.runs.write(job.key, name=name, status='intent')
                try:
                    run_id = self.coval.launch(
                        job.agent,
                        job.persona,
                        job.test_set_id,
                        iterations=job.iterations,
                        concurrency=job.concurrency,
                        name=name,
                    )
                except CovalError as error:
                    return self._uncertain(job, name, error, log)
                self.runs.write(job.key, name=name, status='launched', run_id=run_id)
                print(f'launched run {run_id} ({name})', flush=True)
                return self._result(job, run_id, name, log)
        except (NotReadyError, RuntimeError, ValueError) as error:
            return Row(job.agency, job.test_set_id, problems=[str(error)], log=log)

    def _uncertain(self, job: Job, name: str, error: CovalError, log: Path) -> Row:
        """A launch that raised may still have started a run: look for it by name before saying anything."""
        run_id = self.coval.find_run(job.agent.id, name)
        if run_id:
            self.runs.write(job.key, name=name, status='launched', run_id=run_id)
            print(f'launch answered badly, but run {run_id} exists ({name})', flush=True)
            return self._result(job, run_id, name, log)
        self.runs.write(job.key, name=name, status='uncertain', error=str(error))
        return Row(
            job.agency,
            job.test_set_id,
            problems=[f'launch outcome unknown ({error}); no run named {name!r} found. Not retried'],
            log=log,
        )

    def _result(self, job: Job, run_id: str, name: str, log: Path) -> Row:
        """Wait for the run and judge it. A run still going at the timeout is reported, not failed and not cancelled."""
        run = self.coval.wait(run_id, timeout_s=RUN_TIMEOUT_S)
        if not run.finished:
            self.runs.write(job.key, name=name, status='launched', run_id=run_id, remote=run.status)
            return Row(
                job.agency,
                job.test_set_id,
                run_id,
                [f'run still {run.status} after {RUN_TIMEOUT_S}s: not failed, and still running at Coval'],
                log,
                still_running=True,
            )
        self.runs.write(job.key, name=name, status='finished', run_id=run_id, remote=run.status)
        problems = run.problems(
            self.coval.simulations(run_id), self.coval.composite_ids(job.agent.metric_ids)
        )
        return Row(job.agency, job.test_set_id, run_id, problems, log)

    def serve(self, agency: str, profile: Path, call_seconds: int = MAX_CALL_SECONDS) -> None:
        """Start the agency's bot behind a tunnel, print the host, and keep both up until stopped.

        One bot for several jobs at once (`run --host`), so every call of a
        load test lands on one process. Only a bot that ends its own calls:
        the runner's outside ceiling stops a whole bot after its first call.
        """
        if not self.repo.limits_itself(agency):
            raise ValueError(f'{agency} has no call limit of its own; serve only a bot with MAX_CALL_SECONDS')
        speech = load_profile(profile)
        log = self.workdir / f'{agency}-bot.log'
        env = self.repo.env()
        with (
            Tunnel(self.port, self.workdir) as tunnel,
            self.repo.bot(agency, env, tunnel.host, log, self.port, speech, call_seconds),
        ):
            print(
                f'serving {agency} at {tunnel.host} ({speech.describe()}, calls up to {call_seconds}s; bot log '
                f'{log}); stop with SIGTERM or Ctrl-C',
                flush=True,
            )
            threading.Event().wait()

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
    for command, text in (
        ('plan', 'print the one job, launch nothing'),
        ('run', 'launch the one job, billable'),
    ):
        job = commands.add_parser(command, help=text)
        job.add_argument(
            '--agency', required=True, help='the use case the agent calls: salon for salon-graph'
        )
        job.add_argument('--agent', required=True, help="the Coval agent's exact ID")
        job.add_argument('--test-set', required=True, help='the one Coval test set to run')
        job.add_argument('--persona', required=True, help='a persona ID, or a name exactly one persona has')
        job.add_argument('--iterations', type=int, default=1, help='times to run each test case')
        job.add_argument(
            '--max-simulations', type=int, required=True, help='the most calls this job may place'
        )
        job.add_argument('--label', required=True, help='what this launch belongs to, such as session-1')
        job.add_argument(
            '--concurrency',
            type=int,
            default=1,
            help='calls Coval places at once; a bot with its own limit only',
        )
        job.add_argument(
            '--metrics',
            type=lambda text: [m for m in text.split(',') if m],
            help="comma-separated metric IDs to score instead of the agent's own; Coval needs at least one",
        )
        job.add_argument('--log', type=Path, default=Path('runs.jsonl'), help='the run log')
        job.add_argument(
            '--port',
            type=int,
            default=PORT,
            help='the local port for this bot and tunnel; one per job run at once',
        )
        job.add_argument('--host', help='a host `serve` printed: use its bot, start no bot or tunnel')
        job.add_argument(
            '--profile', type=Path, required=True, help='the speech profile env file the bot runs on'
        )
        job.add_argument(
            '--call-seconds', type=int, default=MAX_CALL_SECONDS, help='the longest a call may run'
        )
    serve = commands.add_parser('serve', help='one bot and tunnel for several jobs, until stopped')
    serve.add_argument('--agency', required=True, help='the use case to serve: salon for salon-graph')
    serve.add_argument('--port', type=int, default=PORT, help='the local port for the bot and tunnel')
    serve.add_argument(
        '--profile', type=Path, required=True, help='the speech profile env file the bot runs on'
    )
    serve.add_argument(
        '--call-seconds', type=int, default=MAX_CALL_SECONDS, help='the longest a call may run'
    )
    add = commands.add_parser('add', help="create a flow's Coval agent")
    add.add_argument('agency', help='a use case folder under use-cases/, salon for salon-graph, or graph')
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command line. Returns the exit code: 0 passed, 1 failed or refused, 3 still running."""
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
    workdir = Path(tempfile.mkdtemp(prefix='coval-sim-'))
    check = ReleaseCheck(
        repo,
        CovalClient(env['COVAL_API_KEY']),
        workdir,
        RunLog(getattr(args, 'log', Path())),
        getattr(args, 'port', PORT),
    )
    try:
        if args.command == 'serve':
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            with contextlib.suppress(KeyboardInterrupt):
                check.serve(args.agency, args.profile, args.call_seconds)
            return 0
        if args.command == 'add':
            line = check.add(args.agency)
            print(line)
            if line.startswith('created'):
                print('next: attach a test set and metrics to this agent in Coval, then run it')
            return 0
        job = check.plan(
            args.agency,
            args.agent,
            args.test_set,
            args.persona,
            iterations=args.iterations,
            max_simulations=args.max_simulations,
            label=args.label,
            concurrency=args.concurrency,
            metrics=args.metrics,
            profile=args.profile,
            call_seconds=args.call_seconds,
        )
        print(job.describe(), flush=True)
        if args.command == 'plan':
            return 0
        row = check.run(job, args.host)
    except (CovalError, ValueError, RuntimeError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    print(row.render())
    return STILL_RUNNING if row.still_running else 1 if row.problems else 0


if __name__ == '__main__':
    sys.exit(main())
