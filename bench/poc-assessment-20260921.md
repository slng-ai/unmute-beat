# POC assessment and native Pipecat comparison

21 September 2026. This is the current assessment. Earlier reports retain the
failures and intermediate implementations; their acceptance summaries do not
describe the current source.

**Latest implementation (22 September):** the authorized code-only optimization
now shares the model client, executes clear tracked approvals through the same
validated business handlers without a provider request, and constrains booking
speech through typed outputs. Existing prompt files, knowledge and authored tool
descriptions are unchanged. The native example remains frozen; exact model-visible
schema parity is not claimed. See the [optimization evidence](optimization-evidence/README.md)
for the current validation and fresh comparison. Older benchmark tables below are
explicitly historical and remain useful as the pre-optimization reference.

The application remains **Pipecat 1.11.0 + Pydantic AI 2.46.0**. Pipecat owns
audio, endpointing, aggregation, interruption and TTS. The shared core owns typed
state, model execution, tools, history and routing. No LiveKit runtime, dependency
addition, endpointing delay or transcript summarization was introduced.

## Current behavior and the causes of the regressions

Public questions need no identification. Booking, moving, cancellation and
complaints require phone readback and caller confirmation first. The original
request resumes after lookup; an unchanged verified caller skips that step.
This confirms a contact number, not ownership of it.

Complaints collects missing facts, reads back caller-sourced wording, waits for
approval, and records once. Its fixed receipt identifies the record and explains
that the requested resolution is pending salon review. The complaint specialist
cannot suggest or book a free redo. A caller asking for a free upcoming visit
can have that request recorded without changing the appointment.

The observed regressions had different causes:

1. **Lost caller fragments:** positional slicing of SDK history assumed old
   messages kept their original length. Pydantic AI merges compatible messages,
   so accepted input could fall outside the retained slice. The core now uses
   `new_messages()` after stream teardown, saves each run once, and marks input
   retained only when it was actually saved. Pre-SDK cancellation leaves input
   for the Pipecat adapter to carry forward. Completed tool receipts survive.
2. **Complaint silence and wrong remedies:** model text could finish a reply
   with an apology or a policy answer before the structured next action. The
   old injected refund document also encouraged redo offers the service could
   not fulfil. A typed string alone did not prevent invented facts or promises.
   Complaint output now goes through a small set of typed functions: KB guidance,
   caller-excerpt readback, recording and goodbye. Provider preambles are kept
   out of TTS. The original policy document remains archived, but the active
   complaint guidance reflects the authority requested for this POC.
3. **Handoff stalls:** a receiving agent sometimes narrated that it had handed
   the caller elsewhere. The existing request-instruction path now supplies an
   application-owned ownership/return status, without inserting caller input twice.
4. **Untracked phone questions:** comparison qualification exposed the same
   plain-text escape in verification. The model could speak a phone question
   without establishing the application's pending question. One yes then led
   to another readback; one response echoed internal routing text. Verification
   now uses typed readback and missing-information guidance, with free provider
   speech suppressed. One complete readback establishes the question that the
  following agreement answers.
5. **“Go for it” rejected:** the latest microphone booking call reached the model
   correctly, but the application's bounded approval matcher omitted this phrase.
   It now accepts that complete reply for the pending readback. Corrections,
   questions and replies with a different time remain refused. This was repaired
   after the matched comparison; its separate [evidence](approval-evidence/README.md)
   includes a negative control and three one-request Gemini completions.

Prompts remain short role instructions. Application code owns approval, identity,
legal transitions, write receipts and the verification/complaint speech boundary.
The extractive complaint readback can sound less polished with fragmented STT;
it establishes text provenance, not semantic completeness. Caller approval still
matters.

## Trace-to-business reconciliation

| Trace | Observed business outcome and defect |
|---|---|
| [532451…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=532451c178735dfa1379c5fcb764067e), 17:36 UTC | Booking, move and complaint: three writes. The highlighted two-part phone actually recovered in the restarted attempt. It demonstrated interruption, not a loss by itself. |
| [14036c…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=14036c1370b57cd94c313b5942b79815), 17:37 | One successful Saturday booking. |
| [3e1a3d…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=3e1a3d340c99f52ce879923d38bb0612), 17:38 | No complaint recorded. Ordinary redo agreement was not approval of an application-owned complaint readback. |
| [055ff5…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=055ff5320eee2ce481654acbc7051225), 17:40 | Eventually one complaint. The country-code fragment was absent from a later model input despite remaining in the audit transcript; this reproduced the history defect. |
| [c0b18b…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=c0b18b8715dc95019c929312074e8ea4), 18:39 | Discarded complaint readback and apology-only reply; see the retained [stall investigation](complaint-stall-20260921.md). |
| [051b9e…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=051b9e25ee1c3ff97f136e4a3911f834), 19:35 | Three writes: Friday booking, Saturday move, complaint. The complaint invented Saturday as the past haircut date and a redo remedy. A free-redo offer led to an eight-request exhausted attempt. The Saturday booking remained; no free conversion occurred. The build hashes matched the then-current source. |
| [4304e7…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=4304e71354352e37c10253f8a5879a3b), 20:51 | Fresh voice: phone readback/confirmation, complaint readback/approval, one CX0001 receipt and one call-state write. The next fresh call reset the shared fixture before the first call's cleanup snapshot; the empty final snapshot does not negate the captured write. |
| [85eca3…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=85eca3e4b3657cc9c31208754c361915), 20:52 | Fresh voice: Friday 13:00 booking moved to Monday 28th 10:00. Two “Go for it” replies were incorrectly refused before “Yes” succeeded. Two real writes; no duplicate booking. |

The 19:35 row is supported by 180 original observations over four pages and the
matching local call record. Its [retained evidence](typed-complaint-evidence/README.md)
separates runtime status, speech and actual diary writes. An answered span is not
proof that the requested business outcome was correct.

## Verification on the current source

| Check | Actual result / evidence |
|---|---|
| Offline regressions | **All 14 modules pass after code optimization**; [output](optimization-evidence/offline.txt). Network model requests are disabled by the runner. |
| Style, formatting, types | Ruff, formatting and ty pass; [Ruff](optimization-evidence/ruff.txt), [format](optimization-evidence/format.txt), [ty](optimization-evidence/types.txt). |
| Gemini recovery | **24/24** after the optimization, three repetitions per scenario; [retained run](optimization-evidence/recovery.jsonl). A subsequent identity-change guard is also covered offline and by comparison qualification. |
| Gemini continuation | **24/24** after the optimization, three repetitions: split-phone completion, complaint entry, upcoming-booking complaint, guidance, correction and abandonment; [retained run](optimization-evidence/continuations.jsonl). The subsequent identity-change guard is covered offline. |
| New approval replay | **3/3**, one real model request per “Go for it” completion; pending question set up by a scripted response. [Evidence](approval-evidence/README.md). |
| Verification negative control | Allowing free model speech emits an untracked phone question before the real readback and fails; [output](native-evidence/verification-negative-control.txt). |
| Other negative controls | Retention/queue/approval regressions in [repair evidence](pipecat-repair-evidence/README.md); complaint provenance and emitted-frame failures in [typed complaint evidence](typed-complaint-evidence/negative-controls.txt). |
| Native comparison qualification | Three repetitions of five scenarios: POC **15/15**, native **10/15**. Retained separately in `native-evidence/qualification-v3.json.gz`; not pooled into measurement. |
| Fresh microphone acceptance | Complaint completes; booking/move completes after a phrase-recognition defect. Fresh audio after the phrase repair, split-number/correction/withheld-ID, write interruption and client playout remain open. |

The recovery and continuation runs use Gemini 3.5 Flash Lite, MINIMAL thinking,
priority service, a fresh diary and a frozen Madrid clock. Split-phone replay
combines deterministic prefix cancellation with real-model completion. Separate
offline cases exercise Pipecat's actual interruption queue. These are not live
STT, TTS or browser playback tests.

## Native comparison

The user-selected [salon-concierge reference](https://github.com/slng-ai/unmute/tree/2ddc7b5c9f15e307a470ff9013200bea1a3000e9/examples/salon-concierge)
is compiled to native **Pipecat Workers/Flows**. The POC defines the knowledge and
policy contract. Both arms use the exact POC service information, complaint KB,
diary, phone validation, synthetic identity, frozen clock and caller scripts.
Both use the same installed Pipecat runtime, Gemini 3.5 model, Express Mode client,
MINIMAL thinking, priority header and unset generation defaults. Native retains
its own model-authored speech/consent and native execution primitives; the POC
retains its typed flow and application-owned approval. Prompts are simplified
for each implementation, not byte-identical across different tool interfaces.

The original external repository and environments were not modified. The local
overlay, exact effective settings and source hashes are documented in the
[comparison evidence](native-evidence/README.md). Compiler target 1.10 is exercised
on the shared installed 1.11 runtime. Qualification checks that combination.

The first cohort was stopped for a native implicit 4,096-token cap that the POC
did not have. The cap and an incorrect time-preference tool description were
corrected before a fresh freeze. All diagnostic failures remain archived and
are excluded from comparative latency conclusions.

The **pre-optimization** corrected cohort follows the predeclared [measurement protocol](native-evidence/MEASUREMENT.md):
20 paired calls per scenario, five scenarios, sequential execution with alternating
arm order, all failures retained. The runner replaces audio with a text sink.
Full turn processing, first submitted speech including filler, and first provider
output are different metrics. Both frozen builds remained unchanged during all
200 calls; the later “Go for it” change is outside this cohort.

| Scenario | POC / native passed | POC full-turn p50 / p90 | Native full-turn p50 / p90 |
|---|---:|---:|---:|
| Public hours | 20 / 20 | .656 / .742s | .626 / .744s |
| Verified complaint | 20 / 17 | .876 / 1.418s | 1.188 / 1.414s |
| Complaint through verification | 20 / 9 | 1.224 / 1.437s | 1.328 / 3.342s |
| New booking | 20 / 18 | 1.360 / 1.883s | 1.148 / 1.589s |
| Same-time move | 20 / 18 | 1.913 / 2.096s | 2.193 / 2.427s |

Each scenario has 20 calls per arm. **POC 100/100, native 82/100** on these fixed
scripts and bounded checks; 360 versus 509 requests. Native failures were incomplete
phone readbacks, redundant complaint approval, and same-time moves using the wrong
preference. All calls, including failures, contribute to the point estimates above.
The fresh “Go for it” failure illustrates what the five scripts did not cover.

The predeclared latency gate **did not pass**. On successful paired calls, booking
was slower with 95% intervals of +121–270ms for median and +159–389ms for p90.
Complaint/move median and complaint-through-verification p90 improved, but no
scenario had both p50 and p90 upper bounds at or below zero. Detailed intervals,
request-first-output and first-speech measurements are in the
[comparison report](native-evidence/README.md) and [results](native-evidence/results.json).
The evidence supports better observed script correctness, not an overall latency
or voice-quality superiority claim.

## Code-only optimization comparison (22 September)

The separately frozen 200-call cohort completed **100/100 POC and 80/100 native**,
with **240 versus 519 provider requests**. Existing prompts, knowledge and tool
prose were unchanged; typed output wrappers and deterministic approval execution
changed only the POC. The native overlay remained unchanged throughout collection.

Booking p50/p90 improved from the native **1.213/1.686s** to **0.699/1.309s**.
Same-time moves and complaints through verification also passed the declared
zero-regression p50/p90 interval rule. Public questions did not establish parity;
verified complaints improved median latency but their p90 interval crossed zero.
Overall zero-regression acceptance therefore remains open. First submitted speech
includes filler, and some first-speech point estimates remain slower even where
full-turn completion improved. See [all metrics and intervals](optimization-evidence/README.md).

Local approval steps keep input, consent/identity checks, write recovery and
receipts; they do not call the provider solely to execute an already approved
operation. That is an implemented typed-flow advantage. The code has not made
individual provider responses faster, and no matched audio/client-playout result
is inferred from these transcript runs.

## Historical voice latency and payloads

These cohorts are deduplicated by trace ID. The four 12:57–13:02 traces appeared
in both original user lists and are counted once. They are observations from
different conversations, not matched A/B trials or evidence about the repaired
microphone build. Median is ordinary p50; p90 uses nearest rank.

The earlier same-source recovery runs also do not close the current model
comparison. The last retained pair passed
[23/24 on Gemini 3.1](recovery-evidence/recovery-bound-31-20260921T164916Z.jsonl)
and [24/24 on Gemini 3.5](recovery-evidence/recovery-bound-35-20260921T164914Z.jsonl),
but predates the current prompts and repairs; only the 3.5 header declares
priority service. Completion-plan §7.3 has since received a separately
matched 3.1/3.5 comparison on the frozen core. These historical results cannot
isolate a model effect or establish current 3.1 acceptance.

| Model / cohort (UTC) | Calls / caller turns | Model requests | Caller→bot p50 / p90 (n) | Request TTFT p50 / p90 (n) |
|---|---:|---:|---:|---:|
| Gemini 3.1, 12:20–12:23 | 2 / 29 | 49 | 1.456 / 2.773s (26) | .781 / 1.097s (45) |
| Gemini 3.5, earlier 12:57–13:02 | 4 / 30 | 53 | **1.154 / 1.922s (29)** | .626 / .764s (50) |
| Gemini 3.5, regressed 14:58–15:59 | 4 / 36 | 75; 71 assigned | 1.161 / 2.277s (25) | .660 / .810s (69) |
| Gemini 3.5, C3 voice 17:36–17:40 | 4 / 30 | 56 | **1.149 / 2.394s (26)** | .607 / .765s (47) |
| Gemini 3.5, complaint regression 19:35 | 1 / 13 | 43 | 1.757 / 2.637s (11) | .628 / .728s (40) |
| Gemini 3.5, fresh complaint 20:51 | 1 / 4 | 8 | .924 / 1.680s (3) | .582 / .623s (6) |
| Gemini 3.5, fresh booking/approval loop 20:52 | 1 / 8 | 17 | 1.128 / 2.353s (6) | .601 / .661s (17) |

The 17:36 cohort's median is 5ms lower than the earlier 3.5 sample; p90 is 472ms
higher. Neither difference establishes causality. The latest pre-repair voice
call had 16 attempts: 12 answered, three interrupted and one exhausted, including
four caller turns with repair evidence. The preceding four-call cohort had 30
answered and eight interrupted attempts. Attempts are not extra caller turns.
Legacy interruption booleans are incomplete; recorded attempt status takes priority.

Caller→bot uses Pipecat server speech markers and can end at filler or entry speech.
It is not first useful audio or client audibility. Request TTFT may end at a tool
call; the latest trace has 35 tool-first and five text-first measurements.

| Cohort | Usage / all requests | Explicit cache field / all | Reported cached / canonical input tokens | Tool output chars p50 / p90 / max |
|---|---:|---:|---:|---:|
| Earlier Gemini 3.1 | 45/49 | 20/49 | 70,816 / 167,836 | 78 / 1,440 / 1,529 |
| Earlier Gemini 3.5 | 50/53 | 10/53 | 34,903 / 140,279 | 53 / 1,464 / 1,572 |
| Regressed Gemini 3.5 | 72/75 | 10/75 | 32,592 / 244,939 | 98 / 1,471 / 1,504 |
| C3 voice | 48/56 | 1/56 | 3,399 / 138,534 | 105 / 854 / 1,504 |
| Latest complaint regression | 40/43 | 11/43 | 43,834 / 160,472 | 108 / 1,290 / 1,504 |

Missing cache fields remain unknown. Canonical usage avoids adding aliases twice.
These payloads do not support adding output truncation or summarization. The
existing evidence also does not isolate a cause of cache variation. The matched
transcript rows lack symmetric POC/native cache capture, so no matched cache or
token-cost comparison is claimed. Reproducible metrics and parentage limitations
are retained in [historical measurement](pipecat-repair-evidence/metrics.json) and
the [complaint-regression metrics](typed-complaint-evidence/trace-metrics.json).
The two newest calls are separate small samples: the complaint has two interrupted
attempts and no repair turn; booking has eight answered attempts and three repair
turns. Their cache/payload figures and [metrics](approval-evidence/trace-metrics.json)
are retained with the approval investigation. They predate the phrase fix.

The separate compiler-worktree trace reader was repaired on 22 September;
[before/after evidence](reader-evidence/README.md) reproduces its inflated N4
token total and upper-middle p50. It now agrees with canonical usage on all 50
historical requests that contain usage data. Missing counts remain unknown,
characters stay separate, and span lifetimes are not called client playout.
The unavailable historical LiveKit export remains an explicit evidence gap.

## Phase 01 requirement ledger

| Requirement | Implementation / evidence | Remaining boundary |
|---|---|---|
| §1 repeatable baseline and SDK capabilities | Source/prompt/knowledge/lock digests, installed SDK probes, baseline 8570aa5 history and all failed cohorts retained. Corrected comparison freezes both arms. | A branch name or timestamp alone does not identify a tested build. Model alias and provider region remain unpinned. |
| §2 state, effective inputs, C1/C2 / V5 | Atomic candidate commits, default/nested validation, authoritative injection, schema input refusal, typed external responses, identity-bound verification. `test_state`, `test_salon`, `test_consent` pass. | Supported schema subset is explicit; this is not a public compiler or production authentication service. |
| §3 trustworthy service oracle | Service-owned slots, offer context, hours/duration/overlap, preference before cap, fixed clock, separate fresh/continuing fixtures, idempotent operation tokens. `test_diary` passes. | In-memory salon fixture with one resource; production scheduling is outside Phase 01. |
| §4 phone / V6 | Spain-scoped structural validation, exact tracked readback, correction invalidation, missing-country guidance, split-input retention. Current verification negative control and recovery checks pass. | Fresh microphone split-number, corrected-number and withheld-ID acceptance; contact confirmation does not prove ownership. |
| §5 legal flow / C4 / V4/V8 | Availability and execution guards agree; successful outcomes control movement; one response action, prerequisite return, abandonment, partial-JSON refusal. Verification precedes booking/complaints. `test_flow`, `test_graph`, `test_recovery` pass. | Finite real-model samples do not prove every conversational paraphrase. |
| §6 bounds and effects / C5 / V12 | Shared requests/tools/hops/deadline, fixed exhaustion response, bounded cancellation, stale-commit fencing and uncertain-write reconciliation. `test_budget`, `test_core`, `test_history`, `test_recovery` pass. | Fresh voice interruption around a write and confirmation still needs acceptance evidence. |
| §7 measurement / C6 | Request/attempt/caller-turn separation, usage normalization, exact-count fixtures, retained failures, source settings and speech origins. `test_bench`, `test_traces` pass. | Client playout and first useful audio are unmeasured. Text comparison cannot establish matched audio parity. |
| §8 reusable loop / C0/C3 / V13 | Runtime-neutral core and Pipecat adapter parity; request snapshots, reset/messages projections, owner completion/unserved returns, segment IDs, retained accepted input/tool pairs. `test_core`, `test_history`, actual queue regressions pass. | Runtime estimates are not exact delivered client audio. The second runtime is Phase 02. |
| Completion-plan §4: speech before TTS | Verification, booking and complaints use constrained output boundaries. Booking availability comes from the validated diary; confirmation follows a receipt. Unsupported complimentary-appointment speech is blocked and its negative control fails. | Public concierge text remains generative; finite tests do not establish universal conversational correctness. |
| Complaint acceptance | Caller-sourced facts, approval, identity, one record/receipt, pending resolution and preserved appointment. Current continuation 24/24, including policy/correction/abandonment. | Fresh spoken quality/acceptance remains open; exact excerpts can be less natural than a free paraphrase. |

The original Phase 01 local core boundaries are implemented and checked. The
broader completion plan is **not fully accepted**: the remaining microphone matrix,
current-build latency acceptance and useful-audio/client-playout measurements remain open.
The completed pre-optimization model comparison is retained [separately](model-evidence/README.md). Passing text tests does not close them. Portability, generated
providers, carrier routes and the public authoring language belong to later phases.

## Reproduce the microphone check

Stop the old server with Ctrl+C, then run from this worktree:

```bash
cd /Users/nicoferdi/.codex/worktrees/517a/slng_voice_harness
CALLER_ID=+34600111222 \
SALON_FIXTURE=fresh \
SALON_CLOCK=2026-09-21T10:00:00+02:00 \
uv run --no-project \
  --python /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.venv/bin/python \
  --env-file /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.env \
  python bot.py -t webrtc --host localhost --port 7861
```

Open <http://localhost:7861/client/>. In one call request a complaint before
verification, approve the full phone number once, then approve the complaint
readback. In another call request a Friday-afternoon haircut and select an offered
time; then try a same-time move to Saturday. Keep the fresh Langfuse trace URLs.
These fixtures reset on each new call. Testing missing caller ID requires starting
with `CALLER_ID=''`; then speak the number in fragments and correct a group.

No commit, branch switch, shared environment installation or server restart was
performed by the agent. The tested source is the detached `c6e8cfd` checkout with
the preserved existing changes and these repairs, identified by file hashes in
[comparison snapshot](native-evidence/snapshot-v2.json) and the later
[approval snapshot](approval-evidence/snapshot.json).
