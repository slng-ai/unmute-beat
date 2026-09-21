# Second POC recheck — 21 September 2026

**Not fully closed yet.** The original reproductions are mostly repaired, and the implementation is stronger. However, six related cases still fail: complaint response loss, recovery validation, identity corrections, reference scoring, pooled turn counting, and speech ordering. These are remaining parts of the same contracts, not requests for a larger framework.

## Revision and validation

Reviewed local `main` at **`14fe6f612086e33c8d452ba79528ac09f47893c6`**, clean when the review and checks began, including the three commits after `465f1f8`. Compared against [the previous recheck](./POC_BENCHMARK_RECHECK.md) and [the closure ledger](./bench/review-20260921.md). No remote fetch, application changes, live provider requests or audio calls were made by this review. This Markdown is its only added file.

During finalization, concurrent uncommitted edits appeared in `bench/scenarios.py`, `bot.py` and new `utils/arm.py`. These extract configuration recording and add live-call outcome recording; they are outside this commit-level verdict and were not covered by the earlier check results. The reviewed oracle, offer, recovery, turn-counting and speech-ordering logic remains unchanged in that observed diff. Source line references refer to the reviewed revision.

Actual check output:

- `.venv/bin/ruff check .`: `All checks passed!`
- `.venv/bin/ty check .`: `All checks passed!`
- `.venv/bin/python -B -m tests`: `all 10 modules ok`.

Additional offline probes used the actual service wrappers, the processor with a scripted model, and the checked-in trace fixture. Pydantic AI model requests were disabled before importing the agency. Service fault injection below represents the boundary failures the previous reviews required; it does not claim the in-memory diary spontaneously returns those faults.

## What this round did right

| Previous finding | Implemented improvement | What now passes |
|---|---|---|
| F1 unknown writes | Unresolved booking token retained; booking and complaint entry points check it; malformed booking responses query the outcome cache | Unknown booking blocks another write; committed-but-garbled booking response recovers |
| F2 stale offers | Every search withdraws its previous offer batch; offers are scoped to a call | Same-day service correction and separate calls no longer share stale offers |
| F3 inconsistent completion | `_disagrees` checks action/status, existing booking ID and offered slot details | Initial mismatched booking response is refused before confirmation |
| F4 weak oracle | Frozen clock, exact expectations for ordinary booking scenarios, write counts/timing and successful-call filtering | Previous Monday/Friday wrong-booking case is rejected; manager/knowledge are excluded from the headline |
| F5 missing native turns | Missing latency alone no longer identifies a greeting; mixed native/POC analysis raises; CLI partitions by arm | Original missing-attribute and mixed-fixture reproductions pass |
| F6 missing transcript | Model speech and core fallback lines enter the ledger; timing is named text output rather than heard audio | Ordinary model answer and exhaustion speech are retained |

The native reference still reads as **five caller turns, ten requests, [2, 3, 1, 2, 2]**. Recording the confirmation-policy mismatch and distinguishing text output from audio are both correct. The new call-level bootstrap is useful descriptive analysis, but is not a paired native/candidate performance result.

## Remaining reproducible failures

### R1 — High: complaints have tokens but no unknown-outcome handling

Location: [record_complaint](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/agents.py:377). Related to F1.

**Probe:** the real complaint service writes its record and caches its outcome, then a wrapper raises `TimeoutError` to simulate a lost response. The tool propagates the exception without consulting the cache or setting `unresolved`. A later paraphrase of the same complaint derives another token and writes again.

Observed: first attempt leaves `writes=0`, `unresolved=''`; the paraphrased attempt leaves **two complaint records, one accepted write**.

The new `a_complaint_is_written_once` test manually decrements `writes` and repeats identical arguments. It checks cache reuse, but never exercises actual response loss, malformed responses or a changed retry.

**Closure:** give complaint outcomes the same enforced reconciliation/unknown boundary as booking outcomes. Keep the original operation identity across retries. A committed response loss must recover its existing record, or retain an unresolved operation that blocks another write. Test by injecting the failure at the service invocation, without manually repairing state.

### R2 — High: recovery bypasses the result/request consistency check

Location: [_settle](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/agents.py:171). Related to F1/F3.

**Probe:** a booking service returns and caches a well-formed `cancelled` result for a `book` request. The initial call correctly refuses it and retains an unresolved token. Call the booking tool again: `_settle` loads exactly that cached result, accepts it, stores an appointment with `action='cancel'`, clears the unresolved token and returns the cancellation confirmation.

Observed sequence: **initial contradiction refused → same contradiction accepted on recovery**.

Only a token is retained. The original action, identity, booking and proposed slot are unavailable to `_settle`, which validates the shape and derives the action from the returned status. It does not apply `_disagrees`. A malformed cached recovery also escapes as `ValidationError` rather than the controlled unknown-outcome response.

**Closure:** retain enough of the pending operation to validate its recovery against the original request. Use the same result checks on both initial and recovered responses. If the service reports a different actual action, record/report the discrepancy explicitly; do not silently treat it as ordinary completion of the requested operation.

### R3 — High: changing identity leaves the previous identity's offer usable

Locations: [offer creation](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/diary.py:392), [offer lookup](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/diary.py:509), [identity completion](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/salon/agents.py:535). Related to F2.

**Probe through the tools:** verify phone A; read Friday haircut offers; call `confirm_customer` for phone B on the same call; save a slot from A's search without another diary read. The booking succeeds under **phone B**.

Call scoping fixes cross-call leakage, but it replaced phone scoping: the offer's `phone` is now populated with the call ID. Neither the offer lookup nor the identity correction binds/invalidates that batch against the current customer identity.

**Closure:** bind current offers to both the call and the accepted identity, or invalidate them when that identity changes. Keep the already-correct search replacement behavior. Test identity correction separately from service/day correction.

### R4 — High: the designated reference can still pass with the wrong time

Location: [scenario expectations, headline filter and comparison output](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/bench/scenarios.py). Related to F4.

The newly added reference asks for **15:00**, but its expectation intentionally omits time. **Reproduced:** create a Friday haircut at **09:00**, with one write and a timeline identifying turn 3 as the write turn. `judge('reference', ...)` returns the passing empty string. This is a direct oracle test, not a claim that a live model chose 09:00.

The mismatch is documented but not excluded: `NOT_COMPARABLE` still contains only manager/knowledge. Reference and booking scenarios with differing confirmation policies can enter the population labelled comparable. The printed reference comparison explicitly says requests are comparable despite differing availability. Availability and consent policy affect tool work and requests as well as latency.

**Closure:** either match the reference's identity, availability and consent fixture in both arms and assert 15:00, or mark this exploratory replay non-comparable and give it an honest expected outcome. A different appointment is not a successful reference completion. Keep within-candidate regressions separate from architectural comparisons until policies match.

The ordinary booking oracle is materially improved. This remaining failure is specifically important because the user chose this conversation as the benchmark.

### R5 — High: pooling native calls includes all but the first greeting

Location: [greeting selection](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/bench/turns.py:201). Related to F5.

**Probe:** duplicate the native reference fixture with distinct observation, trace and session IDs, then analyze the two native calls together. This is the same population shape produced after `--all` groups observations by arm.

Expected: **10 caller turns, 20 requests, 2 greetings**, mean **2.0 requests/turn**.

Actual: **11 caller turns, 20 requests, 1 greeting**, mean **1.818 requests/turn**. One greeting becomes a zero-request caller turn.

`index == 0` applies to all Pipecat turns in the pooled window, rather than the first turn of each trace. Splitting by arm does not fix this. More calls cause progressively larger denominator distortion.

**Closure:** determine turns/greetings per trace and aggregate the normalized results, or use per-trace position inside normalization. Add the two-native-call fixture combination as a check. Continue retaining missing latency as a missing measurement rather than a missing caller turn.

### R6 — Medium: the speech ledger contains both kinds of speech, but loses ordering

Locations: [run-level accumulation](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/voice/core.py:409), [ledger append](/Users/nicoferdi/Documents/GitHub/slng_voice_harness/voice/core.py:449). Related to F6.

**Probe through the processor:** a scripted model emits text, calls a tool that speaks a fixed line, then emits more text in its next model request within the same run.

- Actual emitted frames: `Before. Fixed middle. After.`
- Saved ledger: `fixed: Fixed middle.` followed by `model: Before. After.`

Model text is accumulated for the whole run and appended only in `finally`; fixed speech is appended immediately. A first-token timestamp cannot restore the missing boundary between the two model segments. Sorting those two records would still put both model sentences on one side of the fixed line.

**Closure:** preserve model segments at actual speech boundaries, including tool/fixed-speech interleaving. Assert that reconstructed ledger text equals emitted frame text in order. Keep interruption reconciliation associated with the appropriate segment; `note_heard` still selects the last model segment without such identity.

## What still needs evidence, separately from these defects

The closure ledger itself correctly leaves these open:

- Frozen native implementation/configuration and matched paired runs.
- Reference identity, offered availability and confirmation-policy alignment.
- Audio latency and playout measurements; current text timing cannot establish these.
- Disconnect/between-turn interruption behavior and the broader conversation-history contract.
- The global `invalid-argument-type` suppression; a green type check does not cover that disabled category.

These do not all require more code in this repository. Some require the native reference environment, agreed fixtures and live experiments. The supplied plan's broader second-runtime/provider checks also remain distinct from this Pipecat comparison.

**Decision:** do not mark all F1–F6 closed yet. Retain the passing original regressions, add the six boundary cases above, and close them at their shared enforcement points. After that, proceed to matched text and audio evaluation; only those measurements can establish equal-or-lower latency.
