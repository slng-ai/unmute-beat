"""Run: uv run python -m tests.test_load_report

`load_report.judge` on synthetic calls and on nine real session-4 calls
(`fixtures/session4_calls.json`: their conversation, turn and write spans
only; `fixtures/session4_evidence.json`: an agent's reading of them). Each
rule below once gave a false pass or a false failure: the independent review
of PR #20 (`docs/research/reusable-core/session-4b-evaluator-review.md`)
found the ones marked "review". Then the command itself, run as a process,
for its exit status. Asserts, no pytest, no network.
"""

import json
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from coval_sim.models import Simulation
from ledger import Obs, from_json, read_call, to_json
from load_report import (
    EXIT_FAILED,
    EXIT_INCOMPLETE,
    CallReport,
    Evidence,
    Facts,
    effects,
    judge,
    render,
)

HERE = Path(__file__).parent
T0 = datetime(2026, 9, 26, 12, tzinfo=UTC)
PHONE = '+34671273454'
OTHER = '+10000000000'
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
    """One audited write, landed, answering a question put on turn `asked`: what session 4 recorded."""
    body = {'action': action, 'about': action, 'question': 'Shall I book you in?', 'asked_turn': asked,
            'turn': turn, 'heard': 'yes please', 'status': 'ok', 'args': {'phone': PHONE},
            'receipt': {'status': 'booked', 'booking_id': 'BK0001', 'spoken': 'Friday the 2nd at 9:00 AM', **FRIDAY}
            if receipt is None else receipt} | audit  # fmt: skip
    return Obs(f'w{turn}', 'e', 'SPAN', 'write', at(9 + turn / 10), at(9.1 + turn / 10),
               attrs={'write.audit': json.dumps(body)})  # fmt: skip


def marks(turn: int, *kinds: str, proposal: int | None = None, late: bool = False) -> dict:
    """A `Playback` record: one mark per kind for segment `s<turn>`, before the answer unless `late`."""
    when = at(30 if late else 5).isoformat()
    return {
        'marks': [
            {'proposal': proposal or turn, 'segment': f's{turn}', 'kind': k, 'at': when} for k in kinds
        ],
        'answered_at': at(20).isoformat(),
    }


def supported(turn: int, asked: int, action: str = 'book_appointment', receipt: dict | None = None, **audit) -> Obs:  # fmt: skip
    """A write with everything Stage B is to record: its read-back's arguments and the transport's marks."""
    args = audit.pop('args', {'phone': PHONE})
    return write(turn, asked, action, receipt, args=args, proposal_args=args, proposal=turn,
                 segments=[f's{turn}'], playback=marks(turn, 'played'), **audit)  # fmt: skip


def lookup(turn: int = 2, **audit) -> Obs:
    receipt = {'customer_status': 'created', 'customer_phone': PHONE}
    return write(turn, turn - 1, 'lookup_customer', receipt, args={'phone': PHONE}, **audit)


def lookup_supported(turn: int = 2) -> Obs:
    receipt = {'customer_status': 'created', 'customer_phone': PHONE}
    return supported(turn, turn - 1, 'lookup_customer', receipt)


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
ENDED = {'media_seconds': 60, 'ended_by': 'caller', 'observed': True, 'source': 'synthetic bot log'}


def evidence(**review: Any) -> Evidence:
    """A case asking for one haircut, an agent's review when given, and `facts` (else an observed normal ending).

    Built from JSON, as the command reads it: the models are strict, so a
    date is an ISO string in JSON and never one in Python.
    """
    facts = review.pop('facts', ENDED)
    reviews = {'sim': {'reviewer': 'agent', 'source': 'synthetic', **review}} if review else {}
    return Evidence.model_validate_json(json.dumps({
        'cases': {CASE: {'bookings': 1, 'service': 'haircut'}}, 'reviews': reviews, 'facts': {'sim': facts},
    }))  # fmt: skip


def heard_and_accepted(*keys: str) -> dict:
    return {key: {'accepted': True, 'delivered': True} for key in keys}


def accepted(*keys: str) -> dict:
    return {key: {'accepted': True} for key in keys}


BOOKED_FRIDAY = [{'action': 'booked', 'appointment': FRIDAY}]
COMPLETE = {'final': FRIDAY | {'phone': PHONE}, 'claims': BOOKED_FRIDAY}


def complete_call() -> list[Obs]:
    """The control: a lookup and a Friday booking, each with its read-back's arguments and its marks."""
    return call(lookup_supported(), supported(3, 2, args={'phone': PHONE, 'slot': '2026-10-02T0900|haircut'}))


def complete_evidence(**review) -> Evidence:
    return evidence(**(COMPLETE | {'writes': accepted('lookup_customer@2', 'book_appointment@3')} | review))


def the_control_passes() -> None:
    report = judge('run', SIM, complete_call(), evidence=complete_evidence())
    assert report.verdict == 'pass' and report.missing == [], (report.missing, report)


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


def writes_of(obs: list[Obs]):
    return read_call('x', obs).writes


def a_repeated_receipt_is_one_appointment() -> None:
    """The diary answers a retried token with the first receipt: the same write seen twice books nothing more."""
    done = effects(writes_of(call(lookup(), write(3, 2), write(4, 2))))
    assert done.repeats == 1 and list(done.active) == ['BK0001'] and done.applied == ['book BK0001'], done
    different = {'status': 'booked', 'booking_id': 'BK0001', **SATURDAY}
    clash = effects(writes_of(call(write(3, 2), write(4, 2, receipt=different))))
    assert clash.unknown == ['book_appointment@4: BK0001 booked twice with different details'], clash


def effects_are_remembered_for_the_whole_call() -> None:
    """Review: a cached booking receipt after its cancel made the appointment active again, and exit 0."""
    gone = {'status': 'cancelled', 'booking_id': 'BK0001', **FRIDAY}
    cancel = write(4, 3, 'cancel_appointment', gone, args={'booking_id': 'BK0001'})
    replayed = effects(writes_of(call(lookup(), write(3, 2), cancel, write(5, 2))))
    assert replayed.active == {} and replayed.applied == ['book BK0001', 'cancel BK0001'], replayed
    assert replayed.unknown == [
        'book_appointment@5: BK0001 booked again after its cancel: a replay or a new booking, the audit cannot say'
    ], replayed.unknown
    same = effects(writes_of(call(write(3, 2, operation='op-1'), cancel, write(5, 2, operation='op-1'))))
    assert same.unknown == [] and same.repeats == 1 and same.active == {}, 'the same operation is its replay'
    other = effects(writes_of(call(write(3, 2, operation='op-1'), cancel, write(5, 2, operation='op-2'))))
    assert other.unknown and other.active == {}, 'another operation with matching arguments is not merged'
    twice = effects(writes_of(call(write(3, 2), cancel, write(5, 4, 'cancel_appointment', gone, args={'booking_id': 'BK0001'}))))  # fmt: skip
    assert twice.repeats == 1 and twice.unknown == [] and twice.applied == ['book BK0001', 'cancel BK0001'], (
        twice
    )
    moved = {'status': 'moved', 'booking_id': 'BK0001', **SATURDAY}
    back = effects(writes_of(call(write(3, 2), write(4, 3, receipt=moved, args={'booking_id': 'BK0001'}), write(5, 2))))  # fmt: skip
    assert back.active == {'BK0001': SATURDAY | {'phone': PHONE}} and back.unknown, (
        'the first receipt again after a move changes nothing it can prove'
    )


def missing_audit_fields_and_unmapped_writes_are_incomplete() -> None:
    bare = json.loads(write(3, 2).attrs['write.audit'])
    del bare['question']
    thin = call(
        lookup(), Obs('w3', 'e', 'SPAN', 'write', at(9.5), at(9.6), attrs={'write.audit': json.dumps(bare)})
    )
    report = judge('run', SIM, thin, evidence=evidence(writes=heard_and_accepted('lookup_customer@2')))
    assert report.authorization.verdict == 'unknown', report.authorization
    assert 'book_appointment@3: its audit lacks question' in report.authorization.unknown, (
        report.authorization
    )
    unmapped = judge('run', SIM, call(write(3, 2, 'save_name', {'status': 'saved'})), evidence=evidence())
    assert unmapped.authorization.unknown == [
        'save_name@3: whether it needed a yes (no mapping, no needs_approval in its audit)'
    ], unmapped.authorization
    assert unmapped.delivery.unknown == [
        'save_name@3: whether a proposal had to play (whether it needed a yes is unknown)'
    ], 'review: an unknown need for a yes is not "no proposal needed to play"'
    assert any(m.startswith('authorization:') for m in unmapped.missing), unmapped.missing
    no_receipt = judge('run', SIM, call(write(3, 2, receipt={})), evidence=evidence())
    assert no_receipt.outcome.unknown == ['book_appointment@3: landed, but no receipt says what it did']


def an_unaudited_write_is_never_dropped() -> None:
    """Review: a separate unaudited cancel vanished from a passing call once any write had an audit."""
    hidden = Obs('hidden', 'e', 'TOOL', 'cancel_appointment', at(11), at(12), output='cancelled BK0001')
    report = judge('run', SIM, [*complete_call(), hidden], evidence=complete_evidence())
    assert report.verdict == 'unknown', report
    assert 'cancel_appointment: a tool call with no write audit (ok): what it did is unknown' in (
        report.outcome.unknown
    ), report.outcome
    assert report.authorization.verdict == 'unknown' and report.delivery.verdict == 'unknown', report
    tool = Obs('tb', 'e', 'TOOL', 'book_appointment', at(9), at(9.5), output='booked')
    booked = supported(3, 2, args={'phone': PHONE, 'slot': '2026-10-02T0900|haircut'})
    own = Obs(booked.id, 'tb', booked.type, booked.name, booked.start, booked.end, attrs=booked.attrs)
    pair = judge('run', SIM, call(lookup_supported(), tool, own), evidence=complete_evidence())
    assert pair.verdict == 'pass' and len(pair.writes) == 2, 'a tool call and its own audit are one write'


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


def the_caller_must_be_known() -> None:
    """Review: with no number reviewed and none recorded, a matching service, day and time passed."""
    thin = call(supported(3, 2, args={'slot': '2026-10-02T0900|haircut'}, receipt={'status': 'booked', 'booking_id': 'BK0001', 'service': 'haircut'}))  # fmt: skip
    review = {'final': FRIDAY, 'claims': BOOKED_FRIDAY, 'writes': accepted('book_appointment@3')}
    report = judge('run', SIM, thin, evidence=evidence(**review))
    assert report.outcome.unknown == [
        "whose appointment it should be: the caller's number was not reviewed"
    ], report.outcome
    known = judge('run', SIM, thin, evidence=evidence(**review | {'final': FRIDAY | {'phone': PHONE}}))
    assert known.outcome.unknown == [
        'whose appointment it is: no number in its receipt, its write or the call'
    ]
    other = judge('run', SIM, complete_call(), evidence=complete_evidence(final=FRIDAY | {'phone': OTHER}))
    assert other.outcome.failed == ["booked for another number than the caller's"], other.outcome
    complaint = write(3, 2, 'record_complaint', {'status': 'recorded'}, about='complaint', args={})
    filed = judge('run', SIM, call(complaint), evidence=evidence(no_request=True, claims=[]))
    assert filed.outcome.verdict == 'pass', 'a complaint needs no appointment and no number'


def a_receipt_must_match_its_write() -> None:
    """Review: only the booking ID was compared, so a receipt for another caller or day passed."""
    for receipt, args, why in (
        (
            {'phone': PHONE},
            {'phone': OTHER, 'slot': '2026-10-02T0900|haircut'},
            f'number {PHONE}, the write asked for {OTHER}',
        ),
        (
            {'phone': PHONE},
            {'phone': PHONE, 'slot': '2026-10-03T1000|haircut'},
            'day 2026-10-02, the write asked for 2026-10-03',
        ),
        (
            {},
            {'phone': PHONE, 'slot': '2026-10-02T0900|colour'},
            'service haircut, the write asked for colour',
        ),
        ({}, {'phone': PHONE, 'booking_id': 'BK0007'}, 'booking BK0001, the write asked for BK0007'),
    ):
        mine = supported(
            3, 2, args=args, receipt={'status': 'booked', 'booking_id': 'BK0001', **FRIDAY} | receipt
        )
        report = judge('run', SIM, call(lookup_supported(), mine), evidence=complete_evidence())
        assert report.outcome.verdict == 'fail' and any(why in f for f in report.outcome.failed), (
            why,
            report.outcome,
        )
    swapped = write(3, 2, args={'phone': PHONE, 'slot': '2026-10-02T0900|haircut'},
                    proposal_args={'phone': PHONE, 'slot': '2026-10-03T0900|haircut'})  # fmt: skip
    ran = judge('run', SIM, call(lookup_supported(), swapped), evidence=complete_evidence())
    assert ran.authorization.failed == [
        'book_appointment@3: it ran other arguments than its read-back named (slot)'
    ], ran.authorization
    legacy = judge('run', SIM, call(lookup(), write(3, 2)), evidence=complete_evidence())
    assert 'book_appointment@3: which arguments its read-back named (no proposal_args in its audit)' in (
        legacy.authorization.unknown
    ), 'session 4 recorded no read-back arguments: its binding is unknown, never assumed'


def a_lookup_receipt_backs_no_booking_claim() -> None:
    """Gate 1's cYMMixAc: a goodbye said "your haircut is booked" with only a lookup receipt on the call."""
    report = judge('run', SIM, call(lookup()), evidence=evidence(claims=['booked']))
    assert report.completion.failed == ['said booked; the receipts show no change'], report.completion
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


def a_claim_names_its_appointment() -> None:
    """Review: `claims=['booked']` passed a closing that said Friday over a Saturday booking."""
    booked = call(lookup(), write(3, 2))
    label = judge('run', SIM, booked, evidence=evidence(claims=['booked']))
    assert label.completion.unknown == ['said booked: which appointment, the label does not say'], (
        label.completion
    )
    right = judge('run', SIM, booked, evidence=evidence(claims=BOOKED_FRIDAY))
    assert str(right.completion) == 'pass (review:agent)', right.completion
    wrong_day = judge(
        'run', SIM, booked, evidence=evidence(claims=[{'action': 'booked', 'appointment': SATURDAY}])
    )
    assert wrong_day.completion.failed == [
        'said booked haircut 2026-10-03 09:00:00; active: BK0001 haircut 2026-10-02 09:00:00'
    ], wrong_day.completion
    wrong_time = [{'action': 'booked', 'appointment': FRIDAY | {'time': '10:00:00'}}]
    assert judge('run', SIM, booked, evidence=evidence(claims=wrong_time)).completion.verdict == 'fail'
    wrong_id = [{'action': 'booked', 'booking_id': 'BK0002', 'appointment': FRIDAY}]
    assert judge('run', SIM, booked, evidence=evidence(claims=wrong_id)).completion.verdict == 'fail'
    no_day = judge('run', SIM, booked, evidence=evidence(claims=[{'action': 'booked'}]))
    assert no_day.completion.verdict == 'unknown', 'a claim naming no day cannot be backed'
    moved = {'status': 'moved', 'booking_id': 'BK0001', **SATURDAY}
    then = call(lookup(), write(3, 2), write(5, 4, receipt=moved, args={'booking_id': 'BK0001'}))
    unnamed = {'date': SATURDAY['date'], 'time': SATURDAY['time']}
    net = judge('run', SIM, then, evidence=evidence(claims=[{'action': 'moved', 'appointment': unnamed}]))
    assert net.completion.verdict == 'pass', 'a claim may name no service; what it names must match'
    neutral = judge('run', SIM, call(lookup()), evidence=evidence(claims=[]))
    assert str(neutral.completion) == 'pass (review:agent)', 'a neutral goodbye claims nothing'


def delivery_needs_marks_for_its_own_proposal() -> None:
    """Review: a bare `delivery: acknowledged` passed, even over a reviewed "not delivered"."""
    obs = call(lookup(heard="that's two seven three four five four"))
    report = judge('run', SIM, obs, evidence=evidence())
    assert report.unmatched == [] and report.delivery.verdict == 'unknown', 'a matched label is not delivery'
    assert report.authorization.verdict == 'unknown', 'nor acceptance'
    reviewed = judge('run', SIM, obs, evidence=evidence(writes={'lookup_customer@2': {'accepted': False, 'delivered': False}}))  # fmt: skip
    assert reviewed.delivery.failed == [
        'lookup_customer@2: the proposal had not played in full when it was answered'
    ]
    assert reviewed.authorization.verdict == 'fail'
    flag = judge('run', SIM, call(lookup(delivery='acknowledged')), evidence=evidence())
    assert flag.delivery.unknown == [
        'lookup_customer@2: a bare delivery flag, with no playback marks for its proposal'
    ], flag.delivery

    def delivered(**audit) -> str:
        found = judge('run', SIM, call(lookup(proposal=2, segments=['s2'], **audit)), evidence=evidence())
        return str(found.delivery) + ''.join(
            f' | {w}' for w in found.delivery.failed + found.delivery.unknown
        )

    assert delivered(playback=marks(2, 'played')) == 'pass (deterministic)'
    assert delivered(playback=marks(2, 'played', 'played')) == 'pass (deterministic)', (
        'a duplicate is harmless'
    )
    assert 'cleared before the answer' in delivered(playback=marks(2, 'played', 'cleared')), (
        'a clear is no play'
    )
    assert 'played only after the answer began' in delivered(playback=marks(2, 'played', late=True))
    assert delivered(playback=marks(2, 'played', proposal=1)).startswith('unknown | lookup_customer@2: no played mark'), (
        'a mark for an earlier proposal is stale'
    )  # fmt: skip
    assert delivered(playback={'marks': 'yes'}).startswith('unknown | lookup_customer@2: its playback record is malformed')  # fmt: skip
    against = judge('run', SIM, call(lookup(proposal=2, segments=['s2'], playback=marks(2, 'played'))),
                    evidence=evidence(writes={'lookup_customer@2': {'delivered': False}}))  # fmt: skip
    assert against.delivery.failed == [
        'lookup_customer@2: the marks say it played, a review says not: contradictory'
    ], against.delivery


def an_unknown_outcome_stays_unknown() -> None:
    unsure = call(lookup(), write(3, 2, status='unknown', receipt={}))
    report = judge(
        'run', SIM, unsure, evidence=evidence(claims=BOOKED_FRIDAY, final=FRIDAY | {'phone': PHONE})
    )
    assert report.outcome.unknown == ['book_appointment@3: outcome unknown'] and not report.outcome.failed
    assert report.completion.verdict == 'unknown', (
        'a claim about an unknown outcome is neither true nor false'
    )
    assert report.verdict == 'unknown'


def a_reconciliation_settles_only_its_own_intent() -> None:
    """Stage B: an unknown write, then the runtime's reconciliation of the same intent, is one booking.

    The reconciliation is a write audit marked `reconciled` under the same
    `operation`. For another intent, or with no earlier write, the flag
    excuses nothing: the unknown stays unknown, and the audit is judged like
    any other write: never excused, so never a pass on the flag.
    """
    slot = {'phone': PHONE, 'slot': '2026-10-02T0900|haircut'}
    first = supported(3, 2, args=slot, status='unknown', receipt={}, operation='I1')
    settled = write(4, 2, args=slot, operation='I1', reconciled=True, needs_approval=True)
    report = judge('run', SIM, call(lookup_supported(), first, settled), evidence=complete_evidence())
    assert report.verdict == 'pass', (report.missing, report.authorization, report.outcome)
    other = write(
        4, 2, args=slot, operation='I2', reconciled=True, needs_approval=True, proposal=4, segments=['s4']
    )
    report = judge('run', SIM, call(lookup_supported(), first, other), evidence=complete_evidence())
    assert 'book_appointment@3: outcome unknown' in report.outcome.unknown, report.outcome
    judged = ' '.join(report.authorization.failed + report.authorization.unknown)
    assert 'book_appointment@4' in judged and report.verdict != 'pass', (
        'a reconciled flag excused an unrelated write',
        report.authorization,
    )
    alone = write(3, 2, args=slot, operation='I3', reconciled=True, needs_approval=True)
    report = judge('run', SIM, call(lookup_supported(), alone), evidence=complete_evidence())
    judged = ' '.join(report.authorization.failed + report.authorization.unknown)
    assert 'book_appointment@3' in judged and report.verdict != 'pass', (
        'a reconciliation with nothing to reconcile passed',
        report.authorization,
    )


def a_long_span_is_not_a_ceiling() -> None:
    """Native iPFgi5qq: a 1,105 s span was reported as "ended by the 300 s limit"; its recording is 50 s."""
    report = judge('run', SIM, call(span=1105), limit=300)
    assert report.span_seconds == 1105 and not report.operational.failed, report.operational
    assert report.operational.unknown == [
        'how the call ended: not measured (the trace span, 1105 s, is not its length)'
    ], report.operational
    logged = Evidence(
        facts={'sim': Facts(media_seconds=49.7, ended_by='ceiling', observed=True, source='bot log')}
    )
    assert judge('run', SIM, call(span=1105), 300, logged).operational.unknown == [
        'a ceiling was logged, but the recording is 50 s of 300'
    ]
    stalled = Evidence(facts={'sim': Facts(media_seconds=49.7, ended_by='provider_stall', observed=True, source='bot log')})  # fmt: skip
    assert judge('run', SIM, call(span=1105), 300, stalled).operational.failed == ['ended by provider stall']


def an_ending_is_observed_or_unknown() -> None:
    """Review: a source-free `ended_by: agent` passed, and 118 were read from a failed hang-up request."""
    try:
        evidence(facts={'ended_by': 'agent', 'observed': True})
    except ValidationError:
        pass
    else:
        raise AssertionError('a fact with no source is refused')
    inferred = judge(
        'run', SIM, call(), evidence=evidence(facts=ENDED | {'ended_by': 'agent', 'observed': False})
    )
    assert inferred.operational.unknown == ['ended by agent: inferred, not an observed event'], (
        inferred.operational
    )
    stall = judge('run', SIM, call(), evidence=evidence(facts=ENDED | {'ended_by': 'provider_stall', 'observed': False}))  # fmt: skip
    assert stall.operational.verdict == 'unknown', 'an inferred bad ending is not a measured failure either'
    seen = judge('run', SIM, call(), evidence=evidence(facts=ENDED | {'ended_by': 'agent'}))
    assert seen.operational.verdict == 'pass', seen.operational


def evidence_is_never_coerced() -> None:
    """Review: `"yes"`, `1` and `"1"` became `True`, `True` and `1`: consent, delivery and a count from strings."""
    good = json.loads(complete_evidence().model_dump_json())
    assert Evidence.model_validate_json(json.dumps(good)).reviews['sim'].final is not None, (
        'ISO dates still read'
    )
    review = good['reviews']['sim']
    for bad in (
        {'reviews': {'sim': review | {'writes': {'lookup_customer@2': {'accepted': 'yes'}}}}},
        {'reviews': {'sim': review | {'writes': {'lookup_customer@2': {'delivered': 1}}}}},
        {'cases': {CASE: {'bookings': '1'}}},
        {'cases': {CASE: {'bookings': 1.0}}},
        {'facts': {'sim': ENDED | {'observed': 'true'}}},
    ):
        try:
            Evidence.model_validate_json(json.dumps(good | bad))
        except ValidationError:
            continue
        raise AssertionError(f'coerced: {bad}')


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
        assert str(moved.completion) == 'pass (review:agent)', (native, moved.completion)
    assert "save_booking@15: 'silence underscore tool' did not accept the proposal" in (
        r['3CyVWZmp'].authorization.failed
    ), r['3CyVWZmp'].authorization
    assert r['kwUbc4PR'].authorization.failed == ['confirm_customer@6: "that\'s" did not accept the proposal']
    assert r['kwUbc4PR'].outcome.verdict == 'pass', 'the right final booking, on an invalid identity write'

    stalled = r['C26pPHK6']
    assert stalled.outcome.failed == ['0 active appointment(s), 1 requested'], 'safe, but not a success'
    assert stalled.completion.verdict == 'pass' and stalled.authorization.verdict == 'unknown', stalled

    long = r['iPFgi5qq']
    assert long.span_seconds is not None and long.span_seconds > 1100 and long.media_seconds == 49.742
    assert long.operational.failed == ['ended by provider stall'], long.operational

    for control in ('SB4GGMzU', 'VuhAwZ3e'):
        good = r[control]
        assert good.outcome.verdict == 'pass' and str(good.completion) == 'pass (review:agent)', (
            control,
            good,
        )
        assert good.authorization.verdict == 'unknown' and good.delivery.verdict == 'unknown', (
            'session 4 recorded neither read-back arguments nor marks: not a pass'
        )
        assert good.verdict == 'unknown', control
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

        def save(obs: list[Obs]) -> None:
            saved.write_text(json.dumps([
                {'run': 'run', 'simulation': SIM.model_dump(mode='json'), 'observations': [to_json(o) for o in obs]}
            ]))  # fmt: skip

        save(complete_call())
        good = complete_evidence()
        found.write_text(good.model_dump_json())
        assert run('--saved', str(saved), '--evidence', str(found)) == 0, 'a complete, passing call exits 0'
        assert run('--saved', str(saved)) == EXIT_INCOMPLETE, 'the same call with no evidence is incomplete'
        found.write_text(complete_evidence(claims=['cancelled']).model_dump_json())
        assert run('--saved', str(saved), '--evidence', str(found)) == EXIT_FAILED, (
            'a failed criterion exits 1'
        )
        text = good.model_dump_json()
        assert '"accepted":true' in text and '"bookings":1' in text, 'the probes below must change something'
        for bad in (
            '{"reviews": {"sim": {"reviewer": "agent"}}}',
            text.replace('"accepted":true', '"accepted":"yes"'),
            text.replace('"bookings":1', '"bookings":"1"'),
        ):
            found.write_text(bad)
            assert run('--saved', str(saved), '--evidence', str(found)) == 2, f'a bad evidence file is a usage error: {bad[:60]}'  # fmt: skip
        found.write_text(good.model_dump_json())
        mismatch = supported(3, 2, args={'phone': OTHER, 'slot': '2026-10-03T1000|haircut'},
                             receipt={'status': 'booked', 'booking_id': 'BK0001', 'phone': PHONE, **FRIDAY})  # fmt: skip
        save(call(lookup_supported(), mismatch))
        assert run('--saved', str(saved), '--evidence', str(found)) == EXIT_FAILED, (
            'review: a receipt for another caller and day exited 0'
        )
        save(
            [
                *complete_call(),
                Obs('hidden', 'e', 'TOOL', 'cancel_appointment', at(11), at(12), output='cancelled'),
            ]
        )
        assert run('--saved', str(saved), '--evidence', str(found)) == EXIT_INCOMPLETE, (
            'review: an unaudited cancel beside audited writes exited 0'
        )


if __name__ == '__main__':
    the_control_passes()
    the_diagnostics_still_find_every_fault()
    a_move_is_the_same_appointment()
    a_cancel_leaves_no_appointment()
    two_active_appointments_are_a_duplicate()
    a_repeated_receipt_is_one_appointment()
    effects_are_remembered_for_the_whole_call()
    missing_audit_fields_and_unmapped_writes_are_incomplete()
    an_unaudited_write_is_never_dropped()
    native_confirm_customer_is_checked()
    the_caller_must_be_known()
    a_receipt_must_match_its_write()
    a_lookup_receipt_backs_no_booking_claim()
    a_claim_names_its_appointment()
    delivery_needs_marks_for_its_own_proposal()
    an_unknown_outcome_stays_unknown()
    a_reconciliation_settles_only_its_own_intent()
    a_long_span_is_not_a_ceiling()
    an_ending_is_observed_or_unknown()
    evidence_is_never_coerced()
    a_call_with_no_trace_says_so()
    the_real_session_4_calls_are_scored_on_their_evidence()
    the_command_fails_on_failures_and_on_missing_evidence()
    print('ok')
