"""Run: uv run python -m tests.test_coval_sim

The Coval release check, with no Coval: which one job a set of arguments names
or why it is refused, what the run log remembers between invocations, and
which call results fail a run. The CLI, the tunnel and the bot are not run,
and nothing here starts the coval binary: every method that would is replaced.

Asserts, no pytest, no fixtures, same as the rest.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
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


def test_explicit_cases() -> None:
    """`--cases`: the plan holds exactly the frozen cases, and the launch names them, so the set cannot grow under it."""
    check = checker()
    job = check.plan(
        'salon', 'S', 'THREE', 'calm', iterations=1, max_simulations=2, label='l', cases=['c3', 'c1']
    )
    assert job.case_ids == ('c3', 'c1') and job.simulations == 2, job.describe()
    for cases, problem in (
        (['c1', 'c9'], 'case c9 is not in test set THREE'),
        (['c1', 'c1'], '--cases must name at least one case, each once'),
        ([], '--cases must name at least one case, each once'),
    ):
        try:
            check.plan('salon', 'S', 'THREE', 'calm', iterations=1, max_simulations=3, label='l', cases=cases)
        except PlanError as error:
            assert problem in error.problems, error.problems
        else:
            raise AssertionError(f'accepted --cases {cases}')

    class Launches(CovalClient):
        def _call(self, *args: str):
            self.args = args
            return {'run_id': 'R'}

    coval = Launches(api_key='test', binary='/nonexistent/coval')
    coval.launch(job.agent, job.persona, 'THREE', iterations=1, name='n', case_ids=job.case_ids)
    assert coval.args[coval.args.index('--test-cases') + 1] == 'c3,c1', coval.args
    parsed = _parser().parse_args(['plan', '--agency', 'salon', '--agent', 'S', '--test-set', 'THREE', '--persona', 'calm',
                                   '--max-simulations', '2', '--label', 'l', '--cases', 'c3,c1', '--profile', 'p.env'])  # fmt: skip
    assert parsed.cases == ['c3', 'c1'], 'the command line reaches plan'


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
    test_explicit_cases()
    test_shared_host()
    test_phone_line()
    test_campaign()
    test_correlation()
    test_speech_profile()
    print('ok')
