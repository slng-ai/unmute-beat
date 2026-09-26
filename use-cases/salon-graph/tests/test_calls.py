"""Run: uv run python -m tests.test_calls

The live text checkpoint's own machinery, with no model and no network: every
agent and every reading is scripted. What is checked is the evaluator, not the
salon: that a wrong outcome is reported as failed, that a check which is
itself wrong cannot pass, that a spent allowance stops a case as exhausted,
that a reply the script does not expect stops it too, and that delivery is
recorded per segment rather than assumed.

Asserts, no pytest, no fixtures, same as the rest.
"""

import asyncio
from contextlib import ExitStack
from dataclasses import replace
from unittest.mock import patch

from opentelemetry.sdk.trace import TracerProvider
from pydantic_ai import Agent
from pydantic_ai.models.function import AgentInfo, FunctionModel

from evals.calls import CASES, CLOCK, PHONE, Record, Rule, lose_first_answer, run_case, spans, watch
from salon.app import FLOW
from salon.diary import Diary

AGENTS = FLOW.nodes


def saying(text: str):
    async def stream(messages, info: AgentInfo):
        yield text

    return stream


def every_agent_says(text: str) -> ExitStack:
    stack = ExitStack()
    for node in AGENTS.values():
        stack.enter_context(node.agent.override(model=FunctionModel(stream_function=saying(text))))
    return stack


def book_behind_the_calls_back(diary: Diary) -> None:
    """A booking the call never made: in the diary before the call starts, offered to nobody on it."""
    read = diary.find_slots(PHONE, 'friday', 'haircut', diary.now())
    diary.save_booking(PHONE, 'book', read['slots'][0]['slot_id'], '', False, diary.now())


HELLO = 'Hello, what can I do for you?'
CALLER = (Rule(('what can i do',), ('Goodbye.',), last=True),)


async def one(case, **limits) -> Record:
    """One scripted call, with tracing setup stubbed down to its instrumentation.

    The real setup exports to Langfuse whenever the shell has its keys, and a
    scripted call's spans do not belong there. Instrumenting the agents is
    what makes their requests into spans the evaluator counts.
    """
    with (
        patch('evals.calls.setup', lambda *a, **k: Agent.instrument_all()),
        every_agent_says(HELLO),
    ):
        return await run_case(case, spans(), **limits)


async def wrong_outcomes_are_reported():
    """A booking the call did not make fails T1's check; a check that is itself wrong fails too."""
    t1 = replace(CASES['T1'], rules=CALLER, prepare=book_behind_the_calls_back)
    record = await one(t1)
    assert record.result == 'failed', record.result
    assert any('never offered' in p for p in record.problems), record.problems
    assert any('never identified' in p for p in record.problems), record.problems

    wrong = replace(
        t1, check=lambda r: [] if r.bookings() and r.bookings()[0]['service'] == 'colour' else ['no colour']
    )
    assert (await one(wrong)).result == 'failed', 'a check expecting the wrong service cannot pass'

    right = replace(t1, check=lambda r: [] if len(r.bookings()) == 1 else ['no booking'])
    passed = await one(right)
    assert passed.result == 'passed', (passed.result, passed.problems)
    assert passed.turns[1]['caller'] == CASES['T1'].opening and passed.turns[2]['caller'] == 'Goodbye.'


async def limits_stop_the_case():
    """One caller turn, or no request allowance left: exhausted, never passed, and the check still reported."""
    endless = replace(CASES['T1'], rules=(Rule(('what can i do',), ('Hello?',)),))
    record = await one(endless, max_turns=1)
    assert record.result == 'exhausted', record.result
    assert '1 caller turns used' in record.problems[0], record.problems
    assert any(p.startswith('(check, not decisive)') for p in record.problems), record.problems

    spent = await one(endless, requests_left=0)
    assert spent.result == 'exhausted' and 'allowance reached before turn 1' in spent.problems[0], (
        spent.problems
    )
    assert spent.requests == 0, 'a request went out with no allowance left'

    one_left = await one(endless, requests_left=1)
    assert one_left.result == 'exhausted' and one_left.requests <= 1, (
        'a turn sent more requests than the allowance had left',
        one_left.requests,
        one_left.problems,
    )


async def an_unexpected_reply_is_off_script():
    record = await one(replace(CASES['T1'], rules=(Rule(('which time',), ('Nine.',)),)))
    assert record.result == 'off-script', record.result
    assert HELLO in record.problems[0], record.problems


async def delivery_is_acknowledged_per_segment():
    record = await one(replace(CASES['T1'], rules=CALLER, check=lambda r: []))
    greeting, answer = record.turns[0]['delivered'], record.turns[1]['delivered']
    assert greeting and all(s['after'] == 'played' for s in greeting), greeting
    assert answer and answer[-1]['text'] == HELLO and answer[-1]['after'] == 'played', answer
    assert answer[-1]['before'] != 'played', (
        'played because the evaluator said so, not because the turn returned'
    )
    assert record.turns[1]['requests'] >= 1, 'every model request is counted, a scripted one too'
    assert record.requests == sum(t.get('requests', 0) for t in record.turns), record.requests


def the_lost_answer_fault():
    """T4's fault: the first write lands and raises; the first question about it gets no answer, the second does."""
    diary = Diary(clock=CLOCK)
    lose_first_answer(diary)
    read = diary.find_slots(PHONE, 'friday', 'haircut', diary.now())
    try:
        diary.save_booking(PHONE, 'book', read['slots'][0]['slot_id'], '', False, diary.now(), 'token-1')
    except TimeoutError:
        pass
    else:
        raise AssertionError('the first write must lose its answer')
    assert len(diary.bookings_for(PHONE, CLOCK.date())) == 1, 'but it landed'
    assert diary.outcome_of('token-1') is None, 'unknown the first time it is asked'
    assert diary.outcome_of('token-1') is not None, 'known the second time'


def watching_changes_no_answer():
    """The first live run broke the diary it watched: recording an answer read a key it did not have.

    Every call the salon makes returns exactly what the unwatched diary
    returns, and the record says what was offered.
    """
    plain, watched = Diary(clock=CLOCK), Diary(clock=CLOCK)
    record = Record(case='x', session='x')
    watch(watched, record, 0.0)
    read = watched.find_slots(PHONE, 'friday', 'haircut', CLOCK, offered_to='c')
    assert read == plain.find_slots(PHONE, 'friday', 'haircut', CLOCK, offered_to='c')
    assert record.diary_calls[0]['offered'][0] == ('2026-10-02', read['slots'][0]['time']), record.diary_calls
    saved = watched.save_booking(PHONE, 'book', read['slots'][0]['slot_id'], '', False, CLOCK, 't', 'c')
    plain.save_booking(PHONE, 'book', read['slots'][0]['slot_id'], '', False, CLOCK, 't', 'c')
    assert saved == plain.outcome_of('t') and watched.outcome_of('t') == plain.outcome_of('t')
    assert watched.bookings_for(PHONE, CLOCK.date()) == plain.bookings_for(PHONE, CLOCK.date())
    names = [c['name'] for c in record.diary_calls]
    assert names[0] == 'find_slots' and {'save_booking', 'outcome_of', 'bookings_for'} <= set(names), names
    assert all('error' not in c and c['end'] >= c['start'] for c in record.diary_calls), record.diary_calls


def the_exporting_provider_is_kept():
    """Session 1's checkpoint sent nothing to Langfuse: the collector took the tracer provider before setup did.

    Setup stands in for Langfuse's here and installs the provider; the
    collector must look only after it, hang off it, and replace nothing. The
    process's real provider is never touched: an earlier module may own it.
    """
    exporting, order = TracerProvider(), []
    with (
        patch('evals.calls.setup', lambda *a, **k: order.append('setup')),
        patch('evals.calls.trace.get_tracer_provider', lambda: order.append('look') or exporting),
        patch('evals.calls.trace.set_tracer_provider', lambda p: order.append('replace')),
    ):
        collector = spans()
    assert order == ['setup', 'look'], ('setup first, and nothing replaced', order)
    with exporting.get_tracer('t').start_as_current_span('x'):
        pass
    assert [s.name for s in collector.take()] == ['x'], 'and it sees every span of the exporting provider'


if __name__ == '__main__':
    the_exporting_provider_is_kept()
    watching_changes_no_answer()
    asyncio.run(wrong_outcomes_are_reported())
    asyncio.run(limits_stop_the_case())
    asyncio.run(an_unexpected_reply_is_off_script())
    asyncio.run(delivery_is_acknowledged_per_segment())
    the_lost_answer_fault()
    print('ok')
