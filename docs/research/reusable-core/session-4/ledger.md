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
- Coval places and records the calls. Coval refuses a launch with no metric (`metric_id "" must be exactly 22 characters`) and scores the agent's four when none is named, so each run names Turn Count alone (`--metrics 7VHk6dTjvBcuV6XYPmaeGq`): a count, no judge. Each call is judged by `concierge-tools/load_report.py` from its Langfuse session and its write records.
- Two jobs side by side, 20 calls at once, all into **one bot process** behind one tunnel (`coval_sim serve`, then `run --host`): normal (`kQ8b6nMZ`, `MUFJdYAHdBHU6UbRRu4ykM`) x10 at `--concurrency 10`, and stress (`K3Xwqi7g`, `VSruLZYYyz3w7fkdUaheDR`) x10 at `--concurrency 10`. Changed from the plan's two bots: both jobs use one Coval agent, and each `run` points that agent at its own tunnel, so a second tunnel could take the first job's later calls.
- Every call gets its own diary (`DIARY_PER_CALL=1`, checked offline by `tests.test_diary:calls_with_a_diary_each_share_nothing`, and live by every first booking being BK0001).

### Baseline load run, logged before launch

Question: under 20 calls at once, does the build we have (bot code identical to 52eb245; `git diff 52eb245 HEAD -- core use-cases/salon-graph/salon use-cases/salon-graph/bot.py` is empty) keep every write on a heard yes, book once per call, and how do latency, repeats and request chains look? Expanded count: 20 (12 -> 32 of 60).

This run measures, it does not gate. Its numbers are the baseline for the fixed run's pass criteria (plan step 4).

Attempt 1 (label `session-4-load-baseline`, 2026-09-25T20:44Z): both launches were refused by Coval for the empty metric list, and the runner's lookup for a run that might have started crashed on `--order-by -create_time` (read as a flag). `coval runs list` for the agent shows no run after `StET3kNN`: **no call was placed, none counted.** Fixed: `--metrics` replaces `--no-metrics`, and the lookup sends `--order-by=-create_time` (`tests.test_coval_sim:test_named_metrics`). The two `intent` lines stay in `coval-runs.jsonl`; the relaunch uses label `session-4-load-baseline-b`.

### Baseline load run result (bot code = 52eb245)

| Run | Job | Simulations |
|---|---|---|
| `bV22oPLohTsYUqCWBXpzNz` | normal x10, concurrency 10 | 10 |
| `3hZu7PPkjgn2RyzVVnC8A2` | stress x10, concurrency 10 | 10 |

Used so far: **32** of 60. Report: [`load/baseline/report.md`](load/baseline/report.md) (`load_report.py`, Langfuse and write records only).

**Deviation, found after the run:** `coval_sim serve` was started without the speech profile's env file, so the bot ran on its own defaults (`soniox/speech-ai:rt-v5`, `cartesia/sonic:3.5`, `eu-north`), not `session-3/speech-profile.v1.env` (`deepgram/nova:3`, `slng/fish/tts:s2.1-pro`, `eu-west`) that calls 1-12 used. Engine measures (consent, echo, holding lines, chains, requests per turn) do not depend on the speech provider; latency and misheard digits can. So the fixed run uses the **same defaults** as this baseline, for a clean A/B of the code alone, and neither load run is compared with calls 1-12 on latency.

Findings:
- 38 writes, **0** without a matching question put on an earlier turn. 19 of 20 calls made exactly one booking; every first booking BK0001 (no shared diary).
- `Khs7tVuS` (stress): the simulated caller went silent after 31 s and never spoke again; Coval still shows it IN PROGRESS (same as session 3's `UAeZiGa`). Counted **inconclusive**. Harness gap it shows: 150 s of silence with no re-prompt.
- `UD4fMgUZ` (stress): Friday booked on "Yes. That works for me." to its heard read-back; the caller then asked for Saturday and the call reached 180 s at the move read-back. 85 s went on the phone number: speech-to-text heard "672 1" for "671"; every wrong number was refused, the lookup ran only on "Yes, that's correct".
- Latency held at 20 at once: turn latency p50 1.15 s, p90 2.62 s (187 turns); model request p50 0.66 s. Load average peaked at 11 on 15 cores.
- Repeats: model text beside an operation 19 times in 10 calls; replies opening with a holding line 32; caller turns with more than one holding line 8.
- Chains code could skip: `collect > book_appointment` 24, `collect > lookup_customer` 18, `collect > move_appointment` 1.
- Requests: 1.78 completed per caller turn; 115 of 506 started were cancelled because the caller went on (endpointing).
- 19 of 20 calls logged "call ceiling of 180s reached": Pipecat saw 1 client disconnect in 20, so a call whose caller hung up stayed open until the bot's own limit. It inflates call length, not turn latency.

### What changes next, logged before any launch

Your notes after the baseline: too many holding lines, sentences said more than once, and endpointing may make the bot talk over the caller.
1. Fixed run: 20 calls, same shape and speech defaults as the baseline, fixes F1-F4 only (52 of 60).
2. Endpointing run: the 8 reserve calls (4 normal + 4 stress, fixed build), with the silence the voice detector waits before smart-turn decides raised from 0.2 s to 0.6 s (`VAD_STOP_SECS`, new env knob, default unchanged). Question: do fewer caller turns arrive in pieces, fewer requests get cancelled, and fewer turns get interrupted, and at what latency cost?

### Fixed load run, logged before launch

Revision `a04c0ff` (fixes F1-F4, each with a test that failed before it). Offline gates on it: ruff and ty clean in every member; `python -m tests` core 11/11, salon-graph 11/11, concierge-tools 5/5, Faro 1/1, salon-pipecat-flows 1/1; N1-N13 unchanged (`session-4/matrix-fixed/`: every harness row passes, native N9 fails, N13 not exercised); N12 harness turn max 31.8 ms (ceiling 50 ms), trace tolerance still fails as before.

Same shape and speech defaults as the baseline (one bot, `--metrics 7VHk6dTjvBcuV6XYPmaeGq`, normal x10 + stress x10 at concurrency 10). Label `session-4-load-fixed`. Expanded count: 20 (32 -> 52 of 60).

Pass criteria, fixed before launch:
- 0 writes without a matching question on an earlier turn.
- Every conclusive call ends with exactly one booking on the day the caller chose.
- 0 replies with model text beside an operation call.
- 0 caller turns with more than one holding line.
- 0 `collect > book_appointment` and `collect > lookup_customer` chains.
- Completed requests per caller turn below the baseline's 1.78.
- Turn latency p50 and p90 no worse than the baseline's 1.15 s and 2.62 s (within 0.1 s).
- No harness error.

### Fixed load run result (revision a04c0ff)

| Run | Job | Simulations |
|---|---|---|
| `oPhvY4RQy2VZWfWVQph65Z` | normal x10, concurrency 10 | 10 |
| `np7G4HtyL4n9jcwRn9Uk5V` | stress x10, concurrency 10 | 10 |

Used so far: **52** of 60. Report: [`load/fixed/report.md`](load/fixed/report.md). The baseline stress runner ended with exit 3 (still IN PROGRESS at Coval after 45 min, the stuck `Khs7tVuS`); not relaunched.

| Measure | Baseline (52eb245) | Fixed (a04c0ff) | Criterion |
|---|---|---|---|
| Writes without a matching earlier question | 0 of 38 | 0 of 35 | met |
| Calls with exactly one booking | 19 of 20 (normal 10, stress 9) | 16 of 20 (normal 10, stress 6) | **not met** |
| Model text beside an operation | 19 in 10 calls | 0 | met |
| Replies opening with a holding line | 32 | 0 | met |
| Caller turns with more than one holding line | 8 | 0 | met |
| `collect > lookup_customer` / `collect > book_appointment` | 18 / 24 | 0 / 8 | half met |
| Completed requests per caller turn | 1.78 | 1.56 | met |
| Turn latency p50 / p90 | 1.15 / 2.62 s | 1.29 / 2.68 s | **p50 not met** |
| Model request time p50 / p90 | 0.66 / 0.83 s | 0.80 / 0.98 s | (not a criterion) |
| Continuation pieces answered / turns cut into | 121 / 64 | 118 / 63 | (endpointing, unchanged) |
| Input tokens per call | 45,683 | 35,547 | |

The four stress calls with no booking, read from Langfuse and Coval:
- `QeJTxmKm`: the caller said "Nine o'clock is perfect" and "Yes" before any read-back; code read the booking back (correct: a yes counts only after a heard question); the simulated caller then stayed silent for 70 s until the 180 s limit. A deadlock: question asked, caller silent.
- `gyaSM3Ax`, `Dui5EEvb`: still mid-conversation at 180 s (17 and 13 caller turns; `Dui5EEvb` ran out as the caller said yes).
- `7oRuKHUE`: the simulated caller answered "Of course" and "The rest of the number" instead of digits; the bot said "Please give me the rest of the digits" three times; the caller hung up at 128 s.

No wrong write in any of them. The latency p50 rose by 0.14 s, the same as the model's own request time; the fixes add no request, and F3 holds text on operation steps (0.16 s p50, 0.22 s p90, first token to end of reply), so part of the rise may be F3. Not separable from this run.

Other finding: `b7BSSwtX` (booked correctly): the concierge said "I've got you down for a haircut this Friday morning" before any booking; the unbacked-claim check caught it and said the withheld line. Honest, but it sounds clumsy.

The remaining `collect > book_appointment` chains (8): not investigated yet.

### Endpointing run, logged before launch

Revision a04c0ff with `VAD_STOP_SECS=0.6` (was 0.2). Normal x4 + stress x4, concurrency 4 each, one bot, `--metrics 7VHk6dTjvBcuV6XYPmaeGq`, label `session-4-load-endpointing`. Expanded count: 8 (52 -> 60 of 60; no reserve left after this).

Question: per caller turn, do fewer runs answer a continuation piece (fixed: 118 over 202 caller turns), fewer runtime turns get cut into (63), and fewer requests get cancelled (132 of 447), and what does turn latency cost (fixed: p50 1.29 s)? 8 calls is a direction, not proof.

### Endpointing run result (a04c0ff, `VAD_STOP_SECS=0.6`)

| Run | Job | Simulations |
|---|---|---|
| `CdR8tupvBsxEYD8Lqhfies` | normal x4, concurrency 4 | 4 |
| `Qprjd3ZJzBe2k4yZNmNuQi` | stress x4, concurrency 4 | 4 |

Used: **60** of 60. No reserve left. Report: [`load/endpointing/report.md`](load/endpointing/report.md).

| Per caller turn | Fixed, 0.2 s (20 calls at once) | Endpointing, 0.6 s (8 calls at once) |
|---|---|---|
| Runs answering a continuation piece | 0.58 (118 / 202) | 0.29 (24 / 84) |
| Runtime turns cut into by the caller | 0.31 (63 / 202) | 0.27 (23 / 84) |
| Requests cancelled | 30% (132 / 447) | 13% (20 / 154) |
| Completed requests per caller turn | 1.56 | 1.60 |
| Turn latency p50 / p90 | 1.29 / 2.68 s | 1.84 / 2.47 s |
| Writes without a matching earlier question | 0 of 35 | 0 of 15 |
| Calls with exactly one booking | 16 of 20 | 7 of 8 |

Direction from 8 calls, not proof, and the load differs (8 at once against 20). The longer wait halves the turns that arrive in pieces and cuts cancelled requests from 30% to 13%, at about +0.55 s on the median turn (p90 is lower). `ArS5ZMnu` (no booking): 129 s went on the phone number; the caller then answered the booking read-back with "9:00 is perfect" instead of a yes, it was asked again, and the call reached 180 s.

The 8 `collect > book_appointment` chains left in the fixed run are not read-backs: in each, code had already read the booking back, the caller said yes, and the model recorded the same slot again before calling the operation (the yes). One redundant `collect` request each; nothing code could skip.
