# Revert checks, session 4d

Each fix was reverted on its own, with every other file left as it was, and
its regression run. Each one failed with the defect's own behaviour. The fix
was then restored from the saved diff and the suite passed again.

| Defect | Reverted | Test that failed | What it saw |
|---|---|---|---|
| 2 | `core/src/core/operations.py` | core I11 | the intent `succeeded` with nothing filed, not `unknown` |
| 2 | `core/src/core/steps.py` (settlement repairs) | core I15 | caller B was told caller A's write was done |
| 3 | `salon/functions.py` | salon V1 | the diary's refusal left the move intent `unknown` |
| 3 | `core/src/core/steps.py` (`_record`) | salon V1 | the refused picks stayed in the state |
| 4 | `steps.py`, `flowspec.py`, `salon/agent.yaml` | salon V4 | the day change forgot the appointment being moved |
| 4 | `salon/diary.py` (`_moved_here`) | salon V4 | the cancel after the move was refused as stale |
| 1 | `core/src/core/steps.py` (the `Restated` gate) | salon V6, core F5 | "Yes, that's the same as I read back." and a booking, on "No, do not book …" |
| 5 | `salon/diary.py` (grammar) | salon `day_after_tomorrow_needs_no_article` | `resolve_day('day after tomorrow')` was None |
| 5 | `steps.py`, `salon/functions.py` (texts) | salon V7 | "Not yet: slot missing. Ask the caller and record it first." |
| 6 | `wrote_this_turn` (one line) | core S8 | a claim after a lookup went uncorrected |
| 6 | `salon/agent.yaml` (verify rendered) | salon `verify_says_every_word_in_code` | the model's own digits were said |

The script: save `git diff -- <files>`, `git checkout -- <files>`, run the
module with `GOOGLE_API_KEY=x`, then `git apply` the saved diff.
