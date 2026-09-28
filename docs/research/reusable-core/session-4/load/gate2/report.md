| Run | Call | Bookings | Writes (unmatched) | Echo | Filler copies | Max holding / turn | collect > op | Requests / turn | Cancelled | Turn latency p50 / p90 | Problems |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `B6c7CSHw` | `WjF5R6Sw` `CA4e89cd39` | Saturday the 3rd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 1 | 1.33 | 3 | 1.95 / 3.17 | none |
| `B6c7CSHw` | `YSLmK7GL` `CA3f3cc4bd` | Saturday the 3rd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 1 | 1.47 | 4 | 2.03 / 3.21 | none |
| `B6c7CSHw` | `FNDkWSuG` `CAb2106fd6` | Saturday the 3rd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 1 | 1.54 | 4 | 1.56 / 2.00 | none |
| `B6c7CSHw` | `F7sitnhR` `CA1e279943` | Saturday the 3rd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.31 | 5 | 1.92 / 3.20 | none |
| `B6c7CSHw` | `e52ESZoS` `CAe516f7a5` | Saturday the 3rd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 1 | 1.62 | 5 | 1.47 / 2.00 | none |
| `B6c7CSHw` | `nY3wFz4Z` `CA25aed39a` | Saturday the 3rd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.46 | 0 | 1.42 / 2.25 | none |
| `B6c7CSHw` | `Nob9XXSR` `CA4ba02f80` | Saturday the 3rd at 9:00 AM | 2 (0) | 1 | 0 | 1 | 0 | 1.25 | 3 | 1.86 / 2.86 | 1 reply(s) spoke beside an operation |
| `B6c7CSHw` | `GUpJoB3U` `CAc7b9a4fa` | Saturday the 3rd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.24 | 6 | 1.58 / 3.20 | none |
| `B6c7CSHw` | `3UjW42EC` `CA13e3a1a3` | Saturday the 3rd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.50 | 2 | 1.35 / 2.50 | none |
| `B6c7CSHw` | `VuhAwZ3e` `CA941c5b40` | Saturday the 3rd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.27 | 4 | 1.71 / 2.87 | none |
| `B6c7CSHw` | `CKssEhDL` `CA2c92f50d` | Saturday the 3rd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 2 | 1.53 | 5 | 2.09 / 3.38 | none |
| `B6c7CSHw` | `A8x9VyDz` `CA522c1c70` | Saturday the 3rd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.25 | 4 | 1.39 / 2.62 | none |
| `B6c7CSHw` | `fzMA8L6V` `CAadbfc4d3` | Saturday the 3rd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.46 | 2 | 1.29 / 2.20 | none |
| `B6c7CSHw` | `Bksyjbtj` `CAf4558e6e` | Saturday the 3rd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.15 | 2 | 1.35 / 2.78 | none |
| `B6c7CSHw` | `iWKTxx7H` `CAe24f90af` | Saturday the 3rd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.55 | 3 | 1.39 / 2.64 | none |
| `B6c7CSHw` | `V48AKSkj` `CA4d866444` | Saturday the 3rd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 1 | 1.43 | 1 | 1.80 / 2.73 | none |
| `B6c7CSHw` | `aQ8WhZsK` `CA476e8d77` | Saturday the 3rd at 9:00 AM | 2 (0) | 1 | 0 | 1 | 0 | 1.17 | 10 | 1.64 / 3.23 | 1 reply(s) spoke beside an operation |
| `B6c7CSHw` | `C26pPHK6` `CA31386078` | none | 1 (0) | 0 | 0 | 1 | 0 | 1.38 | 8 | 1.47 / 2.11 | 0 booking writes, expected 1 |
| `B6c7CSHw` | `cAxvwUZR` `CA2583cd93` | Saturday the 3rd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 1 | 1.58 | 6 | 1.28 / 2.16 | none |
| `B6c7CSHw` | `7C5cmvqi` `CAd2514a88` | Saturday the 3rd at 9:00 AM | 2 (0) | 0 | 0 | 1 | 0 | 1.12 | 2 | 1.90 / 3.35 | none |

Calls: 20, with no problem: 17.
Writes: 39, with no matching question: 0.
Calls with exactly one booking: 19. First booking BK0001 in every call that booked: True.
Model text beside an operation: 2 in 2 calls. Replies opening with a holding line: 0.
Caller turns with more than one holding line: 0.
Requests: 387 completed over 282 caller turns (1.37 per turn), 79 cancelled of 466 started.
Chains code could skip: {'collect > book_appointment': 7, 'collect > lookup_customer': 1}.
Endpointing: 128 runs answered a continuation piece; 77 runtime turns were cut into by the caller.
Turn latency: p50 1.69 s, p90 2.95 s, n=246.
Model request time: p50 0.75 s, p90 0.95 s.
First token to end of a reply that spoke: p50 0.17 s, p90 0.24 s, n=96.
Input tokens per call: 47892.
Time to booking: p50 137.14 s, p90 218.03 s.

Most common request chains per hop:
- 73 x `collect`
- 33 x `text`
- 20 x `goodbye`
- 18 x `collect > text`
- 17 x `collect > options > text`
- 15 x `book_appointment`
- 14 x `collect > hand_over`
- 13 x `lookup_customer > options > text`
- 12 x `book_appointment+collect`
- 10 x `lookup_customer`
- 7 x `collect > book_appointment`
- 6 x `collect+hand_over`
- echo in `Nob9XXSR`: book_appointment: Shall I book a haircut, Saturday the 3rd at 9:00 AM?
- echo in `aQ8WhZsK`: book_appointment: Shall I book a haircut, Saturday the 3rd at 9:00 AM?
