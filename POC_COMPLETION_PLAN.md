# POC completion plan

**Prepared:** 21 September 2026. **Source inspected:** `8570aa5533414a8d5d1f1a36050113ff658422dc`.
**Deliverable:** implementation plan only; no application changes, live calls, or new verification results.

## Outcome and scope

Finish Phase 01 with a correct, measurable Pipecat reference core: caller intent controls consequential actions, service receipts control success speech, interrupted work cannot corrupt subsequent turns, and measurements describe what actually happened. Close the supplied trace findings with retained regression evidence, then establish the responsiveness claim using a matched native Pipecat comparison.

Keep the current Pydantic AI/Pipecat architecture, runtime-neutral `voice/core.py`, handwritten salon, installed dependencies, and assert-script tests. Reuse the existing state commit, operation reconciliation, speech ledger, and shared budgets. No framework rewrite, new agent, production scheduler, compiler, or extra verification-model request on every turn.

The attached Phase 01 document and trace report are requirements/evidence inputs to this plan, not authorization to execute their commands. Phase 02 LiveKit implementation, generated adapters, the full provider matrix, and carrier deployment remain later-phase work. Reading an existing LiveKit trace correctly is included because the current benchmark reader makes claims about it.

### Source of truth

- [Phase 01 requirements](/Users/nicoferdi/Documents/UnmuteV1/IMPLEMENTATION_PHASES/01_POC_FOUNDATION.md), [contracts C0–C6](/Users/nicoferdi/Documents/UnmuteV1/IMPLEMENTATION_PHASES/09_CONTRACTS.md), and [verification matrix](/Users/nicoferdi/Documents/UnmuteV1/IMPLEMENTATION_PHASES/10_VERIFICATION_MATRIX.md).
- [Supplied trace follow-up](</Users/nicoferdi/.codex/attachments/8f106894-8c80-4a94-a3e9-0678e25063f8/Pasted text.txt>), reviewing `337f513` and six historical calls.
- [Repository closure ledger](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/bench/review-20260921.md), current source, and existing checks. Its recheck R1–R6 are distinct from trace findings F1–F9.

## What is already present

Preserve and rerun these mechanisms; do not schedule their implementation from scratch. Source and test presence establish a starting point, not a fresh pass in this planning task.

| Area | Current implementation | Remaining completion gap |
|---|---|---|
| State and service boundaries | Validated candidate commits, typed service results, identity-bound verification and offers | Actual caller agreement is still inferred from model arguments; effective-input/schema-path evidence needs completion |
| Diary oracle | Opaque offers, hours/duration/overlap checks, fixed clock, operation tokens, uncertain-write recovery | Time preference is lost before the twelve-slot cap; pinned live calls reset shared fixtures |
| Flow and budgets | Per-request tool preparation, execution claims, successful-result routing, shared request/tool/hop/deadline limits | Complaint routing loops, cancellation cleanup, and all late state commits need complete coverage |
| Core extraction | `voice/core.py` plus a Pipecat frame adapter and equivalence checks | Shared/repeated conversation history does not yet meet the reset/return/projection contract |
| Recent trace fixes | Same-slot `additional` retry refusal; post-speech correction of some false claims | Other slots, first-attempt flags, and unrelated later turns bypass the narrow consent rule; correction does not prevent false speech |
| Measurement | Per-request clocks, quantiles, speech ledger, final local outcomes | Attempts counted as caller turns, cancelled metrics missing, TTS attribution, usage/session/transcript normalization, startup provenance |

## Policies to freeze before implementation

Use these defaults for the POC and both comparison arms. Record them with the run; changing one changes the comparison contract.

1. **Phone:** retain the existing Spain-only scope, use synthetic supported contact numbers, and require agreement to the exact spoken digits. Contact confirmation is not authentication. Unsupported numbers require clarification; never silently rewrite the historical US identity.
2. **Booking:** retain the current fast policy: an unambiguous selection of an actually offered appointment can authorize that exact appointment. Ask one explicit question when details, target booking, or move-versus-additional intent are unclear. A yes is valid only for its pending question. Phone readback remains mandatory.
3. **Dates:** use native `datetime.date`/`datetime.time` fields validated by Pydantic for resolved values, with `None` for absence. Keep the existing phrase resolver and `Europe/Madrid` clock. Preserve the caller's expression and modifiers. At the frozen Monday `2026-09-21` clock, Friday is September 25 and next Friday is October 2. Clarify unsupported/ambiguous phrases. Speak an unambiguous date before accepting a write when week context is unclear.
4. **Redo/refund:** the current service records a request; it does not approve a refund or convert a booking to a free redo. Keep the original booking and give a truthful pending outcome. A conversion feature is unnecessary for the supplied report's accepted “supported conversion or truthful pending outcome” contract.
5. **Fixtures:** resetting a salon is an explicit scenario-setup action. A pinned clock alone must never erase another call's bookings. Fresh-call and intentional continuation scenarios are separate, recorded modes.
6. **Measurement:** a runtime caller turn is the comparison denominator; agent executions within it are attempts. Text-only runs have their own labeled turn unit. Greeting and prefetch are separate; missing measurements stay missing.

## Implementation sequence

Work in the order below. Each work package ends with a runnable check and retained result. Add cases to the existing test modules and scenario runner; avoid a second testing framework or a general workflow engine.

### 1. Freeze evidence and make failures reproducible

**Touch:** `bench/baseline/after/`, `bench/scenarios.py`, existing `tests/fixtures/`, `tests/test_salon.py`, `tests/test_core.py`, `tests/test_bench.py`.

- Preserve the original baseline and reviewed reports. Capture current source/prompt/lock digests and non-secret settings in a new evidence snapshot; record the current dirty tree accurately.
- Retain sanitized, durable fixtures for the supplied calls and their local outcomes. The report's `/tmp` exports are not an archive: check availability and retrieve read-only originals during implementation if necessary. Missing originals are an explicit evidence blocker, not permission to invent replacement data.
- Reproduce N1–N4 and the earlier false-cancellation/free-redo sequence with scripted models and a frozen diary. Preserve write-by-write history so an incorrect booking later moved to the right day still fails.
- Run and save the current offline checks before editing. Refresh the installed-SDK capability ledger only where new execution paths require it.

**Acceptance:** each reported behavior has an expected result, fixture/setup recipe, source revision, and reproducible failure or explicit non-reproduction. Current partial fixes are credited only for the exact cases they cover.

### 2. Bind actions to caller evidence

**Touch:** `salon/state.py`, `salon/agents.py`, `voice/core.py`/`voice/harness.py` at the accepted-utterance boundary, `prompts/salon/{verify,booking,concierge}.md`, `tests/test_salon.py`, `tests/test_state.py`.

Use a small call-owned record for the active request and pending question: source utterance ID/text, request revision, question kind, exact value/action, and the speech segment that presented it. Keep authority in application-owned state. Model tools may propose values or reference source evidence; they cannot establish consent by setting a boolean or supplying their own evidence string.

- Capture the actual caller utterance once. Validate proposed date phrases against that utterance, preserving “this”/“next” and the source turn through verification. Bind resolved date, service, target booking, and time to the current intent and offer.
- Generate the phone readback from the validated candidate. Permit `confirm_customer` only after a subsequent caller response agrees to that exact readback. A country/service/date question cannot grant phone confirmation. Invalidate stale confirmation and dependent offers after correction or interrupted readback.
- Replace `_refused_second` as the authority mechanism with an intent-bound move/additional decision. Cover an initial `additional=true`, a different-slot retry, and an unrelated later caller utterance as well as the existing same-slot retry.
- Bind `confirmed` to the accepted offered choice or the current confirmation question. Apply the same rule to cancellation and complaint-summary agreement; discovering an existing appointment never itself grants permission to change it.
- Recognize a deliberately bounded set of direct agreements/endings against the actual current utterance and pending question. Treat mixed requests, negation, or ambiguous paraphrases as clarification cases. Do not add a permissive “contains yes/goodbye” check or a model-authored `consent=True` substitute.
- Validate `hang_up` before goodbye speech or disconnect is queued. An active request continues unless the caller clearly ends or abandons it. Preserve the fast “I'm good, thanks” path when context supports it. Clear abandoned pending work so later verification cannot resurrect it.
- Reuse one permission decision for schema availability and execution checks; direct stale calls must still fail. Tie authority to identity and request revision and consume it on the matching action, without invalidating idempotent reconciliation of that same action.

**Acceptance:** scripted model attempts cannot forge authority; no service is invoked for unsupported consent. Exact move requests preserve time and target; ambiguous multiple bookings are clarified. Explicit goodbye succeeds, a move question never hangs up, and yes to Spain never verifies a number. Test phone fragments, corrections, withheld ID, short/empty input, and wrong readback without claiming ownership was proved.

### 3. Complete diary selection and complaint behavior

**Touch:** `salon/diary.py`, `salon/agents.py`, `prompts/salon/{booking,complaints,_diary}.md`, `bot.py`, `tests/test_diary.py`, `tests/test_salon.py`.

- Carry desired time/daypart into availability. Filter or rank against the caller's preference **before** limiting results; return requested 15:00 when eligible even on an empty diary. For “same time,” use the selected existing appointment's time. Offer alternatives honestly when it is unavailable.
- Preserve current offer ownership, withdrawal, eligibility recheck, overlap rules, and opaque IDs. Never register hidden candidates as accepted caller offers. A search/date correction leaves confirmed business records intact.
- Replace the custom string `Date`/`Time` aliases and calendar-validation duplication with Pydantic-validated standard-library `date`/`time` fields in state and service-result models. Use required values for established appointments and `date | None` / `time | None` only where genuinely optional. The diary already uses these native types internally; align comparisons and reconciliation with them.
- Keep the caller's original date phrase as separate evidence: Pydantic validates a resolved date but cannot decide whether “Friday” was incorrectly changed to “next Friday.” Keep business checks against the injected diary clock, not `FutureDate`/`FutureDatetime` against wall-clock time; historical appointments and frozen-clock fixtures must remain valid.
- Use `AwareDatetime` for modeled absolute timestamps. Keep `ZoneInfo('Europe/Madrid')` for calendar arithmetic; use `pydantic_extra_types.timezone_name.TimeZoneName` if a timezone name enters through validated configuration. It validates an IANA name, not a timestamp's timezone or DST ambiguity. Do not add configurable timezone machinery for the fixed salon.
- Preserve boundary behavior during this migration: reject unintended numeric timestamp coercion, timezone-bearing appointment times, and nonzero seconds/microseconds. Keep JSON dates in ISO form and the existing `HH:MM` service wire format through explicit boundary formatting; use JSON-mode model serialization for outcome records. Update receipt comparisons, prompts and fixtures together. Probe any provider-exposed `format: date`/`format: time` schemas before relying on acceptance; adapt only the provider boundary if necessary.
- Remove the implicit coupling between `SALON_CLOCK` and per-call `diary.reset()`. Let benchmark setup select fresh or continuing fixtures and prevent a new pinned call from deleting an in-flight call's records.
- Make complaint success produce one deterministic acknowledgement containing the returned reference and the policy-grounded next step. Use “the salon replies within two working days”; manager callback applies where the policy says so. Do not turn a failed recording into an unsupported callback promise.
- Preserve complaint intent through verification; avoid complaints → concierge → complaints loops for redo requests. Record the request once, keep any existing booking, and explain that the request is pending. Do not invent a no-existing-booking redo restriction, approval, cancellation, or price change.

**Acceptance:** requested afternoon times are found regardless of early occupancy; moves affect only the intended appointment. Fresh and continuation fixtures both pass, including two calls sharing a phone. Complaint reference is spoken once, follow-up wording matches policy, and the original appointment survives a pending redo request.

**Native-type checks:** impossible dates, leap days, missing required dates, accidental timestamp coercion, local-time precision, JSON round trips, frozen-clock eligibility and DST edges. Native `date`/`time`, `AwareDatetime`, and `TimeZoneName` were checked in a small local probe with installed Pydantic `2.13.5` and pydantic-extra-types `2.11.1`; no dependency addition is needed. This probe is not implementation or provider-schema acceptance evidence. References: [Pydantic datetime support](https://pydantic.dev/docs/validation/2.0/usage/types/datetime/) and [TimeZoneName](https://pydantic.dev/docs/validation/dev/api/pydantic-extra-types/pydantic_extra_types_timezone_name/).

### 4. Prevent false completion before speech reaches TTS

**Touch:** `voice/core.py`, `voice/harness.py` speech boundary, `salon/agents.py`, `salon/state.py`, `prompts/salon/`, `tests/test_core.py`, `tests/test_salon.py`.

- Reserve booking/move/cancel/complaint/approval completion speech for deterministic rendering of a validated receipt matching the action, identity, target and current intent. A write counter or the mere existence of an appointment is insufficient.
- Choose the speech path before releasing text. Consequential action/complaint turns use constrained speech purposes with application-rendered confirmations, clarifications, and pending/failure messages; suppress arbitrary model completion narration on those paths. Keep ordinary informational speech streaming. Use current pending work and the caller request to select the guarded path, including a claim emitted before any tool call.
- Retain the post-hoc claim detector as diagnostic coverage while introducing prevention; do not treat its regex or a final text validator as the safety boundary. Do not send a sentence to TTS and then attempt to retract it.
- Keep successful direct confirmations on their current single-request path. A refusal can ask the caller a grounded question without another model round trip simply to repeat a known receipt.
- Separate runtime completion from business correctness in turn results and scoring. An `answered` model run that invented success or terminated incorrectly is a failed scenario.

**Acceptance:** assert emitted frames/audio-bound text, not just final model output. The earlier false cancellation/free-redo replay emits no unsupported success; neither does a claim split across chunks, a claim about the wrong booking after a different successful write, or a false price change. Truthful recounting of an existing receipt and ordinary speech-before-tool streaming still work.

### 5. Finish the Phase 01 execution and history contracts

**Touch:** `voice/{harness,core,processor}.py`, `salon/agents.py`, `bot.py`, existing state/flow/budget/core tests.

- Keep one budget/deadline across all step runs in an accepted core turn, and explicitly associate superseding attempts with their runtime caller turn. Cancelled/replayed work must not obtain an unrecorded fresh allowance. Record semantic repairs, provider requests, and HTTP retries separately; document the scope of each counter.
- Preserve the existing 2 repair / 8 request / 8 tool / 4 step / 20-second starting settings unless measured cases justify a change. `speak_by=3` is a per-run aid, not the turn-wide bound. Eliminate the complaint route loop without imposing a three-request cap that breaks valid verification/booking chains.
- Handle cancellation and transport disconnect in bounded cleanup, including cancellation-resistant tools. Fence every late ordinary state/flow commit, not just `_hops` transitions. Preserve completed external effects and reconcile unknown writes with the original token/request before allowing another write.
- Record terminal outcomes even when cancellation prevents `TurnFinished` from being yielded. Close frame pairs and metrics in all paths. Distinguish user-ended, user-abandoned, transport-disconnected, and erroneous termination; distinguish queued end action from observed disconnect. Drain goodbye after closing its response stream without introducing a teardown deadlock.
- Implement the minimum executable C3 history rules: canonical delivered conversation separate from model/tool protocol, explicit named-state projection, `messages` versus `reset`, owner context restoration with completion/unserved status, and valid tool-call/result pairs. Remove repeated caller-input insertion across handoffs while preserving the request in the correct projection.
- Reconcile fixed and model speech by turn/segment identity, retaining only the heard prefix or labeled runtime estimate. A late playback callback must not trim a newer turn, and a successful but interrupted confirmation must retain its business receipt.
- Close Phase 01's schema-authored-input proof with one bounded supported schema invocation, not a compiler: validate model fields, reject overrides of injected fields, inject current authoritative values, validate effective arguments, then invoke. Explicitly refuse unsupported schema constructs. The current “no `from_schema` used” check is not S20 evidence. Keep ordinary handwritten tools typed.
- Recheck initial/default/nested state, atomic phone/status updates, stale state references after commit, presence of `False`/`0`, unique satisfier, one pending return, abandonment, guard changes, competing actions, and partial JSON. Preserve existing behavior where already covered.
- Resolve the documented global `invalid-argument-type` suppression: measure current diagnostics, type the shared node/call boundaries, and narrowly document any unavoidable SDK-specific ignores. A global disabled rule must not support an unqualified type-safety claim.

**Acceptance:** direct-core and Pipecat tests agree for normal, refused, interrupted, exhausted and disconnected turns. No extra fallback request, duplicate external write, stale transition, resurrected abandoned request, duplicated user message, leaked reset history, or unpaired tool protocol. Invalid schema/effective arguments leave handler invocation count at zero. Capture actual request messages and schemas for S14/S20/S21.

### 6. Repair trace causality and measurement

**Touch:** `voice/{harness,core,processor}.py`, `utils/{observability,arm}.py`, `bot.py`, `bench/{turns,scenarios}.py`, `tests/test_{traces,bench,core}.py`. Also locate and inspect the Phase 01-linked `/Users/nicoferdi/.codex/worktrees/a809/unmute_cli/scripts/read_langfuse_trace.py`; apply any required reader fixes as a separate repository patch using the same fixture expectations. If that worktree is unavailable, record the dependent reader check as blocked. No compiler generation changes are needed.

- Extend the existing speech ledger with stable segment, runtime-turn, attempt and origin identifiers: entry, tool filler, confirmation, model, goodbye, and error. Record submission immediately before queue insertion, linked to the originating tool call/observation where available.
- Carry segment correlation to the existing TTS audio context and record synthesis start, first generated audio, and first transport output separately. Client playout remains unknown unless measured. Allow a cancelled segment to have no synthesis and an interrupted segment to have partial output; record those terminal states explicitly.
- Attribute TTFB by emitting service and request/audio context. Processor/model metrics cannot populate TTS metrics. Use supported Pipecat hooks or a small local integration wrapper; inspect the pinned implementation before choosing the seam. Do not edit installed `.venv` files as the fix.
- Keep actual TTS timestamps and contexts. Do not backdate spans, merge independent contexts, or block a tool waiting for filler playback. Keep origins readable as metadata even if OTel links are used.
- Count canonical provider requests by observation identity and ancestry, excluding STT/TTS and wrappers. Group attempts under the runtime caller turn, including cancellations and zero-model turns. Keep greetings separate per call and missing parentage unresolved.
- Normalize usage from canonical input/output counts without double-counting cached-token aliases. Preserve exact streamed text when joining chunks. Propagate session identity and finalize a root transcript/outcome summary from the delivered ledger and established business results.
- Capture immutable process configuration at startup and fixture/call configuration at call start. Record distinct `process_started_at`, `call_started_at`, `ended_at`, and `captured_at`, plus source/lock/prompt hashes, model settings, route/voice/transport, endpointing, budgets, clock and fixture identity. Record unavailable fields honestly.
- Persist per-request, per-attempt, per-turn, speech-segment and per-operation rows as events finish, including failures. Use ordinary median and explicitly labeled nearest-rank tail quantiles consistently; never fabricate an audio-gap estimate from unrelated percentiles.

**Acceptance:** a synthetic multi-turn fixture covers zero-model/greeting turns, multiple attempts, two requests, legitimate repeated tools, a genuine retry, cancellation and unresolved parents. Historical N1–N4 yield 30 caller turns and 53 canonical requests; N2/N4 are 7/9 turns, not 8/11 attempts. The report's separate historical LiveKit export yields 8 user turns/16 requests, not 11/52; do not confuse it with the repository's different 5-turn/10-request native Pipecat fixture. N4's cited usage resolves to 3,907 input + 19 output = 3,926 total, with 3,480 cached input treated as a subset.

For a deliberately slow tool, submission precedes the tool body and audio overlaps work. For a millisecond tool, synthesis may begin after completion. Cover every speech origin, streamed chunk ordering and interruption. No TTS metric comes from the model processor; timing definitions are consistent with measured timestamps.

### 7. Prove behavior live, then isolate responsiveness

**Touch:** existing `bench/scenarios.py`, `bench/preamble.py`, benchmark evidence under `bench/raw/` and `bench/baseline/after/`.

1. Run the focused acceptance scenarios below against the real selected provider and Pipecat audio path. Use at least three repetitions per failure scenario as a smoke gate, including fresh fixtures and the N1 → N3 → N4 continuation sequence. Any consent, false-completion, wrong-write, or unintended-disconnect failure reopens its implementation item. Repetition is evidence, not proof of universal model behavior.
2. Freeze the native Pipecat arm's actual source/build, prompts, schemas, lock and configuration. Align phone scope, consent policy, availability fixture, caller script/audio, clock, model/region/tier/thinking, STT/TTS/voice, transport and endpointing. An unidentified native build or mismatched policy blocks parity claims.
3. Compare native Pipecat with this core on the same model/settings using interleaved matched scenarios. Separately compare 3.1 versus 3.5 on this core; never attribute a model upgrade to the architecture. Isolate endpointing changes with the same recorded caller audio, including fragmented speech, and score premature cutoffs.
4. Before collecting data, record scenarios, sample count and decision rule. Start with 20 paired runs per comparable scenario; treat wide uncertainty as inconclusive and predeclare any larger follow-up. Use call-level pairing/resampling, retaining all failures and reporting their rate separately from successful-call latency.
5. Report caller-gap and first-useful-response p50/p90, request TTFT, requests per runtime caller turn, task completion and correctness, repair counts, and endpointing errors. Separate filler from useful speech and server-output proxy from client playout. The 1.154-second historical median is context, not an acceptance target.

**Acceptance:** all focused live checks pass with trace/audio/outcome evidence. Claim equal-or-lower latency only if paired results support it without worse correctness or premature endpointing. Use a predeclared zero-regression margin for the p50/p90 latency difference; if uncertainty crosses it, report parity as inconclusive. A passing offline suite alone cannot close this gate.

## Required acceptance scenarios

| Case | Observable pass condition |
|---|---|
| N1: Friday booking → “move it to Saturday, same time” | No hang-up; same service/time and selected booking retained; exactly the authorized move or an honest availability clarification |
| N2: Friday → verification → availability → booking | Caller wording survives; first write is September 25 under the frozen clock; “this Friday” and “next Friday” resolve distinctly |
| N3/N4: existing appointment → select another slot | No additional write from a flipped flag, changed slot or unrelated next utterance; explicit additional intent succeeds |
| N4: full +34 number → country question → yes | No phone confirmation; exact readback plus agreement is required; correction revokes stale evidence |
| Existing appointment → complaint → use it as redo | One complaint/reference; booking retained; truthful pending result; no invented free booking, cancellation or restrictive policy |
| Afternoon / same time / unavailable time | Preference reaches the service before the cap; requested eligible slot appears; alternatives never silently replace the request |
| Failed/malformed/uncertain write | No success speech without matching receipt; recovery uses original operation; unknown outcome blocks another write |
| Abandon verification; later complete unrelated verification | Old pending booking stays abandoned; neutral owner return |
| Before/during/after tool; during fixed/model speech; between turns; disconnect | Bounded cleanup, terminal status, no stale mutation; committed effects and delivered prefixes preserved |
| Explicit goodbye / goodbye plus another request | Genuine ending drains then disconnects; mixed intent is clarified without accidental termination |
| Pinned clock, fresh fixture and continuation fixture | Reset only at setup; call B cannot erase A or inherit A's private conversation/authority |
| Core versus Pipecat; reset/return; invalid schema; partial JSON | Matching application outcomes, correct request snapshots, zero handler calls on invalid input, no premature action |
| Fast/slow tool speech and trace fixtures | Accurate causal origin, queue/synthesis/audio timing, request counts and caller-turn denominator |

## Completion ledger

Do not mark the POC complete until every required row has a retained result. Later-phase gates remain explicitly open.

| Requirement | Work packages | Evidence needed |
|---|---|---|
| Phase 01 §1: baseline and SDK capability ledger | 1, 5 | Frozen snapshot/digests, actual offline results, invocation-path probes |
| §2 / V5: state, effective inputs, service facts | 2, 5 | Atomic/nested/provenance checks and S20/S21 handler-counter/request snapshots |
| §3: trustworthy diary oracle | 3, 5 | Eligibility/overlap/identity/idempotency and fresh/continuation checks |
| §4 / V6: phone capture | 2, 7 | Structural and conversation-level checks, including audio fragmentation |
| §5 / strengthened V4/V8: legal flow | 2, 3, 5 | Execution refusals, per-request schemas, partial-stream/conflict/abandonment checks |
| §6 / V12: bounded turns and uncertain effects | 5 | Exhaustion, disconnect, resistant cancellation, reconciliation evidence |
| §7: trustworthy measurement | 6 | Synthetic exact-count fixture and historical replay results |
| §8 / C0 and strengthened V13 | 5 | Runtime-neutral core, adapter parity, history/projection and delivered-speech snapshots |
| N1–N4 | 2, 3, 7 | No unintended end, changed date, extra appointment or false phone agreement |
| Trace F1–F4 | 2–5, 7 | Receipt-bound speech, no redo loop/restriction, preference-aware availability, caller-authorized writes |
| Trace F5–F8 and speech causality | 5, 6 | Complete attempt accounting, correct native reader, TTS attribution, normalized usage/session/transcript |
| Trace F9 and responsiveness claim | 7 | Matched audio/configuration results with failure rates and uncertainty |

For each implementation package, run the targeted offline checks; before closure run and retain the actual output of:

```sh
uv run ruff check .
uv run ty check .
uv run python -m tests
```

Use the existing runner, which disables model requests before scenario imports. Store check/scenario ID, status (`passed`, `failed`, `blocked`, `inconclusive`, `not-run`), expected/actual behavior, command, revision/digests, configuration, sample count, trace/artifact references and limitations. Keep credentials and unnecessary real contact data out of evidence.

**Done means:** Phase 01 local gates and strengthened cases pass; all supplied correctness failures have passing emitted-speech and business-operation regressions plus focused live evidence; traces reconcile attempts, requests, speech and writes; and the matched latency decision is recorded. If the comparison is blocked or inconclusive, report “Phase 01 correctness complete; responsiveness parity unproven,” not full POC completion. Full V1 portability, generated-provider and carrier claims still require their later phases.
