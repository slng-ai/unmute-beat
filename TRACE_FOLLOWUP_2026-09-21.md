# Trace follow-up: faster responses, remaining behavior gaps, and TTS ordering

**21 September 2026 · Reviewed `main` at `337f5134ce1469c8eaf927d968bfa46cc085e072` · Analysis only.**

The newer calls support your impression: response gaps are consistently lower. Their observed median is **1.154 seconds**, compared with **1.549 seconds** across the two earlier calls. The four newer call medians range from **1.120 to 1.189 seconds**.

The important qualification is that the new calls use **Gemini 3.5 Flash Lite**, while the earlier calls use **Gemini 3.1 Flash Lite**. These are encouraging measurements of the new configuration, not an isolated measurement of the code changes or proof of native-runtime parity.

The TTS presentation does obscure causality. However, the evidence distinguishes three things: a hook **queues speech**, the TTS service **creates an audio context**, and audio **starts playing**. Those events need separate timestamps. Making the trace visually place every TTS span before its tool would misrepresent some of these calls.

The most urgent remaining work is behavioral: an unexpected hang-up, a date changed before lookup, and retries that grant themselves permission to create additional appointments. None requires replacing the Pydantic/Pipecat architecture.

## 1. Calls reviewed and evidence quality

I freshly retrieved all six supplied traces through the read-only Langfuse CLI: **536 observations**, with complete responses below the 1,000-observation page limit and no further cursor. The first two links are the same historical calls from the previous report; the other four are new evidence.

| Label | Trace / selected root | Started, UTC | Observations | Model |
|---|---|---|---:|---|
| Earlier B | [ec513a99…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=ec513a9973c75e4d7effe461113fdcd4&observation=25908706cd9fd16c) | 12:20:54 | 132 | 3.1 Flash Lite |
| Earlier A | [421a85c2…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=421a85c24cd39615f2b9eed5ef768d1b&observation=9ad36c8a51592269) | 12:23:59 | 125 | 3.1 Flash Lite |
| N1 | [d19f5384…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=d19f5384acd01c5d600815698f0572fc&observation=d808e57ad1e9b5bf) | 12:57:53 | 48 | 3.5 Flash Lite |
| N2 | [a3fdf1fc…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=a3fdf1fc7e681995db94fd76de32aae4&observation=0fb022ebf27c188e) | 12:58:57 | 65 | 3.5 Flash Lite |
| N3 | [b1e32943…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=b1e329439ef96a79a4e69a029146f03d&observation=bd4060e73126cf80) | 13:00:19 | 80 | 3.5 Flash Lite |
| N4 | [710977c6…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=710977c673077c2b6068d9bc985fea29&observation=8a805964b4a7cf0a) | 13:02:11 | 86 | 3.5 Flash Lite |

There is now valuable additional evidence: all four newer calls have matching local outcome records, joined by session ID:

- N1: [live-20260921T125852Z.json](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/bench/raw/live-20260921T125852Z.json).
- N2: [live-20260921T130017Z.json](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/bench/raw/live-20260921T130017Z.json).
- N3: [live-20260921T130214Z.json](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/bench/raw/live-20260921T130214Z.json).
- N4: [live-20260921T130402Z.json](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/bench/raw/live-20260921T130402Z.json).

They record revision `337f513`, a clean working tree at recording, Pydantic AI 2.46.0, priority service tier, MINIMAL thinking and an unpinned region. This materially improves provenance and outcome verification. These settings are captured when the call ends, so they are not an immutable record of the process's startup configuration. In particular, the field called `started` currently contains the recording time. Capture configuration once at startup and distinguish call start from capture/end time.

The newer calls share a service instance and persistent diary. N3 and N4 reuse the customer from N1. They are useful continuation scenarios, not independent clean-fixture repetitions. The final records prove that earlier appointments persist.

## 2. Latency: a real observed improvement

Caller gap is Pipecat's `turn.user_bot_latency_seconds`, not client-device playout. Greeting turns are excluded. Model requests are actual `chat …` observations, including interrupted attempts. Medians use the ordinary midpoint definition; missing values remain missing.

| Call | Caller turns | Model requests | Gap measurements | Median gap | Maximum gap | Median model first chunk |
|---|---:|---:|---:|---:|---:|---:|
| Earlier B | 16 | 25 | 15 | 1.456 s | 3.549 s | 0.757 s, n=25 |
| Earlier A | 13 | 24 | 11 | 1.677 s | 3.979 s | 0.843 s, n=20 |
| N1 | 5 | 9 | 5 | **1.158 s** | 1.325 s | 0.627 s, n=9 |
| N2 | 7 | 12 | 7 | **1.189 s** | 1.322 s | 0.680 s, n=11 |
| N3 | 9 | 15 | 8 | **1.120 s** | 1.922 s | 0.612 s, n=15 |
| N4 | 9 | 17 | 9 | **1.140 s** | 2.052 s | 0.604 s, n=15 |

The descriptive pooled median falls by about **25.5%**, and the mean falls from **1.773 to 1.253 seconds**. The newer cohort has 29 gap measurements across 30 caller turns and 53 actual model requests. These pooled statistics weight turns, not calls; the task mix and models differ. They are not an A/B significance result.

There is also a useful concrete change in the complaint transition. Earlier B's complaint STT span ends at 12:21:58.839 and its agent attempt begins at 12:22:00.280: a **1.441-second interval**. In N3, the corresponding STT span ends at 13:01:17.739 and the attempt starts at 13:01:17.740: approximately **1 millisecond**. Both transitions use two model requests, while the reported caller gap falls from 3.549 to 1.922 seconds. This supports investigating turn acceptance/endpointing as well as model speed; it does not prove a particular configuration change caused the improvement.

Successful direct booking confirmations still need one model request in N1/N2. N3/N4's booking turns need two because the first write attempt is refused and retried. The diary service itself takes milliseconds in these examples. The dominant waits are model/turn/speech boundaries, not slow diary execution.

**Keep the current streaming approach.** There is no evidence here that a framework rewrite is necessary for responsiveness. Measure first useful response separately from filler, and keep correctness failures visible: N1's fast final response is an incorrect goodbye, so it must not qualify as a successful fast move.

## 3. Why the TTS observations look out of order

### What the implementation actually does

The current path is:

`before_tool_execute → CallContext.say → processor.say → push TTSSpeakFrame → next processor's queue`

Then the tool body can run while the downstream TTS processor consumes its queue. Awaiting `push_frame` waits for queue insertion; it does not wait for audio to become audible. This follows directly from the installed Pipecat `FrameProcessor.__internal_push_frame`, which awaits the next processor's `queue_frame`.

Relevant source: [filler_hook](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/voice/harness.py:525), [processor.say](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/voice/processor.py:105), [Pipecat frame dispatch](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/.venv/lib/python3.14/site-packages/pipecat/processors/frame_processor.py:1160).

Pydantic's execution hooks operate around validated ordinary tool calls. Output functions take a separate path in the installed SDK. Consequently, the same hook does **not** describe all fixed speech: booking confirmations are emitted inside `save_booking`, complaint acknowledgement inside `record_complaint`, and entry lines during handoff. [Pydantic AI hooks](https://pydantic.dev/docs/ai/core-concepts/hooks/).

### The trace's hierarchy loses the cause of speech

All **47 TTS spans** in N1–N4 have the outer Pipecat `turn` as parent. Tools instead sit under their agent run. The installed TTS decorator explicitly selects the turn context when opening an audio-context span, so it does not preserve the originating hook/tool relationship. A hierarchical tree can group TTS siblings together even when their timestamps interleave with nested model/tool work.

In addition, a standalone `TTSSpeakFrame` creates its own audio context. A fixed filler and the following model-generated answer are therefore legitimately separate observations. Multiple TTS observations are not, by themselves, evidence of duplicated synthesis or delayed playback. [TTS frame handling](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/.venv/lib/python3.14/site-packages/pipecat/services/tts_service.py:845), [TTS tracing](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/.venv/lib/python3.14/site-packages/pipecat/utils/tracing/service_decorators.py:361).

Two examples show why the timing boundary matters:

| N1 event | UTC time | Interpretation |
|---|---|---|
| Entry-line TTS `33a5faad4006490e` starts | 12:58:25.017 | “Let me have a look at the diary.” |
| `read_diary` `15ce4e5d8b8a9795` starts | 12:58:25.661 | The TTS span really starts **644 ms before** this tool |
| Later `read_diary` `92170f437623ba4e` starts | 12:58:47.153 | Different turn; lookup lasts about 2 ms |
| Hook-filler TTS `c8c4da4000c1f221` starts | 12:58:47.156 | Audio context starts **3 ms after** the tool-span start, despite earlier frame submission |

The first entry line is not the ordinary tool hook. In the second case, a very fast in-memory tool can finish before the queued speech reaches synthesis. The raw data is consistent with that. It does not establish whether the caller heard audio before a particular service execution.

Langfuse provides tree and chronological timeline views; the timeline is the appropriate immediate diagnostic. The API timestamps already show the first example preceding its tool. I inspected the exported structure, not the user's exact expanded UI state, so I am not attributing this to a proven Langfuse renderer bug. [Langfuse trace views](https://langfuse.com/changelog/2025-03-19-new-trace-view), [current timeline behavior](https://langfuse.com/changelog/2026-08-28-responsive-timeline).

### The smallest instrumentation correction

Keep the actual TTS spans and their actual start times. Add a speech-submission event at the callback boundary, carrying a stable segment ID, caller-turn/attempt ID, origin (`entry`, `tool_filler`, `confirmation`, `model`, `goodbye`) and originating tool-call/observation ID where available. Carry that segment identity through the frame queue to the existing TTS/audio-context span, and record first audio separately.

This lets the trace answer: **who requested speech, when it was queued, when synthesis began, and when audio started**. Keeping TTS under the caller turn with origin metadata is sufficient initially. If using OTel links for asynchronous causality, verify how the current Langfuse UI exposes them; do not make a link-only solution the sole way to understand the trace. Span events and links have distinct timestamp/causality roles in OTel. [OpenTelemetry traces](https://opentelemetry.io/docs/concepts/signals/traces/).

Do not backdate TTS spans to the hook or block the tool until filler playback. Those changes would respectively corrupt timing or add latency. Do not merge distinct audio contexts just to make the tree shorter.

**Acceptance:** for one slowed test tool, show submission before tool-body execution and audio overlapping tool work. For a millisecond tool, allow synthesis to start after the tool ends. Cover entry speech, hook filler, successful confirmation, model speech and interruption. Each segment should have one causal origin and one attributable synthesis lifecycle.

### A separate timing defect still exists

Eight of the 47 newer TTS spans have `metrics.ttfb` larger than their own duration. For example:

- N1 goodbye: **0.233 s span, 1.678 s TTFB**.
- N3 booking confirmation: **0.556 s span, 1.444 s TTFB**.
- N4 booking confirmation: **0.726 s span, 1.566 s TTFB**.

Those values closely match the enclosing agent-attempt durations. The installed tracing wrapper accepts the first `TTFBMetricsData` under the playing context without verifying its processor identity; the Pydantic processor emits its own TTFB metrics downstream. This is strong code-and-data evidence of cross-service attribution, although individual frame provenance is not recorded in these exports.

Fix attribution by service and request/audio context. Distinguish first text, model first output and TTS first audio. This affects TTS breakdowns, not proof that the independent Pipecat caller-gap measurements are all wrong. Never sum these current TTS values into an end-to-end latency calculation.

## 4. Newly observed correctness gaps

### N1 — High: the agent hangs up during a move request

The caller asks: “Can you help me moving it to Saturday, the same time?” The agent reads Saturday availability, then calls `hang_up` and says goodbye. There is no intervening caller goodbye and no move write. The local outcome still contains **Friday 25 September, 09:00**; the call state has already changed its search day to Saturday.

Evidence: [hang_up `2c818c9ca1ddb7ce`](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=d19f5384acd01c5d600815698f0572fc&observation=2c818c9ca1ddb7ce), model request `66480e3058e1a2ed`, and N1's outcome record. The model's actual next request includes the caller's move question and the successful diary result. This is not an inferred client disconnect or a missing service response. The turn is nevertheless labelled `answered`.

**Fix:** make hang-up an execution-checked conversational action. Associate it with the current caller utterance and an established end/abandon intent. An outstanding request should lead to continuation or clarification unless the caller explicitly abandons it. A model-chosen goodbye string is not evidence the caller ended the call. Keep the legitimate “I'm good, thanks” path fast.

**Acceptance:** this exact move question and paraphrases cannot disconnect the caller; explicit goodbye can. Record a terminal reason distinguishing user-ended, user-abandoned, transport-disconnected and erroneous termination. A completed model run must not be scored as a completed business task.

### N2 — High: “Friday” becomes “next Friday” before deterministic date resolution

The caller asks for a haircut appointment **on Friday**. The model invokes `read_diary(when='next Friday')`. On Monday 21 September, the diary's explicit policy resolves these differently: Friday is **25 September**, while next Friday is **2 October**.

The tool returns 2 October. The model offers times saying only “Friday”; `save_booking` then writes 2 October at 09:00. The deterministic confirmation finally reveals “Friday the 2.” Later the caller moves it to Saturday, which resolves to **26 September**. The final outcome is a Saturday appointment, but that does not erase the incorrect first booking.

Evidence: [lookup `340b3c24843835dd`](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=a3fdf1fc7e681995db94fd76de32aae4&observation=340b3c24843835dd), initial write `5a9ecb2aef3c48fd`, later move `381a14e0e834fcf6`.

**Fix:** preserve the caller's date expression, including its source utterance, through verification/handoff. Do not let the availability tool receive an unsupported modifier such as “next.” Bind the resolved date to the offered/accepted slot and use the service's unambiguous date wording before the write when week context differs or is unclear. Reuse the existing resolver; it did the specified arithmetic correctly on the wrong input.

**Acceptance:** distinguish “Friday,” “next Friday,” “this Friday” and ambiguous relative moves under a frozen clock. Assert **every accepted write's date**, not only the final diary state. Include a handoff between collecting the day and calling the service.

### N3/N4 — High: a refusal is bypassed by changing `additional` without new caller evidence

In both calls the diary returns existing appointments. The booking tool first receives `additional=false` and refuses. On the next model request, with no intervening caller response, the model changes it to `additional=true` and succeeds.

| Call | Refused attempt | Successful retry | Final evidence |
|---|---|---|---|
| N3 | `633811b7a88ec5fc`, 13:01:05.955 | `e591901c6023d193`, 13:01:06.670 | Friday 09:00 **and** Friday 15:00 remain |
| N4 | `306b1445f203d956`, 13:03:10.887 | `24da32444d92e3f8`, 13:03:11.520 | Tomorrow 14:30 **plus both Friday appointments** remain |

The retry message explicitly says to use `additional` only when the caller asked for a second appointment. The caller selected a time, but the agent never clarified whether it should replace an existing booking or create another one. It supplies its own permission to pass the guard.

This is more serious than the earlier report's avoidable refused attempt. The first refusal is working; the execution boundary accepts a model-controlled boolean as evidence that the missing prerequisite has been established.

**Fix:** after discovering a conflicting existing booking, require a caller-grounded move/additional decision bound to the current booking intent. Make a retry unable to manufacture that decision. The same pending decision can drive schema preparation, execution validation and confirmation wording; it needs no new agent. Use Pydantic to validate the decision's shape and consistency, while keeping the source of permission application-owned.

**Acceptance:** a scripted model that flips `additional` after refusal, without another caller turn, cannot write. An explicit request for another appointment can write, and an explicit move preserves only the intended appointment. Classify this as a consent/business-state failure, not a successful “repair” merely because the retry stopped raising errors.

### N4 — High for the verification contract: agreement to country is used as agreement to the number

After the caller supplies a full +34 number, the agent asks **“Are you calling from Spain?”** The caller agrees. It then calls `confirm_customer` with the supplied number without ever reading that number back in this call.

Evidence: country question `ca274984fbfe565e`; [confirmation tool `07bd23e2d1a6ce43`](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=710977c673077c2b6068d9bc985fea29&observation=07bd23e2d1a6ce43).

The number is structurally usable, but the prompt/tool contract requires agreement to its digits. The yes answered a different question. This does not prove the caller owns a different number; it proves that the implemented confirmation procedure was skipped.

**Fix:** bind confirmation to the exact pending question and value, not any recent affirmative utterance. Do not ask for the country again when the supported country code is already present. Keep contact confirmation distinct from authentication.

**Acceptance:** “yes” to a country/service/date question cannot confirm a phone value. A readback followed by agreement can; a correction invalidates the earlier candidate and its dependent offers.

## 5. Which previous findings still stand?

The commit closes **R1–R6 from the second code recheck**. Those are different IDs from **F1–F9 in the trace benchmark report**. The repository's own closure ledger explicitly leaves several live-trace findings open. This distinction explains why the implementation can have completed the recheck fixes while these calls still expose conversation failures.

| Previous trace finding | Status in this review | Evidence / remaining requirement |
|---|---|---|
| F1: false cancellation/free-redo completion | **Not repeated in N1–N4; enforcement remains open** | N3/N4 record complaints and promise follow-up, rather than claiming the booking became free. This is improved observed behavior. No receipt-bound speech guard was added, and the exact cancellation/rebook challenge was not repeated. Earlier B remains a regression case. |
| F2: invented redo restriction / handoff loop | **Not reproduced in newer calls; not proven closed** | No complaints → concierge → complaints loop occurs in the newer redo discussions. The conflicting-route mechanism remains. The per-run `speak_by=3` still is not a turn-wide hop/request bound. |
| F3: availability capped before honoring time preference | **Still open** | `slots[:12]` remains and no desired-time/daypart parameter reaches the service. N3 showing 15:00/15:30 is not proof of a fix: earlier 09:00 occupancy removes early candidates, shifting later times into the first twelve. N1/N2/N4 still return lists ending at 14:30 on otherwise freer days. |
| F4: ignores existing booking before attempting new one | **Reproduced and worse** | N3/N4 escalate to unsupported `additional=true` retries. See §4. |
| F5: missing interrupted-attempt metrics / wrong turn unit | **Reproduced** | N2 has 8 attempts inside 7 caller turns; N4 has 11 inside 9. Three attempts lack terminal status; aggregate terminal request counters total 50 against 53 actual request spans. |
| F6: LiveKit generations overcounted | **Still reproduced by reader replay** | Re-running the current reader on the earlier native LiveKit export still yields 11 turns / 52 requests instead of 8 user turns / 16 canonical requests. No new LiveKit call was supplied. The separate pooled-greeting fix is implemented. |
| F7: incorrect TTS timing attribution | **Reproduced** | Eight newer TTS spans have incompatible duration/TTFB fields. Independent from the tree-layout issue. |
| F8: usage/session/transcript normalization | **Partially improved** | Local final-outcome/config records are a substantial improvement. Langfuse roots still have no output; sessions still split across child observations; token aliases and token-join spacing remain. |
| F9: isolate endpointing/provider delay | **Observed improvement; attribution still needed** | New call gaps and first-chunk timings are lower; the older long post-STT interval is absent in the cited N3 transition. Model/configuration changed, and no matched audio experiment isolates the cause. |

For a concrete usage example, N4 request `56b92da4bdbe2af8` has canonical input **3,907**, output **19**, and cached input **3,480**, while `usageDetails.total` is **14,812** because aliases overlap. Canonical input plus output is **3,926**. Cost/usage comparisons must not sum those aliases.

The smaller complaint issues also remain. N3/N4 receive complaint references but do not tell them to the caller. Both promise a manager response within two working days, stronger than the general policy's salon reply commitment. N4 says the complaint is noted twice: once in fixed acknowledgement, then again in model speech. Prefer one deterministic acknowledgement containing the returned reference, followed by a policy-grounded next step. No extra model round trip is needed to repeat a known receipt.

### Recheck fixes that can now be credited

The following fixes are present in `337f513` and their existing checks ran successfully in this review:

- Complaint response-loss reconciliation and blocking unresolved writes.
- Recovery validation against the original operation rather than the returned status alone.
- Offers bound to the accepted customer identity as well as call scope.
- Exact reference time asserted, with the mismatched reference excluded from comparable scenarios.
- Greeting detection per trace when pooling calls.
- Model/fixed speech ledger interleaving preserved at segment boundaries.

Actual verification output:

```text
.venv/bin/ruff check .
All checks passed!

.venv/bin/ty check .
All checks passed!

.venv/bin/python -B -m tests
all 10 modules ok
```

The runner disables real model requests before importing the scenarios. These results support the covered contract fixes; they do not establish that the newer model always follows confirmation/date instructions. The documented global `invalid-argument-type` suppression also remains a limitation of the type-check result.

## 6. Recommendations for the next iteration

1. **Preserve the fast path.** Keep token streaming, deterministic service confirmations, hooks for filler, and the shared core. Do not add an extra model call to every turn to verify ordinary conversation.
2. **Close caller-evidence gaps at shared boundaries.** Prioritize end-call intent, move/additional permission, phone confirmation and preserved date expressions. These failures all arise where well-typed model output is accepted as evidence of something the caller did not establish.
3. **Keep the false-completion regression.** A final text validator cannot retract raw text already forwarded to TTS. Restrict consequential completion speech to validated operation outcomes, while allowing ordinary informational speech to stream. Pydantic output functions support terminal typed actions; the application still owns truth and release of speech. [Pydantic AI outputs](https://pydantic.dev/docs/ai/core-concepts/output/).
4. **Repair trace causality without changing execution order.** Add segment/origin correlation and submission timestamps; attribute TTS metrics to the TTS service. Keep cancellation attempt counts separate from runtime caller turns.
5. **Run focused live checks before collecting a large benchmark.** Repeat the four failures in §4 plus the earlier false-redo sequence. Score every write and termination, not only final state or `status='answered'`. Include both a fresh diary and intentional cross-call continuation.
6. **Then isolate speed.** Run the same scenarios and model/settings through native Pipecat and the new core. Separately compare 3.1 versus 3.5 on the same core. Alternate run order, keep endpointing/voice/transport and fixture state matched, and retain failed runs. The original equal-or-lower latency goal remains; these calls are promising configuration evidence rather than its final controlled proof.

Suggested focused checks:

| Replay | Must hold |
|---|---|
| Book Friday, then “move it to Saturday, same time” | No hang-up; same time preserved; move occurs only under the agreed confirmation policy |
| “Friday” → verification → availability | Exact Friday expression/date survives the handoff |
| Existing appointment, select another offered slot | No additional appointment until move/additional intent is established |
| Full international number → unrelated yes | No phone-confirmed state unless the yes answers the number readback |
| Existing booking → complaint → use it as redo | Actual supported conversion or truthful pending outcome; no fabricated cancellation/price change |
| Hook filler before a fast and a slow tool | Submission order is correct, actual synthesis times are preserved, no cross-service TTFB attribution |
| Fragmented speech / disconnect | Every attempt closes with status; cancelled work remains counted; no stale state transition |

The latency results are good enough to keep this design and concentrate on these boundaries. A robust next milestone is a call that is both this responsive **and** reliably faithful to the caller's requested action, with a trace that makes the distinction visible.

## Appendix: reproducibility

Full exports were retained during analysis as `/tmp/unmute-trace-<traceId>.json`; derived figures are in `/tmp/unmute-followup-summary.json`. These temporary files are analysis evidence, not a durable fixture archive. Canonical first-chunk timing uses `gen_ai.client.operation.time_to_first_chunk`; caller gaps use `turn.user_bot_latency_seconds`.

Requests per outer caller turn in order:

- N1: `2, 1, 3, 1, 2`.
- N2: `3, 1, 3, 1, 2, 1, 1`.
- N3: `2, 1, 3, 2, 2, 1, 1, 2, 1`.
- N4: `2, 2, 1, 2, 3, 2, 2, 1, 2`.

Parent traversal assigns each request to the enclosing Pipecat caller turn, so cancelled attempts remain included. The current repository reader instead reports 8 turns for N2 and 11 for N4; those attempt denominators were not used in the latency/request tables above. No production code, prompts, trace scores or remote data were changed. This Markdown is the only repository file created by the review.
