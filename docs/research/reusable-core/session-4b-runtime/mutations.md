| Repair turned off | Regression | Exit with repair | Exit without |
|---|---|---:|---:|
| B1 a cancellation after dispatch is recorded as a refusal | `core` `tests.test_intents` | 0 | 1 |
| B1 an invalid reconciliation answer clears the guard | `core` `tests.test_intents` | 0 | 1 |
| B1 a read is recorded as an intent to reconcile | `core` `tests.test_tickets` | 0 | 1 |
| B2 a later change does not supersede an earlier receipt | `core` `tests.test_tickets` | 0 | 1 |
| B2 a lookup counts as a standing change | `core` `tests.test_tickets` | 0 | 1 |
| B2 a withheld clause cuts the step's previous fixed line | `core` `tests.test_tickets` | 0 | 1 |
| B3 the aggregate stop signal delivers | `core` `tests.test_delivery` | 0 | 1 |
| B3 a clear does not void pending marks | `use-cases/salon-graph` `tests.test_marks` | 0 | 1 |
| B3 the reply may begin before the mark | `use-cases/salon-graph` `tests.test_marks` | 0 | 1 |
| B3 a mark with no media before it delivers | `core` `tests.test_delivery` | 0 | 1 |
| B4 read-backs are not bounded | `use-cases/salon-graph` `tests.test_recovery` | 0 | 1 |
| B4 each joined piece gets a fresh budget | `use-cases/salon-graph` `tests.test_recovery` | 0 | 1 |
| B4 the ceiling owner does not cancel the timer | `use-cases/salon-graph` `tests.test_ceiling` | 0 | 1 |
| B5 a reconciled flag alone excuses a write | `use-cases/concierge-tools` `tests.test_load_report` | 0 | 1 |
