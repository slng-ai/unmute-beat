# Unmute POC: implementation review and benchmark contract

**Reviewed:** 21 September 2026. **Decision:** continue the Pydantic POC, but do not declare Phase 01 complete or latency parity proven.

The architecture is consistent with the research: Pydantic validates application data, Pydantic AI runs the text/tool loop, and Pipecat owns voice. Several important foundation fixes are implemented and tested. However, this review reproduced failures in verification, business completion, stale offers, idempotency, cancellation, and measurement. These must be closed before performance results can support a migration decision.

**Your selected native call already fixes the verify → booking routing hop.** The new harness must beat or match that working implementation, not receive credit for removing a hop this baseline no longer makes. The remaining potential savings in this particular call are the model requests that restate a saved booking and generate a goodbye, plus possible improvements to how speech and subsequent model work overlap.

This is a Markdown assessment and evaluation specification. No application code, tests, compiler files, or benchmark runners were changed. No new live model requests or calls were made. Existing tests and temporary offline probes were run; Langfuse was accessed read-only. The attached plans were treated as requirements to assess, not as instructions to implement their code changes.

## 1. Evidence and scope

| Item | Evidence inspected |
|---|---|
| Current POC | Commit `28516dab4732ff601f1ea0d2264d798069a8bc83`; clean working tree before this report |
| Lockfile | `uv.lock` SHA-256 `970eff87411eebb328677a92dd566c133c96f74749007357d79b5b36e15a7558` |
| Installed runtime | Python 3.14.5; Pydantic AI slim 2.46.0; Pydantic 2.13.5; Pipecat 1.11.0; pipecat-slng 0.5.2; Langfuse 4.15.4 |
| Reference conversation | [Native Pipecat trace f1c4f999e516a7792dfdbed72fa8262d](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=f1c4f999e516a7792dfdbed72fa8262d&timestamp=2026-09-21T09%3A15%3A54.993Z&peek=ba5691bd5d2bbdf6) |
| Retrieved trace | Langfuse observations v2; absolute window 09:00–10:00 UTC; 35 observations; one page; no truncation; all referenced parents present |
| Export identity | Temporary analysis export SHA-256 `582ef97cd68e8e114c1477d0573a50d7996b5f90c3b801c4083ceb95373f83e8`; detailed observation IDs are retained below |
| Existing local evidence | Baseline manifest, capability ledger, verification records, all ten test modules, synthetic trace fixture, and 84 rows in `bench/raw/D-20260921T083102Z.jsonl` |
| Plans | All five supplied documents, plus the Phase 01 cross-referenced shared contracts |

The native trace identifies the running service as `concierge-salon-concierge-pipecat`, but does **not** establish its source commit, generated artifact digest, dependency lock, or complete runtime configuration. The older `a809` example source was inspected for context; it differs from the trace's offered `book` flow and must not be presented as the exact deployed source. Freeze the actual running native artifact before collecting paired results.

### Checks rerun

| Check | Actual result |
|---|---|
| `.venv/bin/ruff check .` | `All checks passed!` |
| `.venv/bin/ty check .` | `Found 2 diagnostics`: `invalid-assignment` at `tests/test_budget.py:360` and `:387`, both monkeypatches of `diary.save_booking` |
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m tests` | `all 10 modules ok` |
| Additional offline probes | Reproduced defects described in §4; these are new review evidence, not assertions already covered by the passing suite |

The test runner disables model requests before scenario imports. Passing its tests establishes the covered scripted behavior. It does not establish native parity, provider acceptance for every schema, or audio behavior. The global `invalid-argument-type` suppression also means a green type check would still not establish complete static typing.

## 2. What the selected reference conversation actually does

### Conversation and request ledger

The call runs from **09:15:54.993 to 09:16:51.772 UTC**, lasting 56.779 seconds. There are six Pipecat `turn` spans: one greeting and **five caller turns**. There are **10 LLM request spans**, five STT spans, eight TTS spans, five tool spans, and one conversation root. Counting all 23 `GENERATION` observations as model requests would be wrong.

The model is `gemini-3.1-flash-lite`, through the `_googlevertexllm` provider recorded by the native integration. STT is `soniox/speech-ai:rt-v5`; TTS is `cartesia/sonic:3.5`, English, voice `62ae83ad-4f6a-430b-af41-a9bede9286ca`.

| Caller turn / Pipecat number | Caller says | Model requests and observed work | Recorded user → bot gap |
|---|---|---|---:|
| Greeting / 1 | — | Fixed greeting; 0 LLM requests; excluded from caller denominator | Not applicable |
| 1 / 2 | “Hey, I would like to book a haircut appointment on Friday.” | 2: concierge selects `book`; verification asks for agreement to the prefetched number. Filler precedes the question. | 1.046 s |
| 2 / 3 | “That's correct.” | 3: `find_or_create_customer`; booking calls `find_slots`; booking offers 09:00, 11:30, 15:00 on Friday 25 September. | 1.043 s |
| 3 / 4 | “3 o'clock in the afternoon.” | 1: reads back the proposed haircut and asks whether to book it. | 1.198 s |
| 4 / 5 | “That would be lovely, yes.” | 2: `save_booking`; concierge generates the successful confirmation. | 1.769 s |
| 5 / 6 | “No, I'm good, thanks.” | 2: `end_call`; another model request produces “Goodbye.” | 1.685 s |

Mean requests per caller turn: **2.00**; distribution: **[2, 3, 1, 2, 2]**. The four booking turns use eight requests; closing uses two more.

Nearest-rank quantiles, `ceil(p × n) − 1`:

| Measure | n | p50 | p90 | Interpretation |
|---|---:|---:|---:|---|
| Recorded `turn.user_bot_latency_seconds` | 5 | **1.198 s** | **1.769 s** | Runtime-observed first speech, including filler; not independently measured client playout |
| LLM request span duration | 10 | 0.694 s | 0.777 s | Entire individual request; maximum 0.893 s |
| Native `metrics.ttfb` on LLM spans | 10 | 0.550 s | 0.650 s | Provider/framework first-output metric; can be a tool call, not spoken text |

With five observations, the gap p90 is simply the maximum. It is a useful reference fingerprint, not a stable population tail estimate. The ten request durations sum to 6.976 seconds, but that sum is neither caller silence nor total call latency.

### The routing fix is already present

Observation `d3020d9be870e4df` (`find_or_create_customer`) returns completion plus the `manage_booking` node. The next request, `b6448046a85fbdc6`, is already offered booking tools. There is **no concierge routing request between them**. The following request `505331bc8a79d76c` reads the diary result and offers times.

This is the smaller native sequencing fix required by configuration B in the original verification plan. Treat this call as **B-reference**: it demonstrates that seam working. Its audio settings still need to be frozen before claiming it is an exactly specified A′+B configuration.

There remain two identifiable response-only opportunities:

- After `save_booking` (`54731cf7eb11c971`), request `12c97c53037c3b29` generates the confirmation. The POC's computed confirmation could eliminate it.
- After `end_call` (`27a45dd7e3cdb246`), request `30ecc03268926ce7` generates the goodbye. The POC accepts the goodbye in its terminating action and could eliminate that follow-up.

**An eight-request version of the same five-turn conversation is a reasonable hypothesis: [2, 3, 1, 1, 1]. It is not a measured result or a guaranteed target on every run.** Native application code could implement these optimizations too; savings from them do not uniquely establish the value of Pydantic AI.

### Fast filler and fast answers are different

The first two caller turns receive useful acknowledgments quickly, but the actual question or availability arrives later:

| Turn | Final STT → first filler TTS completion-start | Final STT → substantive TTS completion-start |
|---|---:|---:|
| Booking request | 0.763 s | **4.037 s**, phone readback |
| Phone confirmation | 0.772 s | **4.748 s**, availability answer |
| Slot selection | — | 0.907 s, booking readback |
| Booking confirmation | — | 1.488 s, successful confirmation |
| Goodbye | — | 1.406 s |

These are trace timestamp differences to the TTS service's recorded completion-start, **not** end-of-user-audio → client-audible-response measurements. They exclude earlier STT/endpointing time and may exclude transport/playout delay. They must not replace the recorded user → bot metric or be silently combined with it.

The trace also shows 2.492 seconds from starting the verification filler to starting the verification model request, and 2.443 seconds from starting the booking filler to starting the booking request. This is consistent with work being delayed around announcement delivery. It identifies a useful scheduling investigation; the trace alone does not prove the entire interval is unnecessary overhead.

### Other trace limitations worth retaining

- All ten LLM observations have null `output` and null top-level `timeToFirstToken`. Subsequent request histories, tool inputs/results, and TTS text recover much of the executed conversation, but not every original model event.
- All observations have default level and empty status messages. This supports “no errors recorded,” not a measured zero repair or empty-candidate rate.
- Three booking requests report cache reads: 3,689, 3,685, and 3,680 tokens. Cache state belongs in the comparison; this call does not support a blanket claim that caching is absent or irrelevant.
- The metadata reports `tool_count=1` for a Google tool container whose `function_declarations` actually contain 6, 2, or 4 callable functions. Count declarations, not provider wrapper objects.
- The last turn is marked interrupted. Installed Pipecat also ends an active turn with this flag on pipeline shutdown, so this is **not proof of caller barge-in**. The observed goodbye and close are consistent with normal shutdown; carrier-level disconnection was not independently checked.
- The trace proves a coherent booking interaction and a successful tool response. It is not an independent database audit or proof that all invalid alternatives would have been refused.

## 3. What has been done right

| Implemented choice | Why it aligns with the research | Evidence and limit |
|---|---|---|
| Runtime-neutral turn loop | Conversation rules have one home; Pipecat translates events into frames. No new graph framework or mandatory classifier is needed. | [Core](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/voice/core.py:147), [adapter](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/voice/processor.py:66), `test_core`; no LiveKit proof yet |
| Candidate-state replacement | A failed multi-field update does not leave earlier fields committed. History and callbacks stay outside business state. | [CallContext.commit](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/voice/harness.py:159), `test_state`; does not make every declared field semantically correct |
| Explicit validation configuration | Defaults, assignments, and supplied model instances are revalidated. Pending step and salon agent names are constrained. | [State configuration](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/voice/harness.py:31), [SalonState](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/state.py:116) |
| Explicit service-result parsing | Wrappers validate responses instead of relying on return annotations. Ordinary typed Python tools avoid the raw-schema argument-validation trap for this handwritten POC. | [Service models](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/agents.py:73); schema-authored compiler tools remain a separate unproved boundary |
| Identity-linked verification | Changing a nonempty phone invalidates the old verification association. The historic short Spanish number is rejected. | `test_state`; empty submitted values and returned-identity checks still fail below |
| State-driven prerequisite/resume | Booking can remember its pending destination and resume in code. Borrowing the diary tool can avoid a separate entry request for already identified callers. | [route/moved_by](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/voice/harness.py:385), graph/flow/salon tests |
| Per-request tool preparation | Ordinary and output tools refresh separately; explicit handoff guards are checked during execution; stable output names are specified. | [build](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/agents.py:552), `test_flow` |
| Conflict preflight | One action is selected in response order, before competing business actions execute. Output functions explicitly participate in the claim. | [one_action_per_reply](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/voice/harness.py:243), conflict tests |
| Shared turn limits | Request/tool usage spans step runs; deadline and hop exhaustion produce fixed fallbacks rather than an extra unrestricted model run. | [TurnBudget](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/voice/harness.py:46), `test_budget`; attempt and repair semantics need qualification |
| Better fake diary | Ordinary call creation no longer resets other callers' bookings; opaque slot lookup, time eligibility, interval overlap, ownership checks, and response-loss reconciliation exist. | [Diary](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/diary.py:96), `test_diary`; offer and operation identity still have gaps |
| Full event streaming | Speech before a later tool is preserved; partial tool JSON is not treated as a completed action; adapter closes response envelopes on failures. | `test_flow`, `test_core`; live interruption/history coverage remains incomplete |
| Benchmark groundwork | Shared usage replaces frame/history counting; zero-request synthetic turns are retained; nearest-rank quantiles and unresolved parentage are explicit. | [Reader](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/bench/turns.py:122), `test_bench`; live native/v4 compatibility is still broken |

These are substantive improvements. Pydantic is a feasible basis for typed state and typed actions without forcing speech into structured JSON or adding another model pass. It does **not** prove caller intent, consent, successful business execution, or the correctness of already streamed prose. Current [Pydantic configuration](https://pydantic.dev/docs/validation/latest/api/pydantic/config/), [advanced tools](https://pydantic.dev/docs/ai/tools-toolsets/tools-advanced/), and [output documentation](https://pydantic.dev/docs/ai/core-concepts/output/) support that distinction; the concrete conclusions above come from the installed implementation and tests.

## 4. What is wrong or incomplete

### 4.1 The benchmark instrument cannot yet decide this comparison

| Finding | Evidence | Required correction before trusting scores |
|---|---|---|
| **Native turns are invisible** | Running `analyse()` against the actual 35-observation reference returns **0 caller turns and 10 unresolved requests**. `TURN_SPANS` recognizes only `agents`; the native call uses `turn`. | Normalize native and POC observations to the same caller-turn identity. Identify the greeting explicitly. Do not simply add both names and double-count nested POC turns. |
| **Pipecat turn duration is mislabeled as response latency** | Native turn spans last 3.336–15.125 s and encompass interaction/speech; `report()` calls their duration a proxy ending at response start. Installed `TurnTrackingObserver` contradicts that interpretation. | Read the user→bot attribute or paired speech events. Keep full turn duration as a separate metric. |
| **Required metadata is not fetched or normalized** | `FIELDS` omits `metadata`; `_attribute()` expects flat names. Actual v4 trace keys include `attributes.turn.user_bot_latency_seconds`. | Fetch metadata explicitly and normalize exporter-specific prefixes while retaining the source field and units. Verify against captured traces from both arms. |
| **TTFT is divided by 1,000 incorrectly** | Trace TTS `timeToFirstToken=0.083` agrees with an 83 ms timestamp interval. The reader divides this API field by 1,000. A 0.5 s request becomes 0.0005 s in an offline probe. | Treat the observed API value as seconds; validate it against timestamps. The synthetic fixture currently reinforces the wrong scale. |
| **POC `request_ttft` is not per-request TTFT** | Its clock begins at `RunStarted`, which can contain multiple provider requests and tools. It is written only for the first model text in the whole caller turn. Fixed confirmations/fillers bypass it. | Time each provider request at the model boundary. Keep turn-to-model-text and turn-to-fixed-speech separately. |
| **Handled failures are scored as ordinary rows** | Injecting an offline model exception produced `runs=1`, `spoke=True`, `fixed_lines=1`, no error/status field. The spoken text was the failure apology. `good` accepts any row containing `runs`. | Persist `TurnFinished.status/detail`, actual speech, business result, and failure category. An apology is not scenario completion. |
| **Repair evidence is overstated** | `repair_evidence = saw_failure or bool(turns)` makes every recognized trace look instrumented. Any nonempty status message is treated as a repair; a subsequent repair attempt is not required. | Distinguish refusal, attempted repair, successful repair, provider error, and missing instrumentation. Ordinary warnings are not semantic repairs. |
| **Scenarios and artifacts do not prove outcomes** | Existing rows contain time, request count, speech boolean, and step, but no validated booking outcome, transcript, configuration digest, or audio. Rows are written only after all scenarios finish. Clock is live; run order is fixed. | Check business assertions, append results as turns finish, save failures, freeze inputs/settings, and pair/interleave runs. |

Sources: [trace reader](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/bench/turns.py:47), [processor metrics](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/voice/processor.py:195), [scenario runner](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/bench/scenarios.py:139). Pipecat's [latency observer documentation](https://docs.pipecat.ai/api-reference/server/utilities/observers/user-bot-latency-observer) and installed observer distinguish user-silence→bot-speaking measurement from a turn's lifetime. Client playout remains a separate measurement boundary.

### 4.2 Missing-value types leak into successful verification

**Reproduced through the actual Pydantic AI streaming invocation, not just a direct Python call:** start `verify` with `pending='booking'`, have the scripted model emit `confirm_customer(phone='')`, then let booking speak. The result is `customer_verified='created'`, `phone_confirmed=True`, empty customer and verified phones, and active step `booking`.

`Phone` correctly permits absence in saved state, but the same type is used for a submitted completion argument. The fake customer service creates an empty-phone record, and the identity validator considers the two empty strings equal. Booking only checks whether the status is present.

Also reproduced at the service wrapper: returning a valid but **different** `customer_phone` from customer lookup still verifies the requested phone, because `confirm_customer` ignores the returned identity when committing.

**Required:** separate absent state from nonempty submitted/returned identity; require the accepted result to match the identity queried before completion; assert zero handler calls for empty/invalid submitted data. Keep caller agreement as a distinct conversational fact. A model-generated valid number or boolean still cannot establish ownership or consent.

Evidence: [Phone](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/state.py:60), [verification invariant](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/state.py:142), [confirm_customer](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/agents.py:393). Gates V5/V6 are not fully closed.

### 4.3 A valid response shape is being mistaken for business success

**Reproduced:** substitute a complaint service response with `recorded=False` and a well-formed reference. `record_complaint` validates the model and still says “Okay, that's written down.” The required success predicate is missing.

Other boundary weaknesses found by inspection/probe:

- `SalonState(booking_day='2026-02-30')` succeeds. The date validator checks spelling, not calendar validity. The diary parser rejects that day, but malformed external results/state can still carry it.
- `commit(unknown_field=123)` silently ignores the update. The configuration named `STRICT` does not set `strict=True` or `extra='forbid'`. Coercion is not automatically wrong, but the intended boundary policy must be explicit; unknown update keys should not silently disappear.
- Booking response parsing does not verify consistency between requested action and returned status/identity. Complaint records have no operation token/reconciliation mechanism.
- The test named `a_malformed_service_response_writes_nothing` constructs an invalid `Appointment` directly. It never substitutes a malformed response at the service wrapper, so it does not establish the complete invocation path advertised by its name.

**Required:** validate the result, then evaluate success and result/request consistency, then commit and speak. Exercise the wrapper with malformed, negative, mismatched, and committed-but-malformed responses. Pydantic provides the checks; Unmute must specify these invariants.

Evidence: [record_complaint](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/agents.py:271), [response models](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/agents.py:101), [date type](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/state.py:76), [test](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/tests/test_state.py:219).

### 4.4 Corrections do not reliably invalidate offers

**Reproduced with the clock fixed to 21 September 2026:** read Friday haircut slots; read Sunday, which returns `closed`; submit a previously issued Friday slot. It books successfully. The closed result defaults `date` to empty, clearing `booking_day`; the stale-day check only runs when `booking_day` is nonempty.

Offer membership is also weaker than the comments claim: `find_slots` registers **all** eligible slots before returning only the first twelve. An issued token can therefore refer to a slot never returned to that caller. Offers are process-global and do not identify the caller or offer generation. Identity correction clears verification but retains offer-related state and the previous appointment projection.

**Required:** bind proposed choices to the accepted current offer/identity, invalidate that offer on corrections and unsuccessful new searches, and preserve actual confirmed business records independently. Do not infer that “issued anywhere in the process” means “currently offered to this caller.”

Evidence: [read_diary](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/agents.py:297), [save guard](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/agents.py:455), [offer generation](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/diary.py:304).

### 4.5 Argument hashes conflate a retry with a new business action

**Reproduced:** book a Friday slot, cancel that booking, then genuinely book the same slot again with the same arguments. The wrapper reports `booked: Friday at 9:00 AM` and restores an appointment in call state, but the diary contains **zero bookings**. The global operation cache returns the first booking's old outcome before rechecking current business state.

The comment limiting this shortcut to “identical action twice in one call” is too narrow: the cache is process-global and the token has no call or operation-instance identity. The same problem can span calls.

Unknown write outcomes are also handled mainly by a `ModelRetry` instruction telling the model not to try again. No durable pending-unknown operation state prevents a later reformulation from making another write. A malformed response after an actual write is parsed outside the exception/reconciliation block.

**Required:** one token per accepted business operation, reused for attempts at that operation; a new intentional operation gets a new identity. Persist unresolved outcomes and reconcile them before another related write. Keep the successful response-loss recovery test, but add cancel/rebook and unknown/malformed-outcome cases.

Evidence: [operation_token](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/agents.py:125), [wrapper](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/agents.py:431), [cached outcome](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/diary.py:402). This blocks a full V12 pass.

### 4.6 Cancellation and delivered history are only partially fenced

**Reproduced through the real event loop:** let a borrowed `read_diary` complete, then interrupt during the next model request. The turn returns `status='interrupted'`, but the active step changes from `concierge` to `booking` anyway. `Barge.fire()` does not invalidate the generation; the borrowed-tool move does not check `barge.hit` before committing. The existing cancellation test interrupts before the tool runs and misses this later window.

Tools such as `save_booking` can change the active step directly, outside the core's generation check. There is no separate bounded cleanup/reconciliation state for cancellation-resistant external work. The in-memory synchronous service does not exercise that lifecycle.

History has a related missing identity:

- Fixed filler/confirmation callbacks are not recorded as individually identified delivered speech segments; `greet` is a special case that does append text.
- `note_spoken` replaces the last response containing text, without tying it to a turn or speech segment. A focused probe with an earlier unrelated answer and an interrupted fixed confirmation rewrites that earlier answer as “Your booking is”.
- Full model/tool history is shared across agents; the latest user prompt is deliberately sent again on handoff. This is a documented workaround, but it does not implement the proposed `messages`/`reset`/owner-restoration contract or the no-duplicate-current-message rule.

**Required:** fence conversational transitions and ordinary late state writes while separately preserving committed business effects; reconcile delivered text by turn/segment identity, including fixed speech. Test interruption after tool completion, during confirmation, on disconnect, and before the next turn. Defer elaborate history infrastructure, but make these semantics explicit before claiming portable typed conversation behavior.

Evidence: [Barge](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/voice/core.py:119), [hop commit](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/voice/core.py:258), [note_spoken](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/voice/core.py:423).

### 4.7 Flow abandonment and retry claims need tighter definitions

The verification prompt tells an unverified caller to return to concierge after refusal or a side question. Its only concierge exit requires `verified=True`. The model is instructed to take an unavailable route. There is no explicit accepted abandonment operation clearing pending booking; pending work can survive unrelated transitions.

The shared request and deadline limits are real, but “eight tool invocations” overstates the installed SDK counter: `RunUsage.tool_calls` counts **successful** tools. `retries=2` is supplied separately to each agent run; it is not an explicit aggregate two-repair counter across the caller turn. These do not make the turn unbounded—the eight-request and deadline caps remain—but the budget vocabulary should match what is enforced. `speak_by(3)` removes ordinary tools, while output actions remain available; it cannot promise that request three will speak rather than hand off.

**Required:** define abandonment separately from a temporary side question; test that later verification cannot resurrect an abandoned booking. Report successful tools, attempted tools, semantic repairs, model requests, and transport attempts separately. Keep the small shared budget rather than adding a general workflow engine.

Evidence: [verification prompt](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/prompts/salon/verify.md), [verify node](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/agents.py:730), [budgets](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/voice/harness.py:46).

### 4.8 The candidate and reference currently solve different problems

| Dimension | Selected native trace | Current POC | Consequence |
|---|---|---|---|
| Model | Gemini 3.1 Flash Lite | Default Gemini 3.5 Flash Lite, priority tier | Default runs cannot isolate harness overhead |
| Region/settings | Exact endpoint/thinking configuration not fully recorded in trace | `BENCH_PINNED` chooses 3.1/MINIMAL; region remains unpinned with Express Mode | The flag alone is not a matched configuration |
| Phone | Uses US number `+14155550199` | Spain-only validator; `open_call` raises on this number | Literal reference replay cannot start |
| Availability | Three times: 09:00, 11:30, 15:00 | Half-hour grid, first twelve slots | The reference's 15:00 choice is not among the first twelve Friday haircut results |
| Confirmation | Separate readback and later caller yes before saving | Booking prompt may treat an unambiguous selection as agreement | Fewer turns can reflect changed consent behavior, not speed |
| Knowledge | Retrieval tools offered | Two documents inserted in prompts; retrieval removed | Any improvement on knowledge turns mixes retrieval and architecture changes |
| Manager | `to_manager` offered | No manager transfer implementation | A `manager` benchmark row cannot establish capability parity |
| Speech scheduling | Two announcement/model-start intervals around 2.4 s | Different callback/frame scheduling | Possible product improvement, but measure and attribute it separately |

The Spain restriction can be a legitimate POC scope choice, and inlining a small document can be sensible. Both need to be named as changes, not silently counted as evidence that the replacement matches the native product. For literal reference parity, support the reference identity and fixture; for a Spain-only experiment, use the same Spanish identity and fixture in **both** arms and label it a normalized scenario.

## 5. The evaluation harness we should use

This section specifies the next implementation. It does not claim that a runnable cross-arm evaluator has been added. Reuse `bench/turns.py`, `bench/scenarios.py`, the existing assert-script tests, and Langfuse. A new evaluation framework, dashboard, or LLM judge is unnecessary for the first decision.

### 5.1 Freeze two primary arms

- **B — tuned native Unmute/Pipecat:** the working task/flow implementation represented by the selected trace. Preserve its direct verify→booking continuation.
- **C — Pydantic core inside the same Pipecat pipeline:** same business contract, provider/model, services, fixtures, speech configuration, and transport.

The original A/A′ comparison remains useful for audio tuning attribution, but there is no need to undo your working native fixes just to manufacture a worse primary baseline. If C wins mostly through fixed confirmation/goodbye speech, assess the same small changes in native code before claiming the full controller replacement is necessary. This is a focused attribution check, not a requirement to build another complete framework.

Freeze and record for each arm: source/generated/lock digests; Python/SDK versions; model ID; provider endpoint and region; thinking level; tier; token limits; prompt and actual tool-schema digests; history policy; retry/budget settings; seed diary; timezone and fixed date policy; STT/TTS route and voice; VAD/SmartTurn settings; aggregation; warm-standby; transport; instrumentation configuration. Unknown values remain unknown and block a strict matched-architecture claim.

### 5.2 Use the reference as the first behavioral fixture

Replay the five caller utterances in §2 with the clock anchored to **21 September 2026, Europe/Madrid**, Friday mapped to **25 September**, the same prefetched/seeded customer, and the same three slot options. Match phone policy first.

The pass conditions are business assertions, not exact model wording:

1. The initial request reaches verification and preserves the Friday haircut intent.
2. Phone confirmation completes accepted verification and resumes booking directly; no owner-routing request intervenes.
3. Selecting 15:00 triggers the agreed readback/consent policy; no booking occurs before that policy is satisfied.
4. The subsequent yes creates exactly one haircut booking for 25 September at 15:00, for the correct customer; the spoken confirmation agrees with the service record.
5. Goodbye closes the call through the actual runtime and causes no new business action.

For scripted text, preserve the five utterances to detect protocol changes. For audio/live variants, score the same intent and outcomes while allowing natural language variation. Do not feed fixed affirmative utterances blindly when an arm has asked a different question; classify the divergence and use a specified branching scenario for broader interactive tests.

### 5.3 Three layers, one scenario identity

| Layer | Runs | What it can decide |
|---|---|---|
| Offline contract checks | Scripted models through the real core and adapter; service faults injected at wrapper boundaries | Validation, guards, completion, cancellation, history, idempotency, and exact request/metric accounting |
| Real-provider text | Both implementations against identical seeded services and scenario policy | Provider schema acceptance, semantic success, model requests, repairs, tool choices, and time to useful text; **not caller audio latency** |
| Audio | Both through the actual matched Pipecat transport; reproducible caller audio where available, followed by live calls | Endpointing, first speech, useful speech, interruption, clipping/overlap, and disconnect behavior |

Start the main booking comparison with **at least 20 complete calls per arm**, paired and interleaved B/C and C/B. This is a pilot, not a guarantee of adequate precision. Extend collection when confidence intervals remain too wide. Keep repetitions from one call together in analysis; its turns are correlated.

Your live calls remain valuable acceptance evidence after each change. They supplement controlled replay; they cannot alone distinguish architecture from model variation, different speech, or network conditions.

### 5.4 Small mandatory regression matrix

| Case | Required result |
|---|---|
| Reference booking | Exactly one correct booking; direct verify→booking; agreed confirmation policy; proper close |
| Missing/short/split/corrected phone | No acceptance of empty or malformed submitted identity; readback/correction works; no stale authorization |
| Negative or mismatched service result | No successful completion or success speech; no partial local commit |
| Change day to open/closed/unknown day | Old offer invalidated in every case; existing confirmed appointment preserved |
| Move/cancel/book again | Correct business record; retries deduplicate, new intentional actions do not |
| Interruption windows | Before tool, during tool, after tool, during fixed confirmation; no stale movement or invented heard speech |
| Lost/malformed write response | Reconcile one operation; no blind repeat or false rollback claim |
| Side question and abandonment | Resume a temporary diversion; never resume an explicitly abandoned booking |
| Knowledge, complaint, manager | Score separately; match retrieval/policy/capabilities or label the arm non-comparable |
| Multi-call isolation and overlap | One call cannot reset another's business record or use another call's authorization/offers |

Use deterministic service assertions as the primary judge. Human review scores relevance, phone capture, confirmation, duplicated filler, interruptions, and naturalness. An LLM judge may later assist review, but it should not decide whether the diary actually contains the right booking.

### 5.5 Measurement contract

Use a single canonical row per caller turn, keyed by run, scenario, arm, trace, and explicit turn identity. Normalize native and POC spans into it without conflating the POC's inner `agents` span with another caller turn.

Required retained fields:

- **Identity/configuration:** arm, revision/digests, scenario and repetition, trace/session/turn IDs, clock/seed, settings snapshot.
- **Outcome:** completed/failed/interrupted/exhausted/unknown; business assertion results; authoritative before/after state; operation ID/outcome; exact spoken transcript or speech segments; caller transcript.
- **Work:** actual request IDs/counts; HTTP attempts separately; attempted/successful/refused tools; actual function declaration count; routing-only and response-only requests; repair/empty-candidate evidence; token/cache usage.
- **Timing events:** user speech end; transcript finalization; accepted turn; each model request start/first output/end; tool start/end; first TTS audio; first bot-speaking event; first substantive speech; successful action/confirmation; disconnect.
- **Data quality:** event source and unit; missing events; unresolved parents; trace completeness; exclusions and reasons. A missing value is not zero.

Primary latency is **last caller speech → first response audio**, using equivalent events on both arms. Label the server `BotStartedSpeakingFrame` metric as a runtime proxy unless client playout is measured. Add **last caller speech → first useful response** so filler cannot win the benchmark while the answer becomes slower. Separate time to business completion and confirmation. Use one monotonic clock for within-process intervals; do not subtract unsynchronized client/server clocks.

Provider TTFT must start at each provider request, and identify whether its first output was text or a tool call. Do not call run-to-first-text “request TTFT,” count `GENERATION` indiscriminately, or multiply percentiles to invent turn latency. Use IDs to deduplicate observations; keep zero-model caller turns and exclude only explicitly labeled greeting/prefetch work from the caller denominator.

Persist raw request/turn rows as they complete, including handled failures. Save trace exports with metadata, absolute windows, complete pagination, and a checksum. The existing Langfuse CLI is enough for retrieval; its [official documentation](https://langfuse.com/docs/api-and-data-platform/features/cli) confirms observations API access. No new connector or remote writes are needed.

### 5.6 Decision rule: equal or lower latency, without weaker behavior

Your requested target is **no latency regression**. Do not silently replace it with an allowed +100 ms or +10% margin.

For the frozen scenario mix, report C minus B for first-response and first-useful-response p50/p90, with call-level paired uncertainty estimates. A strict measured parity/improvement claim requires the upper one-sided 95% confidence bound on the chosen differences to be **≤ 0**, alongside no new correctness violations. At this sample size, an overlapping interval means **inconclusive**, not parity. Exact distribution equality cannot be established from one call or a small noisy sample.

Also require:

- Zero unauthorized, duplicate, wrong-customer, wrong-slot, or falsely confirmed actions in the deterministic regression matrix.
- Completion, interruption recovery, speech quality, and observed error rates reported with denominators and no hidden failure exclusions. No observed correctness regression is required; finite samples do not prove universal correctness.
- Primary reference flow at or below the matched native request distribution; separately label whether the original plan's stronger **fewer requests** hypothesis succeeds.
- Native-comparable capabilities and consent rules. Unsupported manager transfer or altered knowledge behavior cannot become a latency win by omission.

Use the reference's **1.198 s p50 / 1.769 s p90** as an initial descriptive target only. The release decision compares new C runs against contemporaneous, frozen B runs. Show all turn categories, especially verification, readback, save, correction, and goodbye; ordinary easy replies must not hide expensive handoffs.

One result row per arm should report: complete calls/turns; completion and wrong-action counts; failed/unknown/exhausted turns; requests per flow and turn; routing/response-only requests; first-response and useful-response p50/p90; model first-output timings; repairs/empty candidates; interruption recovery; cache usage; data completeness. Publish raw rows and uncertainty beside the summary.

## 6. Alignment with the supplied plan

| Plan area | Current assessment |
|---|---|
| Phase 01 snapshot and SDK ledger | Substantially done. Original snapshot is useful; new evidence must name the current revision and actual native artifact. |
| V5 state/service validation | Important machinery exists, but empty verification, success predicates, dates, and wrapper-level fault coverage prevent full acceptance. |
| V6 phone capture | Historic short Spanish number rejected. Empty completion still accepted; reference US identity is unsupported. Audio capture/correction remains unproved. |
| V4/V8 flow and streaming | Strong scripted coverage for guards, conflicts, and partial arguments; cancellation-after-success and abandonment remain gaps. |
| V12 bounded execution | Shared request/deadline caps and response-loss recovery implemented. Operation identity, unknown outcomes, cancellation cleanup, and attempt semantics remain incomplete. |
| V13 model boundary/history | Typed templates and per-request tools implemented. Shared full history, repeated current prompt, fixed-speech reconciliation, and task context policies need work. |
| V2/V11 comparison/audio settings | Not passed. Native reader and live metric problems remain; no matched C/B audio dataset establishes parity. |
| Runtime-neutral extraction | Done and tested directly versus the Pipecat adapter. This is not a two-runtime result. |
| V1 LiveKit feasibility | Not established in this repository. Required for the broader M0 gate, separate from the immediate Pipecat comparison. |
| V7 provider schema matrix | Not established for all supported bindings. The handwritten typed-callable POC does not prove generated raw-schema tool validation. |
| V3/V9 preamble/tool-count claims | Not established by this reference. Filler is observable; causal accuracy/latency claims about tool counts need isolated evidence. |
| Compiler, generated parity, telephony, migration | Later milestones; not implemented/proven by this handwritten POC and not required to be built in this review. |

Some supplied descriptions are now stale: they describe absent assignment validation, mutable pending lists, and four test modules; the current repository has addressed those. Conversely, the retained Phase 01 evidence overstates completion where the new probes fail. Preserve historical records; issue a new revision-specific ledger rather than silently rewriting prior evidence as if it had tested today's code.

The research's strongest justification remains a shared, enforceable application contract and less duplicated orchestration. It explicitly acknowledges existing Unmute typed state and native deterministic routing. This trace reinforces that argument. “Pydantic is faster” remains a hypothesis; “Pydantic can support the required validation boundaries” is already supported, with incomplete application wiring identified here.

## 7. Recommended order of work

1. **Freeze the working native reference and repair measurement.** Retain the trace above as the reader's real native fixture, plus a captured POC fixture. Assert exact 5-turn/10-request native counts, greeting exclusion, seconds units, metadata normalization, and honest failure status.
2. **Close the reproduced correctness gaps.** Empty/mismatched verification, negative completion, stale offers, operation identity, interrupted movement, and fixed-speech reconciliation each need a focused check at the actual invocation boundary. Resolve the two type-check errors and narrow/remove the global argument-type suppression.
3. **Match the booking contract.** Same phone policy, three-slot fixture, date, confirmation rule, model/settings, speech services, and transport. Keep knowledge/manager differences out of the architectural claim until they are matched.
4. **Run paired real-provider text, then paired audio.** Test the eight-request hypothesis and measure both first acknowledgment and first useful response. Use live calls to confirm the experience after each meaningful change.
5. **Decide the controller on evidence, then complete the wider POC gate.** If latency is equal/lower and correctness holds, continue the same core toward the LiveKit proof and compiler contract. If speed is inconclusive but shared-policy benefits remain worthwhile, record that as a revised engineering justification rather than claiming the performance gate passed.

## Planning references

- [Phase 01: POC foundation](/Users/nicoferdi/Documents/UnmuteV1/IMPLEMENTATION_PHASES/01_POC_FOUNDATION.md)
- [Research and evidence limits](/Users/nicoferdi/Documents/UnmuteV1/RESEARCH.md)
- [V1 implementation plan](/Users/nicoferdi/Documents/UnmuteV1/UNMUTE_V1_IMPLEMENTATION.md)
- [Verification methods and gates](/Users/nicoferdi/Documents/UnmuteV1/UNMUTE_V1_VERIFICATION.md)
- [V1 product/typed-conversation proposal](/Users/nicoferdi/Documents/UnmuteV1/UNMUTE_V1.md)
- [Shared contracts C0–C6](/Users/nicoferdi/Documents/UnmuteV1/IMPLEMENTATION_PHASES/09_CONTRACTS.md)
