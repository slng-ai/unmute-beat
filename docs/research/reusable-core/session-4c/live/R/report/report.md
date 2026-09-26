| Run | Call | Active after replay | Receipts | Outcome | Authorization | Delivery | Completion | Operational | Failed | Unknown |
|---|---|---|---|---|---|---|---|---|---|---|
| `gVH4ML72` | `m5HJ2vFh` `CA6c887141` | haircut 2026-09-29 09:00:00 | book BK0005 | unknown | unknown | pass (deterministic) | unknown | unknown | - | outcome: what the caller asked for: no case expectation and no reviewed final request; authorization: lookup_customer@5: whether "yes that's right" accepted the proposal (not reviewed); authorization: book_appointment@8: whether "yes please that's perfect" accepted the proposal (not reviewed); completion: what the agent said it had done: its closing was not reviewed; operational: how the call ended: not measured (the trace span, 150 s, is not its length) |
| `gVH4ML72` | `EMXpSVYa` `CA666dd414` | haircut 2026-10-01 09:00:00 | book BK0001 | unknown | unknown | pass (deterministic) | unknown | unknown | - | outcome: what the caller asked for: no case expectation and no reviewed final request; authorization: lookup_customer@3: whether "no it's plus three four six seven one five three zero three zero four" accepted the proposal (not reviewed); authorization: book_appointment@5: whether "yes that's perfect" accepted the proposal (not reviewed); completion: what the agent said it had done: its closing was not reviewed; operational: how the call ended: not measured (the trace span, 99 s, is not its length) |
| `gVH4ML72` | `FjUeifmx` `CAe4704f57` | haircut 2026-10-02 09:00:00 | book BK0004 | unknown | unknown | pass (deterministic) | unknown | unknown | - | outcome: what the caller asked for: no case expectation and no reviewed final request; authorization: lookup_customer@4: whether "yes that's right" accepted the proposal (not reviewed); authorization: book_appointment@7: whether "yes please that's perfect" accepted the proposal (not reviewed); completion: what the agent said it had done: its closing was not reviewed; operational: how the call ended: not measured (the trace span, 134 s, is not its length) |
| `gVH4ML72` | `QPwennqo` `CAaced8a8b` | haircut 2026-09-30 09:00:00 | book BK0002 | unknown | unknown | pass (deterministic) | unknown | unknown | - | outcome: what the caller asked for: no case expectation and no reviewed final request; authorization: lookup_customer@3: whether "no it's plus three four six seven one five three zero three zero three" accepted the proposal (not reviewed); authorization: book_appointment@5: whether "yes that's perfect please book it for me" accepted the proposal (not reviewed); completion: what the agent said it had done: its closing was not reviewed; operational: how the call ended: not measured (the trace span, 102 s, is not its length) |
| `gVH4ML72` | `DASdb96w` `CA6a5374a6` | haircut 2026-09-28 09:00:00 | book BK0003 | unknown | unknown | pass (deterministic) | unknown | unknown | - | outcome: what the caller asked for: no case expectation and no reviewed final request; authorization: lookup_customer@3: whether "no it's plus three four six seven one five three zero three zero one" accepted the proposal (not reviewed); authorization: book_appointment@6: whether "yes please that's perfect" accepted the proposal (not reviewed); completion: what the agent said it had done: its closing was not reviewed; operational: how the call ended: not measured (the trace span, 110 s, is not its length) |

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
| `m5HJ2vFh` | 2 (0) | 0 | 0 | 1 | 0 | 1.30 | 7 | 2.94 / 4.21 | 150 | - | first booking BK0005, not BK0001: calls share a diary |
| `EMXpSVYa` | 2 (0) | 0 | 0 | 1 | 0 | 1.29 | 1 | 2.75 / 3.30 | 99 | - | - |
| `FjUeifmx` | 2 (0) | 0 | 0 | 1 | 0 | 1.45 | 1 | 2.63 / 4.37 | 134 | - | first booking BK0004, not BK0001: calls share a diary |
| `QPwennqo` | 2 (0) | 0 | 0 | 1 | 0 | 1.57 | 5 | 2.19 / 4.30 | 102 | - | first booking BK0002, not BK0001: calls share a diary |
| `DASdb96w` | 2 (0) | 0 | 0 | 1 | 0 | 1.33 | 2 | 2.70 / 4.09 | 110 | - | first booking BK0003, not BK0001: calls share a diary |

Writes: 10, with no question about them on an earlier turn: 0 (a label check, not consent).
First booking BK0001 in every call that booked: False (expected with a fresh diary per call; not an isolation or contention test).
Model text beside an operation: 0 in 0 calls. Replies opening with a holding line: 0.
Caller turns with more than one holding line: 0.
Requests: 61 completed over 44 caller turns (1.39 per turn), 16 cancelled of 77 started.
Chains code could skip: none.
Endpointing: 15 runs answered a continuation piece; 9 runtime turns were cut into by the caller.
Turn latency: p50 2.70 s, p90 4.15 s, n=33.
Model request time: p50 0.94 s, p90 1.08 s.
First token to end of a reply that spoke: p50 0.19 s, p90 0.25 s, n=13.
Input tokens per call: 27586.
Time to first booking write: p50 88.82 s, p90 126.99 s.

Most common request chains per hop:
- 12 x `collect`
- 7 x `text`
- 5 x `collect > hand_over`
- 5 x `book_appointment`
- 5 x `goodbye`
- 2 x `lookup_customer > options > text`
- 1 x `collect > text`
- 1 x `collect > options > text`
- 1 x `lookup_customer`
- 1 x `hand_over > collect > options > text`
- 1 x `lookup_customer > collect > options > text`
