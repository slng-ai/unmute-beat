| Run | Call | Active after replay | Receipts | Outcome | Authorization | Delivery | Completion | Operational | Failed | Unknown |
|---|---|---|---|---|---|---|---|---|---|---|
| `8Z7Qce4U` | `Tiv9sDHH` `CA635821bb` | haircut 2026-09-29 09:00:00 | book BK0005 | pass (deterministic, review:agent) | fail | pass (deterministic) | pass (review:agent) | pass (deterministic) | authorization: lookup_customer@3: "no it's plus three four six seven one five one zero one zero two" did not accept the proposal | - |
| `8Z7Qce4U` | `P7PVUcuW` `CA6e9b5231` | haircut 2026-09-30 09:00:00 | book BK0002 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |
| `8Z7Qce4U` | `MUAJTpYK` `CAaed8621a` | haircut 2026-09-28 09:00:00 | book BK0004 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |
| `8Z7Qce4U` | `cxjjBJ8e` `CA8c170a81` | haircut 2026-10-01 09:00:00 | book BK0003 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |
| `8Z7Qce4U` | `6sLz5TRa` `CAa8e2f9be` | haircut 2026-10-02 09:00:00 | book BK0001 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |

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
| `Tiv9sDHH` | 2 (0) | 0 | 0 | 1 | 1 | 1.70 | 4 | 2.28 / 4.38 | 113 | - | first booking BK0005, not BK0001: calls share a diary |
| `P7PVUcuW` | 2 (0) | 0 | 0 | 1 | 0 | 1.25 | 2 | 2.83 / 4.12 | 103 | - | first booking BK0002, not BK0001: calls share a diary |
| `MUAJTpYK` | 2 (0) | 0 | 0 | 1 | 0 | 1.33 | 1 | 2.84 / 4.32 | 107 | - | first booking BK0004, not BK0001: calls share a diary |
| `cxjjBJ8e` | 2 (0) | 0 | 0 | 1 | 0 | 1.36 | 5 | 2.00 / 4.38 | 105 | - | first booking BK0003, not BK0001: calls share a diary |
| `6sLz5TRa` | 2 (0) | 0 | 0 | 1 | 0 | 1.38 | 1 | 1.67 / 3.45 | 90 | - | - |

Writes: 10, with no question about them on an earlier turn: 0 (a label check, not consent).
First booking BK0001 in every call that booked: False (expected with a fresh diary per call; not an isolation or contention test).
Model text beside an operation: 0 in 0 calls. Replies opening with a holding line: 0.
Caller turns with more than one holding line: 0.
Requests: 65 completed over 46 caller turns (1.41 per turn), 13 cancelled of 78 started.
Chains code could skip: {'collect > lookup_customer': 1}.
Endpointing: 18 runs answered a continuation piece; 15 runtime turns were cut into by the caller.
Turn latency: p50 2.31 s, p90 4.23 s, n=38.
Model request time: p50 0.79 s, p90 0.97 s.
First token to end of a reply that spoke: p50 0.16 s, p90 0.24 s, n=18.
Input tokens per call: 30109.
Time to first booking write: p50 83.46 s, p90 89.07 s.

Most common request chains per hop:
- 12 x `text`
- 10 x `collect`
- 5 x `collect > hand_over`
- 5 x `goodbye`
- 4 x `lookup_customer > options > text`
- 4 x `book_appointment`
- 1 x `collect+hand_over > collect > lookup_customer > collect > options > text`
