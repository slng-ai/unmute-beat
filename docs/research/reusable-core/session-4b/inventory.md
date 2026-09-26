# Session 4 calls, rescored

Built by `rescore.py` from the private export and `evidence.json`. Every placed call is counted once, in
its build and scenario; failures and unknowns stay in every denominator.

| Build | Scenario | Placed | Coval completed | Conversation span | Recording measured | Ending measured | Active after replay: 0 / 1 / 2+ | Ended by | Evidence complete | outcome: pass / fail / unknown | authorization: pass / fail / unknown | delivery: pass / fail / unknown | completion: pass / fail / unknown | operational: pass / fail / unknown |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| harness | interruptive | 51 | 50 | 50 | 50 | 47 | 8 / 43 / 0 | agent 39, ceiling 7, unknown 4, test_runner 1 | 1 | 1 / 8 / 42 | 4 / 1 / 46 | 2 / 1 / 48 | 3 / 0 / 48 | 39 / 8 / 4 |
| harness | normal | 69 | 69 | 69 | 69 | 68 | 2 / 67 / 0 | agent 68, unknown 1 | 0 | 1 / 2 / 66 | 3 / 0 / 66 | 1 / 0 / 68 | 1 / 1 / 67 | 68 / 0 / 1 |
| native | normal | 10 | 10 | 10 | 10 | 10 | 0 / 10 / 0 | agent 9, caller 1 | 0 | 0 / 0 / 10 | 0 / 0 / 10 | 0 / 0 / 10 | 0 / 0 / 10 | 10 / 0 / 0 |
| native | interruptive | 8 | 8 | 8 | 8 | 8 | 5 / 3 / 0 | caller 5, agent 2, provider_stall 1 | 1 | 3 / 5 / 0 | 2 / 3 / 3 | 2 / 2 / 4 | 4 / 0 / 4 | 7 / 1 / 0 |
| all | all | 138 | 137 | 137 | 137 | 133 | 15 / 123 / 0 | agent 118, ceiling 7, caller 6, unknown 5, test_runner 1, provider_stall 1 | 2 | 5 / 15 / 118 | 9 / 4 / 125 | 5 / 3 / 130 | 8 / 1 / 129 | 124 / 9 / 5 |
