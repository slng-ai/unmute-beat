| Run | Call | Active after replay | Receipts | Outcome | Authorization | Delivery | Completion | Operational | Failed | Unknown |
|---|---|---|---|---|---|---|---|---|---|---|
| `GKAmHKaj` | `DKDwjz2o` `CAa680f44b` | haircut 2026-10-03 13:00:00 | book BK0003, move BK0003 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |
| `GKAmHKaj` | `DyBcsvVG` `CA65a4f545` | haircut 2026-10-01 16:00:00 | book BK0004 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |
| `GKAmHKaj` | `2WToptRM` `CA5afc43fc` | haircut 2026-09-30 15:00:00 | book BK0005 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |
| `GKAmHKaj` | `JT2aoh3h` `CAb2fc81b9` | haircut 2026-10-03 10:00:00 | book BK0006 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |
| `GKAmHKaj` | `U8gg4PGj` `CA5e4cbbfe` | haircut 2026-10-02 11:00:00 | book BK0007 | fail | fail | pass (deterministic) | pass (review:agent) | pass (deterministic) | outcome: 1 active appointment(s), 0 requested; authorization: book_appointment@5: "No, don't. Book that." did not accept the proposal | - |
| `GKAmHKaj` | `MZNx5hQo` `CA01f628ff` | none | none | unknown | pass (deterministic, review:agent) | pass (deterministic) | unknown | pass (deterministic) | - | outcome: cancel_appointment@5: cancel of BK0002, which this call never booked; completion: what was done: the replay is unknown |
| `GKAmHKaj` | `cCB5GjSE` `CAe2bba294` | none | none | unknown | pass (deterministic, review:agent) | pass (deterministic) | unknown | pass (deterministic) | - | outcome: move_appointment@5: move of BK0001, which this call never booked; completion: what was done: the replay is unknown |
| `GKAmHKaj` | `3USUpUk2` `CA2085869f` | blowout 2026-09-30 11:00:00 | book BK0008 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |
| `GKAmHKaj` | `LERNmDps` `CA90b99937` | haircut 2026-09-29 14:00:00 | book BK0009 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |
| `GKAmHKaj` | `j67whc8U` `CA220a99ce` | haircut 2026-09-29 10:00:00 | book BK0010 | pass (deterministic, review:agent) | pass (deterministic, review:agent) | pass (deterministic) | pass (review:agent) | pass (deterministic) | - | - |

| Dimension | Pass | Fail | Unknown | Passes resting on an agent review |
|---|---|---|---|---|
| outcome | 7 | 1 | 2 | 7 |
| authorization | 9 | 1 | 0 | 9 |
| delivery | 10 | 0 | 0 | 0 |
| completion | 8 | 0 | 2 | 8 |
| operational | 10 | 0 | 0 | 0 |

Calls: 10. Every dimension passed: 7. Any dimension failed: 1. Evidence complete: 8 of 10.
Calls with exactly one active appointment after replay: 8. Receipts replayed as a repeat or a change of an earlier one are not counted as appointments; this is reconstructed from trace receipts, not checked against a backend snapshot.

## Diagnostics

| Call | Writes (label mismatch) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Span s | Recording s | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `DKDwjz2o` | 3 (0) | 0 | 0 | 0 | 1 | 1.50 | 6 | 1.67 / 3.38 | 156 | 158 | first booking BK0003, not BK0001: calls share a diary |
| `DyBcsvVG` | 2 (0) | 0 | 0 | 0 | 0 | 1.44 | 1 | 2.67 / 3.38 | 131 | 134 | first booking BK0004, not BK0001: calls share a diary |
| `2WToptRM` | 2 (0) | 0 | 0 | 0 | 0 | 1.00 | 2 | 1.68 / 3.25 | 103 | 99 | first booking BK0005, not BK0001: calls share a diary |
| `JT2aoh3h` | 2 (0) | 0 | 0 | 0 | 0 | 1.25 | 4 | 1.70 / 3.80 | 92 | 95 | first booking BK0006, not BK0001: calls share a diary |
| `U8gg4PGj` | 2 (0) | 0 | 0 | 0 | 0 | 1.33 | 1 | 2.19 / 4.32 | 89 | 227 | first booking BK0007, not BK0001: calls share a diary |
| `MZNx5hQo` | 2 (0) | 0 | 0 | 0 | 0 | 1.14 | 5 | 2.72 / 4.08 | 92 | 94 | first booking BK0002, not BK0001: calls share a diary |
| `cCB5GjSE` | 2 (0) | 0 | 0 | 0 | 1 | 1.29 | 3 | 2.12 / 3.05 | 102 | 104 | - |
| `3USUpUk2` | 2 (0) | 0 | 0 | 0 | 0 | 1.25 | 6 | 1.93 / 3.84 | 117 | 112 | first booking BK0008, not BK0001: calls share a diary |
| `LERNmDps` | 2 (0) | 0 | 0 | 0 | 0 | 1.11 | 6 | 1.61 / 2.57 | 122 | 124 | first booking BK0009, not BK0001: calls share a diary |
| `j67whc8U` | 2 (0) | 0 | 0 | 0 | 0 | 1.33 | 0 | 1.37 / 2.94 | 79 | 81 | first booking BK0010, not BK0001: calls share a diary |

Writes: 21, with no question about them on an earlier turn: 0 (a label check, not consent).
First booking BK0001 in every call that booked: False (expected with a fresh diary per call; not an isolation or contention test).
Model text beside an operation: 0 in 0 calls. Replies opening with a holding line: 0.
Caller turns with more than one holding line: 0.
Requests: 99 completed over 78 caller turns (1.27 per turn), 34 cancelled of 133 started.
Chains code could skip: {'collect > move_appointment': 2}.
Endpointing: 25 runs answered a continuation piece; 11 runtime turns were cut into by the caller.
Turn latency: p50 1.92 s, p90 3.38 s, n=65.
Model request time: p50 0.94 s, p90 1.21 s.
First token to end of a reply that spoke: p50 0.27 s, p90 0.27 s, n=1.
Input tokens per call: 23413.
Time to first booking write: p50 78.36 s, p90 104.95 s.

Most common request chains per hop:
- 16 x `collect`
- 10 x `collect > hand_over`
- 9 x `goodbye`
- 8 x `book_appointment`
- 8 x `lookup_customer > wait`
- 3 x `book_appointment+collect`
- 2 x `lookup_customer > collect > collect`
- 2 x `move_appointment`
- 1 x `collect > wait`
- 1 x `collect > collect > move_appointment`
- 1 x `hand_over > text`
- 1 x `collect > hand_over > collect`
