# Corrected cohort declaration

Declared after qualification, before collecting the corrected cohort.
This supersedes `PLAN.md` for measurement. The stopped diagnostic cohort and the
three-repeat qualification cohort are excluded from all comparative intervals.

Use the same five scenarios, identical scripts, shared POC knowledge/diary/phone
validation, frozen clock and synthetic identity described in `PLAN.md`.
Both arms now leave max output tokens unset. Native preference wording uses the
saved appointment's time for a same-time move. The POC uses typed verification
speech, whose offline regression and both 24-case suites pass. Three-repeat
qualification passed 15/15 POC calls; native failures remain in the evidence and
are not silently repaired or discarded.

Collect **20 paired calls per scenario**, 100 pairs / 200 calls total, sequentially.
Alternate POC/native order each repetition. Freeze the exact implementation and
build in `snapshot-v2.json`; do not change either arm during collection. Retain
all failures and process timeouts. Raw destination: `bench/raw/native-matched-v2`.

The decision rule remains zero regression in the p50/p90 full transcript-turn
duration differences, with 10,000 call-level paired bootstrap samples, seed
20260921, percentile 95% intervals. Both upper bounds must be <=0 and observed
correctness must not be worse before describing a transcript-processing advantage.
Uncertainty crossing zero is inconclusive. Report successful-pair latency beside
all-call failure counts, not a success-only headline.

Report request count, first provider output, and first submitted speech including
filler separately. No STT/TTS, endpointing, client playout or first-useful-audio
claim is possible from this cohort. Automated semantic checks are limited;
review failed transcripts and record any score limitation explicitly. A wrong
agent question is not repaired by adapting the fixed caller replies.

Stop and retain partial diagnostic evidence if a new input/configuration mismatch
is found. Never pool a corrected implementation into this cohort. Any additional
sample needs a fresh declaration before collection.
