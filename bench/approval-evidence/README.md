# “Go for it” approval repair

[Trace 85eca3…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=85eca3e4b3657cc9c31208754c361915)
contains 82 observations, fetched over two pages without truncation. STT correctly
returned `Go for it.`. At 20:53:59 and 20:54:08 UTC, `save_booking` was refused
because `salon.state.agrees()` omitted this phrase. The model received the explicit
retry saying the caller had not agreed, then called `ask_booking` again. A later
`Yes.` succeeded. This was an application approval defect, not STT loss or endpointing.

The shared matcher now accepts a complete `Go for it` reply with terminal punctuation,
case-insensitively. It still needs the exact pending action/readback. No readback,
a changed time, negation, or a question does not grant that authority. The same
matcher is used by phone confirmation, booking approval and complaint approval.
This remains a deliberately bounded phrase matcher; broader paraphrase understanding
has not been added or claimed.

`tests.test_consent.go_for_it_approves_only_the_pending_move` checks no pending
readback, a qualified correction, then a successful exact move with one existing
booking and one additional write. The matcher tests also reject “Go for it at
eleven,” “Should I go for it?” and “Don't go for it.” Reverting just the phrase
support fails at the real authorization boundary: `negative-control.txt`.

Current checks: **13/13 offline modules**, Ruff, formatting and ty pass; their
outputs are retained here. `replay.py` establishes the same pending move with a
scripted response, then lets real Gemini process `Go for it.` three times.
All three initial runs passed with one model request each; `replay-initial.jsonl`
retains them. Its `settings` clock/fixture describes the process before scenario
setup, so the replay explicitly records the actual frozen clock and fresh-per-repeat
fixture in the subsequent `replay.jsonl`. Neither run is fresh microphone acceptance.

## The companion complaint call

[Trace 4304e7…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=4304e71354352e37c10253f8a5879a3b)
contains 46 observations, one page. It reads the phone once, accepts confirmation,
reads back caller-sourced complaint text and records **CX0001** once. The call state
records one write. Its raw call-end diary snapshot is empty because the next fresh
fixture call started at 20:52:55, before this call finished cleanup at 20:53:01.
The successful tool receipt and call state establish the write; the later reset
must not be misread as a failed complaint. Use continuation fixtures when testing
cross-call persistence, and disconnect before starting another fresh call.

Both calls used source hash `a25ff22…`, prompt hash `f4467e6…` and the frozen clock,
matching the measured POC before this phrase repair. The new complaint's exact
STT excerpts are choppy (“My hair. Was completely. Uneven.”); source fidelity is
working, but naturalness remains a spoken-quality consideration.

| Trace | Caller turns / model requests | Attempts | Caller→bot p50 / p90 | Request TTFT p50 / p90 |
|---|---:|---|---:|---:|
| Booking / approval loop | 8 / 17 | 8 answered | 1.128 / 2.353s, n=6 | .601 / .661s, n=17 |
| Complaint | 4 / 8 | 4 answered, 2 interrupted | .924 / 1.680s, n=3 | .582 / .623s, n=6 |

These small separate voice samples use server markers, not client playout. The
booking has three turns with repair evidence; complaint has none. Cache is reported
on one of 17 booking requests (3,106 tokens), and no explicit complaint cache
field is available. Largest captured tool returns: 1,594 and 394 characters.
`traces.json.gz` retains sanitized timing, usage, selected business-tool observations
and retrieval hashes; `trace-metrics.json` uses the existing trace measurement code.

The completed native comparison predates this two-line matcher change. Its frozen
sources, results and all failures remain in `../native-evidence/`; it was not rerun
or silently relabeled as evidence for the new phrase.
