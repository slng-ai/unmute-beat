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
import copy
import json
import pathlib

from bench.turns import MixedArmsError, _ttft, analyse, caller_turns, percentile, seconds_between, turn_of

FIXTURE = pathlib.Path(__file__).parent / 'fixtures' / 'trace_s15.json'
SPANS = json.loads(FIXTURE.read_text())['data']
BY_ID = {str(s['id']): s for s in SPANS if s.get('id')}
TURNS = set(caller_turns(SPANS)[0])

# The real thing, from the native arm: Langfuse observations for trace
# f1c4f999e516a7792dfdbed72fa8262d, the reference conversation of
# 2026-09-21 09:15:54Z. Trimmed to the fields and span attributes this reader
# reads -- the `param.*` and `resourceAttributes.*` metadata is dropped -- and
# otherwise exactly as the API returned it. A handwritten fixture can only
# check the reader against what its author expected; this one checks it
# against the arm it has to measure.
NATIVE = json.loads((pathlib.Path(__file__).parent / 'fixtures' / 'trace_native.json').read_text())['data']


# --- turn identity -----------------------------------------------------------


def a_request_belongs_to_its_nearest_turn():
    """Not the outermost ancestor. Both turns are under one call root.

    Walking to the top, `req-1a` and `req-2a` both arrive at `root-call` and
    the two turns become one -- which is what happened whenever tracing was
    on and the call had ended, so the bug was invisible in exactly the runs
    that mattered.
    """
    assert turn_of(BY_ID['req-1a'], BY_ID, TURNS) == 'turn-1'
    assert turn_of(BY_ID['req-2a'], BY_ID, TURNS) == 'turn-2'
    assert turn_of(BY_ID['tool-1a'], BY_ID, TURNS) == 'turn-1'
    assert turn_of(BY_ID['tool-2a'], BY_ID, TURNS) == 'turn-2'


def a_missing_parent_stays_unresolved():
    """`req-orphan` names a parent this query did not return.

    None, and not the dangling id, and not the nearest thing to hand. An
    unknown denominator reported as a confident one is worse than an unknown
    one reported as unknown.
    """
    assert turn_of(BY_ID['req-orphan'], BY_ID, TURNS) is None


def the_call_root_is_not_a_turn():
    assert turn_of(BY_ID['root-call'], BY_ID, TURNS) is None
    assert turn_of(BY_ID['greeting-1'], BY_ID, TURNS) is None


def a_turn_is_its_own_turn():
    assert turn_of(BY_ID['turn-1'], BY_ID, TURNS) == 'turn-1'


def the_pipecat_turn_around_ours_is_not_a_second_turn():
    """Both arms call something a turn, and only one of them may be counted.

    A POC call nests an `agents` span inside Pipecat's `turn` span. Reading
    both names as caller turns doubles every denominator: three turns become
    six, and the requests per turn halve without anything changing.
    """
    turns, greetings, arm = caller_turns(SPANS)
    assert arm == 'poc'
    assert turns == ['turn-1', 'turn-2', 'turn-3'], turns
    assert turn_of(BY_ID['req-1a'], BY_ID, TURNS) == 'turn-1', 'ours, not the pipecat one around it'
    assert [g['id'] for g in greetings] == ['pipecat-turn-greeting'], greetings


# --- what gets counted -------------------------------------------------------


def the_counts_are_exact():
    found = analyse(SPANS)
    assert found.turns == ['turn-1', 'turn-2', 'turn-3'], found.turns
    assert found.requests_per_turn == {'turn-1': 2, 'turn-2': 1, 'turn-3': 0}, found.requests_per_turn
    assert found.counts == [2, 1, 0], found.counts
    assert found.zero_model_turns == 1
    assert found.greeting_spans == 2, 'the named greeting span, and the turn nobody waited through'
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
    assert found.greeting_spans == 2
    assert 'greeting-1' not in found.turns
    assert 'pipecat-turn-greeting' not in found.turns


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
    assert 'percentile(round_trip, 0.5) * percentile' not in text


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
    # Per provider request, stamped at the model boundary. `turn-1` made two
    # and only the second one spoke, so one run-long number cannot describe
    # both.
    assert sorted(found.request_ttft) == [0.3, 0.35, 0.4], found.request_ttft
    assert found.ttft_source.startswith('agents span'), found.ttft_source
    assert found.first_output == {'tool': 1, 'text': 2}, found.first_output


def a_turn_span_lifetime_is_not_a_response_latency():
    """Pipecat's turn span runs until the bot stops speaking.

    It was reported as a proxy "ending at response start", which it does not:
    on the native reference these spans last 3.3 to 15.1 seconds on turns the
    caller waited 1.0 to 1.8 for. The number that answers the latency
    question is the one Pipecat records as an attribute.
    """
    found = analyse(SPANS)
    assert sorted(found.turn_lifetime) == [0.5, 0.6, 1.9, 2.5], found.turn_lifetime
    assert sorted(found.user_bot_latency) == [0.9, 1.2, 1.5], found.user_bot_latency
    assert max(found.turn_lifetime) > max(found.user_bot_latency)


def round_trips_exclude_speech():
    found = analyse(SPANS)
    assert sorted(found.request_latency) == [0.6, 0.9, 1.2], found.request_latency


# --- the native arm ----------------------------------------------------------


def the_native_reference_reads_as_five_turns_and_ten_requests():
    """The arm this POC has to beat, counted off its own trace.

    It used to read as **zero** caller turns and ten unresolved requests: the
    reader knew only the `agents` span name, which the native arm does not
    emit. A comparison whose instrument cannot see one of the two arms is not
    a comparison.
    """
    found = analyse(NATIVE)
    assert found.arm == 'native', found.arm
    assert len(found.turns) == 5, found.turns
    assert found.greeting_spans == 1, 'the fixed greeting is not a caller turn'
    assert sum(found.requests_per_turn.values()) == 10, found.requests_per_turn
    assert found.counts == [2, 3, 1, 2, 2], found.counts
    assert found.unresolved_requests == 0, found.unresolved_requests
    assert found.speech_spans == 13, 'five stt and eight tts, none of them a model request'


def the_native_caller_gap_comes_from_pipecats_own_number():
    """1.198s p50, 1.769s p90 -- and the turn spans say 6.658s and 15.125s."""
    found = analyse(NATIVE)
    assert percentile(found.user_bot_latency, 0.5) == 1.1981770992279053
    assert percentile(found.user_bot_latency, 0.9) == 1.7685189247131348
    assert len(found.turn_lifetime) == 6, 'the greeting turn has a lifetime and no caller gap'


def time_to_first_token_is_recorded_in_seconds():
    """The API field, checked against the timestamps it is derived from.

    It was divided by a thousand. On the native arm the LLM spans report the
    field as null so nothing showed; on the POC arm every recorded value
    became a tenth of a millisecond, which reads as an architecture four
    orders of magnitude faster than the one it is being compared with.
    """
    speech = [s for s in NATIVE if s['name'] == 'tts' and s.get('completionStartTime')]
    assert speech, 'the fixture must contain a span that records both'
    for span in speech:
        reported, measured = _ttft(span), seconds_between(span['startTime'], span['completionStartTime'])
        assert reported is not None and measured is not None, span['id']
        assert abs(reported - measured) < 0.002, (span['id'], reported, measured)
    # And the native LLM spans, whose own field is null, fall back to what the
    # Pipecat integration records instead.
    llm = [s for s in NATIVE if s['name'] == 'llm']
    assert all(s.get('timeToFirstToken') is None for s in llm)
    assert all(_ttft(s) is not None for s in llm), 'metrics.ttfb is there and was not being read'


def a_turn_with_no_latency_recorded_is_still_a_turn():
    """A missing measurement is not a greeting.

    The greeting was identified by the attribute it lacks, so a caller turn
    whose instrumentation did not fire was reclassified as one -- taking its
    two model requests out of the denominator with it and leaving them
    unresolved. Five turns and ten requests became four and eight. An
    interrupted or silent turn is exactly the turn a latency comparison most
    needs to keep.
    """
    hurt = copy.deepcopy(NATIVE)
    spans = sorted([s for s in hurt if s['name'] == 'turn'], key=lambda s: s['startTime'])
    del spans[1]['metadata']['attributes.turn.user_bot_latency_seconds']

    found = analyse(hurt)
    assert len(found.turns) == 5, found.turns
    assert sum(found.requests_per_turn.values()) == 10, found.requests_per_turn
    assert found.unresolved_requests == 0
    assert found.greeting_spans == 1, 'and the real greeting is still the greeting'
    assert len(found.user_bot_latency) == 4, 'the measurement is missing, and only the measurement'


def pooling_two_calls_keeps_both_greetings():
    """Every call has a greeting, not just the first one in the window.

    Reproduced by the second recheck. The greeting was picked by
    `index == 0` over the pooled list, which is one turn however many calls
    are in it: two native reference calls read as **eleven** caller turns and
    one greeting instead of ten and two, and the mean fell from 2.0 requests
    per turn to 1.818. The distortion grows with the sample, which is the
    direction pooling is supposed to improve in -- and partitioning by arm
    does not touch it, because both calls are the same arm.
    """
    second = copy.deepcopy(NATIVE)
    for span in second:
        span['id'] = f'{span["id"]}-b'
        span['traceId'] = f'{span["traceId"]}-b'
        span['sessionId'] = f'{span.get("sessionId")}-b'
        if span.get('parentObservationId'):
            span['parentObservationId'] = f'{span["parentObservationId"]}-b'

    found = analyse([*copy.deepcopy(NATIVE), *second])
    assert found.arm == 'native', found.arm
    assert len(found.turns) == 10, found.turns
    assert found.greeting_spans == 2, found.greeting_spans
    assert sum(found.requests_per_turn.values()) == 20, found.requests_per_turn
    assert found.zero_model_turns == 0, 'no greeting is sitting in the denominator'
    assert sum(found.counts) / len(found.counts) == 2.0, found.counts


def the_greeting_is_identified_by_what_it_holds():
    """Not by position alone either: a first turn that did work is a caller turn."""
    busy = copy.deepcopy(NATIVE)
    first = min((s for s in busy if s['name'] == 'turn'), key=lambda s: s['startTime'])
    busy.append(
        {
            'id': 'llm-in-the-greeting',
            'traceId': first['traceId'],
            'parentObservationId': first['id'],
            'type': 'GENERATION',
            'name': 'llm',
            'startTime': first['startTime'],
            'metadata': {},
        }
    )
    found = analyse(busy)
    assert found.greeting_spans == 0, 'a turn that ran a model is a turn somebody waited through'
    assert len(found.turns) == 6, found.turns


def two_arms_in_one_window_are_refused():
    """`--all` over a window that caught both is two populations, not one.

    It used to pick `poc` for the whole set and return that fixture's three
    turns, losing all five native ones and counting their ten requests as
    unresolved. A pooled number over two architectures is not a measurement
    of either.
    """
    try:
        analyse(NATIVE + SPANS)
    except MixedArmsError as mixed:
        assert 'both arms at once' in str(mixed), mixed
    else:
        raise AssertionError('a mixture of arms must be refused, not silently resolved')
    # Each on its own still reads.
    assert analyse(NATIVE).arm == 'native'
    assert analyse(SPANS).arm == 'poc'


def a_span_attribute_is_found_under_either_spelling():
    """An exporter prefixes what it writes; the SDK does not."""
    found = analyse(NATIVE)
    assert found.user_bot_latency, 'attributes.turn.user_bot_latency_seconds was not read'
    assert analyse(SPANS).turn_first_word, 'and the bare spelling still is'


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
    the_pipecat_turn_around_ours_is_not_a_second_turn()
    the_two_latencies_are_kept_apart()
    a_turn_span_lifetime_is_not_a_response_latency()
    round_trips_exclude_speech()
    the_native_reference_reads_as_five_turns_and_ten_requests()
    the_native_caller_gap_comes_from_pipecats_own_number()
    time_to_first_token_is_recorded_in_seconds()
    a_turn_with_no_latency_recorded_is_still_a_turn()
    the_greeting_is_identified_by_what_it_holds()
    pooling_two_calls_keeps_both_greetings()
    two_arms_in_one_window_are_refused()
    a_span_attribute_is_found_under_either_spelling()
    print('ok')


if __name__ == '__main__':
    # `tests/__main__.py` runs each module with `run_name='__main__'`, so this
    # still runs there -- and a debugging session can import one module
    # without running every check in it.
    asyncio.run(main())
