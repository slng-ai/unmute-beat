| Run | Call | Bookings | Writes (unmatched) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Problems |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `e38JbJ9r` | `cBrx4NhR` `CAb558bd3c` | none | 0 (0) | 0 | 0 | 1 | 0 | 1.67 | 0 | 1.59 / 2.21 | 0 booking writes, expected 1 |
| `e38JbJ9r` | `g6txUkVE` `CA8574f62e` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.38 | 2 | 1.32 / 3.30 | none |
| `e38JbJ9r` | `bF2sNufs` `CAb9c6674e` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.43 | 1 | 1.50 / 2.95 | none |
| `e38JbJ9r` | `oLSm3S8H` `CA423b965d` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 1 | 1.57 | 6 | 1.45 / 2.76 | none |
| `e38JbJ9r` | `b7YMAbn6` `CA3dd29f00` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.25 | 4 | 1.94 / 2.74 | none |
| `e38JbJ9r` | `2UJ48UxR` `CA56604e27` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 1 | 1.57 | 1 | 1.61 / 3.61 | none |
| `e38JbJ9r` | `oYjbYp4Q` `CA3dea100e` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.38 | 6 | 1.41 / 2.91 | none |
| `e38JbJ9r` | `oMcvAjjq` `CA2b5da8b9` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.62 | 2 | 1.64 / 3.41 | none |
| `e38JbJ9r` | `E7YSKtLy` `CAa507aac7` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 1 | 1.62 | 3 | 1.47 / 1.64 | none |
| `e38JbJ9r` | `SB4GGMzU` `CA41a56642` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 1 | 1.67 | 4 | 1.69 / 2.63 | none |
| `e38JbJ9r` | `YpGKThqi` `CA580d9716` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.50 | 4 | 1.57 / 2.93 | none |
| `e38JbJ9r` | `3QtyKPQJ` `CA75a248eb` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.43 | 4 | 1.41 / 3.30 | none |
| `e38JbJ9r` | `CBwwKSbM` `CA32d59ab5` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.50 | 1 | 2.17 / 3.01 | none |
| `e38JbJ9r` | `XSe2nRij` `CAa7ea31f8` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.43 | 2 | 1.35 / 3.07 | none |
| `e38JbJ9r` | `TohdDeaa` `CAa7492dd4` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.38 | 4 | 1.32 / 1.48 | none |
| `e38JbJ9r` | `2Kx2fiJV` `CA2f6f7295` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 1 | 1.50 | 2 | 1.53 / 1.79 | none |
| `e38JbJ9r` | `jVSqvfBN` `CA1fe58382` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 1 | 1.62 | 4 | 1.41 / 2.85 | none |
| `e38JbJ9r` | `ZuuMq9ZJ` `CA22710b30` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.29 | 1 | 1.70 / 2.73 | none |
| `e38JbJ9r` | `MwgpVnuC` `CA95d50883` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.43 | 3 | 1.63 / 2.82 | none |
| `e38JbJ9r` | `o4CAK59e` `CAadbc7898` | Friday the 2nd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 1 | 1.57 | 2 | 1.39 / 2.81 | none |

Calls: 20, with no problem: 19.
Writes: 38, with no matching question: 0.
Calls with exactly one booking: 19. First booking BK0001 in every call that booked: True.
Model text beside an operation: 0 in 0 calls. Replies opening with a holding line: 0.
Caller turns with more than one holding line: 0.
Requests: 223 completed over 150 caller turns (1.49 per turn), 56 cancelled of 279 started.
Chains code could skip: {'collect > book_appointment': 7}.
Endpointing: 49 runs answered a continuation piece; 33 runtime turns were cut into by the caller.
Turn latency: p50 1.57 s, p90 2.85 s, n=121.
Model request time: p50 0.83 s, p90 1.02 s.
First token to end of a reply that spoke: p50 0.17 s, p90 0.25 s, n=55.
Input tokens per call: 22941.

Most common request chains per hop:
- 32 x `collect`
- 19 x `lookup_customer > options > text`
- 19 x `goodbye`
- 17 x `collect > hand_over > text`
- 14 x `text`
- 12 x `book_appointment`
- 7 x `collect > book_appointment`
- 3 x `collect+hand_over > text`
- 3 x `collect+lookup_customer`
- 3 x `book_appointment+collect`
- 1 x `hand_over > goodbye`
- 1 x `options > text`
