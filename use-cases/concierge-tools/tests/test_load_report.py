"""Run: uv run python -m tests.test_load_report

`load_report.judge` on synthetic calls and on nine real session-4 calls
(`fixtures/session4_calls.json`: their conversation, turn and write spans
only; `fixtures/session4_evidence.json`: an agent's reading of them). Each
rule below once gave a false pass or a false failure. Then the command
itself, run as a process, for its exit status. Asserts, no pytest, no network.
"""

import json
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from coval_sim.models import Simulation
from ledger import Obs, from_json, to_json
from load_report import (
    EXIT_FAILED,
    EXIT_INCOMPLETE,
    CallReport,
    CallReview,
    Evidence,
    Facts,
    effects,
    judge,
    render,
)

HERE = Path(__file__).parent
T0 = datetime(2026, 9, 26, 12, tzinfo=UTC)
PHONE = '+34671273454'
FRIDAY = {'service': 'haircut', 'date': '2026-10-02', 'time': '09:00:00'}
SATURDAY = {'service': 'haircut', 'date': '2026-10-03', 'time': '09:00:00'}
CASE = 'case-1'


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def chat(id_: str, start: float, end: float, content: str = '', *tools: str) -> Obs:
    output = {
        'role': 'assistant',
        'content': content,
        'tool_calls': [{'function': {'name': t}} for t in tools],
    }
    usage = {'input': 1000, 'output': 10}
    return Obs(id_, 'e', 'GENERATION', 'chat gemini-3.5-flash-lite', at(start), at(end), 'gemini-3.5-flash-lite',
               None, output, usage)  # fmt: skip


def write(
    turn: int, asked: int, action: str = 'book_appointment', receipt: dict | None = None, **audit
) -> Obs:
    """One audited write, landed, answering a question put on turn `asked`."""
    body = {'action': action, 'about': action, 'question': 'Shall I book you in?', 'asked_turn': asked,
            'turn': turn, 'heard': 'yes please', 'status': 'ok', 'args': {'phone': PHONE},
            'receipt': {'status': 'booked', 'booking_id': 'BK0001', 'spoken': 'Friday the 2nd at 9:00 AM', **FRIDAY}
            if receipt is None else receipt} | audit  # fmt: skip
    return Obs(f'w{turn}', 'e', 'SPAN', 'write', at(9 + turn / 10), at(9.1 + turn / 10),
               attrs={'write.audit': json.dumps(body)})  # fmt: skip


def lookup(turn: int = 2, **audit) -> Obs:
    receipt = {'customer_status': 'created', 'customer_phone': PHONE}
    return write(turn, turn - 1, 'lookup_customer', receipt, args={'phone': PHONE}, **audit)


def call(*extra: Obs, chats: Sequence[Obs] = (), segments: Sequence[str] = (), origins: Sequence[str] = (), span: float = 60) -> list[Obs]:  # fmt: skip
    engine = Obs('e', 'conv', 'AGENT', 'booking', at(1), at(8), attrs={
        'agent': 'booking', 'status': 'answered', 'speech_segments': str(list(segments)),
        'speech_origins': str(list(origins)), 'request_first_output': str(['tool', 'text'][: len(chats)]),
        'request_ttft': str([0.4, 0.5][: len(chats)]),
    })  # fmt: skip
    turn = {'turn.user_bot_latency_seconds': 1.2, 'turn.was_interrupted': 'true'}
    return [
        Obs('conv', None, 'SPAN', 'conversation', at(0), at(span)),
        Obs('t1', 'conv', 'SPAN', 'turn', at(0), at(8), attrs=turn),
        engine,
        *chats,
        *extra,
    ]


SIM = Simulation(simulation_id='sim', status='COMPLETED', call_sid='CA1', test_case_id=CASE)


def evidence(**review) -> Evidence:
    """A case asking for one haircut, and, when given, an agent's review and a measured normal ending."""
    reviews = {'sim': CallReview(reviewer='agent', source='synthetic', **review)} if review else {}
    return Evidence.model_validate({
        'cases': {CASE: {'bookings': 1, 'service': 'haircut'}}, 'reviews': reviews,
        'facts': {'sim': Facts(media_seconds=60, ended_by='caller', source='synthetic')},
    })  # fmt: skip


def heard_and_accepted(*keys: str) -> dict:
    return {key: {'accepted': True, 'delivered': True} for key in keys}


def the_diagnostics_still_find_every_fault() -> None:
    clean = call(lookup(), write(3, 2), chats=[chat('a', 1, 1.6, '', 'collect'), chat('b', 2, 2.9, 'Which time?')],
                 segments=['x:2:1', 'x:2:2'], origins=['tool_filler', 'model'])  # fmt: skip
    report = judge('run', SIM, clean)
    assert report.problems == [], report.problems
    assert report.bookings == ['Friday the 2nd at 9:00 AM'] and report.first_booking == 'BK0001'
    assert report.holding == {2: 1} and report.latency == [1.2] and report.interrupted == 1
    assert [round(t, 2) for t in report.tails] == [0.4], 'the second reply spoke: 0.9 s, first token at 0.5 s'
    assert report.booked_after == 9.3, report.booked_after
    noisy = call(
        write(3, 3, receipt={'status': 'booked', 'booking_id': 'BK0002', **FRIDAY}),
        chats=[chat('a', 1, 1.6, '', 'collect'), chat('b', 2, 2.9, 'One moment. Shall I book you in?', 'book_appointment')],
        segments=['x:2:1', 'x:2:2', 'x:2:3'],
        origins=['tool_filler', 'tool_filler', 'confirmation'],
    )  # fmt: skip
    report = judge('run', SIM, noisy)
    for expected in ('not BK0001', 'spoke beside an operation', 'repeated holding lines'):
        assert sum(expected in p for p in report.problems) == 1, (expected, report.problems)
    assert report.chains == {'collect > book_appointment': 1} and report.filler_copies == 1, report
    assert report.authorization.failed == ['book_appointment@3: no question about it put on an earlier turn']


def a_move_is_the_same_appointment() -> None:
    """Native YzkhPWRp and 3CyVWZmp were reported as two bookings: each was a book, then a move of BK0001."""
    moved = {'status': 'moved', 'booking_id': 'BK0001', **SATURDAY}
    obs = call(
        lookup(), write(3, 2), write(5, 4, receipt=moved, args={'booking_id': 'BK0001', 'phone': PHONE})
    )
    report = judge('run', SIM, obs, evidence=evidence())
    assert report.applied == ['book BK0001', 'move BK0001'] and len(report.bookings) == 2, report.applied
    assert report.active == [SATURDAY | {'phone': PHONE}], report.active
    assert report.outcome.verdict == 'unknown' and not report.outcome.failed, (
        'one appointment: the day is unreviewed'
    )
    final = {'service': 'haircut', 'date': '2026-10-03', 'time': '09:00', 'phone': PHONE}
    reviewed = judge('run', SIM, obs, evidence=evidence(final=final))
    assert str(reviewed.outcome) == 'pass (deterministic, review:agent)', reviewed.outcome
    friday = judge('run', SIM, obs, evidence=evidence(final=final | {'date': '2026-10-02'}))
    assert friday.outcome.failed == ['booked 2026-10-03 09:00:00, asked for 2026-10-02 09:00:00'], (
        friday.outcome
    )


def a_cancel_leaves_no_appointment() -> None:
    gone = {'status': 'cancelled', 'booking_id': 'BK0001', **FRIDAY}
    obs = call(lookup(), write(3, 2), write(5, 4, 'cancel_appointment', gone, args={'booking_id': 'BK0001'}))
    report = judge('run', SIM, obs, evidence=evidence())
    assert report.active == [] and report.applied == ['book BK0001', 'cancel BK0001'], report
    assert report.outcome.failed == ['0 active appointment(s), 1 requested'], report.outcome


def two_active_appointments_are_a_duplicate() -> None:
    second = {'status': 'booked', 'booking_id': 'BK0002', **SATURDAY}
    report = judge('run', SIM, call(lookup(), write(3, 2), write(5, 4, receipt=second)), evidence=evidence())
    assert len(report.active) == 2 and report.outcome.failed == ['2 active appointment(s), 1 requested']


def a_repeated_receipt_is_one_appointment() -> None:
    """The diary answers a retried token with the first receipt: the same write seen twice books nothing more."""
    done = effects(judge_writes(call(lookup(), write(3, 2), write(4, 2))))
    assert done.repeats == 1 and list(done.active) == ['BK0001'] and done.applied == ['book BK0001'], done
    different = {'status': 'booked', 'booking_id': 'BK0001', **SATURDAY}
    clash = effects(judge_writes(call(write(3, 2), write(4, 2, receipt=different))))
    assert clash.unknown == ['book_appointment@4: BK0001 booked twice with different details'], clash


def judge_writes(obs: list[Obs]):
    from ledger import read_call

    return read_call('x', obs).writes


def missing_audit_fields_and_unmapped_writes_are_incomplete() -> None:
    bare = json.loads(write(3, 2).attrs['write.audit'])
    del bare['question']
    thin = call(
        lookup(), Obs('w3', 'e', 'SPAN', 'write', at(9.5), at(9.6), attrs={'write.audit': json.dumps(bare)})
    )
    report = judge('run', SIM, thin, evidence=evidence(writes=heard_and_accepted('lookup_customer@2')))
    assert report.authorization.verdict == 'unknown', report.authorization
    assert report.authorization.unknown == ['book_appointment@3: its audit lacks question'], (
        report.authorization
    )
    unmapped = judge('run', SIM, call(write(3, 2, 'save_name', {'status': 'saved'})), evidence=evidence())
    assert unmapped.authorization.unknown == [
        'save_name@3: whether it needed a yes (no mapping, no needs_approval in its audit)'
    ], unmapped.authorization
    assert any(m.startswith('authorization:') for m in unmapped.missing), unmapped.missing
    no_receipt = judge('run', SIM, call(write(3, 2, receipt={})), evidence=evidence())
    assert no_receipt.outcome.unknown == ['book_appointment@3: landed, but no receipt says what it did']


def native_confirm_customer_is_checked() -> None:
    """The mapping had `confirm_phone`; the native build's write is `confirm_customer`, and it went unchecked."""
    receipt = {'customer_status': 'created', 'customer_phone': PHONE}
    wrong_question = judge('run', SIM, call(write(3, 2, 'confirm_customer', receipt, about='booking')))
    assert wrong_question.authorization.failed == [
        'confirm_customer@3: no question about it put on an earlier turn'
    ]
    thats = call(write(6, 5, 'confirm_customer', receipt, about='phone', heard="that's"))
    report = judge('run', SIM, thats, evidence=evidence(writes={'confirm_customer@6': {'accepted': False}}))
    assert report.authorization.failed == ['confirm_customer@6: "that\'s" did not accept the proposal'], (
        report
    )


def a_lookup_receipt_backs_no_booking_claim() -> None:
    """Gate 1's cYMMixAc: a goodbye said "your haircut is booked" with only a lookup receipt on the call."""
    report = judge('run', SIM, call(lookup()), evidence=evidence(claims=['booked']))
    assert report.completion.failed == ['said booked; the receipts show no change'], report.completion
    booked = judge('run', SIM, call(lookup(), write(3, 2)), evidence=evidence(claims=['booked']))
    assert booked.completion.verdict == 'pass', booked.completion
    gone = {'status': 'cancelled', 'booking_id': 'BK0001', **FRIDAY}
    cancelled = call(
        lookup(), write(3, 2), write(5, 4, 'cancel_appointment', gone, args={'booking_id': 'BK0001'})
    )
    after = judge('run', SIM, cancelled, evidence=evidence(claims=['booked']))
    assert after.completion.failed == ['said booked; the receipts show book BK0001, cancel BK0001'], (
        after.completion
    )
    unreviewed = judge('run', SIM, call(lookup()), evidence=evidence())
    assert unreviewed.completion.verdict == 'unknown', 'no reviewed closing is not a truthful one'


def an_unheard_proposal_is_never_verified_safe() -> None:
    """Call 1's fymMyWTi: the audit matched, so the old report passed it; the read-back had not played."""
    obs = call(lookup(heard="that's two seven three four five four"))
    report = judge('run', SIM, obs, evidence=evidence())
    assert report.unmatched == [] and report.delivery.verdict == 'unknown', 'a matched label is not delivery'
    assert report.authorization.verdict == 'unknown', 'nor acceptance'
    reviewed = judge('run', SIM, obs, evidence=evidence(writes={'lookup_customer@2': {'accepted': False, 'delivered': False}}))  # fmt: skip
    assert reviewed.delivery.failed == [
        'lookup_customer@2: the proposal had not played in full when it was answered'
    ]
    assert reviewed.authorization.verdict == 'fail'
    acked = judge('run', SIM, call(lookup(delivery='acknowledged')), evidence=evidence())
    assert str(acked.delivery) == 'pass (deterministic)', (
        'a transport acknowledgement in the audit is evidence'
    )


def an_unknown_outcome_stays_unknown() -> None:
    unsure = call(lookup(), write(3, 2, status='unknown', receipt={}))
    report = judge('run', SIM, unsure, evidence=evidence(claims=['booked'], final=FRIDAY | {'phone': PHONE}))
    assert report.outcome.unknown == ['book_appointment@3: outcome unknown'] and not report.outcome.failed
    assert report.completion.verdict == 'unknown', (
        'a claim about an unknown outcome is neither true nor false'
    )
    assert report.verdict == 'unknown'


def a_long_span_is_not_a_ceiling() -> None:
    """Native iPFgi5qq: a 1,105 s span was reported as "ended by the 300 s limit"; its recording is 50 s."""
    report = judge('run', SIM, call(span=1105), limit=300)
    assert report.span_seconds == 1105 and not report.operational.failed, report.operational
    assert report.operational.unknown == [
        'how the call ended: not measured (the trace span, 1105 s, is not its length)'
    ], report.operational
    logged = Evidence(facts={'sim': Facts(media_seconds=49.7, ended_by='ceiling', source='bot log')})
    assert judge('run', SIM, call(span=1105), 300, logged).operational.unknown == [
        'a ceiling was logged, but the recording is 50 s of 300'
    ]
    stalled = Evidence(facts={'sim': Facts(media_seconds=49.7, ended_by='provider_stall', source='bot log')})
    assert judge('run', SIM, call(span=1105), 300, stalled).operational.failed == ['ended by provider stall']


def a_call_with_no_trace_says_so() -> None:
    report = judge('run', Simulation(simulation_id='s', status='FAILED', error_message='no answer'), [])
    assert report.operational.failed == ['call FAILED: no answer'], report.operational
    assert 'operational: no Langfuse session for this call' in report.missing, report.missing
    assert report.outcome.verdict == 'unknown' and report.verdict == 'fail'
    waiting = judge('run', Simulation(simulation_id='s', status='IN PROGRESS'), [])
    assert waiting.operational.unknown[0] == 'call IN PROGRESS: not finished' and waiting.verdict == 'unknown'


def real_calls() -> dict[str, CallReport]:
    rows = json.loads((HERE / 'fixtures' / 'session4_calls.json').read_text())
    found = Evidence.model_validate_json((HERE / 'fixtures' / 'session4_evidence.json').read_text())
    return {
        row['simulation']['simulation_id'][:8]: judge(
            row['run'], Simulation.model_validate(row['simulation']), [from_json(o) for o in row['observations']],
            300, found,
        )
        for row in rows
    }  # fmt: skip


def the_real_session_4_calls_are_scored_on_their_evidence() -> None:
    r = real_calls()
    early = r['fymMyWTi']
    assert early.unmatched == [], 'the old check passed the first consent fault'
    assert early.authorization.verdict == 'fail' and early.delivery.verdict == 'fail', early
    assert early.operational.failed == ['ended by test runner'], early.operational

    goodbye = r['cYMMixAc']
    assert goodbye.completion.failed == ['said booked; the receipts show no change'], goodbye.completion
    assert goodbye.outcome.failed == ['0 active appointment(s), 1 requested']

    for native in ('YzkhPWRp', '3CyVWZmp'):
        moved = r[native]
        assert moved.applied == ['book BK0001', 'move BK0001'] and len(moved.active) == 1, moved.applied
        assert str(moved.outcome) == 'pass (deterministic, review:agent)', (native, moved.outcome)
        assert moved.authorization.verdict == 'fail', 'the Friday booking was written on a pick or on noise'
        assert moved.completion.verdict == 'pass'
    assert r['3CyVWZmp'].authorization.failed == [
        "save_booking@15: 'silence underscore tool' did not accept the proposal"
    ], r['3CyVWZmp'].authorization
    assert r['kwUbc4PR'].authorization.failed == ['confirm_customer@6: "that\'s" did not accept the proposal']
    assert r['kwUbc4PR'].outcome.verdict == 'pass', 'the right final booking, on an invalid identity write'

    stalled = r['C26pPHK6']
    assert stalled.outcome.failed == ['0 active appointment(s), 1 requested'], 'safe, but not a success'
    assert stalled.authorization.verdict == 'pass' and stalled.completion.verdict == 'pass', stalled

    long = r['iPFgi5qq']
    assert long.span_seconds is not None and long.span_seconds > 1100 and long.media_seconds == 49.742
    assert long.operational.failed == ['ended by provider stall'], long.operational

    for control in ('SB4GGMzU', 'VuhAwZ3e'):
        good = r[control]
        assert (good.outcome.verdict, good.authorization.verdict) == ('pass', 'pass'), (control, good)
        assert good.delivery.verdict == 'unknown' and good.verdict == 'unknown', (
            'no recording reviewed: not a pass'
        )
    table = render(list(r.values()))
    assert '| outcome | 5 | 4 | 0 | 5 |' in table, table


def run(*args: str) -> int:
    """The command, as a separate process: its real exit status."""
    with tempfile.TemporaryDirectory() as out:
        done = subprocess.run(
            [sys.executable, 'load_report.py', *args, '--out', out], capture_output=True, text=True
        )
        if done.returncode in (0, EXIT_FAILED, EXIT_INCOMPLETE):
            assert (Path(out) / 'report.json').exists(), done.stderr
        return done.returncode


def the_command_fails_on_failures_and_on_missing_evidence() -> None:
    fixtures = HERE / 'fixtures'
    assert run('--saved', str(fixtures / 'session4_calls.json'), '--evidence', str(fixtures / 'session4_evidence.json')) == EXIT_FAILED  # fmt: skip
    with tempfile.TemporaryDirectory() as tmp:
        saved, found = Path(tmp) / 'saved.json', Path(tmp) / 'evidence.json'
        complete = call(lookup(delivery='acknowledged'), write(3, 2, delivery='acknowledged'))
        saved.write_text(json.dumps([
            {'run': 'run', 'simulation': SIM.model_dump(mode='json'), 'observations': [to_json(o) for o in complete]}
        ]))  # fmt: skip
        good = evidence(final=FRIDAY | {'phone': PHONE}, claims=['booked'], writes=heard_and_accepted('lookup_customer@2', 'book_appointment@3'))  # fmt: skip
        found.write_text(good.model_dump_json())
        assert run('--saved', str(saved), '--evidence', str(found)) == 0, 'a complete, passing call exits 0'
        assert run('--saved', str(saved)) == EXIT_INCOMPLETE, 'the same call with no evidence is incomplete'
        wrong = good.model_copy(update={'reviews': {'sim': good.reviews['sim'].model_copy(update={'claims': ['cancelled']})}})  # fmt: skip
        found.write_text(wrong.model_dump_json())
        assert run('--saved', str(saved), '--evidence', str(found)) == EXIT_FAILED, (
            'a failed criterion exits 1'
        )
        found.write_text('{"reviews": {"sim": {"reviewer": "agent"}}}')
        assert run('--saved', str(saved), '--evidence', str(found)) == 2, (
            'a bad evidence file is a usage error'
        )


if __name__ == '__main__':
    the_diagnostics_still_find_every_fault()
    a_move_is_the_same_appointment()
    a_cancel_leaves_no_appointment()
    two_active_appointments_are_a_duplicate()
    a_repeated_receipt_is_one_appointment()
    missing_audit_fields_and_unmapped_writes_are_incomplete()
    native_confirm_customer_is_checked()
    a_lookup_receipt_backs_no_booking_claim()
    an_unheard_proposal_is_never_verified_safe()
    an_unknown_outcome_stays_unknown()
    a_long_span_is_not_a_ceiling()
    a_call_with_no_trace_says_so()
    the_real_session_4_calls_are_scored_on_their_evidence()
    the_command_fails_on_failures_and_on_missing_evidence()
    print('ok')
