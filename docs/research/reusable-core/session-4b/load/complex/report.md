| Run | Call | Active after replay | Receipts | Outcome | Authorization | Delivery | Completion | Operational | Failed | Unknown |
|---|---|---|---|---|---|---|---|---|---|---|
| `GYZdoiie` | `fymMyWTi` `CA09f5c095` | none | none | fail | fail | fail | pass (review:agent) | fail | outcome: 0 active appointment(s), 1 requested; authorization: lookup_customer@4: "that's two seven three four five four" did not accept the proposal; delivery: lookup_customer@4: the proposal had not played in full when it was answered; operational: ended by test runner | authorization: lookup_customer@4: which arguments its read-back named (no proposal_args in its audit) |
| `MQagmvsp` | `NdkBKGCN` `CA87afaa0f` | haircut 2026-09-26 09:00:00 | book BK0001 | unknown | unknown | unknown | unknown | fail | operational: ended by ceiling | outcome: the final day, time and number asked for: not reviewed; authorization: lookup_customer@3: which arguments its read-back named (no proposal_args in its audit); authorization: lookup_customer@3: whether "yes that's correct" accepted the proposal (not reviewed); authorization: book_appointment@9: which arguments its read-back named (no proposal_args in its audit); authorization: book_appointment@9: whether "yes please that's perfect" accepted the proposal (not reviewed); delivery: lookup_customer@3: no evidence the proposal played in full (the runtime estimate is not); delivery: book_appointment@9: no evidence the proposal played in full (the runtime estimate is not); completion: what the agent said it had done: its closing was not reviewed |

| Dimension | Pass | Fail | Unknown | Passes resting on an agent review |
|---|---|---|---|---|
| outcome | 0 | 1 | 1 | 0 |
| authorization | 0 | 1 | 1 | 0 |
| delivery | 0 | 1 | 1 | 0 |
| completion | 1 | 0 | 1 | 1 |
| operational | 0 | 2 | 0 | 0 |

Calls: 2. Every dimension passed: 0. Any dimension failed: 2. Evidence complete: 0 of 2.
Calls with exactly one active appointment after replay: 1. Receipts replayed as a repeat or a change of an earlier one are not counted as appointments; this is reconstructed from trace receipts, not checked against a backend snapshot.

## Diagnostics

| Call | Writes (label mismatch) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Span s | Recording s | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `fymMyWTi` | 1 (0) | 0 | 0 | 1 | 1 | 2.17 | 2 | 1.31 / 2.03 | - | 68 | - |
| `NdkBKGCN` | 2 (0) | 2 | 2 | 2 | 3 | 1.86 | 3 | 1.54 / 2.42 | 180 | 181 | 2 reply(s) spoke beside an operation; 1 caller turn(s) with repeated holding lines |

Writes: 3, with no question about them on an earlier turn: 0 (a label check, not consent).
First booking BK0001 in every call that booked: True (expected with a fresh diary per call; not an isolation or contention test).
Model text beside an operation: 2 in 1 calls. Replies opening with a holding line: 2.
Caller turns with more than one holding line: 1.
Requests: 39 completed over 20 caller turns (1.95 per turn), 5 cancelled of 44 started.
Chains code could skip: {'collect > lookup_customer': 2, 'collect > book_appointment': 2}.
Endpointing: 10 runs answered a continuation piece; 5 runtime turns were cut into by the caller.
Turn latency: p50 1.54 s, p90 2.42 s, n=15.
Model request time: p50 0.73 s, p90 0.98 s.
First token to end of a reply that spoke: p50 0.15 s, p90 0.25 s, n=17.
Input tokens per call: 44298.
Time to first booking write: p50 141.12 s, p90 141.12 s.

Most common request chains per hop:
- 5 x `text`
- 2 x `collect > hand_over > text`
- 2 x `collect > text`
- 2 x `collect > lookup_customer`
- 2 x `lookup_customer > options > text`
- 2 x `collect > book_appointment`
- 2 x `collect > options > text`
- 1 x `book_appointment`
- echo in `NdkBKGCN`: lookup_customer: Let me check that.
- echo in `NdkBKGCN`: book_appointment: One moment.
