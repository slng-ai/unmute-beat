| Run | Call | Active after replay | Receipts | Outcome | Authorization | Delivery | Completion | Operational | Failed | Unknown |
|---|---|---|---|---|---|---|---|---|---|---|
| `CdR8tupv` | `ZiGrTDzY` `CA99804430` | haircut 2026-10-02 09:00:00 | book BK0001 | unknown | unknown | unknown | unknown | unknown | - | outcome: the final day, time and number asked for: not reviewed; authorization: lookup_customer@4: which arguments its read-back named (no proposal_args in its audit); authorization: lookup_customer@4: whether "Yes, that's correct." accepted the proposal (not reviewed); authorization: book_appointment@6: which arguments its read-back named (no proposal_args in its audit); authorization: book_appointment@6: whether "Yes, please. That's perfect." accepted the proposal (not reviewed); delivery: lookup_customer@4: no evidence the proposal played in full (the runtime estimate is not); delivery: book_appointment@6: no evidence the proposal played in full (the runtime estimate is not); completion: what the agent said it had done: its closing was not reviewed; operational: how the call ended: not measured (the trace span, 127 s, is not its length) |
| `CdR8tupv` | `NXBccLf5` `CAe5091ec7` | haircut 2026-10-02 09:00:00 | book BK0001 | unknown | unknown | unknown | unknown | pass (deterministic) | - | outcome: the final day, time and number asked for: not reviewed; authorization: lookup_customer@3: which arguments its read-back named (no proposal_args in its audit); authorization: lookup_customer@3: whether "Yes, that's correct." accepted the proposal (not reviewed); authorization: book_appointment@5: which arguments its read-back named (no proposal_args in its audit); authorization: book_appointment@5: whether "Yes, please. That's perfect." accepted the proposal (not reviewed); delivery: lookup_customer@3: no evidence the proposal played in full (the runtime estimate is not); delivery: book_appointment@5: no evidence the proposal played in full (the runtime estimate is not); completion: what the agent said it had done: its closing was not reviewed |
| `CdR8tupv` | `CSSVxduB` `CA4e63707e` | haircut 2026-10-02 09:00:00 | book BK0001 | unknown | unknown | unknown | unknown | pass (deterministic) | - | outcome: the final day, time and number asked for: not reviewed; authorization: lookup_customer@5: which arguments its read-back named (no proposal_args in its audit); authorization: lookup_customer@5: whether "Yes, that's correct." accepted the proposal (not reviewed); authorization: book_appointment@7: which arguments its read-back named (no proposal_args in its audit); authorization: book_appointment@7: whether "Yes, please. That's perfect." accepted the proposal (not reviewed); delivery: lookup_customer@5: no evidence the proposal played in full (the runtime estimate is not); delivery: book_appointment@7: no evidence the proposal played in full (the runtime estimate is not); completion: what the agent said it had done: its closing was not reviewed |
| `CdR8tupv` | `Skx5ZjD7` `CA6061e3c4` | haircut 2026-10-02 09:00:00 | book BK0001 | unknown | unknown | unknown | unknown | pass (deterministic) | - | outcome: the final day, time and number asked for: not reviewed; authorization: lookup_customer@3: which arguments its read-back named (no proposal_args in its audit); authorization: lookup_customer@3: whether "Yes, that's correct." accepted the proposal (not reviewed); authorization: book_appointment@5: which arguments its read-back named (no proposal_args in its audit); authorization: book_appointment@5: whether "Yes, please. That's perfect." accepted the proposal (not reviewed); delivery: lookup_customer@3: no evidence the proposal played in full (the runtime estimate is not); delivery: book_appointment@5: no evidence the proposal played in full (the runtime estimate is not); completion: what the agent said it had done: its closing was not reviewed |
| `Qprjd3ZJ` | `ArS5ZMnu` `CA11a137f1` | none | none | fail | unknown | unknown | unknown | unknown | outcome: 0 active appointment(s), 1 requested | authorization: lookup_customer@6: which arguments its read-back named (no proposal_args in its audit); authorization: lookup_customer@6: whether "Yes. That's right." accepted the proposal (not reviewed); delivery: lookup_customer@6: no evidence the proposal played in full (the runtime estimate is not); completion: what the agent said it had done: its closing was not reviewed; operational: ended by ceiling: inferred, not an observed event |
| `Qprjd3ZJ` | `MeSFmdkX` `CAa2429a90` | haircut 2026-09-26 09:00:00 | book BK0001 | unknown | unknown | unknown | unknown | pass (deterministic) | - | outcome: the final day, time and number asked for: not reviewed; authorization: lookup_customer@3: which arguments its read-back named (no proposal_args in its audit); authorization: lookup_customer@3: whether "Yes, that's correct." accepted the proposal (not reviewed); authorization: book_appointment@8: which arguments its read-back named (no proposal_args in its audit); authorization: book_appointment@8: whether "Yes, please. That's perfect." accepted the proposal (not reviewed); delivery: lookup_customer@3: no evidence the proposal played in full (the runtime estimate is not); delivery: book_appointment@8: no evidence the proposal played in full (the runtime estimate is not); completion: what the agent said it had done: its closing was not reviewed |
| `Qprjd3ZJ` | `BpraHnbw` `CA8e1389ae` | haircut 2026-09-26 09:00:00 | book BK0001 | unknown | unknown | unknown | unknown | unknown | - | outcome: the final day, time and number asked for: not reviewed; authorization: lookup_customer@4: which arguments its read-back named (no proposal_args in its audit); authorization: lookup_customer@4: whether "Yes, that's right." accepted the proposal (not reviewed); authorization: book_appointment@8: which arguments its read-back named (no proposal_args in its audit); authorization: book_appointment@8: whether "Yes, that's perfect." accepted the proposal (not reviewed); delivery: lookup_customer@4: no evidence the proposal played in full (the runtime estimate is not); delivery: book_appointment@8: no evidence the proposal played in full (the runtime estimate is not); completion: what the agent said it had done: its closing was not reviewed; operational: ended by caller: inferred, not an observed event |
| `Qprjd3ZJ` | `HJS5pYLK` `CAc932265c` | haircut 2026-09-26 09:30:00 | book BK0001 | unknown | unknown | unknown | unknown | unknown | - | outcome: the final day, time and number asked for: not reviewed; authorization: lookup_customer@3: which arguments its read-back named (no proposal_args in its audit); authorization: lookup_customer@3: whether "Yes. That's correct." accepted the proposal (not reviewed); authorization: book_appointment@6: which arguments its read-back named (no proposal_args in its audit); authorization: book_appointment@6: whether "Yes, that's perfect. Thank you." accepted the proposal (not reviewed); delivery: lookup_customer@3: no evidence the proposal played in full (the runtime estimate is not); delivery: book_appointment@6: no evidence the proposal played in full (the runtime estimate is not); completion: what the agent said it had done: its closing was not reviewed; operational: how the call ended: not measured (the trace span, 134 s, is not its length) |

| Dimension | Pass | Fail | Unknown | Passes resting on an agent review |
|---|---|---|---|---|
| outcome | 0 | 1 | 7 | 0 |
| authorization | 0 | 0 | 8 | 0 |
| delivery | 0 | 0 | 8 | 0 |
| completion | 0 | 0 | 8 | 0 |
| operational | 4 | 0 | 4 | 0 |

Calls: 8. Every dimension passed: 0. Any dimension failed: 1. Evidence complete: 0 of 8.
Calls with exactly one active appointment after replay: 7. Receipts replayed as a repeat or a change of an earlier one are not counted as appointments; this is reconstructed from trace receipts, not checked against a backend snapshot.

## Diagnostics

| Call | Writes (label mismatch) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Span s | Recording s | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `ZiGrTDzY` | 2 (0) | 0 | 0 | 1 | 0 | 1.75 | 0 | 1.75 / 1.98 | 127 | 127 | - |
| `NXBccLf5` | 2 (0) | 0 | 0 | 1 | 1 | 1.57 | 1 | 1.90 / 2.98 | 82 | 83 | - |
| `CSSVxduB` | 2 (0) | 0 | 0 | 1 | 0 | 1.33 | 8 | 1.66 / 2.10 | 145 | 146 | - |
| `Skx5ZjD7` | 2 (0) | 0 | 0 | 1 | 1 | 1.57 | 0 | 1.96 / 3.01 | 86 | 86 | - |
| `ArS5ZMnu` | 1 (0) | 0 | 0 | 1 | 0 | 1.43 | 1 | 1.74 / 2.06 | 180 | 181 | - |
| `MeSFmdkX` | 2 (0) | 0 | 0 | 1 | 0 | 1.50 | 2 | 1.85 / 2.18 | 160 | 160 | - |
| `BpraHnbw` | 2 (0) | 0 | 0 | 1 | 0 | 1.62 | 2 | 1.83 / 1.98 | 139 | 134 | - |
| `HJS5pYLK` | 2 (0) | 0 | 0 | 1 | 1 | 2.00 | 6 | 1.92 / 3.10 | 134 | 134 | - |

Writes: 15, with no question about them on an earlier turn: 0 (a label check, not consent).
First booking BK0001 in every call that booked: True (expected with a fresh diary per call; not an isolation or contention test).
Model text beside an operation: 0 in 0 calls. Replies opening with a holding line: 0.
Caller turns with more than one holding line: 0.
Requests: 134 completed over 84 caller turns (1.60 per turn), 20 cancelled of 154 started.
Chains code could skip: {'collect > book_appointment': 3}.
Endpointing: 24 runs answered a continuation piece; 23 runtime turns were cut into by the caller.
Turn latency: p50 1.84 s, p90 2.47 s, n=73.
Model request time: p50 0.73 s, p90 0.91 s.
First token to end of a reply that spoke: p50 0.13 s, p90 0.21 s, n=32.
Input tokens per call: 37003.
Time to first booking write: p50 122.32 s, p90 135.89 s.

Most common request chains per hop:
- 25 x `collect`
- 7 x `collect > hand_over > text`
- 7 x `lookup_customer > options > text`
- 7 x `collect > text`
- 6 x `goodbye`
- 4 x `book_appointment`
- 4 x `collect > options > text`
- 3 x `collect > book_appointment`
- 3 x `text`
- 1 x `collect > hand_over > hand_over > text`
- 1 x `hand_over > text`
- 1 x `lookup_customer > collect > options > text`
