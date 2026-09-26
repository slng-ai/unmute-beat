| Run | Call | Active after replay | Receipts | Outcome | Authorization | Delivery | Completion | Operational | Failed | Unknown |
|---|---|---|---|---|---|---|---|---|---|---|
| `SdkVkoJj` | `S3DKpHVp` `CA48477ad8` | none | none | unknown | pass (no write needed a yes) | pass (no proposal authorised a write) | unknown | unknown | - | outcome: what the caller asked for: no case expectation and no reviewed final request; completion: what the agent said it had done: its closing was not reviewed; operational: how the call ended: not measured (the trace span, 136 s, is not its length) |

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
| `S3DKpHVp` | 0 (0) | 0 | 0 | 1 | 0 | 1.14 | 3 | 2.32 / 3.20 | 136 | - | - |

Writes: 0, with no question about them on an earlier turn: 0 (a label check, not consent).
First booking BK0001 in every call that booked: True (expected with a fresh diary per call; not an isolation or contention test).
Model text beside an operation: 0 in 0 calls. Replies opening with a holding line: 0.
Caller turns with more than one holding line: 0.
Requests: 8 completed over 7 caller turns (1.14 per turn), 3 cancelled of 11 started.
Chains code could skip: none.
Endpointing: 3 runs answered a continuation piece; 0 runtime turns were cut into by the caller.
Turn latency: p50 2.32 s, p90 3.20 s, n=5.
Model request time: p50 0.75 s, p90 1.16 s.
First token to end of a reply that spoke: p50 0.09 s, p90 0.09 s, n=1.
Input tokens per call: 12022.
Time to first booking write: p50 - s, p90 - s.

Most common request chains per hop:
- 2 x `lookup_customer`
- 1 x `collect > hand_over`
- 1 x `collect > text`
- 1 x `collect`
- 1 x `goodbye`
