| Run | Call | Bookings | Writes (unmatched) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Problems |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `StET3kNN` | `d2iF4s2n` `CA179a1d90` | Saturday the 3rd at 9:00 AM | 2 (0) | 2 | 4 | 2 | 3 | 1.93 | 8 | 1.16 / 2.60 | 2 reply(s) spoke beside an operation; 2 caller turn(s) with repeated holding lines |
| `StET3kNN` | `DFMG2uFk` `CA22e29caf` | tomorrow at 9:00 AM | 2 (0) | 3 | 1 | 1 | 2 | 1.67 | 6 | 1.23 / 2.15 | 3 reply(s) spoke beside an operation |
| `StET3kNN` | `8Y5mzxVq` `CAa22558c0` | Saturday the 3rd at 9:00 AM | 2 (0) | 1 | 2 | 2 | 3 | 2.08 | 8 | 1.17 / 1.92 | 1 reply(s) spoke beside an operation; 1 caller turn(s) with repeated holding lines |
| `StET3kNN` | `347RFS8M` `CAf30055fb` | Saturday the 3rd at 9:00 AM | 2 (0) | 1 | 4 | 2 | 2 | 1.81 | 8 | 1.11 / 2.21 | 1 reply(s) spoke beside an operation; 1 caller turn(s) with repeated holding lines |
| `StET3kNN` | `Mm2VjV3L` `CAeac321cc` | tomorrow at 9:00 AM | 2 (0) | 2 | 1 | 1 | 2 | 1.71 | 13 | 1.39 / 2.75 | 2 reply(s) spoke beside an operation |

Calls: 5, with no problem: 0.
Writes: 10, with no matching question: 0.
Calls with exactly one booking: 5. First booking BK0001 in every call that booked: True.
Model text beside an operation: 9 in 5 calls. Replies opening with a holding line: 12.
Caller turns with more than one holding line: 4.
Requests: 130 completed over 71 caller turns (1.83 per turn), 43 cancelled of 173 started.
Chains code could skip: {'collect > lookup_customer': 4, 'collect > book_appointment': 8}.
Endpointing: 41 runs answered a continuation piece; 18 runtime turns were cut into by the caller.
Turn latency: p50 1.24 s, p90 2.21 s, n=63.
Model request time: p50 0.71 s, p90 0.91 s.
First token to end of a reply that spoke: p50 0.15 s, p90 0.23 s, n=45.
Input tokens per call: 66402.
Time to booking: p50 148.87 s, p90 155.21 s.

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
