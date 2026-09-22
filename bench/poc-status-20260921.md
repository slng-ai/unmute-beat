# Phase 01 status after the Pipecat continuation repair

**Superseded assessment:** see the [current POC report](poc-assessment-20260921.md)
for the 200-call native comparison, fresh microphone results and “Go for it” repair.
The implementation descriptions, results and testing sequence below are historical;
the current order is verification before booking or complaint handling.


**Latest follow-up, 21 September:** a new voice call exposed a discarded complaint
readback followed by an apology-only response. The [complaint-stall report](complaint-stall-20260921.md)
records the repair and shorter prompts. The user's final sequence now makes
verification a prerequisite for both booking and complaints; general questions
remain open. Current results are **13/13 offline modules, 23/24 recovery and 6/12
continuation**, with Ruff and ty passing. See [current evidence](verification-first-evidence/README.md).
Complaint quality, fresh audio acceptance and matched native Pipecat comparison
remain open. The results below describe the earlier transcript-retention repair.

2026-09-21. The transcript-retention and complaint-approval repairs are implemented.
All 13 offline modules, Ruff and ty pass. The six added Gemini conversation cases
pass. **Full POC acceptance remains open:** the final original recovery suite is
23/24, a same-time move still fails intermittently, and fresh microphone acceptance,
client playout measurement and a matched native Pipecat comparison remain outstanding.

This report supersedes completion claims in the older ledger, not the retained
historical results. Runtime: Pipecat 1.11.0, Pydantic AI 2.46.0, Python 3.14.5.
Pipecat owns aggregation, interruption, endpointing, audio and TTS; Pydantic AI owns
model execution and protocol history. No LiveKit implementation, new dependency,
public API change, transcript truncation or endpointing delay was introduced.

## What was repaired

### Accepted speech lost when SDK history merged

The old core sliced `all_messages()` or captured messages at the original history
length. Pydantic AI can merge consecutive compatible requests, so that length is
not the boundary of the new run. After cancellation, the slice could omit accepted
caller input and still mark its turn retained. The adapter then correctly trusted
that marker and did not carry the missing input forward.

`voice/core.py` now uses the SDK's public `new_messages()` boundary on successful
results, `RunCancelled`, and the event-stream handle after teardown. A run is retained
once; a caller-turn marker is added only when its accepted prompt is present in the
saved messages. Cancellation before SDK startup leaves input available for adapter
carryover. External task cancellation still propagates. Completed tool results stay
in the audit protocol; delivered-speech projection remains separate.

The Pipecat processor continues to consume every unseen user entry from
`LLMContextFrame`, including entries whose queued frames were discarded by an
interruption. It keeps the existing cancel-and-restart path. This repairs retention
without adding a silence buffer or changing streaming.

The [Pydantic message-history documentation](https://pydantic.dev/docs/ai/core-concepts/message-history/)
supports using the public history boundary. Its `enqueue()` schedules additional
content for a subsequent model request. The offline probe showed that the original
response can still stream first, so enqueue alone is not an interruption policy.

### Complaint verification before structured approval

The complaint agent could ask an ordinary spoken question about a redo, receive
“yes,” and hand off to verification without an application-owned complaint readback.
After verification there was no approved complaint to record. The write guard
prevented an unauthorized record, but the conversation stalled.

`salon/agents.py` now shares one eligibility predicate between offered handoff
destinations and execution. `complaints → verify` requires the existing stored
complaint approval. `SalonCall.complaint_approved` is a derived, read-only prompt
value, not another consent flag. The prompt explicitly distinguishes requested
resolution from approved readback and resumes recording after identification.

Real-model testing also exposed conflicting conversational guidance: quoting policy
could finish the reply before the readback, and a free redo could be treated as a
booking. The prompt now keeps a redo within the complaint flow and calls the
structured readback in the same response once details are known. Corrections require
a new readback. Abandonment and identity changes invalidate approval. The terminal
recording tool still uses the exact approved wording and emits one receipt.

## Reconciliation of the four supplied voice calls

These calls used the earlier C3 build, **before this repair**. Business outcomes
come from the corresponding local call records, not from an `answered` span alone.
The retained [outcome/provenance extract](pipecat-repair-evidence/live-outcomes.json)
omits contact fields; trace links retain the original evidence.

| UTC start / trace | Observed outcome | Interpretation |
|---|---|---|
| 17:36 [532451…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=532451c178735dfa1379c5fcb764067e) | Friday 10:00 booking, move to Saturday 10:00, complaint CX0001; three writes | The highlighted phone continuation actually recovered: the first model attempt at 17:36:22.558 lasted about 77ms; the restart at 23.559 included both fragments. This example demonstrates interruption, not loss by itself. |
| 17:37 [14036c…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=14036c1370b57cd94c313b5942b79815) | Saturday 14:00 booking; one write | Successful booking in this call. |
| 17:38 [3e1a3d…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=3e1a3d340c99f52ce879923d38bb0612) | No complaint; zero writes | Ordinary redo agreement led to verification without a structured complaint readback. The caller heard “Okay” after identification; the requested outcome was unfinished despite answered spans. |
| 17:40 [055ff5…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=055ff5320eee2ce481654acbc7051225) | Eventually complaint CX0001; one write | The country-code fragment at 17:41:25.652 was absent from the next model input at 28.857. Further retries lost prefixes too. The audit transcript retained them, while protocol history skipped the affected caller turns. Eventual success does not erase the information-loss defect. |

## Verification actually run

All live-model suites use fresh in-memory fixtures, Gemini 3.5 Flash Lite, priority
service, MINIMAL thinking and a fixed Madrid fixture clock. They do not exercise a
microphone, STT, TTS or client playback. Every run below, including failures, is
retained in [pipecat-repair-evidence](pipecat-repair-evidence/).

| Check | Result and retained evidence |
|---|---|
| Negative history-merging control | Old retention failed on missing `+34.`: `negative-retention.txt`. |
| Negative native Pipecat queue control | Old code lost the prefix while retaining the queued middle/suffix: `negative-queue.txt`. |
| Negative complaint gate control | Old handoff accepted verification before structured approval: `negative-complaint.txt`. |
| Expanded offline suite | **13/13 modules**, `offline.txt`. Two/three fragments, consecutive requests, identical utterances, pre-SDK cancellation, before-token and streaming cancellation, actual Pipecat discarded queues, completed-write receipts, external cancellation propagation, approval, correction, identity change and abandonment. |
| Ruff / ty / formatting | `ruff.txt`, `types.txt`, `format.txt`: clean. |
| Original recovery suite, initial repair prompt | **24/24**, `pipecat-repair-20260921T182220Z.jsonl`. |
| Added scenarios, first prompt | **3/6**: all phone cases passed; complaint routing/readback failed. `pipecat-continuations-20260921T182219Z.jsonl`. |
| Added scenarios, second prompt | **4/6**: policy-only replies still stalled two complaint cases. `pipecat-continuations-v2-20260921T182640Z.jsonl`. |
| Added scenarios, final prompt | **6/6**, `pipecat-continuations-v3-20260921T182723Z.jsonl`: three phone reconstructions and three approved complaint completions, one record/receipt each. One complaint had an unnecessary concierge detour before recovery. |
| Original recovery suite, final prompt | **23/24**, `pipecat-repair-final-20260921T182836Z.jsonl`. All complaint cases passed. One same-time move offered a list rather than moving the existing appointment. |
| Focused same-time move follow-up | **2/3**, `pipecat-move-followup-20260921T183007Z.jsonl`; the failure reproduced. The booking stayed on Friday, with no unintended write. This is an open model-behavior issue, not a passing acceptance result. |

The phone scenario uses a blocked scripted model for deterministic prefix
cancellation, then real Gemini for full-number readback and verification. It is a
hybrid transcript test. The offline tests separately exercise native Pipecat queue
interruption. Neither is fresh microphone acceptance. The first failed benchmark
did not save speech from its exhausted turn; its exception and prior turns were
retained. The benchmark now records each turn in `finally`, including failed turns.

The earlier [regression investigation](recovery-20260921.md) retained 15/24 for
baseline 8570aa5, 6/24 for the broken implementation, and 24/24 for the first repaired
Gemini 3.5 arm. [C3 testing](c3-20260921.md) also scored 24/24. Those are historical
samples, not guarantees that supersede the final 23/24 result here.

## Latency: deduplicated and separated by model/build cohort

There are **14 unique traces** across all supplied lists. The four 12:57–13:02 traces
appeared in both “before” and “new”; each is counted once. Historical trace time does
not prove a particular Git revision. The two earliest calls use Gemini 3.1, so they
are not a matched model baseline for Gemini 3.5.

| Cohort, UTC | Calls / caller turns | Model requests | Attempts / extra attempts | Caller→bot p50 / p90, n | Request TTFT p50 / p90, n |
|---|---:|---:|---:|---:|---:|
| 12:20–12:23, Gemini 3.1 | 2 / 29 | 49 | 33 / 4 | 1.456 / 2.773s, 26 | .781 / 1.097s, 45 |
| 12:57–13:02, earlier Gemini 3.5 | 4 / 30 | 53 | 33 / 3 | 1.154 / 1.922s, 29 | .626 / .764s, 50 |
| 14:58–15:59, regressed Gemini 3.5 | 4 / 36 | 75 total; 71 assigned | 38 assigned / 4 | 1.161 / 2.277s, 25 | .660 / .810s, 69 |
| 17:36–17:40, supplied C3 voice build | 4 / 30 | 56 | 38 / 8 | **1.149 / 2.394s, 26** | **.607 / .765s, 47** |

The current median differs from earlier Gemini 3.5 by only about **−5ms**. The p90
is **472ms higher (+24.6%)**. Small, different conversations and unpinned provider
routing prevent a causal speed claim. These are observations, not a post-repair
latency test or a native Pipecat comparison.

The four current calls have 30 answered and 8 interrupted agent attempts. Extra
attempts remain in the work count, not the caller-turn denominator. The regressed
cohort has four requests without a resolvable caller-turn parent; they are explicitly
unassigned. Older interrupted attempts lack a status. For current attempts use the
recorded `status`: the legacy boolean interruption attribute does not reliably mark
native task cancellation. Repair-turn counts by cohort are 1, 2, 5 and 1, based on
refusal/error evidence rather than repeated legitimate tools.

Caller→bot is Pipecat's server speech-marker measurement, **not client audibility**.
It can close on filler or entry speech. Request TTFT is the per-request core clock
and includes first output that is a tool call; the current sample has 29 tool-first
and 18 text-first measurements. Missing measurements are not zero. Quantiles use
nearest rank `ceil(p*n)-1`. Do not derive substantive-response latency by multiplying
or subtracting unrelated percentiles; matched substantive audio timing remains open.

### Cache and tool payloads

| Cohort | Requests with usage / all | Explicit cache field / all | Reported cached / canonical input tokens, assigned requests | Tool-output chars p50 / p90 / max, n |
|---|---:|---:|---:|---:|
| Earliest 3.1 | 45/49 | 20/49 | 70,816 / 167,836 | 78 / 1,440 / 1,529; 21 |
| Earlier 3.5 | 50/53 | 10/53 | 34,903 / 140,279 | 53 / 1,464 / 1,572; 26 |
| Regressed 3.5 | 72/75 | 10/75 | 32,592 / 244,939 | 98 / 1,471 / 1,504; 46 |
| Current C3 voice | 48/56 | 1/56 | 3,399 / 138,534 | 105 / 854 / 1,504; 28 |

Canonical usage avoids Langfuse aliases being added twice; its exported `total` can
be inflated. Cache fields absent from a request remain unknown, not a demonstrated
zero. Reported cache reuse is lower in the current sample, but these traces do not
establish whether dynamic prompts, schema/history changes, cache expiry or provider
conditions caused it. [WarnOnCacheBusts](https://pydantic.dev/docs/ai/harness/warn-on-cache-busts/)
is diagnostic, not a history repair; no new instrumentation package was added.

Payload sizes are measured once per actual TOOL observation with a captured output,
not each repeated copy in model history. The current sample has 28 captured outputs
from 29 tool events. UTF-8 byte counts equal character counts for these captures.
The largest across all cohorts is 1,572 characters. This gives no evidence of an
oversized-tool-return problem requiring
[ToolOutputLimits](https://pydantic.dev/docs/ai/harness/tool-output-limits/), which
addresses payload size rather than caller speech retention. No truncation or
summarization was introduced.

[metrics.json](pipecat-repair-evidence/metrics.json) contains every trace and cohort,
including all trace IDs. Reproduce it from retained timing/usage observations:

```bash
PYTHONPATH=. python bench/pipecat-repair-evidence/measure.py
```

The compressed input retains IDs, parentage, timestamps, usage, relevant attributes,
tool-output sizes and hashes. Full transcript payloads, contact fields and API
headers are omitted; original trace links and source hashes remain available.

## Phase 01 requirement-by-requirement status

| Requirement / contract | Implementation and actual evidence | Remaining closure condition |
|---|---|---|
| §1 baseline, SDK ledger | Existing snapshots/capability ledger; earlier baseline recovery results; current per-file snapshot and source/prompt/lock hashes. | Comparisons must identify actual builds, not assume `main` equals the desired baseline. |
| §2 state and effective inputs; C1/C2, V5 | Atomic validated state, nested invariants, schema-authored input validation and injected-input refusal, validated external responses, service-established identity. `test_state`, `test_salon`, `test_consent` pass. | No claim of real number ownership or production service authorization. |
| §3 trustworthy diary | Opaque slot IDs, capacity/overlap, eligibility, offer ownership, fixed clock, identity isolation, persistent cross-call records, idempotent operation tokens. `test_diary` passes. | In-memory fixture remains a POC oracle, not a production scheduler. |
| §4 phone; V6 | Spain-scope validation, readback consent, correction invalidation, missing caller ID and continuation retention. `test_state`, `test_consent`, `test_history`, `test_recovery`; added phone 3/3. | Fresh microphone split-number/correction/withheld-ID acceptance after repair. Prior recordings include a confirmed failure. |
| §5 legal flow; C4, V4/V8 | Per-request schemas and execution guards; one flow action; prerequisites/returns; partial JSON; abandonment; new shared complaint gate. Offline flow/recovery tests pass; added complaint 3/3. | Same-time move remains intermittent; unnecessary complaint routing detour observed. These block claiming uniformly reliable conversation behavior. |
| §6 bounded turns/effects; C5, V12 | Shared budgets/deadlines, bounded fallback, resistant-cancellation fencing and uncertain-effect reconciliation. `test_budget`, `test_core`, `test_history`, `test_recovery` pass. | Fresh live interruption around a write; no duplicate execution/receipt in real audio. |
| §7 measurement; C6 | Request/attempt/caller-turn separation, usage normalization, exact-count fixtures, raw failed benchmark rows, speech origins, deduplicated trace analysis. `test_bench`, `test_traces` pass. | Client playout, filler-versus-substantive audio gaps, old missing trace parentage/status, matched configuration comparison. |
| §8 reusable loop; C0/C3, V13 | Runtime-neutral core plus Pipecat adapter; request snapshots, reset history, owner completed/unserved returns, delivered-speech IDs and retained tool protocol. Core/adapter parity and all history tests pass. | Runtime playback estimates are not exact client playout. Second runtime proof belongs to Phase 02, not this repair. |

The handwritten implementation covers the Phase 01 boundaries. That does **not**
mean every acceptance row is closed. Full V1 portability, carrier routes, generated
providers and the public authoring/compiler contract remain later-phase work.

## Build provenance and testing this worktree

This checkout is detached at `c6e8cfd` with existing recovery/C3 changes plus this
repair. At inspection, local `main` points to `e78ac56`, not `8570aa5`. No branch was
switched or commit created. Benchmark headers record the actual uncommitted source,
prompt and lock digests; [snapshot.json](pipecat-repair-evidence/snapshot.json) records
the final tested files. The four supplied voice records used source
`e48b8cd1b52cb372f7d735bbec830716aa8f673b4908433770f12464e97d9b45` and prompt
`302da912713ad09c58578dc27bb9f29aab79ab182f1fe82a290fd2824e0d8a89`.

Restart the bot from this worktree to load the changed Python and prompts. An
already-running process on port 7861 still holds its imported code. With that port
free, the branch-local microphone run is:

```bash
cd /Users/nicoferdi/.codex/worktrees/517a/slng_voice_harness
SALON_FIXTURE=fresh uv run --env-file /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.env python bot.py --port 7861
```

Open `http://localhost:7861/client/`. Speak a phone number in two/three chunks with
short pauses, interrupt a readback and correct a digit group, then complete an
unverified complaint requesting a different stylist. Confirm the structured complaint
readback, then the phone. Expect one complaint receipt and no booking change. In a
separate call, abandon verification and ensure the old complaint never resumes.
Re-test an explicit same-time move and retain failures as well as successes.

For reproducible non-audio checks, run `python -m tests`, `ruff check .`, `ty check .`,
`python -m bench.recovery --repeats 3` and `python -m bench.continuations --repeats 3`
through the existing `uv run` environment; live suites need the same `.env` option.
The commands used here and logs are listed in the evidence README.

Acceptance requires fresh audio evidence for the repaired scenarios, resolution of
the remaining move behavior, and a matched **native Pipecat** build with the same
model, prompts/policies, fixtures, endpointing, STT/TTS and caller scripts. The
unidentified native trace already on disk cannot establish that comparison.
