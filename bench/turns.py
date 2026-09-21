"""Per-turn numbers for a benchmark configuration, off Langfuse.

Run: uv run python -m bench.turns --env .env [--session-id ...] [--sessions]

`read_langfuse_trace.py` in the unmute package groups observations by span
*name* across the whole call, which answers "how slow is the diary lookup" and
cannot answer "how many model requests did this caller wait through". A rate
needs a denominator and the denominator is the turn, so this walks
`parentObservationId` up to the enclosing turn span and counts children.

Three numbers, and they are the three the comparison turns on:

  1. model requests per caller turn -- the claim about LLM calls;
  2. time to first token, p50 and p90 -- the claim about latency, as a
     distribution because the claim is about the tail;
  3. repair turns -- a turn that called the same tool twice, or produced a
     refusal and then another request. A turn spent fixing itself.

Read-only. It writes nothing and changes nothing.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import math
import os
import pathlib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass

_CA = '/etc/ssl/cert.pem'
SSL_CONTEXT = (
    ssl.create_default_context(cafile=_CA) if pathlib.Path(_CA).exists() else ssl.create_default_context()
)

# `completionStartTime` is the whole point of number 2 and does not come back
# without `time`; `trace_context` is what carries `parentObservationId`, which
# is the whole point of number 1.
#
# `metadata` is where an OTel exporter's span attributes land, and the one
# number Pipecat records for what a caller actually waited through --
# `turn.user_bot_latency_seconds` -- is in there and nowhere else. Left out of
# this list it comes back absent, and an absent field read as "not
# instrumented" is how a whole arm reports no latency at all.
FIELDS = 'core,basic,time,io,model,usage,metrics,trace_context,metadata'


def load_env_file(path: pathlib.Path) -> None:
    """KEY=VALUE lines into os.environ, without overwriting what is already set."""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('\'"'))


def api_get(host: str, auth: str, path: str, params: dict[str, str]) -> dict:
    url = f'{host}{path}?{urllib.parse.urlencode(params)}'
    request = urllib.request.Request(url, headers={'Authorization': f'Basic {auth}'})
    try:
        with urllib.request.urlopen(request, timeout=60, context=SSL_CONTEXT) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as error:
        raise SystemExit(
            f'Langfuse returned {error.code} for {path}: {error.read().decode()[:500]}'
        ) from error


def fetch(host: str, auth: str, params: dict[str, str]) -> list[dict]:
    """Every cursor page: one call is 50 spans and a call is many more."""
    out: list[dict] = []
    cursor = None
    while True:
        page = dict(params, limit='1000', fields=FIELDS)
        if cursor:
            page['cursor'] = cursor
        body = api_get(host, auth, '/api/public/v2/observations', page)
        out.extend(body.get('data', []))
        meta = body.get('meta') or {}
        cursor = meta.get('cursor') or meta.get('nextCursor')
        if not cursor:
            return out


def session_filter(session_id: str) -> str:
    """The v2 API has no sessionId parameter, only the generic column filter."""
    return json.dumps([{'type': 'string', 'column': 'sessionId', 'operator': '=', 'value': session_id}])


# --- turns -------------------------------------------------------------------

# The span this core opens once per accepted caller turn. Turn membership is
# structural and named: the nearest ancestor with one of these names is the
# turn, and an observation that reaches the top without finding one is
# *unresolved*, never guessed.
#
# It used to walk to the outermost ancestor instead and call that the turn.
# That is right only while the call's own root span is missing from the
# query. As soon as it is present -- which it is whenever Pipecat's tracing
# is on and the call has ended -- every observation in the call walks up to
# the same root and the whole call collapses into one "turn". The denominator
# then reads 1, and the rate built on it is the call's total.
TURN_SPANS = frozenset({'agents'})

# Pipecat's own per-turn span. The native arm has no `agents` span at all, so
# there these *are* the caller turns; in a POC trace they are the outer span
# our own one nests inside, and counting both would double every denominator.
# `caller_turns` picks one or the other, never both.
PIPECAT_TURN = 'turn'

# The attribute Pipecat's `TurnTrackingObserver` writes only for a turn that
# began with the caller speaking. The greeting turn has every other turn
# attribute and not this one, which is what tells the two apart -- explicitly,
# rather than by assuming the first one is the greeting.
USER_BOT_LATENCY = 'turn.user_bot_latency_seconds'

# STT and TTS reach Langfuse as `GENERATION` observations, because that is the
# vocabulary Langfuse has for them -- `tests/test_traces.py` asserts exactly
# that mapping. They are not model requests. Counting them inflated every
# request-per-turn figure by however much the caller and the agent spoke.
SPEECH_SPANS = frozenset({'stt', 'tts'})


def caller_turns(spans: list[dict]) -> tuple[list[str], list[dict], str]:
    """Which observations are this trace's caller turns, the greetings, and whose arm it is.

    The two arms name the same thing differently and nest it differently, and
    the comparison is worthless unless one caller turn on one side means one
    caller turn on the other.

    * The POC opens an `agents` span per accepted turn, inside Pipecat's
      `turn` span. Its `agents` spans are the turns; the `turn` spans around
      them carry the latency attribute and nothing else.
    * The native arm has no `agents` span, so Pipecat's `turn` spans are the
      turns -- except the greeting, which is a turn in Pipecat's numbering and
      not a turn anybody waited through.

    Adding both names to one set and walking to the nearest ancestor reads a
    POC call as twice as many turns as it had. Reading only `agents` reads a
    native call as **zero** turns and every one of its model requests as
    unresolved, which is what it did against the reference trace.
    """
    ordered = sorted(spans, key=lambda s: str(s.get('startTime') or ''))
    pipecat = [s for s in ordered if s.get('type') == 'SPAN' and (s.get('name') or '') == PIPECAT_TURN]
    greetings = [s for s in pipecat if _attribute(s, USER_BOT_LATENCY) is None]
    ours = [s for s in ordered if s.get('type') == 'SPAN' and (s.get('name') or '') in TURN_SPANS]
    if ours:
        return [str(s['id']) for s in ours], greetings, 'poc'
    answered = [s for s in pipecat if _attribute(s, USER_BOT_LATENCY) is not None]
    return [str(s['id']) for s in answered], greetings, 'native' if answered else 'unknown'


def turn_of(span: dict, by_id: dict[str, dict], turn_ids: set[str]) -> str | None:
    """The caller turn this observation happened inside, or None if unknown.

    The nearest turn ancestor, not the outermost one, and only the ones
    `caller_turns` chose. `None` means the chain ran out -- either it reached
    a root that is not a turn, or it named a parent this query did not return.
    Both are genuinely unresolved and both stay unresolved: an unknown
    denominator reported as a confident one is worse than an unknown
    denominator reported as unknown.
    """
    current, seen = span, set()
    while current is not None:
        if str(current.get('id')) in turn_ids:
            return str(current.get('id'))
        parent = current.get('parentObservationId')
        if not parent or str(parent) in seen:
            return None
        seen.add(str(parent))
        current = by_id.get(str(parent))
    return None


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest rank, `ceil(p*n) - 1`, or None when there is nothing to rank.

    Nearest rank because three calls is a legitimate sample size here and
    interpolating between two of them invents a number nobody measured.

    `None` and never `0.0`: no data is not a fast p90, and a zero in a results
    table is read as a measurement. The convention is printed next to every
    figure that uses it, because nearest-rank and linear interpolation
    disagree on small samples and a number without its convention cannot be
    compared with anything.

    The previous form was `round(p*n + 0.5) - 1`, which is not nearest rank:
    at n=6 it puts p50 at index 3 where nearest rank puts it at 2, and it
    disagrees again at n=10 for both p50 and p90.
    """
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        began = dt.datetime.fromisoformat(start.replace('Z', '+00:00'))
        ended = dt.datetime.fromisoformat(end.replace('Z', '+00:00'))
    except ValueError:
        return None
    gap = (ended - began).total_seconds()
    return gap if gap >= 0 else None


def _attribute(span: dict, name: str):
    """One value stamped on a span, under either spelling Langfuse stores it.

    An OTel span attribute arrives prefixed -- `turn.user_bot_latency_seconds`
    is `attributes.turn.user_bot_latency_seconds` in the metadata Langfuse
    returns -- while a value set through the SDK's own metadata is bare. The
    exporter decides which, so a reader that knows only one spelling reports
    the other arm as uninstrumented.
    """
    metadata = span.get('metadata') or {}
    if not isinstance(metadata, dict):
        return None
    for key in (name, f'attributes.{name}'):
        if key in metadata:
            return metadata[key]
    return None


def _ttft(span: dict) -> float | None:
    """Seconds from a request going out to its first output, or None if unrecorded.

    `timeToFirstToken` is in **seconds**, like the `latency` beside it: on the
    reference trace a TTS observation reports 0.121 against a 121ms gap
    between `startTime` and `completionStartTime`. This used to divide it by a
    thousand, which turned every recorded value into a tenth of a millisecond
    -- and only the POC arm was affected, because the native arm's LLM spans
    report the field as null, so the bug read as the POC being four orders of
    magnitude faster.

    Three sources, in order of directness, and each one is a real measurement
    rather than a substitute for a missing one: the field, the timestamps it
    is derived from, and the `metrics.ttfb` the native integration records
    instead. `first_output_of` says which kind of output it was, where the
    trace knows.
    """
    reported = span.get('timeToFirstToken')
    if isinstance(reported, (int, float)) and reported >= 0:
        return float(reported)
    if (measured := seconds_between(span.get('startTime'), span.get('completionStartTime'))) is not None:
        return measured
    native = _attribute(span, 'metrics.ttfb')
    return float(native) if isinstance(native, (int, float)) and native >= 0 else None


def _failed(span: dict) -> bool:
    """Whether this observation records a refusal or an error.

    What a repair is made of. A tool called twice is *not* a repair: reading
    the diary for Tuesday and then for Wednesday is one caller changing their
    mind, and counting it as the agency fixing itself is how a repair rate
    becomes a number about nothing. Repairs need evidence of a refusal.
    """
    if str(span.get('level') or '').upper() in ('ERROR', 'WARNING'):
        return True
    return bool(str(span.get('statusMessage') or '').strip())


@dataclass
class Report:
    """What one set of observations says, before any of it is printed."""

    arm: str  # 'poc', 'native', or 'unknown': which shape of trace this was
    turns: list[str]  # every caller turn, zero-model included
    requests_per_turn: dict[str, int]
    tools_per_turn: dict[str, Counter]
    repair_turns: list[str]
    refusals: int  # observations carrying a refusal or an error
    greeting_spans: int
    unresolved_spans: int
    unresolved_requests: int
    speech_spans: int
    request_latency: list[float]
    request_ttft: list[float]
    ttft_source: str
    legacy_ttft: int  # turns carrying the old run-timed number, which is not comparable
    first_output: Counter  # what each request produced first: text, or a tool call
    turn_first_word: list[float]
    turn_lifetime: list[float]  # the whole turn span: user speech to bot finished
    user_bot_latency: list[float]  # what Pipecat measured the caller waiting

    @property
    def zero_model_turns(self) -> int:
        return sum(1 for turn in self.turns if not self.requests_per_turn.get(turn))

    @property
    def counts(self) -> list[int]:
        """Requests per turn, over every caller turn including the empty ones.

        Zero-model turns belong in the denominator. A turn where the agency
        answered without asking a model is the best turn it can have, and
        dropping it makes the average request count go up.
        """
        return [self.requests_per_turn.get(turn, 0) for turn in self.turns]


def analyse(spans: list[dict]) -> Report:
    """Read a set of Langfuse observations into the numbers, and nothing else.

    Pure and printable-free, so `tests/test_bench.py` can hand it a fixture
    and assert exact counts rather than eyeballing a report.
    """
    by_id = {str(s['id']): s for s in spans if s.get('id')}
    turns, greetings, arm = caller_turns(spans)
    turn_ids = set(turns)

    requests_per_turn: dict[str, int] = dict.fromkeys(turns, 0)
    tools_per_turn: dict[str, Counter] = defaultdict(Counter)
    failed_in_turn: set[str] = set()
    request_latency: list[float] = []
    stamped: list[float] = []  # our own per-request clock, off the `agents` span
    provider: list[float] = []  # what the provider or the exporter recorded
    first_output: Counter = Counter()
    turn_first_word: list[float] = []
    turn_lifetime: list[float] = []
    user_bot_latency: list[float] = []
    greeting_spans = len(greetings)
    unresolved_spans = unresolved_requests = speech_spans = refusals = legacy = 0

    for span in spans:
        kind = span.get('type')
        name = span.get('name') or ''
        turn = turn_of(span, by_id, turn_ids)

        if name in SPEECH_SPANS:
            # Counted, and counted apart. A `GENERATION` is not a model
            # request just because Langfuse has one word for both.
            speech_spans += 1
            continue
        if name == PIPECAT_TURN:
            # The whole turn, greeting included: its lifetime and the one
            # latency number Pipecat measures. Whether it is also a caller
            # turn was decided by `caller_turns`.
            if isinstance(lifetime := span.get('latency'), (int, float)):
                turn_lifetime.append(float(lifetime))
            if isinstance(waited := _attribute(span, USER_BOT_LATENCY), (int, float)):
                user_bot_latency.append(float(waited))
            continue
        if name.startswith('greeting') or name.startswith('prefetch'):
            # Said before the caller said anything, so not a caller turn and
            # not in any rate's denominator. Labelled rather than dropped.
            greeting_spans += 1
            continue

        if _failed(span):
            refusals += 1
            if turn is not None:
                failed_in_turn.add(turn)

        if kind == 'GENERATION':
            if turn is None:
                # A real provider request whose turn cannot be established.
                # Reported, never assigned to a turn to make the sums work.
                unresolved_requests += 1
                unresolved_spans += 1
                continue
            requests_per_turn[turn] = requests_per_turn.get(turn, 0) + 1
            if isinstance(latency := span.get('latency'), (int, float)):
                request_latency.append(float(latency))
            if (ttft := _ttft(span)) is not None:
                provider.append(ttft)
        elif kind == 'TOOL':
            if turn is None:
                unresolved_spans += 1
                continue
            tools_per_turn[turn][name] += 1
        elif kind == 'SPAN' and name in TURN_SPANS:
            if isinstance(first := _attribute(span, 'turn_first_word'), (int, float)):
                turn_first_word.append(float(first))
            # Per provider request, stamped at the model boundary by
            # `voice.harness.request_clock`. Pydantic AI's instrumentation
            # emits no first-token event, so the provider field on a POC
            # generation is null and this is the only place the number
            # exists.
            recorded = _attribute(span, 'request_ttft')
            if isinstance(recorded, (int, float)):
                # A trace from before this was timed per request: one number
                # per *run*, which on a turn with a tool in it contains the
                # tool's whole round trip. Counted so the report can say the
                # trace is too old to answer, never pooled with the others --
                # two different measurements under one name is how a
                # distribution stops meaning anything.
                legacy += 1
            else:
                for value in recorded or []:
                    if isinstance(value, (int, float)):
                        stamped.append(float(value))
            for produced in _attribute(span, 'request_first_output') or []:
                first_output[str(produced)] += 1
        # No catch-all for other spans with no turn. A call root has no turn
        # because it is a root, not because its parentage is unknown, and
        # counting it as unresolved would put a number in the "we could not
        # tell" column on every well-formed trace. Only work that should have
        # belonged to a turn -- a model request, a tool execution -- is
        # unresolved when it does not.

    return Report(
        arm=arm,
        turns=turns,
        requests_per_turn=requests_per_turn,
        tools_per_turn=dict(tools_per_turn),
        repair_turns=sorted(failed_in_turn),
        refusals=refusals,
        greeting_spans=greeting_spans,
        unresolved_spans=unresolved_spans,
        unresolved_requests=unresolved_requests,
        speech_spans=speech_spans,
        request_latency=request_latency,
        # Never both. One arm's number is stamped by us and the other's by the
        # exporter, and pooling two clocks into one distribution is how a
        # comparison stops comparing anything.
        request_ttft=stamped or provider,
        ttft_source='agents span, stamped per request' if stamped else 'provider field or timestamps',
        legacy_ttft=legacy,
        first_output=first_output,
        turn_first_word=turn_first_word,
        turn_lifetime=turn_lifetime,
        user_bot_latency=user_bot_latency,
    )


def _pair(values: list[float], unit: str = 's') -> str:
    """p50 and p90, or an honest gap where a number would be a lie."""
    p50, p90 = percentile(values, 0.5), percentile(values, 0.9)
    if p50 is None:
        return 'no data'
    return f'p50 {p50:.3f}{unit}   p90 {p90:.3f}{unit}   (n={len(values)})'


def report(spans: list[dict], subject: str) -> Report:
    found = analyse(spans)
    counts = found.counts

    print(f'\n=== {subject} ===')
    print(f'  arm                         {found.arm}  (which shape of trace this is)')
    print(
        f'  caller turns                {len(found.turns)} ({found.zero_model_turns} of them asked no model)'
    )
    if found.greeting_spans:
        print(
            f'  greeting / prefetch         {found.greeting_spans}  '
            '(not caller turns, not in any denominator)'
        )
    if counts:
        spread = ', '.join(f'{n}x{c}' for c, n in sorted(Counter(counts).items()))
        mean = sum(counts) / len(counts)
        print(
            f'  model requests per turn     mean {mean:.2f}   '
            f'p50 {percentile([float(c) for c in counts], 0.5):.0f}   '
            f'p90 {percentile([float(c) for c in counts], 0.9):.0f}   ({spread})'
        )
    if found.speech_spans:
        print(
            f'  stt / tts observations      {found.speech_spans}  (generation-typed, and not model requests)'
        )
    if found.unresolved_spans:
        print(
            f'  unresolved parentage        {found.unresolved_spans} observations, '
            f'{found.unresolved_requests} of them model requests -- '
            'left unassigned, not guessed'
        )
    if found.user_bot_latency:
        print(
            f'  caller waited               {_pair(found.user_bot_latency)}   '
            '<- Pipecat, end of caller speech to bot speaking; includes endpointing'
        )
    print(f'  model round trip            {_pair(found.request_latency)}')
    print(f'  request time to first token {_pair(found.request_ttft)}   <- {found.ttft_source}')
    if found.legacy_ttft:
        print(
            f'                              {found.legacy_ttft} turns carry the older run-timed '
            'number instead, which includes any tool call in the run. Not pooled above.'
        )
    if found.first_output:
        produced = ', '.join(f'{n} {kind}' for kind, n in sorted(found.first_output.items()))
        print(f'  what came back first        {produced}  (a tool call is not speech)')
    print(
        f'  first word of the turn      {_pair(found.turn_first_word)}   '
        '<- from the finished transcript, not from end of caller audio'
    )
    if found.turn_lifetime:
        print(
            f'  turn span lifetime          {_pair(found.turn_lifetime)}   '
            '<- the whole turn, caller speech to bot finished speaking. NOT a response latency'
        )
    if found.turns:
        share = 100 * len(found.repair_turns) / len(found.turns)
        print(
            f'  repair turns                {len(found.repair_turns)}/{len(found.turns)} '
            f'({share:.0f}%) -- turns containing a refusal or error, from {found.refusals} observations'
        )
        if not found.refusals:
            print(
                '                              no refusal or error recorded anywhere in this trace; '
                'this is "none seen", not "none happened" -- and a warning is not a semantic repair'
            )
    print('  quantile convention         nearest rank, ceil(p*n)-1; "no data" is missing, not zero')
    if len(found.turns) < 10:
        print('\n  A rate needs a denominator. This is one call; the repair rate wants 30-50.')
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session-id', help='read this call instead of the newest one')
    parser.add_argument('--trace-id', help='read this call by its trace instead')
    parser.add_argument('--hours', type=float, default=2.0, help='how far back to look')
    parser.add_argument('--sessions', action='store_true', help='list recent calls and stop')
    parser.add_argument(
        '--all',
        action='store_true',
        help='pool every call in the window into one report, which is what a repair rate actually needs',
    )
    parser.add_argument('--env', type=pathlib.Path, help='read credentials from this .env')
    args = parser.parse_args()

    if args.env:
        if not args.env.exists():
            print(f'no such env file: {args.env}', file=sys.stderr)
            return 1
        load_env_file(args.env)

    host = (os.environ.get('LANGFUSE_BASE_URL') or os.environ.get('LANGFUSE_HOST') or '').rstrip('/')
    public, secret = os.environ.get('LANGFUSE_PUBLIC_KEY'), os.environ.get('LANGFUSE_SECRET_KEY')
    if not (host and public and secret):
        print(
            'set LANGFUSE_BASE_URL, LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY, '
            'or pass --env pointing at a .env that has them',
            file=sys.stderr,
        )
        return 1
    auth = base64.b64encode(f'{public}:{secret}'.encode()).decode()

    now = dt.datetime.now(dt.UTC)
    window = {
        'fromStartTime': (now - dt.timedelta(hours=args.hours)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'toStartTime': (now + dt.timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }

    if args.trace_id:
        report(fetch(host, auth, dict(window, traceId=args.trace_id)), f'trace {args.trace_id}')
        return 0

    everything = fetch(host, auth, window)
    if not everything:
        print(f'nothing in the last {args.hours}h. Was tracing configured for the run?')
        return 1

    if args.all:
        calls = {s.get('sessionId') for s in everything if s.get('sessionId')}
        report(everything, f'{len(calls)} calls in the last {args.hours}h, pooled')
        return 0

    sessions: dict[str, dict] = defaultdict(lambda: {'spans': 0, 'first': None})
    for span in everything:
        entry = sessions[span.get('sessionId') or '']
        entry['spans'] += 1
        if start := span.get('startTime'):
            entry['first'] = min(entry['first'] or start, start)
    rows = sorted(sessions.items(), key=lambda kv: kv[1]['first'] or '', reverse=True)

    if args.sessions:
        print(f'{"started":<20} {"spans":>6}  session')
        for name, entry in rows:
            print(f'{(entry["first"] or "")[:19]:<20} {entry["spans"]:>6}  {name or "-"}')
        return 0

    session_id = args.session_id or rows[0][0]
    report(
        fetch(host, auth, dict(window, filter=session_filter(session_id))),
        f'session {session_id}',
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
