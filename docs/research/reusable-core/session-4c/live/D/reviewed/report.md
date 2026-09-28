| Run | Call | Active after replay | Receipts | Outcome | Authorization | Delivery | Completion | Operational | Failed | Unknown |
|---|---|---|---|---|---|---|---|---|---|---|
| `UMuV6YLm` | `oJFVME6p` `CA919ed2d0` | haircut 2026-10-03 09:00:00 | book BK0003 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | unknown | pass (deterministic) | - | completion: what the agent said it had done: its closing was not reviewed |
| `UMuV6YLm` | `HTt7eSPj` `CAb645d6b2` | haircut 2026-10-02 09:00:00 | book BK0001 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |
| `UMuV6YLm` | `8EiDGNHq` `CA840b88a5` | haircut 2026-10-01 09:00:00 | book BK0004 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |
| `UMuV6YLm` | `f5wvLtzB` `CA58d31a80` | haircut 2026-09-30 09:00:00 | book BK0002 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |
| `UMuV6YLm` | `bTwjazHq` `CA1d28f23e` | haircut 2026-09-29 09:00:00 | book BK0005 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |

| Dimension | Pass | Fail | Unknown | Passes resting on an agent review |
|---|---|---|---|---|
| outcome | 5 | 0 | 0 | 5 |
| authorization | 5 | 0 | 0 | 5 |
| delivery | 5 | 0 | 0 | 0 |
| completion | 4 | 0 | 1 | 4 |
| operational | 5 | 0 | 0 | 0 |

Calls: 5. Every dimension passed: 4. Any dimension failed: 0. Evidence complete: 4 of 5.
Calls with exactly one active appointment after replay: 5. Receipts replayed as a repeat or a change of an earlier one are not counted as appointments; this is reconstructed from trace receipts, not checked against a backend snapshot.

## Diagnostics

| Call | Writes (label mismatch) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Span s | Recording s | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `oJFVME6p` | 2 (0) | 0 | 0 | 1 | 1 | 1.31 | 6 | 2.11 / 2.56 | 149 | - | first booking BK0003, not BK0001: calls share a diary |
| `HTt7eSPj` | 2 (0) | 0 | 0 | 1 | 0 | 1.75 | 9 | 1.80 / 3.05 | 139 | - | - |
| `8EiDGNHq` | 2 (0) | 0 | 0 | 1 | 0 | 1.38 | 4 | 2.05 / 3.46 | 169 | - | first booking BK0004, not BK0001: calls share a diary |
| `f5wvLtzB` | 2 (0) | 0 | 0 | 1 | 1 | 1.50 | 6 | 2.35 / 3.05 | 156 | - | first booking BK0002, not BK0001: calls share a diary |
| `bTwjazHq` | 2 (0) | 0 | 0 | 1 | 0 | 1.43 | 13 | 1.91 / 3.68 | 224 | - | first booking BK0005, not BK0001: calls share a diary |

Writes: 10, with no question about them on an earlier turn: 0 (a label check, not consent).
First booking BK0001 in every call that booked: False (expected with a fresh diary per call; not an isolation or contention test).
Model text beside an operation: 0 in 0 calls. Replies opening with a holding line: 0.
Caller turns with more than one holding line: 0.
Requests: 115 completed over 79 caller turns (1.46 per turn), 38 cancelled of 153 started.
Chains code could skip: {'collect > book_appointment': 2}.
Endpointing: 50 runs answered a continuation piece; 29 runtime turns were cut into by the caller.
Turn latency: p50 2.08 s, p90 3.32 s, n=68.
Model request time: p50 0.84 s, p90 1.05 s.
First token to end of a reply that spoke: p50 0.18 s, p90 0.25 s, n=40.
Input tokens per call: 59206.
Time to first booking write: p50 139.78 s, p90 199.55 s.

Most common request chains per hop:
- 18 x `text`
- 16 x `collect`
- 7 x `collect > text`
- 7 x `book_appointment`
- 6 x `lookup_customer`
- 5 x `collect > hand_over`
- 5 x `lookup_customer > options > text`
- 5 x `collect > options > text`
- 3 x `goodbye`
- 2 x `collect > book_appointment`
- 1 x `hand_over > text`
- 1 x `hand_over`
