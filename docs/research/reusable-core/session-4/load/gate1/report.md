| Run | Call | Bookings | Writes (unmatched) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Problems |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `57wWUrMH` | `BgyW7LQU` `CA896b02a2` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 1 | 1.36 | 3 | 1.61 / 2.81 | none |
| `57wWUrMH` | `mgRcY7No` `CA84e52360` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.20 | 1 | 1.98 / 2.76 | none |
| `57wWUrMH` | `7oMsKxvH` `CAe223b042` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.50 | 4 | 1.50 / 2.89 | none |
| `57wWUrMH` | `c4wZXXVr` `CA4b8deb2b` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.30 | 3 | 1.87 / 2.86 | none |
| `57wWUrMH` | `J8a4nUeY` `CA6354269d` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 1 | 1.50 | 3 | 1.36 / 2.63 | none |
| `57wWUrMH` | `LDCFnwtx` `CAbff5b263` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.44 | 2 | 1.23 / 2.17 | none |
| `57wWUrMH` | `BWqBhyLj` `CA912badf9` | Friday the 2nd at 9:00 AM | 2 (0) | 1 | 0 | 1 | 0 | 1.27 | 6 | 1.68 / 2.64 | ended by the 180 s limit; 1 reply(s) spoke beside an operation |
| `57wWUrMH` | `CWdNQatU` `CA65faacf1` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.12 | 1 | 1.30 / 2.82 | none |
| `57wWUrMH` | `iSJyaJs8` `CAd9ea88ef` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.30 | 2 | 1.60 / 2.67 | none |
| `57wWUrMH` | `DRoSn8Hq` `CAa96da03c` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.40 | 2 | 1.81 / 2.73 | none |
| `57wWUrMH` | `X99Ru3eA` `CA91fd3cbc` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.33 | 4 | 1.27 / 2.08 | none |
| `57wWUrMH` | `5t7pjWna` `CA2c547b33` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.25 | 3 | 1.28 / 2.80 | none |
| `57wWUrMH` | `cYMMixAc` `CAc55c9058` | none | 1 (0) | 0 | 0 | 1 | 0 | 1.50 | 3 | 1.67 / 2.58 | 0 booking writes, expected 1 |
| `57wWUrMH` | `4MLUnEJB` `CA34687a21` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.43 | 1 | 1.33 / 1.40 | none |
| `57wWUrMH` | `4GEQhCav` `CAfae8c887` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.22 | 2 | 1.26 / 2.65 | none |
| `57wWUrMH` | `CFvPcXub` `CA23ec9fa1` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.38 | 2 | 1.20 / 2.73 | none |
| `57wWUrMH` | `7T45xDL3` `CA5ed97ffb` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 1 | 1.50 | 2 | 1.80 / 3.03 | none |
| `57wWUrMH` | `GsPAHtrn` `CAb738ba42` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.57 | 4 | 1.51 / 3.57 | none |
| `57wWUrMH` | `SXrWX2JR` `CAbfa6becb` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.43 | 3 | 1.30 / 2.73 | none |
| `57wWUrMH` | `6Di83S3t` `CA19960fc7` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.50 | 2 | 1.51 / 1.87 | none |

Calls: 20, with no problem: 18.
Writes: 39, with no matching question: 0.
Calls with exactly one booking: 19. First booking BK0001 in every call that booked: True.
Model text beside an operation: 1 in 1 calls. Replies opening with a holding line: 0.
Caller turns with more than one holding line: 0.
Requests: 256 completed over 187 caller turns (1.37 per turn), 53 cancelled of 309 started.
Chains code could skip: {'collect > book_appointment': 3}.
Endpointing: 53 runs answered a continuation piece; 40 runtime turns were cut into by the caller.
Turn latency: p50 1.48 s, p90 2.72 s, n=141.
Model request time: p50 0.76 s, p90 0.96 s.
First token to end of a reply that spoke: p50 0.17 s, p90 0.24 s, n=58.
Input tokens per call: 27390.

Most common request chains per hop:
- 43 x `collect`
- 33 x `book_appointment`
- 20 x `goodbye`
- 18 x `lookup_customer > options > text`
- 16 x `collect > hand_over > text`
- 13 x `text`
- 4 x `collect+hand_over > text`
- 4 x `book_appointment+collect`
- 4 x `collect > text`
- 3 x `collect > book_appointment`
- 2 x `collect+lookup_customer`
- 1 x `hand_over > collect`
- echo in `BWqBhyLj`: book_appointment: Shall I book a haircut, Friday the 2nd at 9:00 AM?
