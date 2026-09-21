"""Run: uv run --env-file .env python -m tests.test_bench

The measurement, measured. Every number below is arithmetic over
`tests/fixtures/trace_s15.json`, which is written by hand so the expected
values can be checked against the file rather than against whatever the code
happens to produce.

This is the module that had no test at all, and it was wrong in five ways at
once: it merged every turn into one whenever the call root was present,
counted STT and TTS as model requests, dropped zero-model turns out of the
denominator, called two legitimate uses of a tool a repair, and reported a
nearest-rank quantile that is not nearest rank. A benchmark nobody checks is
a benchmark that decides an architecture on arithmetic.

Asserts, no pytest, no fixtures, same as the rest.
"""

import asyncio
import json
import pathlib

from bench.turns import analyse, percentile, turn_of

FIXTURE = pathlib.Path(__file__).parent / 'fixtures' / 'trace_s15.json'
SPANS = json.loads(FIXTURE.read_text())['data']
BY_ID = {str(s['id']): s for s in SPANS if s.get('id')}


# --- turn identity -----------------------------------------------------------


def a_request_belongs_to_its_nearest_turn():
    """Not the outermost ancestor. Both turns are under one call root.

    Walking to the top, `req-1a` and `req-2a` both arrive at `root-call` and
    the two turns become one -- which is what happened whenever tracing was
    on and the call had ended, so the bug was invisible in exactly the runs
    that mattered.
    """
    assert turn_of(BY_ID['req-1a'], BY_ID) == 'turn-1'
    assert turn_of(BY_ID['req-2a'], BY_ID) == 'turn-2'
    assert turn_of(BY_ID['tool-1a'], BY_ID) == 'turn-1'
    assert turn_of(BY_ID['tool-2a'], BY_ID) == 'turn-2'


def a_missing_parent_stays_unresolved():
    """`req-orphan` names a parent this query did not return.

    None, and not the dangling id, and not the nearest thing to hand. An
    unknown denominator reported as a confident one is worse than an unknown
    one reported as unknown.
    """
    assert turn_of(BY_ID['req-orphan'], BY_ID) is None


def the_call_root_is_not_a_turn():
    assert turn_of(BY_ID['root-call'], BY_ID) is None
    assert turn_of(BY_ID['greeting-1'], BY_ID) is None


def a_turn_is_its_own_turn():
    assert turn_of(BY_ID['turn-1'], BY_ID) == 'turn-1'


# --- what gets counted -------------------------------------------------------


def the_counts_are_exact():
    found = analyse(SPANS)
    assert found.turns == ['turn-1', 'turn-2', 'turn-3'], found.turns
    assert found.requests_per_turn == {'turn-1': 2, 'turn-2': 1, 'turn-3': 0}, found.requests_per_turn
    assert found.counts == [2, 1, 0], found.counts
    assert found.zero_model_turns == 1
    assert found.greeting_spans == 1
    assert found.speech_spans == 4, 'two stt, two tts'
    assert found.unresolved_requests == 1
    assert found.unresolved_spans == 1


def speech_is_not_a_model_request():
    """STT and TTS arrive typed `GENERATION`, because that is Langfuse's word.

    `tests/test_traces.py` asserts that mapping from the other side. Counting
    them inflated every request-per-turn figure by however much was said.
    """
    found = analyse(SPANS)
    assert sum(found.requests_per_turn.values()) == 3, found.requests_per_turn
    # Four speech observations exist and none of them landed in a turn's count.
    assert found.speech_spans == 4


def a_zero_model_turn_is_still_a_caller_turn():
    """It is the best turn the agency can have, and dropping it flatters the mean.

    Three turns and three requests is a mean of 1.0. Drop the empty turn and
    the same call reports 1.5 -- a 50% worse number for a strictly better
    call.
    """
    found = analyse(SPANS)
    assert len(found.turns) == 3
    assert sum(found.counts) / len(found.counts) == 1.0
    kept = [c for c in found.counts if c]
    assert sum(kept) / len(kept) == 1.5, 'what dropping it would have said'


def the_greeting_is_labelled_not_counted():
    found = analyse(SPANS)
    assert found.greeting_spans == 1
    assert 'greeting-1' not in found.turns


# --- repairs need evidence ---------------------------------------------------


def a_repair_is_a_refusal_and_not_a_repeated_tool():
    """`turn-1` calls `read_diary` twice and is not a repair.

    The caller changed their mind about the day. `turn-2` has one tool call
    that failed validation, and that is one. Counting repeated tools made the
    first turn a repair and the whole rate a number about nothing.
    """
    found = analyse(SPANS)
    assert found.tools_per_turn['turn-1']['read_diary'] == 2
    assert found.repair_turns == ['turn-2'], found.repair_turns


# --- quantiles ---------------------------------------------------------------


def the_quantile_is_nearest_rank():
    """`ceil(p*n) - 1`, and the old form disagreed at n=6 and n=10."""
    six = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert percentile(six, 0.5) == 3.0, 'ceil(0.5*6)-1 = index 2'
    assert percentile(six, 0.9) == 6.0
    ten = [float(n) for n in range(1, 11)]
    assert percentile(ten, 0.5) == 5.0
    assert percentile(ten, 0.9) == 9.0
    three = [1.0, 2.0, 3.0]
    assert percentile(three, 0.5) == 2.0
    assert percentile(three, 0.9) == 3.0


def the_old_convention_really_was_different():
    """Kept so the correction is a fact in the record, not a claim in a commit."""
    def previous(values, fraction):
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, max(0, round(fraction * len(ordered) + 0.5) - 1))]

    six = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert previous(six, 0.5) == 4.0 and percentile(six, 0.5) == 3.0
    ten = [float(n) for n in range(1, 11)]
    assert previous(ten, 0.9) == 10.0 and percentile(ten, 0.9) == 9.0


def no_data_is_missing_and_not_zero():
    """A zero in a results table is read as a measurement."""
    assert percentile([], 0.5) is None
    assert percentile([], 0.9) is None


def there_is_no_estimated_turn_latency():
    """The old report multiplied a median round trip by a p90 request count.

    Two unrelated percentiles, producing a "p90 turn" nobody measured and no
    turn necessarily took. It is gone, and this is what keeps it gone.
    """
    source = pathlib.Path(__file__).parent.parent / 'bench' / 'turns.py'
    text = source.read_text()
    assert 'for a p90 turn at the median round trip' not in text
    assert "percentile(round_trip, 0.5) * percentile" not in text


# --- latencies are two different questions -----------------------------------


def the_two_latencies_are_kept_apart():
    """A request's first token is not the turn's first word.

    `turn-1` took two requests: its first word came 1.2s after the turn
    started, and the request that produced it answered in 0.4s. Reporting
    either as the other hides a turn that spent a whole routing round trip in
    silence.
    """
    found = analyse(SPANS)
    assert found.turn_first_word == [1.2, 0.8], found.turn_first_word
    assert sorted(found.request_ttft) == [0.2, 0.3, 0.4], found.request_ttft
    # And Pipecat's own turn span, which is the only thing here that spans the
    # endpointing wait, is a labelled proxy rather than the caller gap.
    assert sorted(found.pipecat_turn_latency) == [0.6, 1.9, 2.5], found.pipecat_turn_latency


def round_trips_exclude_speech():
    found = analyse(SPANS)
    assert sorted(found.request_latency) == [0.6, 0.9, 1.2], found.request_latency


async def main():
    a_request_belongs_to_its_nearest_turn()
    a_missing_parent_stays_unresolved()
    the_call_root_is_not_a_turn()
    a_turn_is_its_own_turn()
    the_counts_are_exact()
    speech_is_not_a_model_request()
    a_zero_model_turn_is_still_a_caller_turn()
    the_greeting_is_labelled_not_counted()
    a_repair_is_a_refusal_and_not_a_repeated_tool()
    the_quantile_is_nearest_rank()
    the_old_convention_really_was_different()
    no_data_is_missing_and_not_zero()
    there_is_no_estimated_turn_latency()
    the_two_latencies_are_kept_apart()
    round_trips_exclude_speech()
    print('ok')


asyncio.run(main())
