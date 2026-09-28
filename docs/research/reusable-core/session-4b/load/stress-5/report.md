| Run | Call | Active after replay | Receipts | Outcome | Authorization | Delivery | Completion | Operational | Failed | Unknown |
|---|---|---|---|---|---|---|---|---|---|---|
| `StET3kNN` | `d2iF4s2n` `CA179a1d90` | haircut 2026-10-03 09:00:00 | book BK0001 | unknown | unknown | unknown | unknown | pass (deterministic) | - | outcome: the final day, time and number asked for: not reviewed; authorization: lookup_customer@4: which arguments its read-back named (no proposal_args in its audit); authorization: lookup_customer@4: whether "yes that's correct" accepted the proposal (not reviewed); authorization: book_appointment@9: which arguments its read-back named (no proposal_args in its audit); authorization: book_appointment@9: whether "yes please that's perfect" accepted the proposal (not reviewed); delivery: lookup_customer@4: no evidence the proposal played in full (the runtime estimate is not); delivery: book_appointment@9: no evidence the proposal played in full (the runtime estimate is not); completion: what the agent said it had done: its closing was not reviewed |
| `StET3kNN` | `DFMG2uFk` `CA22e29caf` | haircut 2026-09-26 09:00:00 | book BK0001 | unknown | unknown | unknown | unknown | pass (deterministic) | - | outcome: the final day, time and number asked for: not reviewed; authorization: lookup_customer@4: which arguments its read-back named (no proposal_args in its audit); authorization: lookup_customer@4: whether "yes that's correct" accepted the proposal (not reviewed); authorization: book_appointment@9: which arguments its read-back named (no proposal_args in its audit); authorization: book_appointment@9: whether 'yes please' accepted the proposal (not reviewed); delivery: lookup_customer@4: no evidence the proposal played in full (the runtime estimate is not); delivery: book_appointment@9: no evidence the proposal played in full (the runtime estimate is not); completion: what the agent said it had done: its closing was not reviewed |
| `StET3kNN` | `8Y5mzxVq` `CAa22558c0` | haircut 2026-10-03 09:00:00 | book BK0001 | unknown | unknown | unknown | unknown | pass (deterministic) | - | outcome: the final day, time and number asked for: not reviewed; authorization: lookup_customer@3: which arguments its read-back named (no proposal_args in its audit); authorization: lookup_customer@3: whether "yes that's correct" accepted the proposal (not reviewed); authorization: book_appointment@8: which arguments its read-back named (no proposal_args in its audit); authorization: book_appointment@8: whether 'yes please book me in for nine am on saturday the third' accepted the proposal (not reviewed); delivery: lookup_customer@3: no evidence the proposal played in full (the runtime estimate is not); delivery: book_appointment@8: no evidence the proposal played in full (the runtime estimate is not); completion: what the agent said it had done: its closing was not reviewed |
| `StET3kNN` | `347RFS8M` `CAf30055fb` | haircut 2026-10-03 09:00:00 | book BK0001 | unknown | unknown | unknown | unknown | pass (deterministic) | - | outcome: the final day, time and number asked for: not reviewed; authorization: lookup_customer@4: which arguments its read-back named (no proposal_args in its audit); authorization: lookup_customer@4: whether "yes that's correct" accepted the proposal (not reviewed); authorization: book_appointment@10: which arguments its read-back named (no proposal_args in its audit); authorization: book_appointment@10: whether "yes that's perfect saturday the third at nine am" accepted the proposal (not reviewed); delivery: lookup_customer@4: no evidence the proposal played in full (the runtime estimate is not); delivery: book_appointment@10: no evidence the proposal played in full (the runtime estimate is not); completion: what the agent said it had done: its closing was not reviewed |
| `StET3kNN` | `Mm2VjV3L` `CAeac321cc` | haircut 2026-09-26 09:00:00 | book BK0001 | unknown | unknown | unknown | unknown | pass (deterministic) | - | outcome: the final day, time and number asked for: not reviewed; authorization: lookup_customer@4: which arguments its read-back named (no proposal_args in its audit); authorization: lookup_customer@4: whether "yes that's correct" accepted the proposal (not reviewed); authorization: book_appointment@9: which arguments its read-back named (no proposal_args in its audit); authorization: book_appointment@9: whether "yes please that's perfect" accepted the proposal (not reviewed); delivery: lookup_customer@4: no evidence the proposal played in full (the runtime estimate is not); delivery: book_appointment@9: no evidence the proposal played in full (the runtime estimate is not); completion: what the agent said it had done: its closing was not reviewed |

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
| `d2iF4s2n` | 2 (0) | 2 | 4 | 2 | 3 | 1.93 | 8 | 1.16 / 2.60 | 152 | 152 | 2 reply(s) spoke beside an operation; 2 caller turn(s) with repeated holding lines |
| `DFMG2uFk` | 2 (0) | 3 | 1 | 1 | 2 | 1.67 | 6 | 1.23 / 2.15 | 171 | 170 | 3 reply(s) spoke beside an operation |
| `8Y5mzxVq` | 2 (0) | 1 | 2 | 2 | 3 | 2.08 | 8 | 1.17 / 1.92 | 151 | 152 | 1 reply(s) spoke beside an operation; 1 caller turn(s) with repeated holding lines |
| `347RFS8M` | 2 (0) | 1 | 4 | 2 | 2 | 1.81 | 8 | 1.11 / 2.21 | 173 | 173 | 1 reply(s) spoke beside an operation; 1 caller turn(s) with repeated holding lines |
| `Mm2VjV3L` | 2 (0) | 2 | 1 | 1 | 2 | 1.71 | 13 | 1.39 / 2.75 | 171 | 171 | 2 reply(s) spoke beside an operation |

Writes: 10, with no question about them on an earlier turn: 0 (a label check, not consent).
First booking BK0001 in every call that booked: True (expected with a fresh diary per call; not an isolation or contention test).
Model text beside an operation: 9 in 5 calls. Replies opening with a holding line: 12.
Caller turns with more than one holding line: 4.
Requests: 130 completed over 71 caller turns (1.83 per turn), 43 cancelled of 173 started.
Chains code could skip: {'collect > lookup_customer': 4, 'collect > book_appointment': 8}.
Endpointing: 41 runs answered a continuation piece; 18 runtime turns were cut into by the caller.
Turn latency: p50 1.24 s, p90 2.21 s, n=63.
Model request time: p50 0.71 s, p90 0.91 s.
First token to end of a reply that spoke: p50 0.15 s, p90 0.23 s, n=45.
Input tokens per call: 66402.
Time to first booking write: p50 148.87 s, p90 155.21 s.

Most common request chains per hop:
- 11 x `book_appointment`
- 8 x `collect > text`
- 8 x `collect > book_appointment`
- 7 x `collect > options > text`
- 5 x `collect > hand_over > text`
- 5 x `lookup_customer > options > text`
- 5 x `goodbye`
- 4 x `collect > lookup_customer`
- 4 x `text`
- 3 x `lookup_customer`
- 2 x `options > text`
- echo in `d2iF4s2n`: book_appointment: One moment.
- echo in `d2iF4s2n`: book_appointment: One moment.Shall I book you in for a haircut, Saturday the 3rd at 9:00 AM?
- echo in `DFMG2uFk`: lookup_customer: Let's see.
- echo in `DFMG2uFk`: book_appointment: Let me have a look.
- echo in `DFMG2uFk`: book_appointment: Shall I book you in for a haircut, tomorrow at 9:00 AM?
- echo in `8Y5mzxVq`: book_appointment: Let me have a look.
- echo in `347RFS8M`: book_appointment: One moment.Shall I book you in for a haircut, Saturday the 3rd at 9:00 AM?
- echo in `Mm2VjV3L`: lookup_customer: Just to check, is that +34 671 27 34 54?
- echo in `Mm2VjV3L`: book_appointment: Shall I book you in for a haircut, tomorrow at 9:00 AM?
