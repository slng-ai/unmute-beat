| Run | Call | Active after replay | Receipts | Outcome | Authorization | Delivery | Completion | Operational | Failed | Unknown |
|---|---|---|---|---|---|---|---|---|---|---|
| `hzSdfMNn` | `8hDkLVRB` `CAcd4b196e` | none | none | unknown | pass (no write needed a yes) | pass (no proposal authorised a write) | unknown | unknown | - | outcome: what the caller asked for: no case expectation and no reviewed final request; completion: what the agent said it had done: its closing was not reviewed; operational: how the call ended: not measured (the trace span, 180 s, is not its length) |

| Dimension | Pass | Fail | Unknown | Passes resting on an agent review |
|---|---|---|---|---|
| outcome | 0 | 0 | 1 | 0 |
| authorization | 1 | 0 | 0 | 0 |
| delivery | 1 | 0 | 0 | 0 |
| completion | 0 | 0 | 1 | 0 |
| operational | 0 | 0 | 1 | 0 |

Calls: 1. Every dimension passed: 0. Any dimension failed: 0. Evidence complete: 0 of 1.
Calls with exactly one active appointment after replay: 0. Receipts replayed as a repeat or a change of an earlier one are not counted as appointments; this is reconstructed from trace receipts, not checked against a backend snapshot.

## Diagnostics

| Call | Writes (label mismatch) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Span s | Recording s | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `8hDkLVRB` | 0 (0) | 0 | 0 | 1 | 0 | 1.55 | 15 | 2.41 / 4.73 | 180 | - | - |

Writes: 0, with no question about them on an earlier turn: 0 (a label check, not consent).
First booking BK0001 in every call that booked: True (expected with a fresh diary per call; not an isolation or contention test).
Model text beside an operation: 0 in 0 calls. Replies opening with a holding line: 0.
Caller turns with more than one holding line: 0.
Requests: 17 completed over 11 caller turns (1.55 per turn), 15 cancelled of 32 started.
Chains code could skip: none.
Endpointing: 7 runs answered a continuation piece; 3 runtime turns were cut into by the caller.
Turn latency: p50 2.41 s, p90 4.73 s, n=10.
Model request time: p50 0.87 s, p90 1.16 s.
First token to end of a reply that spoke: p50 0.09 s, p90 0.31 s, n=4.
Input tokens per call: 30442.
Time to first booking write: p50 - s, p90 - s.

Most common request chains per hop:
- 5 x `collect`
- 3 x `collect > text`
- 1 x `collect > hand_over`
- 1 x `lookup_customer`
- 1 x `hand_over > collect`
