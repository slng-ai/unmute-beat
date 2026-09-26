| Run | Call | Active after replay | Receipts | Outcome | Authorization | Delivery | Completion | Operational | Failed | Unknown |
|---|---|---|---|---|---|---|---|---|---|---|
| `c77fpUwG` | `EAE4k85A` `CA2389c9a3` | haircut 2026-09-29 09:00:00 | book BK0003 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |
| `c77fpUwG` | `9YiSNJCK` `CAfed4b2d4` | haircut 2026-09-30 09:00:00 | book BK0001 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |
| `c77fpUwG` | `VhiVPLRo` `CAfff3d5b3` | haircut 2026-09-28 09:00:00 | book BK0005 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |
| `c77fpUwG` | `PdxnRie2` `CAc247daba` | haircut 2026-10-01 09:00:00 | book BK0002 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |
| `c77fpUwG` | `amAP5b4A` `CA5ed3d469` | haircut 2026-10-02 09:00:00 | book BK0004 | pass (deterministic, review:agent) | fail | pass (deterministic) | pass (review:agent) | pass (deterministic) | authorization: lookup_customer@3: "no it's plus three four six seven one five one zero one zero five" did not accept the proposal | - |

| Dimension | Pass | Fail | Unknown | Passes resting on an agent review |
|---|---|---|---|---|
| outcome | 5 | 0 | 0 | 5 |
| authorization | 4 | 1 | 0 | 4 |
| delivery | 5 | 0 | 0 | 0 |
| completion | 5 | 0 | 0 | 5 |
| operational | 5 | 0 | 0 | 0 |

Calls: 5. Every dimension passed: 4. Any dimension failed: 1. Evidence complete: 5 of 5.
Calls with exactly one active appointment after replay: 5. Receipts replayed as a repeat or a change of an earlier one are not counted as appointments; this is reconstructed from trace receipts, not checked against a backend snapshot.

## Diagnostics

| Call | Writes (label mismatch) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Span s | Recording s | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `EAE4k85A` | 2 (0) | 0 | 0 | 1 | 0 | 1.33 | 1 | 2.25 / 4.17 | 94 | - | first booking BK0003, not BK0001: calls share a diary |
| `9YiSNJCK` | 2 (0) | 0 | 0 | 1 | 0 | 1.29 | 4 | 1.91 / 4.15 | 93 | - | - |
| `VhiVPLRo` | 2 (0) | 0 | 0 | 1 | 0 | 1.25 | 1 | 3.04 / 4.22 | 101 | - | first booking BK0005, not BK0001: calls share a diary |
| `PdxnRie2` | 2 (0) | 0 | 0 | 1 | 0 | 1.10 | 3 | 2.09 / 3.33 | 103 | - | first booking BK0002, not BK0001: calls share a diary |
| `amAP5b4A` | 2 (0) | 0 | 0 | 1 | 0 | 1.38 | 2 | 2.51 / 4.03 | 105 | - | first booking BK0004, not BK0001: calls share a diary |

Writes: 10, with no question about them on an earlier turn: 0 (a label check, not consent).
First booking BK0001 in every call that booked: False (expected with a fresh diary per call; not an isolation or contention test).
Model text beside an operation: 0 in 0 calls. Replies opening with a holding line: 0.
Caller turns with more than one holding line: 0.
Requests: 53 completed over 42 caller turns (1.26 per turn), 11 cancelled of 64 started.
Chains code could skip: none.
Endpointing: 16 runs answered a continuation piece; 12 runtime turns were cut into by the caller.
Turn latency: p50 2.46 s, p90 4.15 s, n=33.
Model request time: p50 0.82 s, p90 1.01 s.
First token to end of a reply that spoke: p50 0.20 s, p90 0.24 s, n=10.
Input tokens per call: 24136.
Time to first booking write: p50 76.35 s, p90 84.43 s.

Most common request chains per hop:
- 10 x `collect`
- 7 x `book_appointment`
- 5 x `collect > hand_over`
- 5 x `text`
- 5 x `goodbye`
- 4 x `lookup_customer > options > text`
- 1 x `lookup_customer > collect > options > text`
