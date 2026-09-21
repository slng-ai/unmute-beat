# Evidence ledger: what closed, what did not, and what nobody has run

One row per requirement in `POC_COMPLETION_PLAN.md`'s completion ledger.
`passed` means a retained result exists and is named here. `not-run` and
`blocked` are not failures and are not passes either; a plan that reads them
as passes is the thing this file exists to prevent.

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
| §1 baseline and digests | `passed` | `snapshot-e78ac56.json` before, `snapshot-a30824b.json` after, both from `git ls-files` + SHA-256, with the lock and prompt roll-ups |
| §1 SDK capability ledger | `passed` | `capability-ledger.md`, plus `tests.test_state::a_schema_authored_tool_validates_what_a_typed_one_does` which probes the `from_schema` row instead of asserting it is unused |
| §2 / V5 state and service facts | `passed` | `tests.test_state` (atomic commits, nested revalidation, `False`/`0`, native day and time boundaries), `tests.test_salon` (every diary response validated then read) |
| §2 / V5 / S20 effective inputs | `passed` | `a_schema_authored_tool_validates_what_a_typed_one_does`: handler-invocation counter stays at zero for a missing field, a wrong type, and a model overriding an injected one; an unsupported schema construct is refused at build |
| §2 / S21 forged verification | `passed` | `tests.test_salon::a_lookup_about_somebody_else_verifies_nobody`, `an_empty_number_identifies_nobody`, and the state invariant that clears verification when the number changes |
| §3 trustworthy diary oracle | `passed` | `tests.test_diary` — eligibility, overlap, opacity, idempotency, offer ownership, the frozen clock, and `two_calls_from_one_phone_are_two_conversations` |
| §3 preference before the cap | `passed` | `tests.test_diary::a_preference_is_answered_before_the_list_is_cut_down` reproduces the defect on the same fixture and then fixes it |
| §4 / V6 phone capture | `passed` offline | `tests.test_state` (the Spain-only policy, short numbers, absence), `tests.test_consent::n4_*`. Audio fragmentation is `not-run`: it needs a real call |
| §5 / V4 / V8 legal flow | `passed` | `tests.test_flow` — per-request schemas, stale destinations refused at execution, conflicts, partial JSON, abandonment |
| §6 / V12 bounded turns | `passed` | `tests.test_budget` — exhaustion across runs, one allowance per turn, uncertain writes reconciled, superseded turns fenced |
| §6 disconnect and cancellation | `passed` | `tests.test_core::a_turn_that_never_finished_says_so`, `a_late_commit_cannot_move_a_caller_who_moved_on`, and the four barge-in cases |
| §7 trustworthy measurement | `passed` | `tests.test_bench` — the synthetic `trace_s15` fixture (now with a superseded attempt), the four live calls at 30 turns / 53 requests, and the usage aliases |
| §8 / C0 / V13 core and adapter | `passed` | `tests.test_core::the_same_scenarios_both_ways` and the two source-level guards that keep the core runtime-neutral |
| §8 / C3 history | `partial` | `a_reset_step_is_sent_none_of_the_conversation`, `the_transcript_is_the_call_and_not_the_protocol`, `a_handed_over_agent_reads_the_question_without_it_being_asked_twice`. The named-state projection is `{{heard}}` and the delivered-history reconciliation is `note_heard`; a general projection language is **not** implemented and is not claimed |

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
| F9 responsiveness | `not-run` | see below |

## Not run, and what each one needs

| Gate | Status | Why |
|---|---|---|
| Focused live acceptance scenarios | `not-run` | Eight of them are written and scored (`n1_move_same_time` through `goodbye_with_a_request` in `bench/scenarios.py`). They need a provider key and the command below. Nothing in this ledger claims a real model obeys these guards; it claims the guards refuse it when it does not |
| Native Pipecat comparison | `blocked` | The native arm's build is not identified by its trace and no native source is in this repository. `tests/fixtures/trace_native.json` is one captured call of an unidentified build |
| Matched latency decision | `not-run` | Depends on the row above. The 1.154s median in the follow-up is context and not an acceptance target, and the model changed underneath it |
| Audio, playout and endpointing | `not-run` | Every timing in this repository is text handed to TTS. Client playout is a boundary this process cannot see, and it is named that way wherever it is reported |
| V10 carrier routes, V1 portability, generated providers | `blocked` | Later phases. No adapter but Pipecat exists here |

The live command, from this worktree:

```bash
SALON_FIXTURE=fresh SALON_CLOCK=2026-09-21T10:00 uv run --env-file /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.env python -m bench.scenarios 3 focused
```

Three repetitions per scenario, rows flushed per turn to
`bench/raw/focused-<timestamp>.jsonl`. `bench.turns --session-id <id>` reads
the timing half of the same run from Langfuse.

## Negative controls

Every fix above was reverted, one at a time, and the named check run. Eleven
of them; the messages they printed are in the tables of
`bench/review-20260921.md`. One found nothing -- the complaint-summary guard
had no check holding it -- and that row now has one.

## What this adds up to

**Phase 01 correctness complete offline; live focused checks not run;
responsiveness parity unproven.**

Every supplied correctness failure has a passing regression that asserts the
emitted speech or the business operation, and the measurement reconciles the
four live calls exactly. Nothing here was measured against a real provider,
a real voice or a second runtime, and no row above should be read as though
it was.
