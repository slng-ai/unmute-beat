# Same-core model comparison

Declared 22 September 2026 before collection. This is separate from the native
architecture comparison. The production POC and selected native agent stay frozen.

Use the existing `bench.native_compare` POC runner and its unmodified five
scenarios, caller scripts, business scoring, fresh synthetic fixtures and clock.
Each call runs in a separate process. Override only the model through the public
`Agent.override()` context: Gemini 3.1 Flash Lite versus Gemini 3.5 Flash Lite,
both Google Cloud, priority service, MINIMAL thinking, no output-token cap.
Provider region and model-version aliases remain unpinned.

First collect one pair for the public-hours scenario in a separate qualification
folder. This only checks provider/configuration acceptance and is excluded from
comparison. If either model rejects the settings, stop and retain the failure;
do not silently change one arm's configuration.

Then collect 20 pairs per scenario, 200 calls total, alternating model order each
repetition. Retain every failure and process timeout. Freeze source, prompts,
knowledge, lock, runner and plan hashes before the measured cohort. Do not change
either implementation during collection or combine corrected runs with this one.

Report per-scenario completion, request counts, full transcript-turn p50/p90,
request first output and first submitted speech (including filler). Reuse the
existing analysis functions: ordinary median, nearest-rank p90, call-level paired
bootstrap with 10,000 samples, seed 20260921, 95% percentile intervals. Pair order
for differences is 3.5 minus 3.1. A latency advantage requires both upper bounds
at or below zero and no worse observed correctness; otherwise it is inconclusive
or a regression. Report all-call figures beside successful-pair figures.

No microphone, STT, TTS, endpointing, useful-audio, client-playout, native-framework
or general model-superiority conclusion follows from these transcript results.
The existing scripted scorer checks bounded outcomes, not every aspect of speech
quality. Inspect failures and retain their transcripts.
