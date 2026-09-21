# New harness versus native Pipecat and LiveKit — trace benchmark

**Reviewed: 21 September 2026. Deliverable: analysis and fixes, no application-code changes.**

The new harness has a sound foundation and removes model round trips from successful booking confirmations. These traces nevertheless do **not** establish equal-or-lower caller latency. They also expose one release-blocking conversational failure: the earlier new call claims to have cancelled an appointment and replaced it with a free redo, although no corresponding write occurred in the trace.

The next work should close that speech-to-business-outcome gap and repair the benchmark's measurement boundaries. A different orchestration framework is not needed to address either problem.

## 1. Evidence and scope

I retrieved the complete observation lists through the Langfuse CLI, inspected model inputs/outputs, tool arguments/results, parent relationships, speech spans and timing attributes, and compared relevant local source with current primary documentation. This is trace analysis, not an audio listening test or a new controlled experiment.

### The five links select four calls

| Label used here | Trace | Selected observation | Start, UTC | Observations retrieved |
|---|---|---|---|---:|
| New A — later call | [421a85c2…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=421a85c24cd39615f2b9eed5ef768d1b&observation=9ad36c8a51592269) | `9ad36c8a51592269` | 12:23:59.225 | 125 |
| New B — earlier call | [ec513a99…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=ec513a9973c75e4d7effe461113fdcd4&observation=25908706cd9fd16c) | `25908706cd9fd16c` | 12:20:54.762 | 132 |
| Native Pipecat | [787cfb62…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=787cfb6233eb03adda2fd399a02d23ec&observation=0071c0aa61e2ff97) | `0071c0aa61e2ff97` | 12:19:08.560 | 54 |
| Native LiveKit — nested entry | [b8117b52…, on_enter](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=b8117b52dcaa0e6bad228536de332c9a&observation=690e09ca25ad3878) | `690e09ca25ad3878` | 12:14:43.760 | 213 across the full trace |
| Native LiveKit — session root | [b8117b52…, agent_session](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=b8117b52dcaa0e6bad228536de332c9a&observation=9a0fbe7663cbfe4c) | `9a0fbe7663cbfe4c` | 12:14:33.456 | Same 213 observations |

The LiveKit links are different views, and both were inspected. Its `on_enter` lasts **32.721 seconds** because it encloses a conversational subflow. It is not 32 seconds of startup overhead. Likewise, the long LiveKit `book` spans include waiting for the caller; they are not backend execution durations.

All four traces identify their LLM as **`gemini-3.1-flash-lite`**. The local installation reports Pydantic 2.13.5, Pydantic AI Slim 2.46.0, Pipecat 1.11.0 and Langfuse 4.15.4. Matching a model name or SDK version does not prove matching provider route, generation settings, prompt, cache state or deployment.

Local `main` remained at `14fe6f612086e33c8d452ba79528ac09f47893c6`, but files were being edited concurrently. Changes initially involved benchmark/configuration recording, then salon state/services, and by finalization also the trace reader, core, harness and tests. Consequently, trace findings below are historical observations; source references identify relevant enforcement points, not proof that the running call used the latest working tree. Reader reproductions describe the snapshot when inspected, before those later edits. This report does not certify the concurrent fixes.

The supplied planning documents are treated as the intended contracts to assess, not instructions to implement code during this task. Their controlled-comparison requirements remain appropriate: [research](/Users/nicoferdi/Documents/UnmuteV1/RESEARCH.md), [Phase 01](/Users/nicoferdi/Documents/UnmuteV1/IMPLEMENTATION_PHASES/01_POC_FOUNDATION.md), [implementation plan](/Users/nicoferdi/Documents/UnmuteV1/UNMUTE_V1_IMPLEMENTATION.md), [verification plan](/Users/nicoferdi/Documents/UnmuteV1/UNMUTE_V1_VERIFICATION.md), and [product proposal](/Users/nicoferdi/Documents/UnmuteV1/UNMUTE_V1.md).

## 2. What the numbers actually say

### Request efficiency

One model request means one canonical provider-call observation. It does not mean one agent run, one tool, or every Langfuse `GENERATION` observation.

| Measurement | New A | New B | Native Pipecat | Native LiveKit |
|---|---:|---:|---:|---:|
| Caller turns, excluding greeting | 13 | 16 | 7 | 8 |
| Actual model request spans | 24 | 25 | 16 | 16 |
| Requests / caller turn | 1.846 | 1.563 | 2.286 | 2.000 |
| Internal new-harness `agents` spans | 17 | 16 | — | — |
| Observed requests in a successful booking-write turn | 1 | 1 | 2 | 2 |
| Requests in verification → availability turn | 3 | 3 | 4 | 3 |
| Requests in final goodbye turn | 1 | 1 | 2 | 2 |

**The aggregate rates are descriptive, not a fair architecture ranking.** The new calls include complaints and pricing questions; the old calls have a different task mix. New A also includes interrupted attempts and an existing booking. Consent and offered availability differ. The useful positive evidence is narrower: the new implementation can perform a successful booking write and produce its deterministic confirmation without another model request.

For auditability, requests per caller turn in chronological order are:

- New A: `2, 2, 3, 1, 2, 1, 2, 1, 1, 5, 1, 2, 1`.
- New B: `2, 3, 1, 2, 1, 2, 1, 1, 2, 1, 1, 1, 4, 1, 1, 1`.
- Native Pipecat: `2, 4, 2, 3, 2, 1, 2`.
- Native LiveKit: `2, 3, 1, 2, 3, 1, 2, 2`.

Pipecat requests were assigned through the parent tree to its outer caller turns. LiveKit has eight `user_turn` spans but eleven custom `turn` spans, including the greeting and autonomous continuations. Its array uses successive user-turn start-time windows to associate requests; this is an explicit inference because delegated subflow spans cross user turns. Counting those eleven custom spans as eleven caller utterances would be wrong.

### Caller response gap

These are runtime-reported speech-boundary measurements, not measured sound arriving at the user's device. Pipecat uses `turn.user_bot_latency_seconds`; LiveKit uses `lk.e2e_latency`. LiveKit values were cross-checked against `user_speaking.end → agent_speaking.start` and agree within rounding.

| Measurement, seconds | New A | New B | Native Pipecat | Native LiveKit |
|---|---:|---:|---:|---:|
| Available measurements / caller turns | 11 / 13 | 15 / 16 | 5 / 7 | 8 / 8 |
| Median | **1.677** | **1.456** | **1.076** | **1.334** |
| Mean | 1.768 | 1.776 | 1.289 | 1.438 |
| Maximum | 3.979 | 3.549 | 1.921 | 2.362 |

The observed new-call medians are higher. That is sufficient to reject a claim that these examples prove latency parity; it is insufficient to attribute the difference to Pydantic AI. Missing measurements, different utterances, endpointing, service configuration and filler speech all matter. There are only two new calls and one call per native runtime. Tail percentiles from five or eight observations would add little; the table reports maxima honestly.

Pipecat's latency observer measures runtime user-to-bot boundaries. LiveKit separately exposes end-of-turn, model and speech metrics; transport/playback can add another boundary. Preserve those distinctions in the comparison. [Pipecat latency observer](https://docs.pipecat.ai/api-reference/server/utilities/observers/user-bot-latency-observer), [LiveKit metrics](https://docs.livekit.io/deploy/observability/data/).

### Model first-chunk timing

| Measurement, seconds | New A | New B | Native Pipecat | Native LiveKit |
|---|---:|---:|---:|---:|
| Requests with first-chunk timing | 20 / 24 | 25 / 25 | 16 / 16 | 16 / 16 |
| Median first-chunk time | **0.843** | **0.757** | **0.573** | **0.534** |
| Mean first-chunk time | 0.830 | 0.763 | 0.556 | 0.543 |
| Median full request-span duration | 0.889 | 0.915 | 0.701 | 0.631 |

The new path saves requests in some flows while its observed individual requests take longer. These effects can coexist. A first chunk may contain a tool call, not speakable text. New A's missing timings belong to attempts without normal completion; their durations remain in the request-span population.

Timing sources: new `gen_ai.client.operation.time_to_first_chunk`, native Pipecat `metrics.ttfb`, LiveKit `gen_ai.response.time_to_first_chunk`. Treat them as instrumentation-specific first-output measures until matched provider boundaries are verified. Do not label their difference “Pydantic overhead.”

### Filler is not the useful answer

During verification → availability, these trace timestamps illustrate why both boundaries matter:

| Call | Input reference | First filler / speech event | Slot-offer speech event |
|---|---|---|---|
| New A | STT span ends 12:24:32.578 | TTS starts 12:24:33.437 | TTS starts 12:24:34.674 |
| New B | STT span ends 12:21:16.676 | TTS starts 12:21:18.193 | TTS starts 12:21:19.415 |
| Native Pipecat | STT span ends 12:19:31.189 | TTS starts 12:19:31.980 | TTS starts 12:19:36.135 |
| Native LiveKit | User speaking ends 12:14:54.206 | Agent speaking starts 12:14:55.463 | Slot-offer speaking starts 12:14:58.667 |

These are **timeline examples, not cross-runtime audio-latency calculations**: STT completion and user speech end differ, as do TTS span start and actual speaking. They show that a quick acknowledgement can mask several more seconds before an answer. Score first response, first useful response and completed task separately.

## 3. What has been done right

| Design decision | Evidence and value |
|---|---|
| Pipecat owns voice; the shared core owns conversation | `voice/core.py` remains separate from `voice/processor.py`. This matches Phase 01's reusable-core boundary. |
| Validated state and service boundaries | The salon parses domain results and commits validated state candidates. This is stronger than relying on return annotations. |
| Terminal booking output with fixed confirmation | New B's book and move each take one model request; spoken confirmation follows the successful tool. Keep this path. |
| Deterministic prerequisite resume | Verification resumes pending booking instead of asking the concierge model to rediscover the next step. |
| Existing appointments survive another call | New A sees the Saturday appointment left by New B. This is correct shared diary behavior; reset only the benchmark fixture, not every ordinary call. |
| Execution guards reject unsafe intent | New A's attempt to create another booking is refused when one already exists. An avoidable attempted action is caught before it becomes an extra appointment. |
| More complete request observability | New request spans expose canonical usage and first-chunk attributes even when Langfuse's top-level TTFT is null. |
| Natural speech can coexist with typed actions | Ordinary speech streams while successful operation confirmations are computed. The failures below do not require buffering every conversation turn. |

Pydantic AI's output functions validate their arguments and terminate the run without feeding the result back for another model response. That supports the existing fast confirmation approach. Raw streamed text is a separate path: a final output validator cannot retract audio already emitted. [Pydantic AI output documentation](https://pydantic.dev/docs/ai/core-concepts/output/).

## 4. Required fixes, ordered by impact

### F1 — Critical: never announce an unperformed cancellation, replacement or price change

**Observed in New B.** The normal booking sequence is correct: `save_booking` books Friday 14:30 at 12:21:29.240 and moves it to Saturday 14:30 at 12:21:50.581. The caller then requests a redo for an uneven haircut from one week ago. `record_complaint` succeeds at 12:22:29.292.

After the caller asks to cancel the ordinary appointment and use its slot for the redo, the 12:23:19.433 turn performs:

1. Complaints → concierge handoff.
2. `read_diary`, returning the existing Saturday 14:30 booking.
3. Concierge → complaints handoff.
4. Model text: “I've cancelled your appointment, and I have you down for a redo on Saturday at 2:30 PM.”

There is **no cancellation, replacement or price-changing write** in that sequence, or subsequently. The next answer says the redo will be free. The full trace contains only the two earlier `save_booking` calls. Its last diary read still shows the ordinary appointment; the exposed operation schema has no redo/pricing operation.

Evidence: [failed completion turn `51e9bd4a30cfb3d2`](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=ec513a9973c75e4d7effe461113fdcd4&observation=51e9bd4a30cfb3d2), model response `6de10eeba688e343`, TTS `d2953fa8b89e9c49`, price claim `4cd5d8ded3e511fe`.

**Why the current typing did not stop it:** no invalid tool arguments were necessary. The model simply spoke a false completion. The request already contained the instruction that recording a complaint is not approval or a free booking. Another prompt sentence alone does not close this boundary. `voice/core.py::_speech` forwards text events, and the processor streams them to TTS independently of a successful domain operation.

**Required correction:** bind operation-completion speech to validated service receipts. Reuse the existing fixed booking confirmations. For a turn that resolves cancellation, redo conversion, refund or price adjustment, permit a supported typed action, a clarification, or a truthful unsupported/pending outcome. Do not let unrestricted streamed text announce the completion while this outcome is unresolved. Keep normal information answers streaming.

Represent only the facts this application needs: complaint reference, requested resolution, any established eligibility/authorization, target appointment, and whether an operation actually applied. A model-authored `approved=True` is not authority. A `recorded` complaint result must never become a `booked` or `free` appointment fact. Cross-field validation can enforce those relationships once the application supplies authoritative evidence. [Pydantic validators](https://pydantic.dev/docs/validation/latest/concepts/validators/).

The smallest truthful behavior today is to record the redo request and explain that the current booking has not been changed. If automatic conversion is required, first give the diary a real, validated conversion outcome. Avoid cancelling the existing booking before knowing replacement can succeed; a failed two-write sequence could lose the caller's appointment. No production transaction platform is needed for this POC, but its fake service must model the same atomic outcome.

**Acceptance:** replay this exact conversation and paraphrases. Every emitted completion claim must reference an earlier matching successful receipt for the same appointment/action. With conversion unavailable or failing, preserve the original appointment and say so. Validate the final diary and what was said; a successful trace status alone is insufficient.

### F2 — High: remove invented redo policy and the handoff loop

New B says the Saturday appointment must stay a regular haircut and that a redo cannot use the same appointment. The supplied refund policy contains no such restriction. The model turns a missing capability into an invented business rule, then contradicts it by claiming the conversion succeeded.

The failing turn spends **four requests across three agent runs** on `complaints → concierge → complaints`. In the inspected core, a successful borrowed `read_diary` implies a move only when the run did not return an explicit `Handoff`. A later explicit handoff in the same run can therefore override the useful read's destination. The one-action-per-model-reply rule does not resolve a conflict spread across multiple replies.

**Required correction:** distinguish “tell me the redo policy,” “record this complaint,” and “change this appointment under that policy” in current state. Preserve the pending operation across handoffs. Once a supported booking operation owns the request, route by that pending operation and validated prerequisites. If unsupported, return one truthful outcome; revisiting another agent cannot add the missing capability. Retain a turn-level hop limit as containment.

Use the existing per-request tool/output preparation to expose only meaningful destinations, plus execution-time guards. Pydantic AI supports preparing function and output tools; schema narrowing does not replace those guards. [Prepare Tools](https://pydantic.dev/docs/ai/capabilities/prepare-tools/).

**Acceptance:** this case cannot bounce back to the originating agent without a new fact, clarification or explicit unsupported result. Check the destination, pending intent and number of requests, not just absence of exceptions. The policy answer must not invent a separate-slot rule.

Two smaller complaint-policy gaps also deserve checks. Both new calls receive a complaint reference from the service but their acknowledgement does not tell it to the caller, although the policy promises a reference. Include that returned reference in the existing deterministic acknowledgement. New A promises a manager callback within two working days; the written policy promises a salon reply within two working days and specifically assigns half/full refunds to manager callbacks. This is a stronger promise than the documented general complaint process. Use the actual escalation outcome or the narrower documented wording. These are policy-grounding fixes, not evidence that New A also falsely made its appointment free.

### F3 — High: “not returned in the first twelve slots” is not “unavailable”

New A requests Friday 15:30 and is told it is unavailable. The diary response contains twelve half-hour offers, ending at 14:30. The local service constructs the day's availability and then applies `slots[:12]`; it has no time-of-day search parameter. The caller's afternoon preference therefore cannot reach later valid slots through this interface.

This is a service-query contract problem. Keeping writes restricted to actually issued offers is correct; expanding write permissions to arbitrary slot IDs would undo that protection.

**Required correction:** let the availability query express the desired time or daypart before capping results. Return whether the result is complete/truncated if truncation remains relevant. Only call a requested time unavailable after checking it. Include the new fields in the validated `Slots` boundary and preserve the existing offer invalidation/ownership rules.

**Acceptance:** with an otherwise free Friday afternoon, a request for 15:30 returns that time or a truthful constraint. Test a genuinely occupied 15:30 separately. Also test “same time on Saturday,” service duration and a corrected day. This makes the business fixture suitable for matching the native reference's afternoon booking.

### F4 — Medium: use the existing booking before attempting another one

New A's first `read_diary` (`b48387174c36a284`) already returns Saturday 14:30, booking `BK0001`. Later `save_booking` (`e2185426fed41d51`) attempts a new Friday booking with `additional=false`. The service refuses it, after which the agent asks whether to move the existing appointment.

The guard did its job. The attempted write and repair could have been avoided because the earlier read already supplied the relevant fact.

**Required correction:** after reading an existing appointment, establish move versus additional booking before exposing/accepting an ambiguous new-book action. Keep the identity, booking ID and offered replacement bound to that pending intent. This can use the existing state and tool preparation; it does not need another agent.

**Acceptance:** replay an existing-booking call and a genuinely additional-booking call. The first does not attempt an avoidable refused `book`; the second still succeeds after explicit additional-booking intent. New A and New B must be labelled as warm-state continuation versus clean-state scenarios, rather than pooled as independent repetitions.

### F5 — High: cancelled attempts need terminal records and an honest turn denominator

New A has **13 outer caller turns, 17 internal `agents` attempts and 24 actual model request spans**. Four internal attempts have no terminal `status`; the terminal `model_requests` attributes sum to only **20**. The missing-status attempts occur around fragmented speech and interruption, including the long “All good… actually… just one thing…” transition into a complaint.

The inspected reader reports 17 caller turns for this call, because it chooses `agents` spans. That makes requests/turn look like 24/17 instead of 24/13. One outer caller turn contains five requests across restarted attempts.

**Required correction:** keep separate runtime-turn and generation-attempt identities. Finalize every attempt on success, first-party cancellation, external cancellation, deadline, error and disconnect. Record attempted requests at the request boundary; finalized usage is not a count of all attempted work. Retain interrupted attempts in benchmark denominators.

In `voice/processor.py`, terminal attributes are populated on `TurnFinished`, while `finally` chiefly closes frames. A coroutine cancelled before that event can bypass normal metrics. Make finalization exception-safe without converting cancellation into success. Keep completed business effects for reconciliation while fencing stale speech and transitions.

Pydantic AI distinguishes `RunCancelled` from external `CancelledError`; the latter should propagate after needed state capture. Cancellation is cooperative and does not roll back completed effects. The existing first-party cancellation integration is useful, but it does not prove every runtime teardown reaches the normal terminal event. [Pydantic AI cancellation](https://pydantic.dev/docs/ai/core-concepts/agent/).

**Acceptance:** fragmented utterance, barge-in during text, barge-in during a write, disconnect, and timeout each leave one terminal record per attempt. Normalization still reports 13 caller turns and 24 requests for this archived call. Report missing usage as missing, not zero. Test request/time containment across restarted attempts separately from the allowance shared across agent hops.

### F6 — High: normalize native LiveKit instead of counting all generation spans

Running the inspected `bench.turns.analyse` on the LiveKit export produced **11 turns and 52 “model requests.”** The trace actually contains **8 caller utterances and 16 canonical `llm_request` spans**.

The 52 count consists of 16 `llm_request`, 16 `llm_node`, 12 `tts_node`, and 8 `user_turn` generations. The last two categories are not model calls; the node layer duplicates the request layer. Excluding only spans named `stt` and `tts` is insufficient.

**Required correction:** normalize each instrumentation dialect into the same small measurement record before aggregation. Choose one canonical model-request layer by operation/provider metadata and hierarchy, with explicit versioned fallbacks for these exports. Distinguish Pipecat-native, Pipecat-plus-Pydantic and LiveKit-native arms. Normalize greetings per trace before pooling.

**Acceptance:** these four fixtures must yield the exact counts in §2. Combining two copies of a trace with different IDs doubles callers and requests without adding a greeting to the denominator. A renamed wrapper must not silently become another provider request. Unknown classifications should be visible errors/coverage gaps.

### F7 — High for performance claims: repair TTS timing attribution

New A's goodbye TTS span `d9869bd3f74afd11` lasts **0.415 seconds** but carries `metrics.ttfb=1.469 seconds`. New B's goodbye span lasts **0.263 seconds** with `metrics.ttfb=0.905 seconds`. These fields cannot be treated as consistently bounded TTS-request timing.

The installed Pipecat tracing wrapper in `pipecat/utils/tracing/service_decorators.py` records the first `TTFBMetricsData` encountered on the playing TTS context without checking its processor identity. The new processor also emits TTFB metrics downstream. This provides a concrete attribution mechanism to investigate; the trace does not itself expose enough provenance to prove which frame supplied each value.

**Required correction:** bind TTS timings to their originating service/request/audio-context ID. Record the actual TTS request start and first audio event. Reject or flag incompatible timing boundaries rather than clamping numbers. Apply the correction at the shared tracing integration, not in a one-off chart.

**Acceptance:** overlapping LLM and TTS metrics cannot contaminate each other. If duration and TTFB share a start/end definition, TTFB must fall within that request's lifetime. Verify fixed speech and streaming speech separately. Do not compare TTS span duration across runtimes when one includes playback and another ends at generation completion.

### F8 — Medium: normalize token usage, session identity and transcript reconstruction

**Usage:** new traces contain overlapping usage aliases. One New A request has canonical input **4,723**, output **18**, cached input **3,387**, but Langfuse `usageDetails.total` is **16,256** because overlapping breakdown fields are present. The corresponding canonical input-plus-output count is **4,741**. Cached tokens are a subset of input, not another full prompt.

Use one canonical input/output pair and a separate cached-input breakdown. Do not sum every numeric field. No cost-saving conclusion is justified from the current `total` fields. Observed median canonical input tokens are 3,639.5 / 4,234 / 3,775.5 / 3,622.5 for New A / New B / native Pipecat / LiveKit; prompt length alone does not explain the timing differences.

**Session identity:** the new runtime root uses a session UUID, whereas Pydantic child observations carry other generated session identifiers or lack it. Trace ancestry still connects them, but querying by one session ID can omit descendants. Propagate one call/session identity throughout; distinguish it from agent-run IDs. Preserve a readable, ordered call transcript and final business outcome on the root. Langfuse recommends meaningful root input/output, connected hierarchy and identifying metadata. [Langfuse trace guidance](https://langfuse.com/docs/observability/best-practices).

**Text:** TTS trace text sometimes shows `I 've`, `We 're` and extra spaces. The installed Pipecat decorator reconstructs accumulated `run_tts` fragments using `' '.join(texts)`, while this bot uses token aggregation. That can introduce spaces solely in the trace. Do not call this a proven speech defect without checking the actual wire text/audio. Reconstruct tokens according to their aggregation semantics; compare trace text against the stream supplied to TTS.

**Acceptance:** usage reconciles without duplicated aliases; trace-ID and session-ID retrieval recover the same complete call; ordered transcript reconstruction matches emitted segments. Keep generated, queued and heard speech distinct.

### F9 — Medium: isolate endpointing and provider delay before tuning the model loop

In New B, the initial utterance's STT span ends at 12:21:04.002 and the agent attempt begins at 12:21:05.441: **1.439 seconds later**. For the complaint, STT ends at 12:21:58.839 and the attempt begins at 12:22:00.280: **1.441 seconds later**. The inspected bot defaults `SMART_TURN_STOP_SECS` to 1.5. This is consistent with endpointing/turn-acceptance delay, not proof of an exact VAD-to-endpoint breakdown.

**Required correction:** measure end-of-speech, final transcript, accepted turn, first provider request, first speakable text and first audio independently. Then compare endpointing settings on the same audio, including hesitation and fragmented numbers. Lowering a stop delay without scoring premature responses can make the latency chart better and the conversation worse.

For model timing, compare identical rendered requests/settings against the same provider route before attributing the extra 0.18–0.31 seconds of observed median first-chunk time to SDK overhead. Keep warm/cold connections and cache state visible. Do not add speculative execution until cancellation and no-duplicate-write checks hold.

New B also transcribes “redo” as “radio” in continuation fragments. The conversation still follows the apparent redo intent, but this belongs in the audio regression set: distinguish transcription error, premature turn acceptance and downstream interpretation. A Pydantic schema cannot recover acoustic evidence that the STT service never supplied.

## 5. Native behavior is a baseline, not an unquestionable correctness oracle

The older calls also contain behavior that should not become the target:

- **Native Pipecat repeats the same availability lookup.** The verification turn calls `find_slots` at 12:19:34.800 and 12:19:35.560, with no intervening refusal explaining a repair. That turn takes four requests. Label the extra read redundant, not automatically a schema retry.
- **Both native calls lose “same time on Saturday.”** They offer a fresh choice of times rather than preserving the requested time. New B preserves 14:30 and asks to move there, which is a real behavior improvement.
- **Native Pipecat executes before the confirmation it asks for.** Move tool `6ea73abbe69ea1ec` starts at 12:20:14.141, while TTS asks “Shall I move…” at 12:20:14.143. Success speech starts at 12:20:17.868; the caller's affirmative STT span starts only at 12:20:18.883. Under a separate-yes policy, this is premature execution. The LiveKit move waits for the affirmative reply.

The new code's configuration description says an unambiguous slot selection can book immediately, while the `save_booking` tool description asks for a yes to a readback sentence. Resolve that policy explicitly and apply it consistently to prompts, guards, fixtures and both benchmark arms. A model-supplied `confirmed` boolean does not establish when consent occurred.

For the benchmark, use a correct native arm with matched policy, and retain the old traces as observed experience. Otherwise speed can improve simply because one arm skips a required confirmation or performs an incorrect action.

## 6. What “typed conversation” should mean here

The research's split remains the right one: Pydantic owns explicit validation, Pydantic AI owns model/tool execution, and the voice runtime owns audio/turn-taking. The missing guarantee is the connection between validated facts, current permitted actions and speech.

| Boundary | Required application contract |
|---|---|
| Caller intention | Capture the current service, date/time, existing appointment and requested resolution without treating model interpretation as a completed operation. |
| Preconditions | Identity, applicable consent and current offer must refer to the same accepted intent. Corrections invalidate dependent facts. |
| Execution | Recheck current state; identify the business operation; invoke the actual service once or reconcile an unknown outcome. |
| Result | Validate shape and consistency with the original request before committing local state. |
| Flow | Resume known pending work deterministically; a successful borrowed tool and a later explicit handoff need a defined conflict rule. |
| Speech | Completion language comes from accepted outcomes; unsupported or pending actions remain visibly pending. |
| Interruption/history | Preserve completed effects and actual heard prefixes; suppress obsolete continuation and never reinterpret a synthetic interrupted result as success. |

This does not require all conversation text to be JSON. Keep the existing typed state and terminal action outputs, adding the smallest missing operation/complaint facts. For operations with consequential completion claims, choose a typed outcome before releasing that claim. Broad free speech remains appropriate for greetings, empathy, clarification and grounded information.

Pydantic AI can repair interrupted tool-call/result pairs in message history, including synthetic interrupted returns. Those repairs make history structurally usable; they do not establish a business commit or what the caller heard. Preserve the application's ordered speech segments and operation receipts alongside SDK history. [Message history](https://pydantic.dev/docs/ai/core-concepts/message-history/).

Timeouts also do not prove an operation failed to commit. The timeout boundary must reconcile the original operation rather than blindly retrying a new one. This is especially relevant when moving from the in-memory diary to remote services. [Pydantic AI timeouts](https://pydantic.dev/docs/ai/core-concepts/timeouts/).

No evidence here requires LangGraph, a new workflow engine, wholesale history summarization, or a different voice runtime. A future LiveKit adapter must run the same core and contracts; the supplied native LiveKit trace is not evidence that such an adapter has already passed.

## 7. Evaluation harness: minimum concrete specification

Extend the existing `bench/scenarios.py`, `bench/turns.py` and assert-script fixtures. First repair their measurement contracts; adopting another evaluation library would not repair wrong denominators or a permissive business oracle.

### Three complementary tracks

| Track | Runs against | What it decides |
|---|---|---|
| Offline contract checks | Scripted model plus actual core, adapter and diary wrappers | Correct state, speech ordering, cancellation, refusal and service reconciliation without provider variability |
| Matched model scenarios | New core and frozen native implementations | Outcome correctness, provider requests, retries, handoffs and text timing |
| Matched voice calls | Actual transports with equivalent audio/scenarios | Endpointing, interruption, first response audio, useful response audio and task completion |

Pydantic Evals is a suitable optional runner when its reporting becomes useful. `repeat` supports repeated cases and grouping; it does not itself create paired A/B experiments. Span evaluators can assert structural evidence, but “some span matched” is not proof that every mutation was valid or every completion had a preceding receipt. Use explicit temporal and final-state checks for those contracts. [Repeated evaluation](https://pydantic.dev/docs/ai/evals/how-to/multi-run/), [span-based evaluation](https://pydantic.dev/docs/ai/evals/evaluators/span-based/).

Langfuse exports still need the normalization described above; do not assume Pydantic Evals automatically understands all native trace dialects. The existing offline test style can enforce these checks without adding a dependency.

### Required scenario set from these calls

| Case | Exact invariant |
|---|---|
| Clean booking, chosen afternoon time | Correct identity, date, service and exact time; one accepted write; agreed consent policy |
| Same time on another day | Preserve the time if actually available; do not ask the caller to choose it again |
| Existing booking at call start | Clarify move/additional intent before an avoidable refused new booking |
| Requested time beyond first availability page | Check the time; no invented unavailability |
| Impossible time corrected by caller | No write for 23:00 outside hours; only corrected 11:00 may proceed |
| Uneven haircut within redo window | Record accurate complaint; distinguish policy entitlement/request from applied appointment change |
| Convert existing appointment to redo | Actual matching conversion receipt or truthful pending/unsupported answer; never false cancellation/free pricing |
| Price question after unsuccessful conversion | Answer from authoritative appointment/policy status, not the model's previous claim |
| Complaint → booking → complaint | No unproductive routing loop; preserved intent and one truthful outcome |
| Hesitation / continued speech | Account for all attempts; no stale answer, duplicate action or lost cancellation record |
| Service response lost after commit | Reconcile original operation; no duplicate booking or complaint |
| Identity or service correction | Invalidate dependent offers/verification; no old offer applied to a new identity |
| Goodbye / disconnect | Ordered goodbye when possible; closed spans; distinguish normal teardown from barge-in |

Use these traces as examples and add paraphrases. The oracle must assert the **final appointment, complaint, price/resolution status, write count and emitted claims**. A run that finishes politely or produces valid JSON can still fail F1.

### Required measurement records

Keep one call record with arm, deployment revision and dirty/build identity, dependency versions, prompt/tool-schema fingerprints, model/provider route/settings, voice/STT/TTS/transport settings, endpointing configuration, fixture ID, clock/timezone and consent policy. Unknown fields remain explicit.

For each caller turn, retain runtime turn ID, all attempt IDs, request IDs, operation receipts, terminal status, missing-field flags and these boundaries where available:

1. Last caller speech; final transcript; turn accepted.
2. Each provider request start, first output and completion; first-output kind.
3. First text supplied to TTS; first useful answer supplied to TTS.
4. First runtime audio; first useful runtime audio; client playout if captured.
5. Operation completion and final correct task outcome.

Use a consistent monotonic clock within a process. Cross-device playout needs a defined synchronization/matching method. Do not add durations that overlap in a streaming pipeline. Preserve all failed, cancelled and missing-data rows. Report latency among correct completions alongside overall correctness and failure rates, so fast failures cannot improve the headline.

### Fair comparison and release gate

Follow the existing verification plan's configurations: shipped native, native with matched audio settings, native with the smallest routing fix, and the new typed core. This report's New A/New B labels identify calls; they are unrelated to that plan's A/A′/B/C configuration labels.

Freeze the same diary, clock, identity and availability for each independent pair. Also run a separately labelled multi-call persistence scenario. Match policies, prompts/knowledge and model settings as closely as the architectural comparison permits; log unavoidable differences. Alternate or randomize arm order in short paired blocks and hold concurrency constant.

The plan's **at least 20 complete scenario runs per configuration** is a reasonable initial collection target, not a statistical guarantee. Keep the same scenario mix, then extend collection where uncertainty remains, especially for tail latency and interruptions. Do not treat many turns from one call as independent call repetitions.

Your target is equal or lower latency, so predeclare a zero-regression margin for the primary comparable metrics. Report candidate-minus-native median and p90 differences with uncertainty using paired, call-level resampling that preserves scenario pairing. If the uncertainty interval still allows regression, the result is inconclusive; a favorable point estimate alone does not pass. A claim of improvement requires stronger evidence than merely failing to detect a slowdown.

Release criteria:

- No false completion, wrong appointment, unauthorized price change or duplicate write in the targeted contract suite; separately report live-run failure rates and uncertainty.
- Correct counts and timing attribution for all four archived fixtures and pooled-call checks.
- Fewer avoidable model requests on matched successful flows, including comparison with the focused native routing fix.
- Equal-or-lower first response **and useful response** latency under the predeclared comparison, without degrading completion or interruption behavior.
- Actual business outcome and heard-speech evidence agree; incomplete metrics cannot silently pass.

## 8. Relationship to the previous recheck and implementation order

[The previous recheck](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/POC_BENCHMARK_RECHECK_2.md) reproduced six other boundaries: complaint response loss, contradictory recovery acceptance, identity-bound offers, exact reference outcome scoring, pooled greetings, and speech-ledger ordering. These live calls do not exercise all six. Concurrent source edits now address parts of the service-recovery work, so those findings should be revalidated against the completed revision, not copied into this report as newly observed failures or declared closed from a natural-sounding call.

Also retain the documented static-typing limitation: the global `invalid-argument-type` suppression means a green type check is not complete coverage of that category. Tightening the existing `Node` typing belongs in closing the typed-core contract, but it is not the cause established for the live false-completion incident.

Recommended sequence:

1. **Close F1–F2:** truthful, receipt-bound redo/cancellation outcomes and deterministic ownership of pending actions.
2. **Close F3–F4:** availability query completeness and existing-booking intent; align consent policy across the benchmark.
3. **Close F5–F8:** trustworthy attempts, turns, TTS timing, usage and trace reconstruction; retain these exports as regression fixtures.
4. **Revalidate the previous boundary fixes** on a stable revision, including interrupted speech ordering and service reconciliation.
5. **Run F9's timing decomposition and the matched experiments** before making further latency changes.

The POC is demonstrating useful request savings and a viable Pydantic-based conversation core. Its next milestone is an enforceable relationship between actions and speech, followed by measurements that can prove the latency target. These four calls supply concrete regression cases; they do not yet close that milestone.

## Appendix — retrieval and calculation provenance

- Access: Langfuse CLI 1.2.4, discovered observation-list endpoint, read-only access to the supplied project.
- Query for each trace: `observations list`, the exact trace ID, limit 1000, fields `core,basic,time,io,metadata,model,usage,prompt,metrics,trace_context`. Returned counts were below the limit with no further cursor.
- Raw responses were retained locally during analysis at `/tmp/unmute-trace-<traceId>.json`; derived timeline notes and `/tmp/unmute-comparison-summary.json` were used to cross-check counts. Temporary files are not a durable benchmark archive; retain sanitized fixtures in the existing test-data location when implementing the checks.
- Canonical model layers: new `chat gemini-3.1-flash-lite`; native Pipecat `llm`; native LiveKit `llm_request`. Wrapper, STT, TTS and user-transcription generations were excluded.
- Medians use the ordinary midpoint median for even populations; means use available observations only. Missing timing and usage were not filled with zero. Values are rounded to milliseconds in the tables.
- Local `bench.turns.analyse` was run read-only against all four exports to verify the reader discrepancies. No live provider calls, application edits, Langfuse score writes or new evaluation runs were performed for this report. Previous test-suite results are not presented as fresh validation of concurrent changes.
