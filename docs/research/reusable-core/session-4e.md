# Session 4e: state consistency, reliable speech, latency and lifecycle

Branch `feat/session-4e-lifecycle`, stacked on PR #25 (`feat/session-4d-correctness`, head `31e998d`, open).
Tested revision: see "Evidence" (filled in at the end of the session).

## Result

Five general rules replace the exceptions the last live calls exposed. Each has a regression through a
public entry point, and a revert check that fails without it ([revert-checks.md](session-4e/revert-checks.md)).

1. **A pick counts only against an offer made for the state an update ends in.** `after:` may name another
   choice, resets follow a chain, and a pick that arrives beside the change that voided its offer is refused.
2. **State compares meaning, not spelling.** Core passes a Pydantic validation context (the call's clock and
   the previous state) whenever it validates the application's state. The salon keeps the day asked for as the
   date it means.
3. **A rendered step may offer choices, in the application's words.** Salon booking is now rendered: nothing
   the booking model writes can reach the caller.
4. **Recovery is bounded.** An unheard receipt is said again once, then the caller is heard. A read-back the
   caller heard and turned down is not "left undone" at goodbye.
5. **No filler for a request count, and warm-ups end before their clients close.**

## Faults, by layer

| Symptom | Initiating layer | Core behaviour after it (before) | Evidence, confidence | Fix owner | Regression / limit |
|---|---|---|---|---|---|
| A resolution offered for billing stayed usable after the category changed | core state | `collect` removed every choice from the dependency set | public-path reproducer (desk), high | core | R12 (A) |
| A pick arriving with the change that voided its offer was kept | core state | supplied picks were exempt from the stale reset; the tool checked old offers | reproducer, high | core | R12 (B), plain and chained |
| "thursday" then "2026-10-01" took the times away (live call 5) | core state + application | change computed from raw inputs | V10 replays call 5, high | core (compare validated values), salon (canonical day) | V10; limit below |
| Booking spoke its own read-back copy with a wrong day (call 6, text T5) | model on a prose step | prose went to TTS | live + text traces, high | salon (rendered booking), core (rendered choices) | `model_speech_never_claims_a_completed_action`, R13 |
| Booking spoke a copy of the options JSON (call 4) | model on a prose step | prose went to TTS | live trace, high | same | same |
| A receipt said six times as the caller talked over it (call 6) | caller barge-in (upstream) exposing core recovery | hop-1 replay on every interruption, no limit; the replay also dropped the receipt's intent | trace, high | core | `an_unheard_receipt_is_said_again_once_then_the_caller_is_heard` |
| "I haven't made that last change" after an explicit no (call 7) | core goodbye | `left_undone` = intent never dispatched, whatever the caller said | trace, high | core | R14 |
| "One moment" before a fast answer | core filler | holding line on every second model request | code + tests, high | core | `a_second_request_is_no_reason_to_hold` |
| Warm-up request could outlive its client | core lifecycle | tasks cancelled, never awaited | code, high | core | `a_warm_up_ends_before_its_client_closes` |
| Memory diagnostic always counted 0 live calls | application tooling | `type(o).__name__ == 'FlowCall'`, but the class is `FlowCall[SalonState]` | shown by printing the class name, high | salon | A6 |
| Turn ended after 1.6 s silence while smart-turn said incomplete (call 3) | endpointing (upstream) | a new turn, as designed | 4d bot log | Pipecat configuration, out of scope | not counted as a core regression |

## 1. State updates

(details written below in "How the rules work")

## Evidence

(filled in at the end)
