# PR #20 review: Stage A needs a focused follow-up

Reviewed 26 September 2026 against PR #20 head `8737717ff93a234c71b8b89b09413e763e3b8cc6`, base `9df9928ac16d312c7c848032e60b650e4820ea95`. The tested implementation commit `5166590` and final head differ only in the playbook. PR #20 is open and correctly stacked on `feat/session-4-node-agents-cc`.

**Verdict: request changes before using this evaluator as the Stage B release gate.** Receipt replay and the explicit unknown verdict are useful improvements, and the corrected inventory reproduces. Additional offline probes still produce release exit 0 for missing or contradictory evidence. Fix these evaluator paths in PR #20, without spending the voice allowance or expanding into runtime repairs.

This review changed no PR #20 code, issued no model requests, placed no calls and triggered no paid rescoring. It used the committed fixtures, the locally retained private export, local bot logs, installed library source and offline commands. It did not listen to audio or independently redownload recordings.

## Verified results

| Check | Independent result |
|---|---|
| Changed runtime surface | No diff in core, salon-graph, salon-pipecat-flows or `uv.lock` between base and head. |
| Concierge-tools tests | All five assert-script modules pass. |
| Concierge-tools Ruff and formatting | Both pass; 17 files already formatted. |
| Concierge-tools ty | Passes. |
| Repository formatting | 211 files already formatted; `git diff --check` passes. |
| Repository Ruff | One I001 failure in **unchanged** `docs/research/reusable-core/session-3/package-scenario.py:23`. This is not a new Stage A defect, but the unconditional root-Ruff-green claim did not reproduce in this environment. |
| Full saved rescore | Recomputed all **138 unique calls**, in memory, from the retained 80 MB export and committed evidence. After normalizing JSON dictionary keys, every committed report matches exactly. |
| Cohorts | Harness 69 normal + 51 interruptive; native 10 normal + 8 interruptive. |
| Reconstructed effects | 123 calls with one active booking, 15 with none, zero with two or more. This is receipt reconstruction, not surviving backend snapshots. |
| Current evaluator verdicts | 118 unknown, 20 fail, zero pass. These reproduce; the false-pass findings below concern additional inputs the gate should handle. |
| Review provenance | Nine call reviews, seven reviewed final requests, all labeled `agent`; no human review claimed. |
| Recording durations | 137 measurements agree with the saved recording-measurement file. `iPFgi5qq` is 49.742 seconds in that file. The audio itself was deleted after the prior measurement, so these durations were not independently remeasured here. |
| Long native stall | Local log shows a model request at 01:44:16 and completion timeout/idle timeout at 02:01:56, supporting the ~17.7-minute stall finding. |
| Ceiling lifecycle | Code cancels the timer on `on_client_disconnected`, but does not place timer cancellation in unconditional cleanup after `runner.run()`. The recorded shared-worker cleanup concern is valid and remains Stage B/C work. |

The old rules being restored one at a time is useful reported mutation evidence. The original five suites were rerun here; the ten-mutation experiment was not rerun. No account-wide claim about historical paid activity was independently established; the scope, unchanged runtime and retained run inventory are consistent with the reported zero-call Stage A.

## Standards review

These findings concern the documented boundary and reporting requirements, independently of the functional review below.

1. **[P2] Evidence models accept non-boolean and non-numeric JSON through coercion.** `load_report.py:99` sets `extra='forbid'` but not strict validation. The independent standards check reproduced `accepted: "yes"`, `delivered: 1`, and `bookings: "1"` becoming `true`, `true`, and `1`. The stability plan requires strict validation at external evidence boundaries. Reject malformed evidence rather than converting it into consent/delivery support; retain JSON date/time parsing through the appropriate Pydantic boundary.

2. **[P2] README still publishes superseded claims.** `README.md:874–887` retains 140 tested calls, native interruptive success 1/8, two wrong bookings, and an unqualified statement that the safety faults are fixed. The linked historical report has an erratum, but the README's adjacent table appears current beneath the new evaluator description. Mark that block superseded and link the corrected evidence; distinguish the specific tested runtime patch from a general guarantee.

## Spec review

These are separate omissions in the requirement to check final identity and exact write/receipt binding.

1. **[P1] Missing caller identity passes the release gate.** `load_report.py:318–322` only checks identity if the optional `Final.phone` was supplied. With `Final.phone` absent and write `args={}`, matching service/date/time produces all five passes, `missing=[]`, and exit 0. Require the expected and observed identity when the evaluated operation depends on identity; otherwise report unknown. This violates “Check final service/date/time/identity” and “missing evidence ... unknown/incomplete, never a pass.”

2. **[P1] Receipt binding checks only booking ID.** `load_report.py:234–240` and `268–269` prefer receipt phone/date/time without comparing available submitted arguments. A receipt for caller X can pass against write arguments for Y; a Friday 09:00 receipt can pass against a Saturday 10:00 slot argument, provided the final request matches the receipt. Both produced exit 0, including an invocation of the actual CLI. Validate all available identity, service and slot bindings and distinguish a proven mismatch from missing binding evidence. This violates the requested “proposal/argument/identity binding” check.

## Additional functional and evidence findings

### [P1] An unaudited operation disappears when another write has an audit

`ledger.py:338–339` removes every unaudited tool operation if **any** audited write exists. This is inherited extraction behavior, but the new gate relies on its output as though all relevant writes were covered (`load_report.py:537–547`). A passing lookup/booking fixture followed by a distinct unaudited `cancel_appointment` tool observation still reports only the two audited writes, an active booking and release exit 0.

Deduplicate a tool observation only against its corresponding audit, using observation ancestry or a stable operation identifier. Preserve unmatched consequential tool/write observations as incomplete evidence. Do not report “no write needed a yes” merely because extraction discarded the write. Add the mixed-audit fixture to the CLI regression suite.

### [P1] A bare acknowledgement overrides contradictory delivery evidence

`load_report.py:368–375` returns a deterministic delivery pass for `delivery='acknowledged'` before reading the reviewed delivery verdict. No segment IDs, proposal binding, acknowledgement ordering or clear status are required. A fully passing fixture with reviewed `delivered=False` still exits 0 if that string is present.

This is especially important because Stage A's report prescribes this field as the future runtime contract. Define the minimum correlated acknowledgement evidence Stage B must emit. Missing correlation is unknown; cleared, premature, wrong-proposal or contradictory evidence cannot pass. At minimum, never silently override an explicit reviewed failure. Until the stronger contract exists, avoid treating the bare string as verified delivery.

### [P1] Completion scoring cannot express a wrong date or wrong booking claim

`CallReview.claims` at `load_report.py:131` contains only action labels; `completion` at lines 392–401 asks whether that action ever occurred and, for book/move, whether any appointment remains. It cannot represent which appointment, identity, date or time was claimed. An actual Saturday appointment and a closing statement falsely saying Friday are both reduced to `booked`, and the entire call can pass.

Record the minimum claim details needed to bind consequential speech to the effective receipt, or keep completion unknown when only an action label is available. Check claims against the relevant booking and effective state, including later cancellation/move. Preserve neutral speech and cases with no consequential claim. The fix need not introduce a language classifier; it needs an evidence schema that can represent the reviewer's actual finding.

### [P2] A cached booking receipt can resurrect a cancelled appointment

`load_report.py:242–256` deduplicates a booking receipt only while that booking is active. Replaying `book BK0001 → cancel BK0001 → cached original book receipt BK0001` creates an active booking again and can return exit 0. A repeated cancellation instead becomes unknown because the active record is gone.

Track applied operation/receipt identity across the whole sequence, not just the active dictionary. If the legacy record cannot distinguish a replay from a new effect, report that ambiguity rather than assert a new appointment. Add book/cancel/replayed-book and repeated-cancel regressions; preserve the existing book/move behavior.

### [P2] The “measured ending” contract accepts unsupported causal labels

`Facts.source` at `load_report.py:145` is optional, and `operational` at lines 419–435 accepts caller/agent as a deterministic pass without checking provenance. Removing the source from an otherwise passing fixture leaves exit 0.

The historical fact extraction also needs qualification. The retained scratch `facts.py` searches for **“Failed to terminate Twilio call”** and interprets the timestamp as agent or ceiling termination. All **118** committed `ended_by: agent` entries correspond to that message with **HTTP 401**. Installed Pipecat emits it on a failed REST request during either `EndFrame` or `CancelFrame`; the message proves an attempt, not successful termination or who originally ended the call. Recording length agreement does not by itself establish that cause.

Require a nonempty source for supplied facts and preserve the distinction between observed events and inferred cause. Recheck the 118 labels against per-call ending events, or mark the cause unknown/inferred. Do not claim that all 118 labels are necessarily wrong; the evidence currently used is insufficient to certify them as measured facts. Keep the separate, well-supported long provider-stall diagnosis.

## Credential exposure

The Coval help leak is reproducible: both `coval simulations get --help` and its `--agent` form echo a configured API key. This review tested with a **dummy value only**, capturing output and reporting just whether the dummy appeared. It did not print the real key.

An exact-match scan of every Stage A commit patch found no occurrence of the currently configured Coval key. This does not inspect all historical credentials or prove the earlier task output contained no secret. Rotate the key reported exposed in that task, and avoid running help with real credentials in the environment. No credential rotation was performed during this review.

## Next action

Amend PR #20 with the evaluator corrections and offline counterexamples above. Preserve the demonstrated 138-call rescore; correct or qualify the ending attribution and README assertions. Update the Stage B audit contract to carry stable operation/proposal identity, exact binding, correlated delivery evidence and explicit termination cause. Keep unprovable historical facts unknown.

Rerun all five concierge-tools suites, its Ruff/ty checks, the actual CLI false-pass cases and the saved rescore. These changes require no new Coval calls or model requests. The 82-call allowance remains available. Then resume Stage B's runtime repairs against the strengthened evaluator.

**Finding counts:** standards 2; spec 2; additional functional/evidence findings 5. The most consequential issue is that contradictory or incomplete evidence can still produce release exit 0.

## Local reproduction artifacts

This review's standalone offline probes are saved at `/tmp/slng-pr20-review-probes.py`; the all-call reproduction script is `/tmp/slng-pr20-inventory-check.py`. They reference the reviewed implementation worktree explicitly and write only temporary data. Run them with that worktree's `.venv/bin/python` and `PYTHONDONTWRITEBYTECODE=1`. The first script asserts the observed defects, so those assertions should cease to pass after repair; convert each into a regression that asserts the corrected behavior.

The private export and scratch evidence remain outside Git. Their paths are intentionally not promoted to the package's permanent interface; a portable handoff should retain sanitized minimal fixtures and provenance, not raw customer conversations or secrets.

## Copy-paste prompt — PR #20 follow-up

Paste the entire block below into the coding session working on PR #20. This is a complete Stage A repair prompt; it does not start Stage B.

```text
Amend PR #20 to close the Stage A evaluator's remaining false passes before we use it to judge runtime repairs.

Repository: slng-ai/slng-voice-harness
PR: https://github.com/slng-ai/slng-voice-harness/pull/20
Expected branch: feat/session-4b-evaluator
Reviewed head: 8737717ff93a234c71b8b89b09413e763e3b8cc6
Reviewed base: feat/session-4-node-agents-cc at 9df9928ac16d312c7c848032e60b650e4820ea95 (PR #19).

First verify the current branch, working tree and latest PR state. Preserve any newer work. Continue the existing PR #20 branch; do not reset to the reviewed revision, merge either PR, or begin the next phase. The review was performed against 8737717; its implementation is the same as tested commit 5166590, with only a subsequent playbook edit.

Read AGENTS.md and CLAUDE.md, then:
- docs/research/reusable-core/session-4b-evaluator.md
- docs/research/reusable-core/session-4b-evaluator-review.md
- docs/plans/reusable-voice-core-stability.md
- the relevant Stage A handoff in docs/plans/reusable-voice-core-sessions.md.

The review and stability plan may exist only in the planning worktree:
/Users/nicoferdi/.codex/worktrees/ce27/slng_voice_harness/
Read them there if missing locally. Copy the relevant documents into this branch if needed, reconciling existing versions rather than overwriting them. A git pull will not retrieve uncommitted planning files. The requirements below are self-contained if those local files are unavailable. This prompt narrows the broader stability plan to Stage A only.

Scope and architecture

Fix evaluation code, evidence schemas, offline regressions and documentation. Keep core, application prompts, native Pipecat behavior, transport and hosting unchanged. The intended runtime remains agents per node with typed shared state and deterministic authorization guards, using Pydantic AI/Graph and Pipecat. This task must neither reintroduce a classifier nor add LangChain or another orchestration layer.

Use existing Pydantic models and assert-script test patterns. Read the pydantic and python-dev skills; use langfuse and relevant Coval audit/compare skills if retrieving existing evidence. Trace each affected helper's callers and fix the shared cause. No new dependencies or test framework. Keep the five judgments separate: outcome, authorization, delivery, completion and operational ending. Missing evidence stays unknown; contradictory evidence must not yield pass. Preserve exit codes: 0 only when all required judgments pass, 1 on any failure, 3 when incomplete without a failure, 2 for invalid inputs.

Fix all nine review findings, with a regression for each

1. Strict external evidence. In load_report.py, extra='forbid' currently does not prevent coercion. Reject accepted='yes', delivered=1 and bookings='1' instead of converting them into valid evidence. Valid JSON booleans, integers and documented date/time strings must still work. Verify malformed evidence through the real CLI returns 2.

2. Required identity. An identity-dependent booking can currently pass when the expected final request omits phone and the write/receipt establish no identity. Require enough expected and observed identity evidence to bind that booking to the customer; absence is unknown. Do not impose phone identity on unrelated operation types. Keep a fully evidenced control case passing.

3. Exact receipt binding. Comparing booking_id alone is insufficient. A receipt for customer X with write arguments for customer Y, or a Friday receipt with Saturday write arguments, must never pass. Compare available identity, service, slot and proposal/operation identity across the authorization, execution and receipt. Missing necessary binding is unknown; an established contradiction is fail. Use existing schemas and normalized values rather than ad hoc text matching.

4. Preserve unmatched consequential operations. ledger.py currently discards every unaudited write whenever any audited write exists. A separate unaudited cancel_appointment can disappear from a passing lookup/book call. Deduplicate corresponding TOOL and audit observations using available correlation/ancestry or stable operation identity. Preserve unmatched consequential operations and mark missing audit evidence unknown. Test both a legitimate duplicate pair and a distinct unaudited cancellation; do not simply count both copies of every audited tool.

5. Delivery requires correlated evidence. A bare audit.delivery='acknowledged' currently overrides reviewed delivered=false. It must not. Define the minimum evidence contract needed to bind playback to the relevant proposal/generation/segment, establish completion before the answer, and distinguish playback acknowledgements from clear/cancel acknowledgements. Stale, duplicate, late or unrelated acknowledgements cannot certify delivery. An unsupported flag is unknown; contradictory or explicit negative evidence cannot pass. Test positive, missing, interrupted/cleared and contradictory cases with synthetic evidence. Document the runtime audit fields Stage B must emit; do not implement Twilio marks in this task. Unknown consent requirements must not silently become 'delivery unnecessary'.

6. Completion claims need exact content. claims=['booked'] cannot distinguish a correct Saturday booking from the assistant falsely announcing Friday. Extend reviewed completion evidence minimally to identify the consequential claim and its relevant booking/customer/service/slot, then compare it with the effective receipt-backed state. Cover wrong date/time, wrong booking and a valid claim. Support a neutral ending without a consequential claim. Legacy action-only labels stay unknown wherever the exact claim cannot be established; do not add an LLM judge to fill the gaps.

7. Deduplicate effects across their lifetime. book BK0001 → cancel BK0001 → replay the original booking receipt must not resurrect the appointment. Repeating the same cancellation must not create a new effect or unexplained failure. Track applied operation/receipt identity beyond the active-booking dictionary. When legacy records cannot distinguish a replay from a new intent, retain uncertainty. Do not deduplicate distinct intents solely because their arguments match. Preserve the existing book-then-move behavior: one appointment, updated in order.

8. Ending provenance. Require meaningful provenance for supplied operational facts and distinguish observed events from inferred causes. A source-free ended_by='agent' or 'caller' cannot certify a deterministic pass. The historical extractor used 'Failed to terminate Twilio call' with HTTP 401 to label 118 calls as agent-ended. That proves a failed termination attempt, not who actually ended the call; Pipecat can attempt hangup during either EndFrame or CancelFrame. Recheck those labels using available per-call events, or qualify them as inferred/unknown. Do not claim all 118 labels are necessarily wrong. Preserve the separately supported long provider-stall diagnosis. Recording duration alone cannot establish termination cause.

9. Correct superseded README claims. Qualify the historical 140-tested count, native success/double-booking table and unqualified safety-fix assertions around README.md lines 874–887 in the reviewed revision. The corrected inventory is 140 authorized, 138 placed: 120 harness and 18 native. The two native alleged double bookings were book-then-move of BK0001; their premature authorization faults remain real. Preserve historical artifacts and link the corrected report. Do not publish a corrected success rate without adequate evidence.

Verification and evidence

The review probes are /tmp/slng-pr20-review-probes.py and /tmp/slng-pr20-inventory-check.py. They reference the original implementation worktree at /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.claude/worktrees/harness-change. Inspect before using and adapt paths to this checkout. The defect probes assert the old behavior; convert them into permanent sanitized regressions that assert the repaired behavior. If temporary artifacts have gone, recreate the small counterexamples above using existing test helpers.

Exercise the actual load_report CLI, not only helper functions: a fully supported pass returns 0, a contradiction returns 1, missing evidence returns 3, malformed input returns 2. Demonstrate that each regression detects its original defect, using a temporary reversible mutation or the old implementation, without disturbing unrelated work.

Run all five concierge-tools test modules through its existing python -m tests entry point with model requests disabled. Run Ruff check, Ruff format --check and member-scoped ty check, plus git diff --check. The independent review found one pre-existing root Ruff I001 in unchanged docs/research/reusable-core/session-3/package-scenario.py:23; verify and report it separately if still present. Do not call the whole repository clean when it is not.

Recompute the historical reports offline from retained exports when available. The previous review reproduced all 138 reports: 69 harness normal, 51 harness interruptive, 10 native normal and 8 native interruptive. These are inventory counts, not passing counts. Expect corrected judgments to change after these repairs and explain every category of change. Preserve raw evidence and provenance. If exports cannot be recovered, report that limitation and do not claim a full rescore based on fixtures alone. Existing Langfuse/Coval reads are allowed; new simulations, model requests and paid rescoring are not.

All nine existing reviewed records were agent-authored; only seven established a final request. No recording was listened to in Stage A or this independent review. Do not relabel these as human judgments or claim audible delivery verification. Keep private exports, recordings and secrets out of Git.

The Coval CLI's help can echo its configured API key. Never run help with real credentials; unset them or use a dummy and capture output. Do not repeat the exposed key. Rotation remains an account-owner follow-up, not an implicit action in this code task.

Deliverables and stop condition

Update the evaluator, regressions, corrected report and README in PR #20. Document the concrete Stage B audit contract: stable operation/proposal identities; authorization and receipt binding; delivery correlation and cancellation semantics; observed termination events versus inference. Record which historical judgments remain unknowable without new evidence.

Commit and push this focused follow-up to the existing PR #20, attach/link that PR, and provide the tested commit, exact checks, before/after counterexamples, corrected inventory/judgments and remaining limitations. Do not merge. Stop after Stage A is trustworthy enough to expose missing runtime evidence without inventing passes; do not begin runtime fixes automatically.

This task uses zero new Coval calls, zero model requests and zero paid rescoring. The next phase's allowance remains up to 82 calls: up to 20 targeted repair calls, 20 normal + 20 interruptive final harness calls, and 10 normal + 12 interruptive native top-ups if the existing 10 + 8 remain comparable. Runtime repairs, delivery integration, 50 simultaneous calls on one server and worker-failure testing remain subsequent stages.
```
