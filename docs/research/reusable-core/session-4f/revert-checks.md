# Session 4f revert checks

Each repair was taken out alone, and its regression was run against the rest of the code. Every row failed
as expected. The file was then restored, and the full suites were run again.

| Repair | What was put back | Regression | Failure seen |
|---|---|---|---|
| 1. An offer's words go with its ids | the old `_offer_words` record, before the fix (run at `420de86` through the public API) | core R15, salon V12 | the caller heard "Puedo dejarlo como un reembolso o un abono en la cuenta" after the category became access, while `_offered` held only `ticket_id` |
| 2. A decline is the agent's typed call | `begin_turn` recording every timely reply as turned down (the 4e rule) | core R16 (desk), P19 (clinic) | `('a question was taken as a no', 'Gracias por llamar al soporte. Hasta pronto.')`; clinic: `(False, ['Thanks for calling. Goodbye.'])` |
| 2b. A joined turn drops a no read from its first piece | the rewind no longer drops declines | core R16 | `('a no kept from a piece the caller carried on from', {1: 2})` |
| 3. A receipt said again on request | no `say_again` output on any step | core R17 | `('a requested repeat was not said', (Spoken(... text='Perdona, he tenido un problema. ¿Me lo repites?' ...),))` |
| 2c. A receipt-set field rests on the receipt that set it | resets read only the union of every operation's sources | core R16 (revision case) | `('a new resolution took the revision the look-up found, and the look-up would not run again', ['Perdona, he tenido un problema. ¿Me lo repites?'])` |
