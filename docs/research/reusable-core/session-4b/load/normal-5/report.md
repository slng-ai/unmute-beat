| Run | Call | Active after replay | Receipts | Outcome | Authorization | Delivery | Completion | Operational | Failed | Unknown |
|---|---|---|---|---|---|---|---|---|---|---|
| `jCBGSipW` | `GbULHRHP` `CAf657289f` | haircut 2026-10-02 09:00:00 | book BK0001 | unknown | unknown | unknown | unknown | pass (deterministic) | - | outcome: the final day, time and number asked for: not reviewed; authorization: lookup_customer@4: which arguments its read-back named (no proposal_args in its audit); authorization: lookup_customer@4: whether 'yes' accepted the proposal (not reviewed); authorization: book_appointment@7: which arguments its read-back named (no proposal_args in its audit); authorization: book_appointment@7: whether "yes that's perfect" accepted the proposal (not reviewed); delivery: lookup_customer@4: no evidence the proposal played in full (the runtime estimate is not); delivery: book_appointment@7: no evidence the proposal played in full (the runtime estimate is not); completion: what the agent said it had done: its closing was not reviewed |
| `jCBGSipW` | `BsdTQTsZ` `CAeeeb8603` | haircut 2026-10-02 09:00:00 | book BK0001 | unknown | unknown | unknown | unknown | pass (deterministic) | - | outcome: the final day, time and number asked for: not reviewed; authorization: lookup_customer@4: which arguments its read-back named (no proposal_args in its audit); authorization: lookup_customer@4: whether "yes that's correct" accepted the proposal (not reviewed); authorization: book_appointment@6: which arguments its read-back named (no proposal_args in its audit); authorization: book_appointment@6: whether "yes that's right" accepted the proposal (not reviewed); delivery: lookup_customer@4: no evidence the proposal played in full (the runtime estimate is not); delivery: book_appointment@6: no evidence the proposal played in full (the runtime estimate is not); completion: what the agent said it had done: its closing was not reviewed |
| `jCBGSipW` | `jEisR2g6` `CA0903cc44` | haircut 2026-10-02 09:00:00 | book BK0001 | unknown | unknown | unknown | unknown | pass (deterministic) | - | outcome: the final day, time and number asked for: not reviewed; authorization: lookup_customer@4: which arguments its read-back named (no proposal_args in its audit); authorization: lookup_customer@4: whether "yes that's correct" accepted the proposal (not reviewed); authorization: book_appointment@6: which arguments its read-back named (no proposal_args in its audit); authorization: book_appointment@6: whether "yes that's right friday the second at nine in the morning" accepted the proposal (not reviewed); delivery: lookup_customer@4: no evidence the proposal played in full (the runtime estimate is not); delivery: book_appointment@6: no evidence the proposal played in full (the runtime estimate is not); completion: what the agent said it had done: its closing was not reviewed |
| `jCBGSipW` | `nnGmXURQ` `CAd8969417` | haircut 2026-10-02 09:00:00 | book BK0001 | unknown | unknown | unknown | unknown | pass (deterministic) | - | outcome: the final day, time and number asked for: not reviewed; authorization: lookup_customer@4: which arguments its read-back named (no proposal_args in its audit); authorization: lookup_customer@4: whether "yes that's correct" accepted the proposal (not reviewed); authorization: book_appointment@7: which arguments its read-back named (no proposal_args in its audit); authorization: book_appointment@7: whether "yes that's right" accepted the proposal (not reviewed); delivery: lookup_customer@4: no evidence the proposal played in full (the runtime estimate is not); delivery: book_appointment@7: no evidence the proposal played in full (the runtime estimate is not); completion: what the agent said it had done: its closing was not reviewed |
| `jCBGSipW` | `cyYESUGZ` `CA8a6154a3` | haircut 2026-10-02 09:00:00 | book BK0001 | unknown | unknown | unknown | unknown | pass (deterministic) | - | outcome: the final day, time and number asked for: not reviewed; authorization: lookup_customer@3: which arguments its read-back named (no proposal_args in its audit); authorization: lookup_customer@3: whether "yes that's correct" accepted the proposal (not reviewed); authorization: book_appointment@5: which arguments its read-back named (no proposal_args in its audit); authorization: book_appointment@5: whether 'yes please' accepted the proposal (not reviewed); delivery: lookup_customer@3: no evidence the proposal played in full (the runtime estimate is not); delivery: book_appointment@5: no evidence the proposal played in full (the runtime estimate is not); completion: what the agent said it had done: its closing was not reviewed |

| Dimension | Pass | Fail | Unknown | Passes resting on an agent review |
|---|---|---|---|---|
| outcome | 0 | 0 | 5 | 0 |
| authorization | 0 | 0 | 5 | 0 |
| delivery | 0 | 0 | 5 | 0 |
| completion | 0 | 0 | 5 | 0 |
| operational | 5 | 0 | 0 | 0 |

Calls: 5. Every dimension passed: 0. Any dimension failed: 0. Evidence complete: 0 of 5.
Calls with exactly one active appointment after replay: 5. Receipts replayed as a repeat or a change of an earlier one are not counted as appointments; this is reconstructed from trace receipts, not checked against a backend snapshot.

## Diagnostics

| Call | Writes (label mismatch) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Span s | Recording s | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `GbULHRHP` | 2 (0) | 0 | 0 | 1 | 2 | 1.42 | 7 | 1.17 / 2.71 | 109 | 110 | - |
| `BsdTQTsZ` | 2 (0) | 0 | 1 | 1 | 2 | 1.78 | 3 | 1.03 / 2.66 | 107 | 107 | - |
| `jEisR2g6` | 2 (0) | 1 | 2 | 1 | 2 | 1.67 | 5 | 1.01 / 2.59 | 114 | 115 | 1 reply(s) spoke beside an operation |
| `nnGmXURQ` | 2 (0) | 0 | 0 | 1 | 2 | 1.73 | 5 | 1.04 / 2.42 | 108 | 108 | - |
| `cyYESUGZ` | 2 (0) | 0 | 0 | 1 | 2 | 2.00 | 4 | 1.15 / 3.32 | 109 | 110 | - |

Writes: 10, with no question about them on an earlier turn: 0 (a label check, not consent).
First booking BK0001 in every call that booked: True (expected with a fresh diary per call; not an isolation or contention test).
Model text beside an operation: 1 in 1 calls. Replies opening with a holding line: 3.
Caller turns with more than one holding line: 0.
Requests: 81 completed over 48 caller turns (1.69 per turn), 24 cancelled of 105 started.
Chains code could skip: {'collect > lookup_customer': 5, 'collect > book_appointment': 5}.
Endpointing: 21 runs answered a continuation piece; 11 runtime turns were cut into by the caller.
Turn latency: p50 1.14 s, p90 2.62 s, n=40.
Model request time: p50 0.64 s, p90 0.82 s.
First token to end of a reply that spoke: p50 0.11 s, p90 0.20 s, n=19.
Input tokens per call: 32702.
Time to first booking write: p50 94.38 s, p90 101.05 s.

Most common request chains per hop:
- 5 x `collect > hand_over > text`
- 5 x `collect > lookup_customer`
- 5 x `collect > book_appointment`
- 5 x `book_appointment`
- 5 x `goodbye`
- 4 x `lookup_customer > options > text`
- 3 x `text`
- 2 x `hand_over > text`
- 2 x `collect > text`
- 1 x `options > text`
- echo in `jEisR2g6`: lookup_customer: Let me have a look.
