# Session 4f revert checks

Each repair was taken out alone, and its regression was run against the rest of the code. Every row failed
as expected. The file was then restored, and the full suites were run again.

| Repair | What was put back | Regression | Failure seen |
|---|---|---|---|
| 1. An offer's words go with its ids | the old `_offer_words` record, before the fix (run at `420de86` through the public API) | core R15, salon V12 | the caller heard "Puedo dejarlo como un reembolso o un abono en la cuenta" after the category became access, while `_offered` held only `ticket_id` |
| 2. A decline is the agent's typed call | `begin_turn` recording every timely reply as turned down (the 4e rule) | core R16 (desk), P19 (clinic) | `('a question was taken as a no', 'Gracias por llamar al soporte. Hasta pronto.')`; clinic: `(False, ['Thanks for calling. Goodbye.'])` |
| 2b. A joined turn drops a no read from its first piece | the rewind no longer drops declines | core R16 | `('a no kept from a piece the caller carried on from', {1: 2})` |
| 3. A receipt said again on request | no `say_again` output on any step | core R17 | `('a requested repeat was not said', (Spoken(... text='Perdona, he tenido un problema. ¿Me lo repites?' ...),))` |
| 5b. A changed resource is offered again | no refresh after a successful change | salon V13, core R18 | salon: `('the booking just made was not on offer', {'appointment': {}})`; desk: `KeyError: 'ticket_id'` (no ticket offer left after the resolve) |
| 5c. The fields a reply recorded are this turn's only | `_recorded` kept across turns (no `FlowCall.turn_started` reset) | salon `test_requests` K7 | `AssertionError: ['Shall I book a haircut, Friday the 2nd at 2:00 PM?']`: after a no, the booking was read back again |
| 6. Fake phones follow the TwiML stream URL (placement by process) | `Phone.run` dials `/ws` on the webhook's port again | offline F6 | `same worker: not armed [(..., 68290, 8020), (..., 68290, 8020)]`; six F6 verdicts fail, starting with `media_followed_the_webhook` and `placed_on_two_processes` |
| 2c. A receipt-set field rests on the receipt that set it | resets read only the union of every operation's sources | core R16 (revision case) | `('a new resolution took the revision the look-up found, and the look-up would not run again', ['Perdona, he tenido un problema. ¿Me lo repites?'])` |

## Follow-up: browser delivery and bounded recovery

Same method, one piece at a time, with the file restored after each run (script: every edit asserted before it
was made). All seven were caught.

| Repair | What was put back | Regression | Failure seen |
|---|---|---|---|
| F1. A read-back past its limit is withdrawn | `Prompted.put` returns `gave_up` and leaves the intent prepared (the incident's code) | core R19 | `status='prepared' readbacks=2`: the intent stayed actionable after the fallback |
| F2. Missing evidence is not "did not hear" | the model is told "They did not hear all of it" for an unconfirmed copy | core R19 | the request text carried no "missing evidence" line |
| F3. Written audio is not played audio | the mark also counts the moment its audio passes the output, bound still sent | core W1 | `heard_by='mark'` with `played_at` before the page's playout: heard before the browser played past it |
| F3b. The same, as the old `LocalPlayout` did | the mark counts on passing the output and no bound is sent | core W1 | `TimeoutError`: the page never got a mark to report |
| F4. A report must pass its bound | `played` ignores the RTP timestamp | core W3 | `{'mark': ..., 'rtp': 544720682}`: a report short of its bound counted |
| F5. An interruption voids pending marks | `WebRTCPlayout` ignores `InterruptionFrame` | core W4 | `TimeoutError`: the cut copy was never voided |
| F6. A page that reports nothing is told before anything | `run_bot` greets whatever the page said on `client-ready` | salon BR3 | `["Hi, you've reached Sage and Stone. Robin speaking, what can I do for you?"]`: the greeting went out |
