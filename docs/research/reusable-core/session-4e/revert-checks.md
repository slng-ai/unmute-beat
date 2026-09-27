# Revert checks, session 4e

Each fix was reverted on its own, with every other file left as it was, and
its regression run. Each one failed with the defect's own behaviour. The fix
was then restored with `git checkout` and the suite passed again.

| Fix | Reverted | Test that failed | What it saw |
|---|---|---|---|
| A: `after:` may name a choice | `steps.py` `collect`: `(moved - {name}) & after` back to `plain & after` (choices stripped from the change set) | core R12 | "A: a resolution offered for billing outlived its category, itself a choice" |
| B: a pick whose offer the same update took away | `steps.py` `collect`: `late = []` (the supplied pick exempted, as before) | core R12 | "B: a resolution offered for billing came in with access" |
