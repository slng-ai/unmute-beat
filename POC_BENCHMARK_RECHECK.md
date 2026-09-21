# POC benchmark recheck — 21 September 2026

**Verdict: substantial fixes landed, but not everything in the original review is closed.** The selected native trace can now be measured correctly, and many important validation/routing regressions are covered. Remaining reproducible defects prevent calling the write boundary or evaluation harness complete. Equal-or-lower latency is still unproven.

## Scope and evidence

Reviewed local `main` at **`465f1f805dcc09bc285951c5ff5e0d177fc317f7`**, against the previous [review](./POC_BENCHMARK_REVIEW.md) of `28516da`. No application code was changed for this recheck; this Markdown is the deliverable. Planning documents were used as requirements/evidence, not instructions to implement their contents.

Two files acquired working-tree edits during review: `bench/scenarios.py` adds a fifth complaint utterance; `bench/turns.py` handles legacy scalar `request_ttft` values separately. Those changes are **not part of the reviewed commit**. Checks below ran against the available working tree, including those edits. Findings below concern behavior shared by the commit and that working tree; neither edit resolves them. This is a local-main review, not verification of a fetched remote revision.

Validation output:

- `.venv/bin/ruff check .`: `All checks passed!`
- `.venv/bin/ty check .`: `All checks passed!`
- `.venv/bin/python -B -m tests`: `all 10 modules ok`.

Additional probes exercised actual service wrappers, the Pipecat processor with a scripted model, and the trace reader. They ran offline with Pydantic AI model requests disabled. No live calls, provider benchmarks, or audio measurements were made. Trace evidence uses the checked-in fixture of reference `f1c4f999e516a7792dfdbed72fa8262d`.

## What is fixed

| Previous finding | Verified current behavior | Scope of closure |
|---|---|---|
| Empty phone verifies nobody | Separate `SubmittedPhone` validation; invocation regression passes | Fixed for submitted verification arguments |
| Lookup returns another identity | Returned phone must equal queried phone | Fixed |
| Refused complaint gets a success confirmation | `recorded=False` raises a retry before confirmation | Fixed for a negative response |
| Invalid calendar date / unknown commit key | Invalid dates and unknown update fields are rejected | Fixed |
| Closed/unknown day revives an old offer | Empty `booking_day` no longer disables the guard | Fixed for that sequence; see F2 |
| Unreturned slots become bookable | Only the returned twelve slots are registered | Fixed for one offer batch |
| Another caller can use an offer | Offer keys include phone | Fixed across different phone identities; no offer-generation isolation |
| Book → cancel → rebook returns cached old success | Token includes call identity and accepted-write count | Fixed for that sequence; see F1 |
| Unreadable save response escapes as an ordinary error | Wrapper reports an unknown outcome | Error classification fixed; unresolved-operation handling is not |
| Barge-in after tool success still routes | Handoff and borrowed-tool movement check interruption | Covered paths pass; not a complete side-effect cancellation contract |
| Interrupted fixed-only speech overwrites an earlier answer | `_spoke` guard prevents this case | Fixed for fixed-only turns; not segment-aware history |
| No way out of verification | `stop_verifying` clears pending work and returns to concierge | Fixed |
| Native trace reads as zero caller turns | Reference reads **5 turns, 10 model requests**, distribution **[2, 3, 1, 2, 2]** | Fixed for this complete reference; see F5 |
| TTFT units and turn duration are wrong | Seconds preserved; caller gap separate from whole-turn duration | Fixed for inspected fields |
| POC TTFT includes tool time | Request hook starts a separate clock for each model request | Scripted regression passes |
| Failure apologies score as ordinary completions | Rows include status/detail; failures reported separately | Improved; business scoring remains insufficient |
| Results disappear until all scenarios finish | Each result row is written and flushed immediately | Fixed |

The native reference now reproduces caller-gap **p50 1.198 s / p90 1.769 s**. This validates the reader against that trace, not a population latency target or candidate improvement.

The revision ledger at [bench/review-20260921.md](./bench/review-20260921.md) explicitly leaves several items open. Its narrower claim—specific reproduced cases were repaired—is supported. It should not be read as completion of the whole previous report or Phase 01.

## Remaining findings

### F1 — High: unknown writes still permit another business write

Location: [save_booking](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/agents.py:565).

**Reproduced:** wrap the real diary service so it commits a booking and returns a malformed response. The wrapper raises `ModelRetry`; the diary holds one booking, but `state.writes` remains zero and no unresolved operation is retained. Then submit a different offered slot with `additional=True`: the wrapper accepts it. The diary now holds **two bookings**, while the call records only one accepted write.

`additional=True` deliberately models an erroneous subsequent model action; it does not establish caller consent. The application currently relies on “do not call this a second time” in retry text to prevent that action. Changing arguments changes the token, so the cache does not prevent it. The malformed-response path also skips `outcome_of(token)` even when the service has recorded a valid outcome.

**Required closure:** reconcile malformed responses using the operation identity where possible. If still unknown, retain that operation and enforce a guard against conflicting writes until resolved. Test committed-but-malformed response → attempted reformulation, asserting no second service write. Complaint writes still lack equivalent idempotency/reconciliation.

### F2 — High: corrections invalidate a day, not the current offer

Locations: [read_diary](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/agents.py:330), [offer storage](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/diary.py:374), [save guard](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/agents.py:507).

**Reproduced through the tools:** read Friday haircut slots → read Friday hair-color slots → submit the original haircut slot. The booking succeeds as **haircut**, despite the newer search having replaced the requested service.

`booking_day` has not changed, and the offer dictionary retains both batches. Phone scoping also does not isolate two calls using the same phone. The correction requirement was to bind a choice to the current accepted offer, not merely a matching date.

**Required closure:** retain current offer membership/version with its relevant identity and invalidate it on a replacing search/correction. A stale slot from an earlier service search must be rejected even on the same date. Preserve confirmed bookings separately from proposed offers.

### F3 — High: a schema-valid booking result can contradict the requested action

Locations: [Saved](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/agents.py:103), [result commit](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/agents.py:586).

**Reproduced:** for an `action='book'` request, substitute a well-formed `status='cancelled'` response with booking ID `BK9999`. The wrapper accepts it, increments `writes`, stores an appointment with `action='book'`, and returns a cancellation result. The substitute service made no booking. With a speech callback attached, the selected confirmation is also the cancellation wording.

Pydantic validates allowed field values; there is no request/result consistency check. The customer-lookup fix applies this principle correctly, but booking completion still does not.

**Required closure:** verify action/status correspondence and the result's identity and booking details against the operation before committing or confirming. Add mismatched-but-valid response cases alongside malformed-response tests.

### F4 — High: the scenario oracle can pass the wrong outcome

Locations: [scenario assertions](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/bench/scenarios.py:128), [summary population](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/bench/scenarios.py:390).

**Reproduced:** `book` asks for Monday at 09:00. Create one haircut booking for Friday at 10:30; `CHECKS['book']()` returns the passing empty string. It checks only one booking and service. Other booking checks add at most a weekday, not the exact expected date/time. No assertion ties the write to a readback and subsequent consent.

The fixed `book` script also moves directly from selecting a time to “no that's all, thanks,” leaving no explicit affirmative answer if the agent correctly asks for booking confirmation. The additional complaint utterance in the working tree does not repair this booking script.

Separately, `NOT_COMPARABLE` labels manager/knowledge scenarios, but the aggregate `good` population still includes their answered turns. It also includes answered turns belonging to scenarios that failed their business assertions. Failures are visibly counted, which is good, but the pooled timing/request headline is not the matched successful-completion comparison requested.

**Required closure:** freeze a scenario clock and explicit expected result: identity, service, exact date/time, number of writes, consent sequence, and completion/hangup where applicable. Use scripts that can complete that contract. Partition arm-comparable scenarios before aggregation; report success rates over all attempts and latency conditional on clearly specified outcomes. Keep incomparable explorations separate.

### F5 — High: native turn counting still depends on successful latency instrumentation

Locations: [caller_turns](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/bench/turns.py:133), [`--all`](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/bench/turns.py:577).

Two offline probes reproduce distinct errors:

1. Remove only `turn.user_bot_latency_seconds` from the first genuine caller turn in the native fixture. **5 turns / 10 requests becomes 4 turns / 8 assigned requests**, with two requests unresolved and that caller turn classified as a greeting. A missing measurement does not prove a greeting. Silent, interrupted or incompletely instrumented turns must remain in the caller-turn denominator.
2. Combine the native fixture with the POC fixture, as a mixed-window `--all` query can do. The reader selects `poc` globally and returns only the POC distribution **[2, 1, 0]**, losing all five native turns; unresolved requests rise to **11** (ten native plus the fixture's existing orphan).

**Required closure:** identify greetings positively and preserve caller turns with missing latency as missing measurements. Determine arm/turn hierarchy per trace, then partition comparisons by arm/configuration. Reject unsupported mixtures explicitly rather than silently selecting one arm.

### F6 — Medium: the saved transcript omits ordinary model speech

Locations: [Counter](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/bench/scenarios.py:188), [`said_back`](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/bench/scenarios.py:288), [Spoken](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/voice/harness.py:89).

**Reproduced through the processor with a scripted model:** model says “We close at six.” Counter reports `spoke=True`, but `said_back=[]`. Only fixed speech is appended to `call.spoken`; `Counter.push_frame` discards model text after setting a boolean/time. The declared `Spoken.kind='model'` is never populated.

This prevents reviewing what the model offered, whether it asked for consent, or whether the response was useful. Core-generated error/exhaustion fixed lines also bypass the call's speech ledger. Separately, `first_speech`/`turn_first_speech` clocks text submission, not audio heard by the caller; the descriptions overstate that boundary.

**Required closure:** retain ordered model and fixed output segments, including fallbacks. Label text-emission timing accurately. Measure runtime audio start/client playout separately for the latency decision. A sent-text ledger alone cannot prove heard speech.

## Requirements still incomplete rather than repaired by this commit

| Requirement | Current status |
|---|---|
| Typed state, submitted arguments, templates and basic flow guards | Substantially implemented and useful; tests support these boundaries |
| Typed business completion | Partial: F1–F3 still violate the application contract |
| Portable typed conversation/history | Shared full history and repeated-prompt workaround remain; task `messages`/`reset`/owner restoration not implemented as the proposed contract |
| Delivered history and cancellation | Fixed-only overwrite and post-tool routing cases improved. `note_spoken` still edits the newest text response without segment identity; mixed speech, late callbacks, between-turn interruption and disconnect need their own evidence |
| Static typing guarantees | `ty` passes with global `invalid-argument-type` suppression. The ledger reports 31 suppressed diagnostics; green does not mean this category is checked |
| Semantic repair metric | Still treats warnings/errors/nonempty status text as refusal evidence; it does not establish attempted and successful repair as separate events |
| Matched native/candidate fixture | Still absent: US reference identity versus Spain-only validator; three native offered times versus the current capped grid; differing knowledge/manager behavior |
| Reproducible paired benchmark | More metadata exists, but live clock/fixed scenario order remain. No frozen native source/configuration, paired runner/result set or complete environment/prompt/schema identity establishes equivalence |
| Real-provider behavior | Scripted checks cannot establish the prompt changes or whether `speak_by` produces timely useful prose with the selected provider |
| Equal-or-lower audio latency | No matched paired audio result provided or generated in this recheck |
| Broader POC gate | A second-runtime/LiveKit proof and provider schema matrix remain separate unproven items. Compiler/migration work is later scope, not something this review asks to build now |

This remains aligned with the research's rationale: enforce a shared application contract and reduce duplicated orchestration. It does **not** establish that Pydantic itself makes the call faster, nor that native Pipecat lacks typed state or deterministic routing.

## What closes this review

1. Close F1–F3 at the real service wrapper boundary, retaining a regression for each reproduced sequence.
2. Close F4–F6 so the benchmark cannot reward a wrong booking, missing turns, or missing transcript evidence. Keep failure denominators explicit.
3. Freeze the native revision and a matched reference fixture. Keep Spain-only policy if desired, but normalize the identity in **both** arms and label it a normalized replay. Match slot availability, confirmation rules, model settings and audio configuration as well.
4. Run the original report's paired/interleaved text protocol, then paired audio calls with correctness gates. Report first acknowledgment separately from first useful response, and use the same audio-gap boundary in both arms.

**Decision today:** continue hardening this implementation; do not declare Phase 01 complete or latency parity achieved. The selected happy-path trace reader is repaired. The correctness boundary and comparison instrument still need the specific closures above.
