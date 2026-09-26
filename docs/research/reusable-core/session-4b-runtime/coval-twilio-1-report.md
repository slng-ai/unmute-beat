| Run | Call | Active after replay | Receipts | Outcome | Authorization | Delivery | Completion | Operational | Failed | Unknown |
|---|---|---|---|---|---|---|---|---|---|---|
| `FzMcHyvp` | `dTJmpxEY` `CA2f57eed1` | haircut 2026-10-03 09:00:00 | book BK0001 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |

| Dimension | Pass | Fail | Unknown | Passes resting on an agent review |
|---|---|---|---|---|
| outcome | 1 | 0 | 0 | 1 |
| authorization | 1 | 0 | 0 | 1 |
| delivery | 1 | 0 | 0 | 0 |
| completion | 1 | 0 | 0 | 1 |
| operational | 1 | 0 | 0 | 0 |

Calls: 1. Every dimension passed: 1. Any dimension failed: 0. Evidence complete: 1 of 1.
Calls with exactly one active appointment after replay: 1. Receipts replayed as a repeat or a change of an earlier one are not counted as appointments; this is reconstructed from trace receipts, not checked against a backend snapshot.

## Diagnostics

| Call | Writes (label mismatch) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Span s | Recording s | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `dTJmpxEY` | 2 (0) | 0 | 0 | 1 | 0 | 1.62 | 2 | 2.36 / 3.54 | 149 | - | - |

Writes: 2, with no question about them on an earlier turn: 0 (a label check, not consent).
First booking BK0001 in every call that booked: True (expected with a fresh diary per call; not an isolation or contention test).
Model text beside an operation: 0 in 0 calls. Replies opening with a holding line: 0.
Caller turns with more than one holding line: 0.
Requests: 21 completed over 13 caller turns (1.62 per turn), 2 cancelled of 23 started.
Chains code could skip: none.
Endpointing: 4 runs answered a continuation piece; 3 runtime turns were cut into by the caller.
Turn latency: p50 2.36 s, p90 3.54 s, n=11.
Model request time: p50 0.83 s, p90 1.08 s.
First token to end of a reply that spoke: p50 0.11 s, p90 0.14 s, n=5.
Input tokens per call: 50974.
Time to first booking write: p50 129.70 s, p90 129.70 s.

Most common request chains per hop:
- 3 x `collect`
- 3 x `book_appointment`
- 2 x `collect > options > text`
- 1 x `text`
- 1 x `collect > hand_over`
- 1 x `collect > text`
- 1 x `lookup_customer`
- 1 x `lookup_customer > text`
- 1 x `goodbye`
