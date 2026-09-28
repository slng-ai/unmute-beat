| Run | Call | Bookings | Writes (unmatched) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Problems |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `jCBGSipW` | `GbULHRHP` `CAf657289f` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 2 | 1.42 | 7 | 1.17 / 2.71 | none |
| `jCBGSipW` | `BsdTQTsZ` `CAeeeb8603` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 1 | 1 | 2 | 1.78 | 3 | 1.03 / 2.66 | none |
| `jCBGSipW` | `jEisR2g6` `CA0903cc44` | Friday the 2nd at 9:00 AM | 2 (0) | 1 | 2 | 1 | 2 | 1.67 | 5 | 1.01 / 2.59 | 1 reply(s) spoke beside an operation |
| `jCBGSipW` | `nnGmXURQ` `CAd8969417` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 2 | 1.73 | 5 | 1.04 / 2.42 | none |
| `jCBGSipW` | `cyYESUGZ` `CA8a6154a3` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 2 | 2.00 | 4 | 1.15 / 3.32 | none |

Calls: 5, with no problem: 4.
Writes: 10, with no matching question: 0.
Calls with exactly one booking: 5. First booking BK0001 in every call that booked: True.
Model text beside an operation: 1 in 1 calls. Replies opening with a holding line: 3.
Caller turns with more than one holding line: 0.
Requests: 81 completed over 48 caller turns (1.69 per turn), 24 cancelled of 105 started.
Chains code could skip: {'collect > lookup_customer': 5, 'collect > book_appointment': 5}.
Endpointing: 21 runs answered a continuation piece; 11 runtime turns were cut into by the caller.
Turn latency: p50 1.14 s, p90 2.62 s, n=40.
Model request time: p50 0.64 s, p90 0.82 s.
First token to end of a reply that spoke: p50 0.11 s, p90 0.20 s, n=19.
Input tokens per call: 32702.
Time to booking: p50 94.38 s, p90 101.05 s.

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
