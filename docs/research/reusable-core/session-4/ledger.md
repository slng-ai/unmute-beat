# Session 4 ledger

## Starting point

| Item | Value |
|---|---|
| Parent | PR #18, `feat/session-3-authoring`, `074a6c290ec1273334190e72dc32279b4e289d6a` (open, checked 2026-09-25T17:45Z) |
| Grandparent | PR #17, `feat/session-2-public-boundary`, `47f41923064f57fa01847785a46d14c1999a1fd5` (open) |
| Branch | `feat/session-4-node-agents-cc`, PR target `feat/session-3-authoring` |
| Speech profile | `session-3/speech-profile.v1.env`, sha256 `a168116910c50ec399dd0ccb654ac103d87b8a8387faa78701b57da0e6f28444` (unchanged) |
| `uv.lock` | sha256 `ff362a07b142fca7c305b4f7607475ae9d6979e65264fce0809694e42a063f49` |
| Native reference pin | `550dd67c4bae37901d93e6af069c2fd556a59b9a022a3a2677e83272f06496af` (`matrix/scenarios.v1.json`) |
| SDK | pydantic-ai-slim 2.46.0, pydantic-graph 2.46.0, pipecat-ai 1.11.0 |

## Baseline checks on the parent

All green except one inherited lint problem: `docs/research/reusable-core/session-3/package-scenario.py` had an unsorted import block and one unformatted line. Fixed with `ruff check --fix` and `ruff format`, no behaviour change.

- `uv run ruff check .`: 1 error (the file above), then clean.
- `uv run ruff format --check .`: 1 file, then clean.
- `core`: `ty check` clean, `python -m tests` 10 modules ok.
- `salon-graph`: `ty check` clean, `python -m tests` 18 modules ok (9.4 s wall).
- `faro-language-learning`: `ty check` clean, `python -m tests` 1 module ok.

## Coval allowance

Session 4 has its own cap of 20 calls. Used so far: **12** (see the table below).

Inherited run reconciled by ID on 2026-09-25T17:45Z, no relaunch:

| Run | Simulation | State at Coval | Counted in |
|---|---|---|---|
| `DDxDkFaSDhpvNc4Yax7vE6` (session-3 bulk stress) | `E2n7Wg6ZYpzwJgcpvukHnt`, `EAZZjLcMCxSoGHjfe7X4kV`, `L2T4QnHbJcCoUDoKJGScY9` | COMPLETED, audio present | session 3 |
| same | `UAeZiGa7wzMijRpQmENSLq` | still IN PROGRESS, no audio, 2 h 24 min after launch; run status IN PROGRESS | session 3, **inconclusive** |

The stuck simulation stays inconclusive. The local bot for that run stopped long ago, so it cannot still be serving the call.

| # | Job | Run | Simulations | Result |
|---|---|---|---|---|
| 1 | complex stress (K3Xwqi7g, VSruLZYYyz3w7fkdUaheDR, 1x1, concurrency 1), revision 044057e | `GYZdoiie3dVF9VuguVRyQi` | 1: `fymMyWTiyz6eqfK3RwRTRX` (call CA09f5c0958d564d9782dc10947e8e421c) | **Failed, critical.** The number read-back was queued behind the new holding line, counted as heard when that line's audio ended, and cut by the caller about 2 s in; their restated digits then authorised the lookup (write audit: question asked turn 3, segment `5a0c789242ed:3:8`, heard "that's two seven three four five four"). Also: the runner exited on a Coval HTTP 429 during a status poll and took the bot down 68 s into the call (at booking). No booking was made. |

Call 1 stopped further launches. Repairs on the same branch before any other call: core counts a segment that joined playing audio as heard only once its own words had time to play (`CallContext.note_drained`, regression `tests.test_continuation:a_read_back_queued_behind_a_holding_line_is_not_heard_when_it_ends`, which fails with the rule disabled); `coval_sim` retries a rate-limited read instead of ending the job. Offline suites and N1-N13 rerun clean.

Reserve call 2, logged before launch: the same frozen complex case on the repaired revision. Question: with the read-back delivery repaired, does the fragmented number, the day change at the booking read-back and any interruption end with one Saturday booking and every write on a heard question? Expanded count: 1.
| 2 | complex stress, reserve 1 (same job), revision 52eb245 | `MQagmvspVLRNbWM3kBtMmu` | 1: `NdkBKGCNZfxfrcsDLXKN8L` (call CA87afaa0f20e84e9faf5665b54829c100) | **Business outcome correct; complex trigger exercised.** Number in two pieces, read back in full, "Yes, that's correct", then the lookup (audit: asked turn 2, answered turn 3). Friday read back; "Actually Saturday morning" wrote nothing; the new date rule asked "tomorrow the 26th or Saturday October 3"; 26th read back, "Yes please, that's perfect", one booking BK0001 Saturday 09:00 (audit: asked turn 8, answered turn 9). The receipt was cut ("...Anything else I can help") and the caller said "Yes, please, that's great": joined to turn 9, the receipt was said again by code, no model request, no move, no second write. Coval: Changes match agreement YES; expected behaviours 0.67 (4 of 6, no explanations from the API); Answered question not asked again NO; Latency 1.79 s; 23 turns. Ended on the 180 s ceiling (Twilio REST hang-up returned 401; the line closed anyway). Finding: a turn continued over several pieces says one holding line per piece. |

Gate before bulk: the complex trigger was exercised and the business outcome passed on the repaired revision (52eb245). The five-call jobs run on that frozen revision.
| 3-7 | normal (kQ8b6nMZ, MUFJdYAHdBHU6UbRRu4ykM, 5 iterations, concurrency 5), revision 52eb245 | `jCBGSipW7FuUKu4VVtgnin` | 5: `GbULHRHPbL4taaEzHryhbt`, `BsdTQTsZ23otiZg2wHLYnu`, `jEisR2g6VdBaGZ9tjcX3b7`, `nnGmXURQxCYeELXcW46YBx`, `cyYESUGZwB8PjmxpGwmXXh` | **5 of 5 correct.** Each: lookup on a yes to the heard number read-back, exactly one booking (Friday the 2nd 09:00) on a yes to its heard read-back (Langfuse write audits). Expected behaviours 1.0 x5; Changes match agreement YES x5; Answered question not asked again YES x4, NO x1 (jEisR2g6: repeated holding lines talked over the caller's first number, who said it again). Coval latency 1.31-1.70 s (five at once: not comparable to single calls). |
| 8-12 | stress (K3Xwqi7g, VSruLZYYyz3w7fkdUaheDR, 5 iterations, concurrency 5), revision 52eb245 | `StET3kNNbtDswUeoMRWfgZ` | 5: `d2iF4s2nkV7AnmerVszL6M`, `DFMG2uFkuX4gqCM3usiKVu`, `8Y5mzxVqZXYU35SGPFi8tz`, `347RFS8MFxwzmxkbSZGg4T`, `Mm2VjV3LzFDkPqQ8LcWYUw` | **5 of 5 correct business outcome.** Each: number in pieces, lookup on a yes to the heard read-back, the Saturday change wrote nothing, the date rule asked which Saturday, exactly one booking on a yes to its heard read-back (3 on Saturday the 3rd, 2 on tomorrow the 26th, as the caller chose). Changes match agreement YES x5. Expected behaviours 1.0, 0.83, 1.0, 0.83, 1.0: both 0.83 say "the agent confirmed booking Friday"; the write audits show no Friday write, so a judge error, provoked by the doubled read-back below. Answered question not asked again **NO x5**. Coval latency 1.29-1.73 s (five at once). |

### What the stress review found (Langfuse `tts` and `chat` spans)

Two causes of repeated speech, neither a consent fault:

1. **Re-said after a barge-in.** The caller cut the offer, the read-back or the receipt; core said it again, as it must (a read-back counts only once delivered, a cut receipt is said again). Example: CAf30055 turns 8/9 and 14/15. Correct behaviour, but a judge hears a repeat.
2. **The model says the application's words itself.** In 4 of 11 calls, a model response carried both text and an operation call, and the text copied the fixed read-back ("One moment.Shall I book you in for a haircut, Saturday the 3rd at 9:00 AM?") or a holding line. The operation then said the read-back again 150 ms later. The model also starts replies with "One moment." or "Let me have a look." right after core's own holding line, so the caller hears it twice. Counted with `echo.py` over the call ids: text beside an operation 12 times in 11 calls, a holding line in model text 18 times.

Langfuse ledger (`concierge-tools/ledger.py`, the same tool as session 3's bulk ledgers):

| | Session 3 classifier, normal x4 | Session 4 node agents, normal x5 | Session 3 classifier, stress x3 | Session 4 node agents, stress x5 |
|---|---|---|---|---|
| Requests per caller turn | 1.79 | 1.69 | 1.76 | 1.83 |
| Input tokens per call | 8,887 | 32,702 | 15,177 (3 full calls) | 66,402 |
| Input tokens per caller turn | 1,226 | 3,406 | 1,314 | 4,676 |
| Pipeline turn latency p50 / p90 | 1.77 / 3.69 s | 1.14 / 2.62 s | 2.08 / 3.40 s | 1.24 / 2.21 s |
| Answer time p50 (work start to first speech) | 0.81 s | 0.67 s | 0.88 s | 0.72 s |
| Waited over 2 s before work started | 8 of 24 turns | 3 of 40 | 6 of 32 | 2 of 63 |

The ledger's write audit line reads the old audit attribute names and says "no application question"; the write audits above were read from the `write` spans directly.

## Load test (added 2026-09-26)

Allowance: you added 20 + 20 calls, so the session cap is now **60**. Used so far: 12. The 8 reserve calls from the first 20 stay in reserve.

Setup, the same for both load runs:
- Coval places and records the calls, and scores nothing (`coval_sim run --no-metrics`). Each call is judged by `concierge-tools/load_report.py` from its Langfuse session and its write records.
- Two jobs side by side, 20 calls at once, all into **one bot process** behind one tunnel (`coval_sim serve`, then `run --host`): normal (`kQ8b6nMZ`, `MUFJdYAHdBHU6UbRRu4ykM`) x10 at `--concurrency 10`, and stress (`K3Xwqi7g`, `VSruLZYYyz3w7fkdUaheDR`) x10 at `--concurrency 10`. Changed from the plan's two bots: both jobs use one Coval agent, and each `run` points that agent at its own tunnel, so a second tunnel could take the first job's later calls.
- Every call gets its own diary (`DIARY_PER_CALL=1`, checked offline by `tests.test_diary:calls_with_a_diary_each_share_nothing`, and live by every first booking being BK0001).

### Baseline load run, logged before launch

Question: under 20 calls at once, does the build we have (bot code identical to 52eb245; `git diff 52eb245 HEAD -- core use-cases/salon-graph/salon use-cases/salon-graph/bot.py` is empty) keep every write on a heard yes, book once per call, and how do latency, repeats and request chains look? Expanded count: 20 (12 -> 32 of 60).

This run measures, it does not gate. Its numbers are the baseline for the fixed run's pass criteria (plan step 4).
