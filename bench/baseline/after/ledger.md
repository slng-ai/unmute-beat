# Evidence ledger: what closed, what did not, and what nobody has run

**Current assessment:** [POC report](../../poc-assessment-20260921.md). The table
below supersedes earlier status statements; the rest of this ledger preserves
historical evidence and intermediate failures.

| Gate | Current status | Retained evidence |
|---|---|---|
| Phase 01 §§1–8 local core contracts | `passed` offline | All 13 modules after the approval-phrase repair; [output](../../approval-evidence/offline.txt). Full requirement mapping in the current report. |
| Ruff / formatting / ty | `passed` | [Latest outputs](../../approval-evidence/README.md). |
| Verification and complaint typed speech | `passed` offline and focused live samples | Provider text suppressed; tracked phone question, KB guidance, caller-excerpt complaint and deterministic receipt. Negative controls in `native-evidence/` and `typed-complaint-evidence/`. |
| Real Gemini recovery / continuation | `passed` on measured build | 24/24 + 24/24 after typed verification, before the final phrase-only change; [evidence](../../native-evidence/README.md). |
| Native Pipecat comparison | implementation comparison complete; exact schema parity `not claimed` | Latest scope preserves both implementations. Frozen 200-call comparison: POC 100/100, native 82/100, using the documented native overlay. Tool contracts differ; [audit](../../native-evidence/TOOL_CONTRACT_AUDIT.md). [Adoption assessment](../../native-evidence/ADOPTION.md) separates gains from existing native capabilities. The later shared-tool experiment is excluded. |
| No-regression transcript latency | `failed` declared gate | Mixed intervals; booking slower on p50 and p90. No general speed/parity claim. |
| Completion-plan §7.3 separate model comparison | current matched comparison `not-run` | Historical same-source recovery: 3.1 23/24, 3.5 24/24, with differing declared service-tier settings and earlier prompts/source. These do not establish current 3.1 acceptance or isolate the model effect; [details](../../poc-assessment-20260921.md#historical-voice-latency-and-payloads). |
| Fresh voice complaint / booking | `partially passed` | Complaint recorded once; booking/move completed but “Go for it” was rejected twice. [Trace reconciliation](../../approval-evidence/README.md). |
| “Go for it” approval | `passed` offline + hybrid replay; audio retest `not-run` | Shared matcher fixed; direct authorization negative control and 3/3 Gemini completions, one request each. |
| C3 history and delivered projection | `passed` offline + hybrid replay | `test_history`, `test_core`, actual Pipecat queue cases, public SDK history boundaries; no client-playout claim. |
| Completion-plan §6 separate trace reader | numeric repair `passed`; historical LiveKit export still missing | Actual external reader patched and tested, with [before/after and 50-request usage reconciliation](../../reader-evidence/README.md). N1–N4 remain 30 turns/53 requests; the distinct native Pipecat fixture is 5/10. No 8/16 LiveKit verification is claimed. |
| Completion-plan §4 booking speech prevention | `partial` | Complaint/verification speech constrained; booking still has the clause/claim detector, not a universal prevention boundary. |
| Remaining microphone matrix / matched audio / client playout | `not-run` | Split/corrected/withheld phone and write interruption need current audio evidence; no native matched-audio run or client-playout measurement. |

## Historical ledger


One row per requirement in `POC_COMPLETION_PLAN.md`'s completion ledger.
`passed` means a retained result exists and is named here. `not-run` and
`blocked` are not failures and are not passes either; a plan that reads them
as passes is the thing this file exists to prevent.

**Latest assessment, 2026-09-21:** the user's final sequence is general questions
without identification, then verification before either booking or complaints.
The graph enforces that prerequisite and resumes the original request. Complaint
approval is still required before recording, after verification. This supersedes
the earlier readback-before-verification rule.

The final build passes all **13 offline modules**, Ruff, formatting and ty. Real
Gemini recovery is **23/24**; continuation is **6/12**. Phone fragments and the
initial complaint through verification pass 3/3 each. Upcoming-booking complaints
still miss readbacks and can make unsupported free-service claims. Fresh microphone
acceptance, client playout and matched native Pipecat comparison remain open.
See the [current flow evidence](../../verification-first-evidence/README.md),
[complaint-stall report](../../complaint-stall-20260921.md) and
[Phase 01 report](../../poc-status-20260921.md). Earlier results below are historical,
not the final build's acceptance result.

**Started from:** `e78ac56`, digests in `snapshot-e78ac56.json`, checks in
`checks-e78ac56.txt`. **Ends at** the commit that carries this file, on
`feat/poc-completion-plan-3340a5`; `snapshot-complete.json` holds its
digests and `checks-complete.txt` its output. The snapshot is taken from the
committed tree, so its own digest is not in it.

Every offline row below was produced by one command, whose real output is in
`checks-complete.txt`:

```bash
uv run ruff check . && uv run ty check . && uv run python -m tests
```

`all 11 modules ok`, `All checks passed!` twice. No network, no provider: the
runner sets `ALLOW_MODEL_REQUESTS = False` before any scenario module is
imported, so a module that forgot an override raises rather than billing.

## Phase 01 requirements

| Requirement | Status | What holds it |
|---|---|---|
| §1 baseline and digests | `passed` | `snapshot-e78ac56.json` before, `snapshot-complete.json` after, both from `git ls-files` + SHA-256, with the lock and prompt roll-ups |
| §1 SDK capability ledger | `passed` | `capability-ledger.md`, plus `tests.test_state::a_schema_authored_tool_validates_what_a_typed_one_does` which probes the `from_schema` row instead of asserting it is unused |
| §2 / V5 state and service facts | `passed` | `tests.test_state` (atomic commits, nested revalidation, `False`/`0`, native day and time boundaries), `tests.test_salon` (every diary response validated then read) |
| §2 / V5 / S20 effective inputs | `passed` | `a_schema_authored_tool_validates_what_a_typed_one_does`: handler-invocation counter stays at zero for a missing field, a wrong type, and a model overriding an injected one; an unsupported schema construct is refused at build |
| §2 / S21 forged verification | `passed` | `tests.test_salon::a_lookup_about_somebody_else_verifies_nobody`, `an_empty_number_identifies_nobody`, and the state invariant that clears verification when the number changes |
| §3 trustworthy diary oracle | `passed` | `tests.test_diary` — eligibility, overlap, opacity, idempotency, offer ownership, the frozen clock, and `two_calls_from_one_phone_are_two_conversations` |
| §3 preference before the cap | `passed` | `tests.test_diary::a_preference_is_answered_before_the_list_is_cut_down` reproduces the defect on the same fixture and then fixes it |
| §4 / V6 phone capture | `passed` offline + hybrid replay; fresh audio `not-run` | Structural/consent tests plus two/three fragments, history merging and native Pipecat queue cancellation in `test_history` / `test_recovery`; real Gemini readback/verification 3/3. Supplied voice trace 055ff5… exposed prefix loss; no post-repair microphone call yet |
| §5 / V4 / V8 legal flow | `passed` | `tests.test_flow` — per-request schemas, stale destinations refused at execution, conflicts, partial JSON, abandonment |
| §6 / V12 bounded turns | `passed` | `tests.test_budget` — exhaustion across runs, one allowance per turn, uncertain writes reconciled, superseded turns fenced |
| §6 disconnect and cancellation | `passed` | `tests.test_core::a_turn_that_never_finished_says_so`, `a_late_commit_cannot_move_a_caller_who_moved_on`, and the four barge-in cases |
| §7 trustworthy measurement | `passed` | `tests.test_bench` — the synthetic `trace_s15` fixture (now with a superseded attempt), the four live calls at 30 turns / 53 requests, and the usage aliases |
| §8 / C0 / V13 core and adapter | `passed` | `tests.test_core::the_same_scenarios_both_ways` and the two source-level guards that keep the core runtime-neutral |
| §8 / C3 history | `passed` offline + hybrid replay | Per-request snapshots, resets, owner completed/unserved returns, speech IDs, late playback, completed tool receipts and exact-once caller input. New repair uses SDK `new_messages()` after teardown instead of positional slicing. All 13 modules pass and split-phone replay is 3/3. [Current evidence](../../poc-status-20260921.md); [historical C3 evidence](../../c3-20260921.md). Client playout remains unmeasured |
| Verification before booking or complaints; approval before complaint recording | `passed` offline; broader complaint acceptance `failed` | User-directed replacement of the old sequence. Native graph prerequisite, retained destination, already-verified fast path and abandonment tests; negative control removes the prerequisite and fails. Caller-approved wording and single receipts remain enforced. New complaint-through-verification replay 3/3; full recovery 23/24, continuation 6/12. [Current evidence](../../verification-first-evidence/README.md). |

## The supplied trace findings

| Finding | Status | What holds it |
|---|---|---|
| N1 hang-up on a move question | `passed` offline | `tests.test_consent::n1_a_move_question_cannot_hang_up` and `n1_the_move_it_asked_for_is_the_move_it_makes` |
| N2 "Friday" became "next Friday" | `passed` offline | `n2_friday_is_not_next_friday`, which asserts the day of the accepted write, and `n2_the_day_survives_the_verification_detour` |
| N3/N4 flag flipped without consent | `passed` offline | `n3_a_flipped_flag_is_not_consent` (same slot, a different slot, an unrelated utterance) and `n3_an_answered_question_books_what_they_answered` |
| N4 yes about Spain verified a number | `passed` offline | `n4_a_yes_about_spain_verifies_nobody`, `n4_a_correction_is_not_an_agreement` |
| A complaint written in words nobody agreed to | `passed` offline | `a_complaint_is_written_in_the_words_they_agreed_to`. Found by the negative controls: the guard was in place and no check held it |
| F1 false completion speech | `passed` offline | `earlier_b_a_cancellation_nobody_made_is_not_spoken` asserts the *frames*, not the final output; `recounting_what_is_really_booked_still_streams` is the false positive |
| F2 redo loop and invented restriction | `passed` offline | `a_redo_request_keeps_the_booking_and_is_told_the_truth`; `prompts/salon/complaints.md` states the pending outcome and invents no restriction |
| F3 availability capped before preference | `passed` | see §3 above |
| F5 attempts counted as turns | `passed` | `tests.test_bench::the_live_calls_count_the_turns_the_caller_took` — 30 turns, 53 requests, N2 at 7/8 and N4 at 9/11 |
| F6 the native LiveKit export overcounted | `blocked` | the export is not on disk anywhere and was not re-supplied. The reader divergences that *are* known are in `trace-reader-notes.md`; the 8-turn/16-request figure cannot be reproduced without the file |
| F7 TTS timing attribution | `passed` offline | `tests.test_core::the_processor_reports_no_metrics_of_its_own`. The live half -- that the eight impossible TTFB values are gone from a new call's spans -- is `not-run` |
| F8 usage, session and transcript | `passed` offline | `the_aliases_in_a_usage_record_are_not_added_up`; the call record carries the transcript and three distinct times |
| Speech causality | `passed` offline | `every_segment_says_where_it_came_from`: origin, attempt, id, the covering tool call, and the same id on the frame |
| F9 responsiveness | `inconclusive` | Deduplicated current/earlier Gemini 3.5 voice samples: p50 1.149s / 1.154s, p90 2.394s / 1.922s. These pre-repair calls are not matched or client-playout measurements; see the full report |

## Not run, and what each one needs

| Gate | Status | Why |
|---|---|---|
| Focused live acceptance scenarios | `failed` on final sample | Updated verification-first recovery 23/24; continuation 6/12. One recovery complaint did not complete; six upcoming-booking complaints missed the readback, with unsupported free-service wording in some replies. Earlier same-time move variability is not proven resolved. [Current results](../../verification-first-evidence/README.md). Fresh audio acceptance remains open. |
| Native Pipecat comparison | `blocked` | The native arm's build is not identified by its trace and no native source is in this repository. `tests/fixtures/trace_native.json` is one captured call of an unidentified build |
| Matched latency decision | `not-run` | Depends on the row above. The 1.154s median in the follow-up is context and not an acceptance target, and the model changed underneath it |
| Fresh audio acceptance and client playout | `not-run` after this repair | The supplied voice calls provide Pipecat server speech-marker timing and expose the defects. They predate the repair. Text replay measures neither microphone endpointing nor client audibility; client playout is still unobserved |
| V10 carrier routes, V1 portability, generated providers | `blocked` | Later phases. No adapter but Pipecat exists here |

The live command, from this worktree:

```bash
SALON_FIXTURE=fresh SALON_CLOCK=2026-09-21T10:00 uv run --env-file /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.env python -m bench.recovery --repeats 3 --label focused
```

Three repetitions per scenario, rows flushed per turn to
`bench/raw/focused-<timestamp>.jsonl`. `bench.turns --session-id <id>` reads
the timing half of the same run from Langfuse.

## One live call, unscored

Trace `db4a8f687ac508bf0eb371b294a362f6`, 2026-09-21 14:58, 128s, 12 caller
turns, run by hand and not by `bench.scenarios`. It is not an acceptance run
and scores nothing -- one call answers no rate. It found three defects, two
of them in the runtime seam this work changed:

| Found | Status | What holds it |
|---|---|---|
| an utterance cut off by the caller's next one reached no model and no history | `passed` offline | `tests.test_voice::a_cancelled_turn_carries_what_the_caller_said` |
| a barge-in recorded as `ended=transport` on every later span | `passed` offline | `a_barge_is_not_a_disconnect` |
| the complaint readback put the caller's request to complain back to them | `passed` offline | `tests.test_consent::asking_to_complain_is_not_a_complaint` |
| an apology spoken after the receipt it belonged before | `prompt` | `prompts/salon/complaints.md`; nothing in the harness delivered those out of order and no check asserts a model's ordering |

The guards this branch exists for held on that call: two refused hang-ups, a
move that asked before it wrote, and the move it made was the one it asked
about. That is one call's worth of evidence and is recorded as such.

## Negative controls

Every fix above was reverted, one at a time, and the named check run. Eleven
of them, and three more for the live call's findings; the messages they
printed are in the tables of `bench/review-20260921.md`. One found nothing --
the complaint-summary guard had no check holding it -- and that row now has
one.

## What this adds up to

**Phase 01 boundaries checked offline, with verification now first for booking and
complaints. Complaint conversation quality, fresh audio acceptance and native
comparison remain open; responsiveness parity is unproven.**

The historical hand-run voice call above was unscored. The subsequent recovery
work has now run a matched text suite against the baseline and implementation
revisions and the repaired default model. See [the recovery report](../../recovery-20260921.md)
for retained results and limitations. Audio, native-runtime comparison and
Gemini 3.1 acceptance remain open; this ledger does not close the POC. The final
default-model results and current closure conditions are in the
[2026-09-21 status report](../../poc-status-20260921.md).


## 22 September: code-only optimization

| Boundary | Status | Evidence |
|---|---|---|
| Shared client/lifecycle | implemented, offline passed | One model/client for four salon agents; bot/talk own lifecycle; deduplication/cleanup test. |
| Known readback/approval advancement | implemented, offline passed | Same guarded executors; zero provider requests for clear approved writes; ambiguous input falls back. |
| Retention/budget/interruption | passed offline | Input retained once; application receipts before awaited speech; cancellation and stale-generation/barge fences. |
| Booking speech prevention | implemented, passed offline | Typed diary availability/clarification; receipt-only completion; false complimentary-appointment negative control fails. |
| Existing prompt/KB/tool prose | unchanged | before.json hashes and AST docstrings match. |
| Full offline/static checks | passed | 14 modules, Ruff and ty; optimization-evidence retained outputs. |
| Gemini recovery/continuations | 24/24 + 24/24 | Three repetitions each; final additional identity guard covered offline and in qualification. |
| Matched native latency acceptance | partially demonstrated | Frozen cohort: POC 100/100, native 80/100; 240 vs 519 requests. Booking/move/complaint-through-verification pass both latency bounds; public hours and verified-complaint p90 remain unproven. |
| Fresh microphone / useful audio / playout | open | No post-optimization microphone or matched audio evidence yet. |

[Current optimization report](../../optimization-evidence/README.md) supersedes
older implementation-status statements in this ledger. Historical evidence stays
historical; passing text tests does not close the POC.

## 22 September: fresh microphone evidence

The four calls at 07:20–07:27 UTC now verify the optimized runtime on microphone
input: 4/4 intended call outcomes, 29 answered caller turns, six reconciled
writes (two bookings, one move, three complaints), and no unresolved operations.
Three interrupted continuations recover; four approved writes use no model.
Two booking consent refusals recover through readback and approval.

Caller→bot p50 is 1.152s, p90 2.575s; request TTFT p50/p90 is 0.615/0.782s.
These establish fresh audio evidence for the exercised paths, while matched
native useful-audio/client-playout parity and the remaining microphone scenarios
stay open. The earlier “no post-optimization microphone” row is superseded by
this sample, not by a claim of full acceptance. See the
[review and retained metrics](../../microphone-evidence-20260922/README.md).
