| Run | Call | Bookings | Writes (unmatched) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Problems |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `CdR8tupv` | `ZiGrTDzY` `CA99804430` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.75 | 0 | 1.75 / 1.98 | none |
| `CdR8tupv` | `NXBccLf5` `CAe5091ec7` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 1 | 1.57 | 1 | 1.90 / 2.98 | none |
| `CdR8tupv` | `CSSVxduB` `CA4e63707e` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.33 | 8 | 1.66 / 2.10 | none |
| `CdR8tupv` | `Skx5ZjD7` `CA6061e3c4` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 1 | 1.57 | 0 | 1.96 / 3.01 | none |
| `Qprjd3ZJ` | `ArS5ZMnu` `CA11a137f1` | none | 1 (0) | 0 | 0 | 1 | 0 | 1.43 | 1 | 1.74 / 2.06 | 0 booking writes, expected 1; ended by the 180 s limit |
| `Qprjd3ZJ` | `MeSFmdkX` `CAa2429a90` | tomorrow at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.50 | 2 | 1.85 / 2.18 | none |
| `Qprjd3ZJ` | `BpraHnbw` `CA8e1389ae` | tomorrow at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.62 | 2 | 1.83 / 1.98 | none |
| `Qprjd3ZJ` | `HJS5pYLK` `CAc932265c` | tomorrow at 9:30 AM | 2 (0) | 0 | 0 | 1 | 1 | 2.00 | 6 | 1.92 / 3.10 | none |

Calls: 8, with no problem: 7.
Writes: 15, with no matching question: 0.
Calls with exactly one booking: 7. First booking BK0001 in every call that booked: True.
Model text beside an operation: 0 in 0 calls. Replies opening with a holding line: 0.
Caller turns with more than one holding line: 0.
Requests: 134 completed over 84 caller turns (1.60 per turn), 20 cancelled of 154 started.
Chains code could skip: {'collect > book_appointment': 3}.
Endpointing: 24 runs answered a continuation piece; 23 runtime turns were cut into by the caller.
Turn latency: p50 1.84 s, p90 2.47 s, n=73.
Model request time: p50 0.73 s, p90 0.91 s.
First token to end of a reply that spoke: p50 0.13 s, p90 0.21 s, n=32.
Input tokens per call: 37003.

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
