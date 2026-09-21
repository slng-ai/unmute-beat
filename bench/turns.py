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
import os
import pathlib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

_CA = '/etc/ssl/cert.pem'
SSL_CONTEXT = (
    ssl.create_default_context(cafile=_CA)
    if pathlib.Path(_CA).exists()
    else ssl.create_default_context()
)

# `completionStartTime` is the whole point of number 2 and does not come back
# without `time`; `trace_context` is what carries `parentObservationId`, which
# is the whole point of number 1.
FIELDS = 'core,basic,time,io,model,usage,metrics,trace_context'

# Pipecat names its per-turn span this. `agents` is the span this harness's own
# processor opens inside it, and it is the fallback when there is no pipeline --
# a `talk.py` run has turns but no Pipecat.
TURN_SPANS = ('turn', 'agents')  # kept for reference; grouping is structural, see turn_of


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
        raise SystemExit(f'Langfuse returned {error.code} for {path}: {error.read().decode()[:500]}') from error


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
    return json.dumps(
        [{'type': 'string', 'column': 'sessionId', 'operator': '=', 'value': session_id}]
    )


# --- turns -------------------------------------------------------------------


def turn_of(span: dict, by_id: dict[str, dict]) -> str:
    """The id of the turn this observation happened inside.

    The outermost ancestor, found by walking `parentObservationId` up until it
    names something this query did not return. That dangling id *is* the turn:
    the per-turn span is opened by the pipeline, one level above everything the
    agents emit, and it is frequently not in the same observation set -- a
    `talk.py` run has the parent id and no exporter behind it at all. Matching
    on a span called `turn` would work only when the pipeline is running and
    would silently collapse every call into one bucket when it is not, which is
    the failure this is written to avoid.
    """
    seen, current = set(), span
    while True:
        parent = current.get('parentObservationId')
        if not parent or str(parent) in seen:
            # The top of what was returned, with nothing above it.
            return str(current.get('id'))
        seen.add(str(parent))
        above = by_id.get(str(parent))
        if above is None:
            return str(parent)  # the turn span, named but not exported here
        current = above


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank, because three calls is a legitimate sample size here and
    interpolating between two of them invents a number nobody measured."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * len(ordered) + 0.5) - 1))
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


def report(spans: list[dict], subject: str) -> None:
    by_id = {str(s['id']): s for s in spans if s.get('id')}

    requests_per_turn: Counter[str] = Counter()
    tools_per_turn: dict[str, Counter[str]] = defaultdict(Counter)
    first_token: list[float] = []
    round_trip: list[float] = []
    turn_first_word: list[float] = []

    for span in spans:
        kind, name = span.get('type'), span.get('name') or ''
        if kind == 'GENERATION':
            requests_per_turn[turn_of(span, by_id)] += 1
            if isinstance(latency := span.get('latency'), (int, float)):
                round_trip.append(float(latency))
            # Both of these come back empty on a pydantic-ai trace: its OTel
            # instrumentation does not emit a first-token event that Langfuse
            # maps onto `completionStartTime`. Read anyway, because a Pipecat
            # or LiveKit leg does emit it, and reported separately from
            # `latency`, because a round trip is not a time to first word and
            # quietly substituting one for the other is how a benchmark lies.
            millis = span.get('timeToFirstToken')
            if isinstance(millis, (int, float)) and millis >= 0:
                first_token.append(float(millis) / 1000)
            elif (ttft := seconds_between(span.get('startTime'), span.get('completionStartTime'))) is not None:
                first_token.append(ttft)
        elif kind == 'SPAN' and name == 'agents':
            # What the caller actually waited for: the first word of the whole
            # turn, however many requests it took to get there. The processor
            # writes it because nothing else can -- see the note there.
            ttft = (span.get('metadata') or {}).get('ttft')
            if isinstance(ttft, (int, float)):
                turn_first_word.append(float(ttft))
        elif kind == 'TOOL':
            # Counted as spans and never as call rows: two rows can be one
            # execution, and a rate built on rows is a rate built on noise.
            tools_per_turn[turn_of(span, by_id)][name] += 1

    # A turn that never ran a model is not a caller turn -- it is the greeting,
    # or a span the pipeline opened and closed with nothing in it.
    turns = [t for t, n in requests_per_turn.items() if n]
    counts = [requests_per_turn[t] for t in turns]
    repairs = [
        t for t in turns
        if any(n > 1 for n in tools_per_turn[t].values())
    ]

    print(f'\n=== {subject} ===')
    print(f'  caller turns                {len(turns)}')
    if counts:
        spread = ', '.join(f'{n}x{c}' for c, n in sorted(Counter(counts).items()))
        print(f'  model requests per turn     mean {sum(counts) / len(counts):.2f}   '
              f'p50 {percentile([float(c) for c in counts], 0.5):.0f}   '
              f'p90 {percentile([float(c) for c in counts], 0.9):.0f}   ({spread})')
    if round_trip:
        print(f'  model round trip, s         p50 {percentile(round_trip, 0.5):.3f}   '
              f'p90 {percentile(round_trip, 0.9):.3f}   (n={len(round_trip)})')
        print(f'  worst turn, s               {max(round_trip):.3f} for one request; '
              f'{percentile(round_trip, 0.5) * percentile([float(c) for c in counts], 0.9):.3f} '
              'for a p90 turn at the median round trip')
    if turn_first_word:
        print(f'  first word of the turn, s   p50 {percentile(turn_first_word, 0.5):.3f}   '
              f'p90 {percentile(turn_first_word, 0.9):.3f}   (n={len(turn_first_word)})'
              '   <- what the caller waits')
    if first_token:
        print(f'  time to first token, s      p50 {percentile(first_token, 0.5):.3f}   '
              f'p90 {percentile(first_token, 0.9):.3f}   (n={len(first_token)})')
    elif not turn_first_word:
        print('  time to first token         not in this trace -- pydantic-ai emits no '
              'first-token event,\n'
              '                              and the processor writes none either. '
              'Round trip above is real.')
    if turns:
        print(f'  repair turns                {len(repairs)}/{len(turns)} '
              f'({100 * len(repairs) / len(turns):.0f}%) -- same tool called twice in one turn')
    if len(turns) < 10:
        print('\n  A rate needs a denominator. This is one call; the repair rate wants 30-50.')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session-id', help='read this call instead of the newest one')
    parser.add_argument('--trace-id', help='read this call by its trace instead')
    parser.add_argument('--hours', type=float, default=2.0, help='how far back to look')
    parser.add_argument('--sessions', action='store_true', help='list recent calls and stop')
    parser.add_argument('--all', action='store_true',
                        help='pool every call in the window into one report, which is '
                             'what a repair rate actually needs')
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
        print('set LANGFUSE_BASE_URL, LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY, '
              'or pass --env pointing at a .env that has them', file=sys.stderr)
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
