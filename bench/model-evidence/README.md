# Frozen model comparison (before code optimization)

This is the completed 200-call same-core model cohort, not the later optimized build. Twenty paired calls per scenario used identical frozen prompts/business logic, priority service and MINIMAL thinking; only the Gemini model changed. Source hashes matched at analysis time. The aliases and provider region are unpinned.

| Scenario | Gemini 3.1 passed / 20 | Gemini 3.5 passed / 20 |
|---|---:|---:|
| hours | 20 | 20 |
| complaint | 17 | 16 |
| complaint_verify | 18 | 19 |
| booking | 12 | 18 |
| move | 0 | 20 |

Overall: **67/100 versus 93/100**. This supports retaining 3.5 for this POC; it does not establish general model superiority or post-optimization acceptance.

Failure inspection separates model behavior from infrastructure: 3.1 repeatedly refused the available Saturday same-time move and sometimes repeated booking approval instead of completing the selected time. The trace logs contain 2 connection-reset/read failures in each model arm, plus deadline messages in 9 of the 3.1 logs and 5 of the 3.5 logs. These categories can overlap and are not an additive count of failures. Wall-clock gaps are retained; their cause is not established. Do not treat transport errors as model-quality evidence or silently discard them.

All-call distributions, successful-pair denominators, request-first-output timings and paired p50/p90 intervals are in [results.json](results.json). [calls.json.gz](calls.json.gz) retains the business outcomes and speech, including failures. [PLAN.md](PLAN.md) defines the unchanged sampling and scoring rule. This is transcript replay; no microphone, STT/TTS or client playout was measured.
