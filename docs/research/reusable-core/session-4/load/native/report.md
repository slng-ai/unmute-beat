| Run | Call | Bookings | Writes (unmatched) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Problems |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `jszHHpQY` | `LdVa6nrm` `CA20d97d84` | Friday at 9:00 AM | 2 (0) | 0 | 0 | 0 | 0 | 1.17 | 6 | 2.37 / 3.33 | none |
| `4LvLqjiF` | `a3y8pY2d` `CAa5d7a300` | Friday at 9:00 AM | 2 (0) | 0 | 0 | 0 | 0 | 1.00 | 0 | 2.32 / 3.09 | none |
| `T99A9kjo` | `7UEVy92Z` `CAb6b587c8` | Friday at 9:00 AM | 2 (0) | 0 | 0 | 0 | 0 | 1.29 | 2 | 3.53 / 4.20 | none |
| `8c3YGp48` | `7YU34Ve8` `CAa430b1de` | Friday at 9:00 AM | 2 (0) | 0 | 0 | 0 | 0 | 1.17 | 2 | 2.20 / 2.88 | none |
| `RcuWnFup` | `fk5ZrLQm` `CA8174ccde` | Friday at 9:00 AM | 2 (0) | 0 | 0 | 0 | 0 | 1.29 | 3 | 3.17 / 22.77 | none |
| `am3AhJfS` | `5dGYa2jX` `CA3c25ae3c` | Friday at 9:00 AM | 2 (0) | 0 | 0 | 0 | 0 | 1.33 | 2 | 1.61 / 2.97 | none |
| `6xen23d2` | `8HJCRNEg` `CAddbe27b9` | Friday at 9:00 AM | 2 (0) | 0 | 0 | 0 | 0 | 1.33 | 1 | 1.54 / 2.46 | none |
| `E5waefF7` | `Xay9L9U4` `CA69be271d` | Friday at 9:00 AM | 2 (0) | 0 | 0 | 0 | 0 | 1.33 | 7 | 1.46 / 4.12 | none |
| `WYg8wacM` | `HHqqbKJz` `CAeda71145` | Friday at 9:00 AM | 2 (0) | 0 | 0 | 0 | 0 | 1.29 | 1 | 1.58 / 4.00 | none |
| `GQwAWwwD` | `B9xrkb4S` `CA8e9489db` | Friday at 9:00 AM | 2 (0) | 0 | 0 | 0 | 0 | 1.33 | 2 | 1.50 / 3.31 | none |
| `mM5ma5y6` | `YzkhPWRp` `CAa24472f0` | Friday at 9:00 AM; Saturday the 3rd at 9:00 AM | 3 (0) | 0 | 0 | 0 | 0 | 1.20 | 10 | 1.84 / 4.25 | 2 booking writes, expected 1 |
| `dK9ZvemK` | `j3aCXXq8` `CA751fa5eb` | none | 1 (0) | 0 | 0 | 0 | 0 | 1.38 | 7 | 2.76 / 3.89 | 0 booking writes, expected 1 |
| `5YWfZDoP` | `3CyVWZmp` `CA4fb153ba` | Friday at 9:00 AM; Saturday the 3rd at 9:00 AM | 3 (0) | 0 | 0 | 0 | 0 | 1.38 | 8 | 2.67 / 3.31 | 2 booking writes, expected 1 |
| `TKGEkWhA` | `YJRpB5JE` `CA49f481ad` | none | 1 (0) | 0 | 0 | 0 | 0 | 1.75 | 16 | 2.95 / 4.01 | 0 booking writes, expected 1 |
| `AXhNRqcz` | `8opr2oig` `CA8ab6402b` | none | 1 (0) | 0 | 0 | 0 | 0 | 1.18 | 3 | 2.31 / 3.04 | 0 booking writes, expected 1 |
| `2NZdaNpZ` | `kwUbc4PR` `CA3bf04516` | Saturday the 3rd at 9:00 AM | 2 (0) | 0 | 0 | 0 | 0 | 1.17 | 4 | 2.08 / 4.75 | none |
| `f2dSbb3u` | `F88eigQQ` `CA36f6aed4` | none | 0 (0) | 0 | 0 | 0 | 0 | 1.14 | 8 | 1.92 / 4.10 | 0 booking writes, expected 1 |
| `GwX9FkFX` | `iPFgi5qq` `CAecf6261f` | none | 0 (0) | 0 | 0 | 0 | 0 | 0.75 | 3 | 2.11 / 2.38 | 0 booking writes, expected 1; ended by the 300 s limit |

Calls: 18, with no problem: 11.
Writes: 31, with no matching question: 0.
Calls with exactly one booking: 11. First booking BK0001 in every call that booked: True.
Model text beside an operation: 0 in 0 calls. Replies opening with a holding line: 0.
Caller turns with more than one holding line: 0.
Requests: 187 completed over 146 caller turns (1.28 per turn), 85 cancelled of 272 started.
Chains code could skip: none.
Endpointing: 0 runs answered a continuation piece; 40 runtime turns were cut into by the caller.
Turn latency: p50 2.32 s, p90 4.00 s, n=110.
Model request time: p50 0.93 s, p90 1.09 s.
First token to end of a reply that spoke: p50 - s, p90 - s, n=0.
Input tokens per call: 32972.
Time to booking: p50 77.64 s, p90 107.54 s.

Most common request chains per hop:
