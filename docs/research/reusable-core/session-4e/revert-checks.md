# Revert checks, session 4e

Each fix was reverted on its own, with every other file left as it was, and
its regression run. Each one failed with the defect's own behaviour. The fix
was then restored with `git checkout` and the suite passed again.

| Fix | Reverted | Test that failed | What it saw |
|---|---|---|---|
| A: `after:` may name a choice | `steps.py` `collect`: `(moved - {name}) & after` back to `plain & after` (choices stripped from the change set) | core R12 | "A: a resolution offered for billing outlived its category, itself a choice" |
| B: a pick whose offer the same update took away | `steps.py` `collect`: `late = []` (the supplied pick exempted, as before) | core R12 | "B: a resolution offered for billing came in with access" |
| Salon day by meaning | `salon/models/state.py`: `booking_day: str` (no `_day_meant` validator) | salon V10 | "thursday" kept as words; the Thursday times were never tied to one date |
| Core change by meaning | `steps.py` `collect`: `changed` compared to the raw input again | salon V10 | "the same day said again took the times away" ("Thursday" beside a stored 2026-10-01) |
| Rendered choices speak the application's offer | `steps.py` `wait`: the offer words left out | core R13 | "Perdona, ¿me lo repites?" instead of the desk's offer |
| Salon booking rendered | `salon/agent.yaml`: booking back to `guarded` prose (refusals dropped) | salon `model_speech_never_claims_a_completed_action` | the model's own copy of a move read-back was said, as in live call 6 |
| Receipt said again once | `state.py` `unheard_receipt`: the repeat count ignored | salon `an_unheard_receipt_is_said_again_once_then_the_caller_is_heard` | "the caller was not heard": the receipt was replayed again, and the correction never reached the agent |
| Goodbye after a no | `state.py` `last_asked_left_undone`: the answered check dropped | core R14 | "No he hecho ese último cambio." said after the caller's no |
| No filler for a second request | `turn.py` `prepare`: the holding line on every request after the first, put back | core `a_second_request_is_no_reason_to_hold` | "One moment." in front of a fast answer after a fast tool |
| Warm-up awaited | `graph.py` `model_clients`: the `gather` after cancel removed | core `a_warm_up_ends_before_its_client_closes` | `['client closed', 'warm-up ended']`: the client closed under a running warm-up |
| Live calls counted | `server.py` `live_counts`: own class name only | salon A6 | "a live call was not counted" (`FlowCall[SalonState]`) |
| Offer instead of pardon | `steps.py` `wait`: the offer look-up before pardon disabled | salon `a_wait_with_nothing_to_say_offers_what_there_is` | "Sorry, could you say that again?" right after verify, as in live call s4e-latency-1 |
| Own booking not "taken" | `salon/functions.py` `find_times`: the own-booking line disabled | salon V11 | "9:00 AM is taken. Tomorrow I have 10:00 AM ..." about the caller's own booking, as in live call s4e-worker-bystander |
