| Run | Call | Active after replay | Receipts | Outcome | Authorization | Delivery | Completion | Operational | Failed | Unknown |
|---|---|---|---|---|---|---|---|---|---|---|
| `C4jjBivq` | `8ggjwDXY` `CA637b7620` | haircut 2026-10-03 09:00:00 | book BK0004 | unknown | unknown | pass (deterministic) | unknown | unknown | - | outcome: what the caller asked for: no case expectation and no reviewed final request; authorization: lookup_customer@5: whether "yes that's right" accepted the proposal (not reviewed); authorization: book_appointment@11: whether "yes please saturday the third at nine am that's great yes please nine am on saturday the third" accepted the proposal (not reviewed); completion: what the agent said it had done: its closing was not reviewed; operational: how the call ended: not measured (the trace span, 178 s, is not its length) |
| `C4jjBivq` | `J9bFqmkh` `CAe67c8fcc` | haircut 2026-10-02 09:00:00 | book BK0003 | unknown | unknown | pass (deterministic) | unknown | unknown | - | outcome: what the caller asked for: no case expectation and no reviewed final request; authorization: lookup_customer@4: whether "yes that's right" accepted the proposal (not reviewed); authorization: book_appointment@9: whether "yes please that's exactly what i want thank you" accepted the proposal (not reviewed); completion: what the agent said it had done: its closing was not reviewed; operational: how the call ended: not measured (the trace span, 174 s, is not its length) |
| `C4jjBivq` | `n4pRSiBp` `CA82dcb4d3` | haircut 2026-10-01 09:00:00 | book BK0002 | unknown | unknown | pass (deterministic) | unknown | unknown | - | outcome: what the caller asked for: no case expectation and no reviewed final request; authorization: lookup_customer@5: whether "yes that's correct" accepted the proposal (not reviewed); authorization: book_appointment@10: whether "yes please that's perfect" accepted the proposal (not reviewed); completion: what the agent said it had done: its closing was not reviewed; operational: how the call ended: not measured (the trace span, 168 s, is not its length) |
| `C4jjBivq` | `SqB3qpaH` `CA2d066cee` | haircut 2026-09-30 09:00:00 | book BK0001 | unknown | unknown | pass (deterministic) | unknown | unknown | - | outcome: what the caller asked for: no case expectation and no reviewed final request; authorization: lookup_customer@4: whether "yes that's right" accepted the proposal (not reviewed); authorization: book_appointment@9: whether 'yes please book it for wednesday the thirtieth at nine am' accepted the proposal (not reviewed); completion: what the agent said it had done: its closing was not reviewed; operational: how the call ended: not measured (the trace span, 142 s, is not its length) |
| `C4jjBivq` | `9ut6yboS` `CA8b041777` | haircut 2026-09-29 09:00:00 | book BK0005 | unknown | unknown | pass (deterministic) | unknown | unknown | - | outcome: what the caller asked for: no case expectation and no reviewed final request; authorization: lookup_customer@6: whether "yes that's correct" accepted the proposal (not reviewed); authorization: book_appointment@11: whether "yes please that's perfect" accepted the proposal (not reviewed); completion: what the agent said it had done: its closing was not reviewed; operational: how the call ended: not measured (the trace span, 209 s, is not its length) |

| Dimension | Pass | Fail | Unknown | Passes resting on an agent review |
|---|---|---|---|---|
| outcome | 0 | 0 | 5 | 0 |
| authorization | 0 | 0 | 5 | 0 |
| delivery | 5 | 0 | 0 | 0 |
| completion | 0 | 0 | 5 | 0 |
| operational | 0 | 0 | 5 | 0 |

Calls: 5. Every dimension passed: 0. Any dimension failed: 0. Evidence complete: 0 of 5.
Calls with exactly one active appointment after replay: 5. Receipts replayed as a repeat or a change of an earlier one are not counted as appointments; this is reconstructed from trace receipts, not checked against a backend snapshot.

## Diagnostics

| Call | Writes (label mismatch) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Span s | Recording s | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `8ggjwDXY` | 2 (0) | 0 | 0 | 1 | 0 | 1.71 | 10 | 2.32 / 3.33 | 178 | - | first booking BK0004, not BK0001: calls share a diary |
| `J9bFqmkh` | 2 (0) | 0 | 0 | 1 | 0 | 1.47 | 7 | 2.52 / 3.18 | 174 | - | first booking BK0003, not BK0001: calls share a diary |
| `n4pRSiBp` | 2 (0) | 0 | 0 | 1 | 1 | 1.25 | 4 | 2.05 / 3.24 | 168 | - | first booking BK0002, not BK0001: calls share a diary |
| `SqB3qpaH` | 2 (0) | 2 | 0 | 1 | 0 | 1.47 | 2 | 2.16 / 2.71 | 142 | - | 2 reply(s) spoke beside an operation |
| `9ut6yboS` | 2 (0) | 0 | 0 | 1 | 1 | 1.40 | 7 | 2.89 / 4.66 | 209 | - | first booking BK0005, not BK0001: calls share a diary |

Writes: 10, with no question about them on an earlier turn: 0 (a label check, not consent).
First booking BK0001 in every call that booked: False (expected with a fresh diary per call; not an isolation or contention test).
Model text beside an operation: 2 in 1 calls. Replies opening with a holding line: 0.
Caller turns with more than one holding line: 0.
Requests: 116 completed over 80 caller turns (1.45 per turn), 30 cancelled of 146 started.
Chains code could skip: {'collect > book_appointment': 2}.
Endpointing: 43 runs answered a continuation piece; 31 runtime turns were cut into by the caller.
Turn latency: p50 2.34 s, p90 3.33 s, n=69.
Model request time: p50 0.78 s, p90 0.92 s.
First token to end of a reply that spoke: p50 0.17 s, p90 0.26 s, n=43.
Input tokens per call: 61830.
Time to first booking write: p50 155.27 s, p90 182.27 s.

Most common request chains per hop:
- 14 x `collect`
- 13 x `collect > text`
- 12 x `text`
- 10 x `book_appointment`
- 5 x `collect > hand_over`
- 5 x `lookup_customer > options > text`
- 5 x `collect > options > text`
- 4 x `goodbye`
- 3 x `lookup_customer`
- 2 x `collect > book_appointment`
- echo in `SqB3qpaH`: book_appointment: Shall I book a haircut, Wednesday the 30th at 9:00 AM?
- echo in `SqB3qpaH`: book_appointment: Shall I book a haircut, Wednesday the 30th at 9:00 AM?
