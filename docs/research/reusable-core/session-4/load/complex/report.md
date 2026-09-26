| Run | Call | Bookings | Writes (unmatched) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Problems |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `GYZdoiie` | `fymMyWTi` `CA09f5c095` | none | 1 (0) | 0 | 0 | 1 | 1 | 2.17 | 2 | 1.31 / 2.03 | 0 booking writes, expected 1 |
| `MQagmvsp` | `NdkBKGCN` `CA87afaa0f` | tomorrow at 9:00 AM | 2 (0) | 2 | 2 | 2 | 3 | 1.86 | 3 | 1.54 / 2.42 | ended by the 180 s limit; 2 reply(s) spoke beside an operation; 1 caller turn(s) with repeated holding lines |

Calls: 2, with no problem: 0.
Writes: 3, with no matching question: 0.
Calls with exactly one booking: 1. First booking BK0001 in every call that booked: True.
Model text beside an operation: 2 in 1 calls. Replies opening with a holding line: 2.
Caller turns with more than one holding line: 1.
Requests: 39 completed over 20 caller turns (1.95 per turn), 5 cancelled of 44 started.
Chains code could skip: {'collect > lookup_customer': 2, 'collect > book_appointment': 2}.
Endpointing: 10 runs answered a continuation piece; 5 runtime turns were cut into by the caller.
Turn latency: p50 1.54 s, p90 2.42 s, n=15.
Model request time: p50 0.73 s, p90 0.98 s.
First token to end of a reply that spoke: p50 0.15 s, p90 0.25 s, n=17.
Input tokens per call: 44298.
Time to booking: p50 141.12 s, p90 141.12 s.

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
