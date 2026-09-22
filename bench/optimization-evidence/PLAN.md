# Code-only optimization measurement

Declared before the new cohort. Prompts, knowledge, existing authored tool
instructions, model binding, priority/MINIMAL settings and audio settings remain
unchanged from before.json. The typed implementation adds application steps and
booking output wrappers; this is architecture comparison, not identical schemas.
The native reference and bench.native_compare runner remain unchanged.

First run the existing recovery and continuation suites, three repetitions each.
Retain failures separately; repair and rerun before freezing a measured cohort.
Run unsupported booking-speech and disabled-local-action negative controls.

After qualification, freeze source hashes. Reuse the unmodified native_compare
runner: 20 pairs per scenario, all five scenarios, 200 sequential calls, alternate
arm order. Same five scripts, fixtures, clock, model and native overlay documented
in bench/native-evidence/PLAN.md. Retain every failure/timeout; never pool cohorts
across changes. Imports and fixture setup remain outside turn timing. Each child
process owns its model pool for its lifetime; production bot/talk additionally
close pools explicitly through their session contexts.

Reuse the existing metrics and call-paired bootstrap (10,000 samples, seed
20260921). Report all calls and successful pairs. The zero-regression criterion
requires both p50/p90 upper 95% bounds <= zero and no worse observed correctness.
No follow-up sample merely to obtain a passing result. Historical results are
context, not contemporaneous controls.

Transcript processing excludes microphone, endpointing, STT, TTS, network audio
transport and client playout. Fresh microphone acceptance and matched audio remain
separate closure gates. Provider aliases/region remain unpinned.
