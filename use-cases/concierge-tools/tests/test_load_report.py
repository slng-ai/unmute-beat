"""Run: uv run python -m tests.test_load_report

`load_report.judge` on one synthetic call: each fault it must find, found
once, and a clean call reported clean. Asserts, no pytest, no network.
"""

import json
from datetime import UTC, datetime, timedelta

from coval_sim.models import Simulation
from ledger import Obs
from load_report import judge, render

T0 = datetime(2026, 9, 26, 12, tzinfo=UTC)


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


def write(turn: int, asked: int, about: str = 'book_appointment', booking: str = 'BK0001') -> Obs:
    audit = {'action': 'book_appointment', 'about': about, 'question': 'Shall I book you in?', 'asked_turn': asked,
             'turn': turn, 'heard': 'yes please', 'status': 'ok',
             'receipt': {'status': 'booked', 'booking_id': booking, 'spoken': 'Friday the 2nd at 9:00 AM'}}  # fmt: skip
    return Obs(f'w{turn}', 'e', 'SPAN', 'write', at(9), at(9.1), attrs={'write.audit': json.dumps(audit)})


def call(*extra: Obs, chats: list[Obs], segments: list[str], origins: list[str]) -> list[Obs]:
    engine = Obs('e', 'conv', 'AGENT', 'booking', at(1), at(8), attrs={
        'agent': 'booking', 'status': 'answered', 'speech_segments': str(segments), 'speech_origins': str(origins),
        'request_first_output': str(['tool', 'text'][: len(chats)]), 'request_ttft': str([0.4, 0.5][: len(chats)]),
    })  # fmt: skip
    return [
        Obs('conv', None, 'SPAN', 'conversation', at(0), at(60)),
        Obs(
            't1',
            'conv',
            'SPAN',
            'turn',
            at(0),
            at(8),
            attrs={'turn.user_bot_latency_seconds': 1.2, 'turn.was_interrupted': 'true'},
        ),
        engine,
        *chats,
        *extra,
    ]


SIM = Simulation(simulation_id='sim', status='COMPLETED', call_sid='CA1')


def a_clean_call_has_no_problem() -> None:
    obs = call(write(3, 2), chats=[chat('a', 1, 1.6, '', 'collect'), chat('b', 2, 2.9, 'Which time?')],
               segments=['x:2:1', 'x:2:2'], origins=['tool_filler', 'model'])  # fmt: skip
    report = judge('run', SIM, obs)
    assert report.problems == [], report.problems
    assert report.bookings == ['Friday the 2nd at 9:00 AM'] and report.first_booking == 'BK0001'
    assert (
        report.holding == {2: 1}
        and report.chains == {}
        and report.latency == [1.2]
        and report.interrupted == 1
    )
    assert [round(t, 2) for t in report.tails] == [0.4], 'the second reply spoke: 0.9 s, first token at 0.5 s'
    assert report.booked_after == 9.0, report.booked_after
    assert 'with no problem: 1' in render([report])


def every_fault_is_found() -> None:
    obs = call(
        write(3, 3, booking='BK0002'),
        write(4, 2),
        chats=[chat('a', 1, 1.6, '', 'collect'), chat('b', 2, 2.9, 'One moment. Shall I book you in?', 'book_appointment')],
        segments=['x:2:1', 'x:2:2', 'x:2:3'],
        origins=['tool_filler', 'tool_filler', 'confirmation'],
    )  # fmt: skip
    problems = judge('run', SIM, obs).problems
    for expected in ('with no matching question', '2 booking writes', 'not BK0001', 'spoke beside an operation',
                     'repeated holding lines'):  # fmt: skip
        assert sum(expected in p for p in problems) == 1, (expected, problems)
    report = judge('run', SIM, obs)
    assert report.chains == {'collect > book_appointment': 1} and report.filler_copies == 1, report


def a_call_with_no_trace_says_so() -> None:
    report = judge('run', Simulation(simulation_id='s', status='FAILED', error_message='no answer'), [])
    assert report.problems == ['call FAILED: no answer', 'no Langfuse session for this call'], report.problems


if __name__ == '__main__':
    a_clean_call_has_no_problem()
    every_fault_is_found()
    a_call_with_no_trace_says_so()
    print('ok')
