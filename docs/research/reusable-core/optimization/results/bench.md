### Compile

| workload | run | cold compile ms | warm compile ms | load ms |
|---|---|---|---|---|
| desk | baseline | 36.6 (p90 37.5, sd 0.5) | 19.4 (p90 20.2, sd 0.4) | 1.9 (p90 2.5, sd 3.2) |
| desk | fix1 | 37.5 (p90 38.0, sd 0.2) | 19.9 (p90 20.5, sd 3.1) | 1.9 (p90 2.8, sd 0.3) |
| desk | hooks | 38.2 (p90 39.6, sd 0.5) | 21.0 (p90 22.6, sd 3.8) | 2.0 (p90 2.2, sd 0.3) |
| salon | baseline | 72.8 (p90 75.0, sd 3.9) | 45.3 (p90 47.8, sd 3.5) | 3.8 (p90 4.0, sd 0.2) |
| salon | fix1 | 73.9 (p90 75.8, sd 0.7) | 45.6 (p90 46.8, sd 3.2) | 3.8 (p90 4.0, sd 0.2) |
| salon | hooks | 79.5 (p90 98.8, sd 23.4) | 46.6 (p90 49.8, sd 3.7) | 3.9 (p90 4.8, sd 0.3) |
| faro | baseline | 100.8 (p90 102.1, sd 0.8) | 94.0 (p90 164.7, sd 33.6) | 5.6 (p90 8.8, sd 2.0) |
| faro | fix1 | 103.1 (p90 106.7, sd 1.8) | 87.9 (p90 110.4, sd 9.1) | 5.5 (p90 6.3, sd 0.5) |
| faro | hooks | 103.3 (p90 106.1, sd 1.5) | 88.1 (p90 104.2, sd 5.7) | 5.5 (p90 5.6, sd 0.1) |

### Turns (scripted, simulated delivery)

| workload | run | requests | first call in a fresh process ms | warm call wall ms | warm call CPU ms | slowest turn ms | mean first output ms |
|---|---|---|---|---|---|---|---|
| desk | baseline | 7 | 38.0 (p90 39.0, sd 0.4) | 31.3 (p90 32.3, sd 0.4) | 23.8 (p90 24.1, sd 0.4) | 16.0 | 6.2 |
| desk | fix1 | 7 | 37.7 (p90 39.7, sd 0.9) | 31.0 (p90 31.7, sd 0.5) | 23.4 (p90 24.2, sd 0.4) | 16.1 | 6.2 |
| desk | hooks | 7 | 36.3 (p90 37.9, sd 0.7) | 29.8 (p90 31.4, sd 3.3) | 22.2 (p90 23.7, sd 3.3) | 15.1 | 5.9 |
| salon | baseline | 10 | 56.6 (p90 61.3, sd 1.5) | 46.8 (p90 52.2, sd 2.1) | 36.4 (p90 41.6, sd 2.0) | 13.0 | 6.3 |
| salon | fix1 | 10 | 56.4 (p90 57.9, sd 0.8) | 49.1 (p90 51.9, sd 1.8) | 38.6 (p90 41.4, sd 1.7) | 13.7 | 6.6 |
| salon | hooks | 10 | 55.5 (p90 59.9, sd 2.2) | 44.7 (p90 45.8, sd 1.0) | 34.2 (p90 35.5, sd 1.0) | 12.3 | 6.0 |
| salon-long | baseline | 34 | 180.9 (p90 190.1, sd 5.7) | 179.1 (p90 195.8, sd 8.5) | 141.1 (p90 157.1, sd 8.4) | 15.0 | 5.3 |
| salon-long | fix1 | 34 | 171.3 (p90 184.8, sd 4.6) | 164.1 (p90 183.0, sd 6.7) | 126.4 (p90 142.4, sd 6.4) | 13.3 | 4.8 |
| salon-long | hooks | 34 | 165.4 (p90 172.3, sd 3.3) | 166.5 (p90 183.0, sd 11.9) | 128.5 (p90 141.4, sd 11.4) | 13.3 | 4.8 |
| faro | baseline | 8 | 57.3 (p90 60.2, sd 1.0) | 50.2 (p90 56.9, sd 4.1) | 40.1 (p90 46.7, sd 4.0) | 10.1 | 6.8 |
| faro | fix1 | 8 | 60.4 (p90 62.5, sd 1.3) | 51.7 (p90 58.1, sd 4.0) | 41.7 (p90 46.8, sd 3.8) | 10.3 | 7.0 |
| faro | hooks | 8 | 56.2 (p90 95.1, sd 11.7) | 50.1 (p90 52.0, sd 3.0) | 40.1 (p90 42.0, sd 3.0) | 9.8 | 6.7 |

### Payload per call (bytes, not tokens)

| workload | run | requests | instructions B | of which context B | history B | tool schemas B | warm-up requests |
|---|---|---|---|---|---|---|---|
| desk | baseline | 7 | 8685 | 4998 | 2510 | 14684 | 1 |
| desk | fix1 | 7 | 8685 | 4998 | 2510 | 14684 | 1 |
| desk | hooks | 7 | 8685 | 4998 | 2510 | 14684 | 1 |
| salon | baseline | 10 | 62586 | 11551 | 9026 | 27923 | 1 |
| salon | fix1 | 10 | 62586 | 11551 | 9026 | 27923 | 1 |
| salon | hooks | 10 | 62586 | 11551 | 9026 | 27923 | 1 |
| salon-long | baseline | 34 | 176888 | 44239 | 69887 | 75779 | 1 |
| salon-long | fix1 | 34 | 176888 | 44239 | 69887 | 75779 | 1 |
| salon-long | hooks | 34 | 176888 | 44239 | 69887 | 75779 | 1 |
| faro | baseline | 8 | 33047 | 17420 | 5563 | 20625 | 1 |
| faro | fix1 | 8 | 33047 | 17420 | 5563 | 20625 | 1 |
| faro | hooks | 8 | 33047 | 17420 | 5563 | 20625 | 1 |

### Python memory (tracemalloc) and RSS

| workload | run | discarded flows alive | retained by compiles KiB | completed calls alive | retained by calls KiB | open call KiB each | released calls alive | RSS MB |
|---|---|---|---|---|---|---|---|---|
| desk | baseline | 50/50 | 7950 | 0/20 | -3 | 39 | 0/5 | 91 -> 111 |
| desk | fix1 | 0/50 | 78 | 0/20 | -9 | 40 | 0/5 | 91 -> 98 |
| desk | hooks | 0/50 | 77 | 0/20 | -5 | 40 | 0/5 | 91 -> 98 |
| salon | baseline | 50/50 | 18004 | 0/20 | -3 | 73 | 0/5 | 129 -> 168 |
| salon | fix1 | 0/50 | 101 | 0/20 | -18 | 74 | 0/5 | 130 -> 145 |
| salon | hooks | 0/50 | 104 | 0/20 | -10 | 74 | 0/5 | 129 -> 145 |
| salon-long | baseline | 50/50 | 18001 | 0/20 | -6 | 190 | 0/5 | 130 -> 179 |
| salon-long | fix1 | 0/50 | 103 | 0/20 | -17 | 193 | 0/5 | 130 -> 140 |
| salon-long | hooks | 0/50 | 104 | 0/20 | -25 | 193 | 0/5 | 130 -> 151 |
| faro | baseline | 50/50 | 32272 | 0/20 | -6 | 56 | 0/5 | 94 -> 165 |
| faro | fix1 | 0/50 | 94 | 0/20 | -8 | 55 | 0/5 | 94 -> 111 |
| faro | hooks | 0/50 | 94 | 0/20 | -8 | 56 | 0/5 | 94 -> 104 |

### Profile (cProfile own time, inflated by the profiler)

| workload | run | profiled ms/call | in core ms/call | core share |
|---|---|---|---|---|
| desk | baseline | 64.7 | 1.7 | 3% |
| desk | fix1 | 66.5 | 1.7 | 3% |
| desk | hooks | 61.9 | 1.9 | 3% |
| salon | baseline | 99.4 | 3.0 | 3% |
| salon | fix1 | 101.2 | 3.0 | 3% |
| salon | hooks | 91.8 | 3.0 | 3% |
| salon-long | baseline | 341.3 | 12.6 | 4% |
| salon-long | fix1 | 359.4 | 14.1 | 4% |
| salon-long | hooks | 316.1 | 13.9 | 4% |
| faro | baseline | 100.4 | 3.1 | 3% |
| faro | fix1 | 105.1 | 3.4 | 3% |
| faro | hooks | 93.0 | 3.1 | 3% |

