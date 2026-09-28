| Run | Call | Active after replay | Receipts | Outcome | Authorization | Delivery | Completion | Operational | Failed | Unknown |
|---|---|---|---|---|---|---|---|---|---|---|
| `o5T5ygcV` | `MkDCXouU` `CA8f4db79b` | none | none | unknown | unknown | pass (deterministic) | unknown | unknown | - | outcome: what the caller asked for: no case expectation and no reviewed final request; authorization: lookup_customer@3: whether "nope sorry it's plus three four six seven one five four zero one zero one" accepted the proposal (not reviewed); completion: what the agent said it had done: its closing was not reviewed; operational: how the call ended: not measured (the trace span, none, is not its length) |
| `dqpPTERW` | `2LLPcZXb` `CA5c24944d` | haircut 2026-09-28 09:00:00 | book BK0002 | unknown | unknown | pass (deterministic) | unknown | unknown | - | outcome: what the caller asked for: no case expectation and no reviewed final request; authorization: lookup_customer@3: whether "yes that's right" accepted the proposal (not reviewed); authorization: book_appointment@6: whether 'yes please go ahead and book me in for nine am tomorrow' accepted the proposal (not reviewed); completion: what the agent said it had done: its closing was not reviewed; operational: how the call ended: not measured (the trace span, 102 s, is not its length) |

| Dimension | Pass | Fail | Unknown | Passes resting on an agent review |
|---|---|---|---|---|
| outcome | 0 | 0 | 2 | 0 |
| authorization | 0 | 0 | 2 | 0 |
| delivery | 2 | 0 | 0 | 0 |
| completion | 0 | 0 | 2 | 0 |
| operational | 0 | 0 | 2 | 0 |

Calls: 2. Every dimension passed: 0. Any dimension failed: 0. Evidence complete: 0 of 2.
Calls with exactly one active appointment after replay: 1. Receipts replayed as a repeat or a change of an earlier one are not counted as appointments; this is reconstructed from trace receipts, not checked against a backend snapshot.

## Diagnostics

| Call | Writes (label mismatch) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Span s | Recording s | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `MkDCXouU` | 1 (0) | 0 | 0 | 0 | 0 | 1.20 | 2 | 1.97 / 3.41 | - | - | - |
| `2LLPcZXb` | 2 (0) | 0 | 0 | 0 | 0 | 1.00 | 4 | 2.11 / 3.11 | 102 | - | first booking BK0002, not BK0001: calls share a diary |

Writes: 3, with no question about them on an earlier turn: 0 (a label check, not consent).
First booking BK0001 in every call that booked: False (expected with a fresh diary per call; not an isolation or contention test).
Model text beside an operation: 0 in 0 calls. Replies opening with a holding line: 0.
Caller turns with more than one holding line: 0.
Requests: 18 completed over 17 caller turns (1.06 per turn), 6 cancelled of 24 started.
Chains code could skip: none.
Endpointing: 6 runs answered a continuation piece; 8 runtime turns were cut into by the caller.
Turn latency: p50 2.11 s, p90 3.11 s, n=12.
Model request time: p50 0.84 s, p90 1.11 s.
First token to end of a reply that spoke: p50 - s, p90 - s, n=0.
Input tokens per call: 20642.
Time to first booking write: p50 91.87 s, p90 91.87 s.

Most common request chains per hop:
- 4 x `collect`
- 2 x `collect > hand_over`
- 1 x `lookup_customer > wait`
- 1 x `lookup_customer`
- 1 x `lookup_customer > options > wait`
- 1 x `wait`
- 1 x `say_again`
- 1 x `book_appointment`
- 1 x `goodbye`
