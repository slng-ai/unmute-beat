| Run | Call | Active after replay | Receipts | Outcome | Authorization | Delivery | Completion | Operational | Failed | Unknown |
|---|---|---|---|---|---|---|---|---|---|---|
| `ACdLZnmT` | `iQBErmK4` `CAfb240f6f` | haircut 2026-09-29 09:00:00 | book BK0001, move BK0001 | unknown | unknown | unknown | unknown | unknown | - | outcome: move_appointment: a tool call with no write audit (unknown): what it did is unknown; authorization: lookup_customer@3: whether "yes that's right plus three four six seven one five four zero one zero two" accepted the proposal (not reviewed); authorization: book_appointment@5: whether 'yes please go ahead and book it for tomorrow at nine in the morning' accepted the proposal (not reviewed); authorization: move_appointment@None: whether it needed a yes (no mapping, no needs_approval in its audit); authorization: move_appointment@8: whether "yes please that's exactly what i want" accepted the proposal (not reviewed); delivery: move_appointment@None: whether a proposal had to play (whether it needed a yes is unknown); completion: what the agent said it had done: its closing was not reviewed; operational: how the call ended: not measured (the trace span, 185 s, is not its length) |

| Dimension | Pass | Fail | Unknown | Passes resting on an agent review |
|---|---|---|---|---|
| outcome | 0 | 0 | 1 | 0 |
| authorization | 0 | 0 | 1 | 0 |
| delivery | 0 | 0 | 1 | 0 |
| completion | 0 | 0 | 1 | 0 |
| operational | 0 | 0 | 1 | 0 |

Calls: 1. Every dimension passed: 0. Any dimension failed: 0. Evidence complete: 0 of 1.
Calls with exactly one active appointment after replay: 1. Receipts replayed as a repeat or a change of an earlier one are not counted as appointments; this is reconstructed from trace receipts, not checked against a backend snapshot.

## Diagnostics

| Call | Writes (label mismatch) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Span s | Recording s | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `iQBErmK4` | 4 (0) | 0 | 0 | 0 | 1 | 1.57 | 16 | 2.32 / 4.15 | 185 | - | - |

Writes: 4, with no question about them on an earlier turn: 0 (a label check, not consent).
First booking BK0001 in every call that booked: True (expected with a fresh diary per call; not an isolation or contention test).
Model text beside an operation: 0 in 0 calls. Replies opening with a holding line: 0.
Caller turns with more than one holding line: 0.
Requests: 22 completed over 14 caller turns (1.57 per turn), 16 cancelled of 38 started.
Chains code could skip: {'collect > move_appointment': 1}.
Endpointing: 11 runs answered a continuation piece; 6 runtime turns were cut into by the caller.
Turn latency: p50 2.32 s, p90 4.15 s, n=11.
Model request time: p50 0.87 s, p90 0.99 s.
First token to end of a reply that spoke: p50 - s, p90 - s, n=0.
Input tokens per call: 72097.
Time to first booking write: p50 96.32 s, p90 96.32 s.

Most common request chains per hop:
- 2 x `collect`
- 2 x `wait`
- 1 x `collect > hand_over`
- 1 x `lookup_customer`
- 1 x `lookup_customer > options > wait`
- 1 x `book_appointment`
- 1 x `options > wait`
- 1 x `collect > options > move_appointment > wait`
- 1 x `collect > move_appointment`
- 1 x `move_appointment`
- 1 x `goodbye`
