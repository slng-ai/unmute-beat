| Run | Call | Active after replay | Receipts | Outcome | Authorization | Delivery | Completion | Operational | Failed | Unknown |
|---|---|---|---|---|---|---|---|---|---|---|
| `DFUKKk8C` | `DE4RbL4J` `CA46c31c4c` | haircut 2026-09-30 14:00:00 | book BK0001, move BK0001 | unknown | unknown | pass (deterministic) | unknown | unknown | - | outcome: what the caller asked for: no case expectation and no reviewed final request; authorization: lookup_customer@3: whether "yes that's correct" accepted the proposal (not reviewed); authorization: book_appointment@9: whether 'yes please' accepted the proposal (not reviewed); authorization: move_appointment@13: whether 'yes please move it to wednesday at two in the afternoon' accepted the proposal (not reviewed); completion: what the agent said it had done: its closing was not reviewed; operational: how the call ended: not measured (the trace span, 204 s, is not its length) |

| Dimension | Pass | Fail | Unknown | Passes resting on an agent review |
|---|---|---|---|---|
| outcome | 0 | 0 | 1 | 0 |
| authorization | 0 | 0 | 1 | 0 |
| delivery | 1 | 0 | 0 | 0 |
| completion | 0 | 0 | 1 | 0 |
| operational | 0 | 0 | 1 | 0 |

Calls: 1. Every dimension passed: 0. Any dimension failed: 0. Evidence complete: 0 of 1.
Calls with exactly one active appointment after replay: 1. Receipts replayed as a repeat or a change of an earlier one are not counted as appointments; this is reconstructed from trace receipts, not checked against a backend snapshot.

## Diagnostics

| Call | Writes (label mismatch) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Span s | Recording s | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `DE4RbL4J` | 3 (0) | 0 | 0 | 0 | 1 | 1.47 | 3 | 3.01 / 4.49 | 204 | - | - |

Writes: 3, with no question about them on an earlier turn: 0 (a label check, not consent).
First booking BK0001 in every call that booked: True (expected with a fresh diary per call; not an isolation or contention test).
Model text beside an operation: 0 in 0 calls. Replies opening with a holding line: 0.
Caller turns with more than one holding line: 0.
Requests: 22 completed over 15 caller turns (1.47 per turn), 3 cancelled of 25 started.
Chains code could skip: {'collect > move_appointment': 1}.
Endpointing: 3 runs answered a continuation piece; 2 runtime turns were cut into by the caller.
Turn latency: p50 3.01 s, p90 4.49 s, n=13.
Model request time: p50 0.88 s, p90 1.05 s.
First token to end of a reply that spoke: p50 0.15 s, p90 0.15 s, n=2.
Input tokens per call: 61435.
Time to first booking write: p50 124.57 s, p90 124.57 s.

Most common request chains per hop:
- 3 x `collect`
- 1 x `collect > hand_over`
- 1 x `lookup_customer > wait`
- 1 x `wait`
- 1 x `hand_over > text`
- 1 x `text`
- 1 x `collect > hand_over > wait`
- 1 x `book_appointment`
- 1 x `say_again`
- 1 x `collect > wait`
- 1 x `collect > move_appointment`
- 1 x `move_appointment`
