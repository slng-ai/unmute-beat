"""Run: uv run python -m tests.test_coval_sim

The Coval release check, with no Coval: which one job a set of arguments names
or why it is refused, what the run log remembers between invocations, and
which call results fail a run. The CLI, the tunnel and the bot are not run,
and nothing here starts the coval binary: every method that would is replaced.
A LiveKit job's token endpoint runs for real on 127.0.0.1; no LiveKit server
and no worker do.

Asserts, no pytest, no fixtures, same as the rest.
"""

import base64
import contextlib
import hashlib
import hmac
import http.client
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
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
    _parser,
    load_profile,
)
from coval_sim.coval import CovalClient, CovalError
from coval_sim.livekit import (
    SIMULATION_HEADER,
    TokenServer,
    bind_rooms,
    connection,
    registered,
    token_requests,
)
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
from coval_sim.runtime import CONNECTED, Campaign, Process, call_sids, ceiling, correlate, shown


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
    return ReleaseCheck(Repository.find(Path.cwd()), coval or FakeCoval(AGENTS), Path('.'), RunLog(log))


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

    assert Agent(id='x', display_name='voice-harness-salon-livekit-twilio').agency is None
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
    """An agency is its own folder, and a folder with no bot.py is refused."""
    repo = Repository.find(Path(__file__).resolve().parent)
    assert repo.use_case('salon') == repo.root / 'use-cases' / 'salon'
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

    lines = [turn.STUMBLE, turn.EXHAUSTED]
    assert all(fragment in line.lower() for fragment, line in zip(FALLBACKS.values(), lines, strict=True))

    heard = [
        'Sorry.',
        'Had a problem there.',
        'Sorry. Me correct myself.',
        "I'm going round in circles there.",
    ]
    sim = Simulation(
        simulation_id='c',
        status='COMPLETED',
        transcript=[Message(role='assistant', content=c) for c in heard],
    )
    assert run().problems([sim]) == [
        'call c: the harness said its STUMBLE (a turn failed) line',
        'call c: the harness said its EXHAUSTED (a turn ran out of budget) line',
    ]
    assert spoke('a').fallbacks == []


def test_call_ceiling() -> None:
    """A bot with no call limit of its own is stopped from outside, counted from when its call connects."""
    repo = Repository.find(Path.cwd())
    assert repo.limits_itself('salon') and not repo.limits_itself('salon-pipecat-flows')
    with tempfile.TemporaryDirectory() as folder:
        program = f'import time; time.sleep(0.5); print({CONNECTED!r}, flush=True); time.sleep(60)'
        bot = Process(
            'sleeper',
            [sys.executable, '-c', program],
            Path(folder),
            dict(os.environ),
            Path(folder) / 'bot.log',
        )
        started = time.monotonic()
        ceiling(bot, 1.0).join(15)
        assert bot._proc.poll() is not None, 'the bot outlived its ceiling'
        assert 1.4 < time.monotonic() - started < 15, 'stopped before its call connected, or far too late'


def test_concurrency() -> None:
    """Calls at once, the one speed-up on this side: planned, passed to Coval, and refused where it would cut calls."""
    native = Agent(id='N', display_name='voice-harness-salon-pipecat-flows-pipecat', test_set_ids=['THREE'])
    check = checker(FakeCoval([*AGENTS, native]))

    def plan(agency: str = 'salon', agent: str = 'S', concurrency: int = 3) -> list[str]:
        try:
            job = check.plan(
                agency,
                agent,
                'THREE',
                'calm',
                iterations=1,
                max_simulations=3,
                label='s',
                concurrency=concurrency,
            )
        except PlanError as error:
            return error.problems
        assert f'concurrency  {concurrency}' in job.describe(), job.describe()
        return []

    assert plan() == [] and plan(concurrency=1) == []
    assert (
        plan(concurrency=0)
        == plan(concurrency=4)
        == ['concurrency must be from 1 to the 3 simulation(s) planned']
    )
    assert 'concurrency 1 only' in plan('salon-pipecat-flows', 'N', 2)[0], 'its ceiling would cut later calls'
    assert plan('salon-pipecat-flows', 'N', 1) == []

    class Launches(CovalClient):
        """The real `launch`, with the coval binary replaced by a record of its arguments."""

        def _call(self, *args: str):
            self.args = args
            return {'run_id': 'R'}

    coval = Launches(api_key='test', binary='/nonexistent/coval')
    agent, persona = AGENTS[0], Persona(id='calm', name='Standard Customer')
    assert coval.launch(agent, persona, 'THREE', iterations=1, concurrency=3, name='n') == 'R'
    assert coval.args[coval.args.index('--concurrency') + 1] == '3', coval.args


def test_named_metrics() -> None:
    """`--metrics`: the launch names these instead of the agent's own; the lookup of a lost launch parses."""
    scored = Agent(id='S', display_name='voice-harness-salon-pipecat', test_set_ids=['ONE'], metric_ids=['m'])
    check = checker(FakeCoval([scored]))
    job = check.plan(
        'salon', 'S', 'ONE', 'calm', iterations=1, max_simulations=1, label='l', metrics=['count']
    )
    assert job.agent.metric_ids == ['count'] and 'metrics      count' in job.describe(), job.describe()
    kept = check.plan('salon', 'S', 'ONE', 'calm', iterations=1, max_simulations=1, label='l')
    assert kept.agent.metric_ids == ['m'], "no flag, the agent's own"

    class Launches(CovalClient):
        def _call(self, *args: str):
            self.args = args
            return [] if args[1] == 'list' else {'run_id': 'R'}

    coval = Launches(api_key='test', binary='/nonexistent/coval')
    coval.launch(job.agent, job.persona, 'ONE', iterations=1, name='n')
    assert coval.args[coval.args.index('--metric-ids') + 1] == 'count', coval.args
    parsed = _parser().parse_args(['run', '--agency', 'salon', '--agent', 'S', '--test-set', 'ONE', '--persona', 'calm',
                                   '--max-simulations', '1', '--label', 'l', '--metrics', 'a,b', '--profile', 'p.env'])  # fmt: skip
    assert parsed.metrics == ['a', 'b'], 'the command line reaches plan (a missed parser edit, 2026-09-26)'
    coval.find_run('S', 'n')
    assert '--order-by=-create_time' in coval.args, 'a bare -create_time is read as a flag (2026-09-26)'


PROFILE = Path(__file__).parent / 'fixtures' / 'speech-profile.v1.env'


def test_speech_profile() -> None:
    """A job names its speech profile; a file missing a name, or with a stray one, is refused before any bot starts."""
    check = checker()
    job = check.plan(
        'salon',
        'S',
        'ONE',
        'calm',
        iterations=1,
        max_simulations=1,
        label='p',
        profile=PROFILE,
        call_seconds=300,
    )
    assert job.profile is not None and job.profile.SLNG_STT_MODEL == 'deepgram/nova:3', job.profile
    assert 'speech       stt deepgram/nova:3' in job.describe() and 'call limit   300s' in job.describe()
    folder = Path(tempfile.mkdtemp())
    for name, body, says in (
        ('short.env', 'SLNG_STT_MODEL=deepgram/nova:3\n', 'SLNG_TTS_MODEL'),
        ('stray.env', PROFILE.read_text() + 'VAD_STOP_SECS=0.6\n', 'VAD_STOP_SECS'),
    ):
        (folder / name).write_text(body)
        try:
            check.plan(
                'salon', 'S', 'ONE', 'calm', iterations=1, max_simulations=1, label='p', profile=folder / name
            )
        except PlanError as error:
            assert any(says in problem for problem in error.problems), error.problems
        else:
            raise AssertionError(f'{name} was accepted')


def test_shared_host() -> None:
    """`run --host`: no tunnel, no bot, the agent pointed at the host `serve` holds. `serve` needs a self-limiting bot."""

    class Pointed(FakeCoval):
        def update_connection(self, agent: Agent, metadata: dict[str, Any]) -> Agent:
            self.voice_url = metadata['voice_url']
            return agent

    coval = Pointed(AGENTS)
    check = checker(coval)
    job = check.plan('salon', 'S', 'ONE', 'calm', iterations=1, max_simulations=1, label='shared')
    row = check.run(job, 'already-up.trycloudflare.com')
    assert coval.voice_url == 'https://already-up.trycloudflare.com/' and coval.launches == 1, row.render()
    try:
        check.serve('salon-pipecat-flows', PROFILE)
    except ValueError as error:
        assert 'no call limit of its own' in str(error)
    else:
        raise AssertionError('served a bot the outside ceiling would stop after one call')


FAKE_TWILIO = """#!/usr/bin/env python3
import json, sys
from pathlib import Path
state = Path(__file__).with_suffix('.json')
numbers = json.loads(state.read_text())
args = sys.argv[1:]
value = lambda flag: args[args.index(flag) + 1]
if args[0] == 'api:core:incoming-phone-numbers:list':
    print(json.dumps([n for n in numbers if n['phoneNumber'] == value('--phone-number')]))
elif args[0] == 'api:core:incoming-phone-numbers:update':
    for n in numbers:
        if n['sid'] == value('--sid'):
            n['voiceUrl'], n['voiceMethod'] = value('--voice-url'), value('--voice-method')
            n.setdefault('history', []).append(n['voiceUrl'])
    state.write_text(json.dumps(numbers))
    print('[]')
elif args[0] == 'api:core:calls:list':
    calls = json.loads(state.with_name('calls.json').read_text()) if state.with_name('calls.json').exists() else []
    print(json.dumps([c for c in calls if c['to'] == value('--to') and c['status'] == value('--status')]))
"""


def test_phone_line() -> None:
    """A `-twilio` voice agent is a real Twilio line: its number points at the bot for the job, then back."""
    line = Agent(id='T', display_name='voice-harness-salon-pipecat-twilio', model_type='MODEL_TYPE_VOICE',
                 phone_number='+447427919928')  # fmt: skip
    assert line.agency == 'salon' and line.phone_line
    assert not Agent(id='W', display_name='voice-harness-salon-pipecat').phone_line
    assert not Agent(id='X', display_name='voice-harness-salon-pipecat-twilio').phone_line, (
        'no number, no line'
    )
    assert (
        Agent(id='F', display_name='voice-harness-salon-pipecat-flows-pipecat').agency
        == 'salon-pipecat-flows'
    )
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / 'bot.log'
        log.write_text(
            'start CA' + 'a' * 32 + ' ok\nagain CA' + 'a' * 32 + '\nnext CA' + 'b' * 32 + '\nCAX\n'
        )
        assert call_sids(log) == ['CA' + 'a' * 32, 'CA' + 'b' * 32]
        assert call_sids(Path(tmp) / 'none.log') == []
        assert (
            shown('https://x.example/voice?token=secret') == 'https://x.example/voice'
            and shown('') == '(none)'
        )


def _fake_twilio(folder: Path) -> tuple[Path, Path]:
    binary = folder / 'twilio'
    binary.write_text(FAKE_TWILIO)
    binary.chmod(0o755)
    state = binary.with_suffix('.json')
    state.write_text(json.dumps([
        {'sid': 'PN1', 'phoneNumber': '+447427919928', 'voiceUrl': 'https://old.example/voice', 'voiceMethod': 'GET'},
        {'sid': 'PN2', 'phoneNumber': '+441111111111', 'voiceUrl': 'https://keep.example/x', 'voiceMethod': 'POST'},
    ]))  # fmt: skip
    return binary, state


def test_campaign() -> None:
    """One campaign owns the number for its life: jobs never touch it, and it is given back once, only when safe."""
    number = '+447427919928'
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        binary, state = _fake_twilio(folder)
        records = folder / 'campaigns'
        numbers = lambda: {n['sid']: n for n in json.loads(state.read_text())}  # noqa: E731
        owner = Campaign(
            number, 'up.trycloudflare.com', 'c-1', records, binary=str(binary), wait_s=0.3, poll_s=0.05
        )
        with owner:
            assert numbers()['PN1']['voiceUrl'] == 'https://up.trycloudflare.com/?campaign=c-1'
            held = Campaign.owning(number, 'up.trycloudflare.com', records)
            assert held['label'] == 'c-1' and held['previous'] == 'https://old.example/voice'
            for host in ('other.trycloudflare.com',):
                try:
                    Campaign.owning(number, host, records)
                except RuntimeError as error:
                    assert 'another host' in str(error), error
                else:
                    raise AssertionError('a job was sent at a host the campaign does not own')
            try:
                Campaign(number, 'second.trycloudflare.com', 'c-2', records, binary=str(binary)).__enter__()
            except RuntimeError as error:
                assert "owned by campaign 'c-1'" in str(error), error
            else:
                raise AssertionError('a second campaign took a number the first still owns')
            assert len(numbers()['PN1']['history']) == 1, 'jobs on one campaign pointed the number again'
        assert (
            numbers()['PN1']['voiceUrl'] == 'https://old.example/voice'
            and numbers()['PN1']['voiceMethod'] == 'GET'
        )
        assert numbers()['PN2']['voiceUrl'] == 'https://keep.example/x', 'another number was touched'
        assert not (records / 'campaign-447427919928.json').exists()
        try:
            Campaign.owning(number, 'up.trycloudflare.com', records)
        except RuntimeError as error:
            assert 'no live campaign' in str(error), error
        else:
            raise AssertionError('a job ran on a number no campaign owns')

        state.with_name('calls.json').write_text(
            json.dumps([{'sid': 'CA1', 'to': number, 'status': 'in-progress'}])
        )
        with Campaign(
            number, 'up.trycloudflare.com', 'c-3', records, binary=str(binary), wait_s=0.3, poll_s=0.05
        ):
            pass
        assert numbers()['PN1']['voiceUrl'].endswith('campaign=c-3'), 'restored while a call was in progress'
        kept = json.loads((records / 'campaign-447427919928.json').read_text())
        kept['pid'] = 999_999
        (records / 'campaign-447427919928.json').write_text(json.dumps(kept))
        state.with_name('calls.json').write_text('[]')
        with Campaign(
            number, 'up.trycloudflare.com', 'c-4', records, binary=str(binary), wait_s=0.3, poll_s=0.05
        ) as took:
            assert took.previous == 'https://old.example/voice', 'a takeover lost the URL from before'
        assert numbers()['PN1']['voiceUrl'] == 'https://old.example/voice', (
            'a takeover did not give the number back'
        )

        runner = folder / 'runner.db'
        db = sqlite3.connect(runner)
        db.execute('CREATE TABLE calls (call_sid TEXT, state TEXT)')
        db.execute("INSERT INTO calls VALUES ('CA2', 'active')")
        db.commit()
        db.close()
        with Campaign(
            number,
            'up.trycloudflare.com',
            'c-5',
            records,
            runner_db=runner,
            binary=str(binary),
            wait_s=0.3,
            poll_s=0.05,
        ):
            pass
        assert numbers()['PN1']['voiceUrl'].endswith('campaign=c-5'), (
            'restored while the runner still had a call'
        )
        (records / 'campaign-447427919928.json').unlink()

        with Campaign(
            number, 'up.trycloudflare.com', 'c-6', records, binary=str(binary), wait_s=0.3, poll_s=0.05
        ):
            subprocess.run([str(binary), 'api:core:incoming-phone-numbers:update', '--sid', 'PN1', '--voice-url', 'https://someone.else/', '--voice-method', 'POST'], check=True, capture_output=True)  # fmt: skip
        assert numbers()['PN1']['voiceUrl'] == 'https://someone.else/', (
            'a later change by somebody else was undone'
        )


def test_correlation() -> None:
    """A simulation is bound to a call by evidence: Coval's CallSid, or the one call that acted for its caller."""
    calls = {
        'CA1': {'+34699000001'},
        'CA2': {'+34699000002'},
        'CA3': set(),
        'CA4': {'+34699000004'},
        'CA5': {'+34699000004'},
    }
    cases = {'K1': '+34699000001', 'K2': '+34699000002', 'K3': '+34699000003', 'K4': '+34699000004'}
    sims = [
        {'simulation_id': 'S0', 'test_case_id': 'K9', 'call_sid': 'CA3'},
        {'simulation_id': 'S1', 'test_case_id': 'K1', 'call_sid': None},
        {'simulation_id': 'S2', 'test_case_id': 'K2', 'call_sid': None},
        {'simulation_id': 'S3', 'test_case_id': 'K3', 'call_sid': None},
        {'simulation_id': 'S4', 'test_case_id': 'K4', 'call_sid': None},
    ]
    assert correlate(sims, calls, cases) == {
        'S0': 'CA3',
        'S1': 'CA1',
        'S2': 'CA2',
        'S3': 'unknown',
        'S4': 'unknown',
    }
    twice = [{'simulation_id': 'A', 'test_case_id': 'K1'}, {'simulation_id': 'B', 'test_case_id': 'K1'}]
    assert correlate(twice, calls, cases) == {'A': 'unknown', 'B': 'unknown'}, (
        'one call bound to two simulations'
    )


LIVEKIT = {
    'LIVEKIT_URL': 'wss://voice.example',
    'LIVEKIT_API_KEY': 'APIcoval',
    'LIVEKIT_API_SECRET': 'secret-' + 'x' * 32,
}
LIVEKIT_AGENTS = [
    *AGENTS,
    Agent(id='L', display_name='voice-harness-salon-livekit', model_type='livekit', test_set_ids=['ONE', 'THREE']),
    Agent(id='LP', display_name='voice-harness-salon-prompt-livekit', test_set_ids=['ONE']),
    Agent(id='LK', display_name='voice-harness-kestrel-livekit', model_type='MODEL_TYPE_WEBSOCKET',
          test_set_ids=['ONE']),
    Agent(id='LW', display_name='voice-harness-wasla-pipecat', model_type='MODEL_TYPE_LIVEKIT', test_set_ids=['ONE']),
]  # fmt: skip


class Settled(Repository):
    """The checkout, with its `.env` and environment replaced: a test never reads the real ones."""

    def __init__(self, env: dict[str, str], dotenv: dict[str, str] | None = None) -> None:
        super().__init__(Repository.find(Path.cwd()).root)
        self._env, self._dotenv = env, dotenv or {}

    def dotenv(self) -> dict[str, str]:
        return dict(self._dotenv)

    def env(self) -> dict[str, str]:
        return {**self._dotenv, **self._env}


def on_livekit(
    env: dict[str, str] = LIVEKIT, dotenv: dict[str, str] | None = None, coval: FakeCoval | None = None
) -> ReleaseCheck:
    folder = Path(tempfile.mkdtemp())
    return ReleaseCheck(
        Settled(env, dotenv), coval or FakeCoval(LIVEKIT_AGENTS), folder, RunLog(folder / 'runs.jsonl')
    )


def livekit_refusals(check: ReleaseCheck, agency: str = 'salon', agent_id: str = 'L') -> list[str]:
    """The problems `plan` gives for a one-call LiveKit job; fails the test if it accepts it."""
    try:
        check.plan(agency, agent_id, 'ONE', 'calm', iterations=1, max_simulations=1, label='lk')
    except PlanError as error:
        return error.problems
    raise AssertionError(f'accepted: {agency} {agent_id}')


def test_livekit_names() -> None:
    """A `-livekit` agent calls the same folder as a LiveKit worker; never a phone line; the old names unchanged."""
    salon = Agent(id='x', display_name='voice-harness-salon-livekit', phone_number='+447427919928')
    assert salon.agency == 'salon' and salon.livekit and not salon.phone_line
    flows = Agent(id='x', display_name='voice-harness-salon-pipecat-flows-livekit')
    assert flows.agency == 'salon-pipecat-flows' and flows.livekit
    assert not Agent(id='x', display_name='voice-harness-salon-pipecat').livekit
    assert not Agent(id='x', display_name='voice-harness-salon-pipecat-twilio', phone_number='+44').livekit
    assert Agent(id='x', display_name='voice-harness-salon-livekit-twilio').agency is None
    assert Agent(id='x', display_name='voice-harness-salon-livekit-pipecat').agency == 'salon-livekit'
    assert Agent.name_for('salon', 'livekit') == 'voice-harness-salon-livekit'
    assert Agent.name_for('salon') == 'voice-harness-salon-pipecat'


def test_livekit_plan() -> None:
    """A LiveKit job: a use case on core, a wss:// server with its key and secret, and nothing in `.env` over the job."""
    check = on_livekit()
    job = check.plan(
        'salon', 'L', 'THREE', 'calm', iterations=1, max_simulations=3, label='lk', concurrency=3
    )
    assert job.server == 'wss://voice.example' and job.agent.livekit, job
    assert 'line         LiveKit: Coval joins a room on wss://voice.example' in job.describe(), job.describe()
    assert 'concurrency  3' in job.describe()

    assert livekit_refusals(on_livekit({})) == [
        'a LiveKit job needs LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET in the environment or .env'
    ]
    assert livekit_refusals(on_livekit({**LIVEKIT, 'LIVEKIT_API_SECRET': ''})) == [
        'a LiveKit job needs LIVEKIT_API_SECRET in the environment or .env'
    ]
    from_dotenv = on_livekit(
        {**LIVEKIT, 'LIVEKIT_URL': 'wss://shell.example'}, dotenv={'LIVEKIT_URL': 'wss://file.example'}
    )
    assert from_dotenv.plan(
        'salon', 'L', 'ONE', 'calm', iterations=1, max_simulations=1, label='lk'
    ).server == ('wss://file.example'), 'the server the worker reads: bot.py loads .env over its environment'
    assert livekit_refusals(on_livekit({**LIVEKIT, 'LIVEKIT_URL': 'ws://localhost:7880'})) == [
        "LIVEKIT_URL is 'ws://localhost:7880': Coval joins only a wss:// server"
    ]
    assert livekit_refusals(on_livekit(dotenv={'DELIVERY': 'marks', 'LIVEKIT_AGENT_NAME': 'mine'})) == [
        ".env sets DELIVERY, LIVEKIT_AGENT_NAME, which bot.py would load over the job's own: remove them"
    ]
    assert livekit_refusals(on_livekit(), 'salon-prompt', 'LP') == [
        'salon-prompt cannot run on LiveKit: its bot.py does not run through core.voice.serve, '
        'which only a use case on core does'
    ], 'the one-prompt baseline is Pipecat only'
    assert livekit_refusals(on_livekit(), 'kestrel', 'LK') == [
        'agent LK is named for LiveKit, but Coval says its type is MODEL_TYPE_WEBSOCKET'
    ]
    assert livekit_refusals(on_livekit(), 'wasla', 'LW') == [
        'agent LW is a Coval LiveKit agent (MODEL_TYPE_LIVEKIT), but its name does not end -livekit: '
        'name it voice-harness-wasla-livekit'
    ]
    assert livekit_refusals(on_livekit({}), 'salon-prompt', 'LP')[0].startswith('salon-prompt cannot run'), (
        'every reason at once'
    )

    coval = FakeCoval(LIVEKIT_AGENTS)
    shared = on_livekit(coval=coval)
    job = shared.plan('salon', 'L', 'ONE', 'calm', iterations=1, max_simulations=1, label='lk')
    row = shared.run(job, 'already-up.trycloudflare.com')
    assert coval.launches == 0 and '--host' in row.problems[0], row.render()


def test_livekit_worker_env() -> None:
    """The worker gets `.env`, the job's settings, and no Twilio placeholder: LiveKit carries no Twilio."""
    env = Repository.worker_env(
        {'TWILIO_AUTH_TOKEN': 'real', 'KEEP': '1'}, 'voice-harness-salon-ab12', 0, None, 120
    )
    assert env['DELIVERY'] == 'simulated' and env['LIVEKIT_AGENT_NAME'] == 'voice-harness-salon-ab12'
    assert env['LIVEKIT_IDLE_PROCESSES'] == '1' and env['MAX_CALL_SECONDS'] == '120', (
        'at least one process ready'
    )
    assert env['CALLER_ID'] == '' and env['DIARY_PER_CALL'] == '1' and env['KEEP'] == '1'
    assert env['LIVEKIT_HTTP_PORT'] == '0', 'two jobs on one machine would collide on LiveKit 8081'
    assert 'TWILIO_ACCOUNT_SID' not in env and env['TWILIO_AUTH_TOKEN'] == 'real', 'only the placeholders go'
    profiled = Repository.worker_env({}, 'n', 3, load_profile(PROFILE), MAX_CALL_SECONDS)
    assert profiled['LIVEKIT_IDLE_PROCESSES'] == '3' and profiled['SLNG_STT_MODEL'] == 'deepgram/nova:3'


def test_livekit_registered() -> None:
    """Ready is LiveKit's own `registered worker` line, naming this job's worker; never another's."""
    line = json.dumps({
        'message': 'registered worker', 'level': 'INFO', 'name': 'livekit.agents',
        'agent_name': 'voice-harness-salon-ab12', 'id': 'AW_x', 'url': 'wss://voice.example', 'pid': 1,
    })  # fmt: skip
    assert registered(f'{{"message": "starting worker"}}\n{line}\n', 'voice-harness-salon-ab12')
    assert not registered(line, 'voice-harness-salon-cd34'), 'a worker under another name'
    assert not registered('unregistered worker voice-harness-salon-ab12', 'voice-harness-salon-ab12')
    coloured = '12:00:01 - INFO livekit.agents - registered worker {"agent_name": "voice-harness-salon-ab12"}'
    assert registered(coloured, 'voice-harness-salon-ab12'), 'the dev format too'


def _post(
    url: str, body: bytes, headers: dict[str, str], method: str = 'POST'
) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(
        url, data=body if method == 'POST' else None, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _unpadded(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def test_token_server() -> None:
    """A real POST gets a LiveKit token that dispatches the job's own worker; anything else is refused, unrecorded."""
    key, secret, worker = 'APIcoval', LIVEKIT['LIVEKIT_API_SECRET'], 'voice-harness-salon-ab12'
    asked = {
        'room_name': 'b1c4e0de-0000-4000-8000-000000000001',
        'participant_name': 'simulated_user',
        'room_config': {'agents': [{'agent_name': 'somebody-else'}]},
    }
    auth = {'Authorization': 'Bearer job-key', 'Content-Type': 'application/json'}
    with tempfile.TemporaryDirectory() as tmp:
        records = Path(tmp) / 'tokens.jsonl'
        with TokenServer(key, secret, 'wss://voice.example', worker, records, key='job-key') as server:
            url = f'http://127.0.0.1:{server.port}/token'
            status, body = _post(url, json.dumps(asked).encode(), {**auth, SIMULATION_HEADER: 'sim-1'})
            assert status == 200, body
            assert body['serverUrl'] == 'wss://voice.example' and body['room_name'] == asked['room_name'], (
                body
            )
            token = str(body['token'])
            head, payload, signature = token.split('.')
            assert json.loads(_unpadded(head)) == {'alg': 'HS256', 'typ': 'JWT'}
            expected = hmac.new(secret.encode(), f'{head}.{payload}'.encode(), hashlib.sha256).digest()
            assert hmac.compare_digest(_unpadded(signature), expected), 'signed with the API secret'
            claims = json.loads(_unpadded(payload))
            assert claims['exp'] - claims['nbf'] == 3600 and abs(claims['nbf'] - time.time()) < 60, claims
            assert {k: v for k, v in claims.items() if k not in ('nbf', 'exp')} == {
                'iss': key,
                'sub': 'simulated_user',
                'name': 'simulated_user',
                'video': {
                    'room': asked['room_name'],
                    'roomJoin': True,
                    'canPublish': True,
                    'canSubscribe': True,
                    'canPublishData': True,
                },
                'roomConfig': {'agents': [{'agentName': worker}]},
            }, 'the job worker is dispatched, never the name the request asked for'
            _check_with_livekit(token, key, secret, worker, asked['room_name'])

            again = json.dumps({'room_name': 'room-2'}).encode()
            assert _post(url, again, {**auth, SIMULATION_HEADER: '{{simulation_id}}'})[0] == 200
            assert _post(url, json.dumps({'room_name': 'room-3'}).encode(), auth)[0] == 200

            good = json.dumps(asked).encode()
            for why, status, body_, headers, address in (
                ('no key', 401, good, {'Content-Type': 'application/json'}, url),
                ('a wrong key', 401, good, {'Authorization': 'Bearer guess'}, url),
                ('not JSON', 400, b'{', auth, url),
                ('a list', 400, b'[]', auth, url),
                ('no room', 400, b'{"participant_name": "simulated_user"}', auth, url),
                ('a room with a space', 400, b'{"room_name": "a b"}', auth, url),
                ('a room too long', 400, json.dumps({'room_name': 'r' * 200}).encode(), auth, url),
                ('a participant not a string', 400, b'{"room_name": "r", "participant_name": 7}', auth, url),
                ('another path', 404, good, auth, url + 's'),
            ):
                got = _post(address, body_, headers)
                assert got[0] == status, (why, got)
            assert _post(url, b'', auth, method='GET')[0] == 405
            raw = http.client.HTTPConnection('127.0.0.1', server.port, timeout=10)
            raw.putrequest('POST', '/token')
            raw.putheader('Content-Length', '999999')
            raw.endheaders()
            assert raw.getresponse().status == 413, 'a body too large is not read'
            raw.close()
        assert [(r['room'], r['simulation_id'], r['agent_name']) for r in token_requests(records)] == [
            (asked['room_name'], 'sim-1', worker),
            ('room-2', None, worker),
            ('room-3', None, worker),
        ], 'answered requests only; an unfilled placeholder is no simulation ID'
    assert token_requests(Path(tmp) / 'gone.jsonl') == []
    try:
        TokenServer(key, secret, 'wss://voice.example', worker, Path(tmp) / 'x.jsonl', key='')
    except ValueError:
        pass
    else:
        raise AssertionError('a token server with no key would answer anybody')


def _check_with_livekit(token: str, key: str, secret: str, worker: str, room: str) -> None:
    """LiveKit's own verifier reads the token, and its own minter writes the same claims. Skipped without livekit-api."""
    try:
        from livekit import api
        from livekit.protocol.agent_dispatch import RoomAgentDispatch
        from livekit.protocol.room import RoomConfiguration
    except ImportError:
        return
    claims = api.TokenVerifier(key, secret).verify(token)
    assert claims.identity == 'simulated_user' and claims.video is not None, claims
    assert claims.video.room == room and claims.video.room_join and claims.video.can_publish
    assert claims.room_config is not None and [a.agent_name for a in claims.room_config.agents] == [worker]
    theirs = (
        api.AccessToken(key, secret)
        .with_identity('simulated_user')
        .with_name('simulated_user')
        .with_grants(api.VideoGrants(room_join=True, room=room))
        .with_room_config(RoomConfiguration(agents=[RoomAgentDispatch(agent_name=worker)]))
        .to_jwt()
    )
    ours, written = (json.loads(_unpadded(t.split('.')[1])) for t in (token, theirs))
    assert {k: v for k, v in ours.items() if k not in ('nbf', 'exp')} == {
        k: v for k, v in written.items() if k not in ('nbf', 'exp')
    }, (ours, written)


def test_bind_rooms() -> None:
    """A simulation is bound to the one room its own ID asked for; never by order, never shared."""
    requests: list[dict[str, object]] = [
        {'room': 'R1', 'simulation_id': 'A'},
        {'room': 'R1', 'simulation_id': 'A'},
        {'room': 'R2', 'simulation_id': 'B'},
        {'room': 'R3', 'simulation_id': 'B'},
        {'room': 'R4', 'simulation_id': 'C'},
        {'room': 'R4', 'simulation_id': 'D'},
        {'room': 'R5', 'simulation_id': None},
    ]
    assert bind_rooms(['A', 'B', 'C', 'D', 'E'], requests) == {
        'A': 'R1',
        'B': 'unknown',
        'C': 'unknown',
        'D': 'unknown',
        'E': 'unknown',
    }, 'a retry is one room; two rooms, a shared room, or no ID is unknown'


def test_livekit_result() -> None:
    """After a LiveKit run each simulation is printed with its room, from the token log, ready for load_report."""

    class Finished(FakeCoval):
        def simulations(self, run_id: str) -> list[Simulation]:
            return [spoke('A'), spoke('E')]

    check = on_livekit(coval=Finished(LIVEKIT_AGENTS))
    job = check.plan('salon', 'L', 'ONE', 'calm', iterations=1, max_simulations=1, label='lk')
    tokens = check.workdir / 'salon-tokens.jsonl'
    tokens.write_text(
        '\n'.join(
            json.dumps(r)
            for r in ({'room': 'R1', 'simulation_id': 'A'}, {'room': 'R5', 'simulation_id': None})
        )
        + '\n{"torn'
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        row = check._result(job, 'R', 'n', check.workdir / 'bot.log', {'tokens': str(tokens)})
    assert row.problems == [], row.render()
    printed = out.getvalue()
    assert 'simulation A = room R1 (load_report --call A=R1)' in printed, printed
    assert 'simulation E = room unknown' in printed, 'no request carried its ID: not guessed by order'
    assert '1 token request(s) carried no simulation ID' in printed, printed
    assert latest(check, job.key)['bound'] == {'A': 'R1', 'E': 'unknown'}
    assert latest(check, job.key)['calls'] == ['R1', 'R5']


def test_livekit_add() -> None:
    """`add --livekit` creates a Coval LiveKit agent with a placeholder connection, for a use case on core only."""

    class Creates(FakeCoval):
        def create_agent(self, name: str, kind: str, metadata: dict[str, Any]) -> Agent:
            self.created = (name, kind, metadata)
            return Agent(id='new', display_name=name)

    coval = Creates(AGENTS)
    check = on_livekit(coval=coval)
    assert check.add('salon', on_livekit=True) == 'created voice-harness-salon-livekit'
    name, kind, metadata = coval.created
    assert kind == 'livekit' and metadata['livekit_agent_name'] == name, coval.created
    assert set(metadata) == {
        'generate_token_endpoint',
        'livekit_url',
        'livekit_agent_name',
        'generate_token_headers',
    }
    assert check.add('salon') == 'exists  voice-harness-salon-pipecat'
    try:
        check.add('salon-prompt', on_livekit=True)
    except ValueError as error:
        assert 'cannot run on LiveKit' in str(error), error
    else:
        raise AssertionError('a LiveKit agent for a bot that cannot run on LiveKit')
    assert _parser().parse_args(['add', 'salon', '--livekit']).livekit

    pointed = connection('up.trycloudflare.com', 'wss://voice.example', 'voice-harness-salon-ab12', 'job-key')
    assert pointed['generate_token_endpoint'] == 'https://up.trycloudflare.com/token'
    assert (pointed['livekit_url'], pointed['livekit_agent_name']) == (
        'wss://voice.example',
        'voice-harness-salon-ab12',
    )
    assert json.loads(pointed['generate_token_headers']) == {
        SIMULATION_HEADER: '{{simulation_id}}',
        'Authorization': 'Bearer job-key',
    }, 'a JSON string, as Coval takes it'


if __name__ == '__main__':
    test_call_ceiling()
    test_plan()
    test_run_log()
    test_still_running()
    test_use_case_folders()
    test_graph_build()
    test_problems()
    test_fallbacks()
    test_concurrency()
    test_named_metrics()
    test_shared_host()
    test_phone_line()
    test_campaign()
    test_correlation()
    test_speech_profile()
    test_livekit_names()
    test_livekit_plan()
    test_livekit_worker_env()
    test_livekit_registered()
    test_token_server()
    test_bind_rooms()
    test_livekit_result()
    test_livekit_add()
    print('ok')
