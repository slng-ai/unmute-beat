"""Run: uv run python -m tests.test_ledger

`ledger` on a synthetic call, one observation per rule it keeps: every
count below was once wrong by hand. Asserts, no pytest, no network.
"""

import json
from datetime import UTC, datetime, timedelta

from ledger import Obs, groups, quantile, read_call, summary

T0 = datetime(2026, 9, 24, 12, tzinfo=UTC)
CLASSIFY = [{'role': 'system', 'content': 'You classify the answer to a question. ...'}]


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def gen(
    id_,
    parent,
    start,
    end,
    usage,
    *,
    name='chat gemini-3.5-flash-lite',
    model='gemini-3.5-flash-lite',
    input=None,
):
    return Obs(id_, parent, 'GENERATION', name, at(start), at(end), model, input, None, usage)


def harness_call() -> list[Obs]:
    """Three caller turns, one of them with no model request at all."""
    return [
        Obs('conv', None, 'SPAN', 'conversation', at(0), at(30)),
        Obs('t1', 'conv', 'SPAN', 'turn', at(0), at(5), attrs={'turn.user_bot_latency_seconds': 2.4}),
        gen('c1', 't1', 1.0, 1.8, {'input': 400, 'output': 9}, input=CLASSIFY),
        gen('c2', 't1', 1.0, 1.7, {'input': 300, 'output': 6}, input=CLASSIFY),
        gen('s1', 't1', 1.8, 2.3, {'input': 1000, 'text_prompt_tokens': 1000, 'output': 30, 'total': 2030}),
        gen('x1', 't1', 0.5, 0.6, {}, input=CLASSIFY),
        Obs('t2', 'conv', 'SPAN', 'turn', at(10), at(12), attrs={'turn.user_bot_latency_seconds': '1.1'}),
        gen('s2', 't2', 10.2, 10.9, {'input': 200, 'input_cached_tokens': 800, 'output': 20}),
        Obs('stt3', 'conv', 'GENERATION', 'stt', at(19), at(20), 'soniox', 'audio', 'Yes.'),
        Obs('t3', 'conv', 'SPAN', 'turn', at(20), at(21)),
        Obs(
            'w1',
            't3',
            'SPAN',
            'write',
            at(20.1),
            at(20.2),
            attrs={
                'write.audit': json.dumps(
                    {
                        'action': 'save_booking',
                        'about': 'booking',
                        'question': 'Shall I book that in?',
                        'asked_turn': 2,
                        'turn': 3,
                        'heard': 'Yes.',
                        'status': 'ok',
                    }
                )
            },
        ),
        Obs('stt', 'conv', 'GENERATION', 'stt', at(0.2), at(0.9), 'soniox', 'audio', 'A haircut on Friday.'),
    ]


def livekit_call() -> list[Obs]:
    """LiveKit: the model request sits under `agent_turn`, its `llm_node` wrapper is not a second request."""
    return [
        Obs('turn', None, 'SPAN', 'turn', at(0), at(5)),
        Obs(
            'user',
            'turn',
            'GENERATION',
            'user_turn',
            at(0),
            at(2),
            'slng',
            'Nine, please.',
            attrs={'lk.end_of_turn_delay': '0.5'},
        ),
        Obs('agent', 'turn', 'AGENT', 'agent_turn', at(2), at(5)),
        gen('node', 'agent', 2.0, 2.9, {}, name='llm_node', model='gemini-3.1-flash-lite'),
        gen(
            'req',
            'agent',
            2.0,
            2.9,
            {'input': 3000, 'output': 40},
            name='llm_request',
            model='gemini-3.1-flash-lite',
        ),
        Obs('tts', 'agent', 'GENERATION', 'tts', at(2.9), at(3.2), 'cartesia', 'Shall I book it?'),
        Obs('speak', 'agent', 'SPAN', 'agent_speaking', at(3.1), at(4.5)),
        Obs('tool', 'agent', 'TOOL', 'save_booking', at(4.6), at(4.7), input={'slot_id': 'x'}),
    ]


def counts_every_request_once():
    call = read_call('harness', harness_call())
    assert call.caller_turns == 3
    assert dict(call.completed) == {'classifier': 2, 'step': 2}, call.completed
    assert call.cancelled == 1, 'a request with no usage is cancelled or unreported, not zero tokens'
    assert call.input_tokens == 400 + 300 + 1000 + 1000, 'text_prompt_tokens is the same input again'
    assert call.output_tokens == 65
    shapes = [(t.shape, t.in_a_row, t.latency) for t in call.turns]
    assert shapes == [('CCS', 2, 2.4), ('S', 1, 1.1), ('-', 0, None)], shapes


def reads_the_audit_of_a_write():
    call = read_call('harness', harness_call())
    (write,) = call.writes
    assert (write.name, write.status, write.heard_before) == ('save_booking', 'ok', 'Yes.'), write
    assert call.tasks == 1 and call.unmatched() == []
    lines = summary('harness', [call])
    assert any('1 of 1 writes answered a matching question' in line for line in lines), lines

    unasked = [o for o in harness_call() if o.id != 'w1']
    unasked.append(
        Obs(
            'w2',
            't3',
            'SPAN',
            'write',
            at(20.1),
            at(20.2),
            attrs={
                'write.audit': json.dumps(
                    {
                        'action': 'save_booking',
                        'about': 'booking',
                        'question': 'Shall I book that in?',
                        'asked_turn': 3,
                        'turn': 3,
                        'heard': 'Yes.',
                        'status': 'ok',
                    }
                )
            },
        )
    )
    assert len(read_call('harness', unasked).unmatched()) == 1, (
        'a question put this same turn answers nothing'
    )

    def operation(about: str) -> list[Obs]:
        """Session 4's audit: the operation is asked about under its own name."""
        audit: dict[str, object] = {'action': 'book_appointment', 'about': about, 'question': 'Shall I book?'}
        audit |= {'asked_turn': 8, 'turn': 9, 'heard': 'yes please', 'status': 'ok'}
        write = Obs('w3', 't3', 'SPAN', 'write', at(20.1), at(20.2), attrs={'write.audit': json.dumps(audit)})
        return [o for o in harness_call() if o.id != 'w1'] + [write]

    booked = read_call('harness', operation('book_appointment'))
    assert booked.tasks == 1 and booked.unmatched() == [], booked.writes
    assert len(read_call('harness', operation('lookup_customer')).unmatched()) == 1, (
        'a yes to the number read-back never books'
    )


def reads_livekit_the_same_way():
    call = read_call('livekit', livekit_call())
    assert call.caller_turns == 1 and dict(call.completed) == {'step': 1} and call.cancelled == 0
    assert call.input_tokens == 3000, 'llm_node wraps the request; it is not a second one'
    (turn,) = call.turns
    assert turn.estimated and abs((turn.latency or 0) - (1.1 + 0.5)) < 1e-9, turn
    (write,) = call.writes
    assert write.status == 'unknown' and write.said_before == 'Shall I book it?' and call.tasks == 1


def splits_latency_into_turn_taking_and_answer():
    """The same split for both builds: from the work that answered to the first speech.

    The harness's first piece started a run the caller then talked over; the
    answer is timed from the piece that answered, not from that one.
    """
    harness = [
        Obs('t', None, 'SPAN', 'turn', at(0), at(9), attrs={'turn.user_bot_latency_seconds': 4.4}),
        Obs('p', 't', 'AGENT', 'booking', at(1.0), at(1.4), attrs={'turn_pieces': '1'}),
        gen('x', 'p', 1.01, 1.4, {}),
        Obs('a', 't', 'AGENT', 'booking', at(3.2), at(4.5), attrs={'turn_pieces': '2'}),
        gen('c', 'a', 3.21, 4.1, {'input': 380, 'output': 11}, input=CLASSIFY),
        Obs('s', 't', 'GENERATION', 'tts', at(4.2), at(4.5), 'sonic', 'Shall I book it?'),
    ]
    graph = [
        Obs('t', None, 'SPAN', 'turn', at(0), at(9), attrs={'turn.user_bot_latency_seconds': 1.3}),
        gen('g', 't', 0.1, 0.9, {'input': 4000, 'output': 30}, name='llm', model='gemini-3.1-flash-lite'),
        Obs('s', 't', 'GENERATION', 'tts', at(0.8), at(1.2), 'sonic', 'Let me have a look.'),
    ]
    (slow,) = read_call('harness', harness).turns
    (fast,) = read_call('graph', graph).turns
    assert slow.answer is not None and abs(slow.answer - 1.0) < 1e-6, slow
    assert fast.answer is not None and abs(fast.answer - 0.7) < 1e-6, fast
    lines = summary('both', [read_call('harness', harness), read_call('graph', graph)])
    assert any('answer time (answering work to first speech): p50' in line for line in lines), lines
    assert any('turn taking): 1 of 2 turns' in line for line in lines), lines


def reports_what_it_cannot_measure():
    lines = summary('none', [read_call('empty', [Obs('t', None, 'SPAN', 'turn', at(0), at(1))])])
    assert any('undefined, no task completed' in line for line in lines), lines
    assert any('latency: missing' in line for line in lines), lines
    assert quantile([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0], 0.9) == 9.0
    assert groups(['harness', 'CA1', 'CA2', '--', 'graph', 'trace:x']) == [
        ('harness', ['CA1', 'CA2']),
        ('graph', ['trace:x']),
    ]
    for bad in ([], ['harness'], ['harness', 'CA1', '--']):
        try:
            groups(bad)
        except SystemExit:
            continue
        raise AssertionError(f'{bad} was accepted')


def main():
    counts_every_request_once()
    reads_the_audit_of_a_write()
    reads_livekit_the_same_way()
    splits_latency_into_turn_taking_and_answer()
    reports_what_it_cannot_measure()
    print('ledger ok')


if __name__ == '__main__':
    main()
