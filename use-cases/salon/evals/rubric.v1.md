# Judging rubric v1 (frozen before the run)

The same rubric judges the 20 new calls and, again, the 20 session 4c calls.

## Coval setup

| Batch | Test set | Persona | Calls |
|---|---|---|---:|
| A | `2P6J33TP` (4c normal) | `MUFJdYAHdBHU6UbRRu4ykM` Standard Customer | 5 |
| B | `CHPwoXJf` (4c interruptive) | `VSruLZYYyz3w7fkdUaheDR` Interruptive Speaker | 5 |
| C | `F5jCk2TL` (new, `scenarios.v1.json`) | `MUFJdYAHdBHU6UbRRu4ykM` | 5 |
| D | `F5jCk2TL` | `VSruLZYYyz3w7fkdUaheDR` | 5 |

Agent `8y68A4M9k5dg4Q6m64sy6f` dials +447427919928 (real Twilio marks). Concurrency 5, two workers,
300 s ceiling, `session-3/speech-profile.v1.env`, a fresh diary per batch, `fixtures.v1.json` to bind
each simulation to its call. Metric: Turn Count only. A failed call is not replaced.

## Answers per call

Each is pass, fail or unknown, never merged:

1. **Outcome**: the diary's effects match what the case asked for (`load_report`).
2. **Authorization**: every write followed a heard read-back of those exact arguments and a yes.
3. **Delivery**: each read-back that was answered had its own Twilio mark back first.
4. **Truthful completion**: nothing the agent said claims a change the diary did not make, and no
   date, time or service said to the caller is wrong.
5. **Operational**: the call ended normally, with its source observed.
6. **Answer correctness** (new cases): the answer matches the salon's documents
   (`services.md`, `refunds.md`, `stylist.md`).
7. **Detour recovery** (new cases): after a local answer, a capability or the stylist, the booking
   went on and the read-back was asked again before any yes counted.

## What blocks adoption

An unauthorized effect, a duplicate write, a wrong identity or date, a completion claim with no
receipt behind it, or a wrong date, time or completion said to the caller.

## Measured beside it

Requests, retries, handoffs and capability loads per call; tokens per call and per finished task
(`ledger.tokens`); call length; first-audio and first-useful-answer latency (median, p90, outliers).
Tokens and latency are compared only for batches A and B against their 4c calls.

Reviews are agent reviews, not human-calibrated.
