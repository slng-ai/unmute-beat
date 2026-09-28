| Repair turned off | Regression | Exit with repair | Exit without |
|---|---|---:|---:|
| C0.1 a pending receipt is a refusal | `core` `tests.test_outcomes` | 0 | 1 |
| C0.1 a retry signal after dispatch is a refusal | `core` `tests.test_outcomes` | 0 | 1 |
| C0.2 no claim before the first await | `core` `tests.test_outcomes` | 0 | 1 |
| C0.3 a reconciled old receipt is said as current | `core` `tests.test_outcomes` | 0 | 1 |
| C0.3 a restore reopens a finished intent | `core` `tests.test_outcomes` | 0 | 1 |
| C0.4 a resource is keyed by its id alone | `core` `tests.test_outcomes` | 0 | 1 |
| C0.5 an estimate delivers without a simulation (core) | `core` `tests.test_delivery` | 0 | 1 |
| C0.5 an estimate delivers without a simulation (adapter) | `use-cases/salon-graph` `tests.test_marks` | 0 | 1 |
| C0.6 a rendered step streams model text | `core` `tests.test_speech` | 0 | 1 |
| C0.6 a rendered step may answer in text | `core` `tests.test_speech` | 0 | 1 |
| C1 the chair trigger is off | `use-cases/salon-graph` `tests.test_store` | 0 | 1 |
| C1 reconciliation files no void | `use-cases/salon-graph` `tests.test_store` | 0 | 1 |
| C1 a stale revision is not checked | `use-cases/salon-graph` `tests.test_store` | 0 | 1 |
| C2 admission has no ceiling | `use-cases/salon-graph` `tests.test_runner` | 0 | 1 |
| C2 a dead worker's call is never closed | `use-cases/salon-graph` `tests.test_runner` | 0 | 1 |
| C2 a campaign restores over a later change | `use-cases/concierge-tools` `tests.test_coval_sim` | 0 | 1 |
| C2 a campaign restores with calls still running | `use-cases/concierge-tools` `tests.test_coval_sim` | 0 | 1 |
