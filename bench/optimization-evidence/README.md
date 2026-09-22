# Code-only POC optimization — 22 September 2026

Implemented: one shared provider client; application steps for tracked approvals;
typed booking speech. Prompts, knowledge and existing authored tool docstrings
match `before.json` byte-for-byte/AST-for-AST. No dependency, audio, endpointing or
native-reference changes. This comparison permits internal tool/output differences;
it does not claim one-to-one model schemas.

## What changed and why

All four agents use the same GoogleModel/client. `bot.py` and `talk.py` own the
client lifecycle through the SDK's context manager. Pool sharing avoids four
separate clients during routing; its independent latency effect is not isolated.

A conservative application step can read back a prefetched phone number on entry
or accept an unambiguous answer to the immediately preceding tracked question.
Phone lookup, booking writes and complaint recording use the same validated
handlers as the model tools. Corrections, questions, stale consent/identity and
unfamiliar wording continue through the model. The path keeps the normal tool
budget, generation/barge fence, operation tokens and uncertain-write barrier.

Application steps retain accepted caller input once, record validated results
before awaiting speech, and report zero provider requests when none occur. Their
receipts are labelled application results, not invented model tool exchanges.
A cancelled spoken receipt therefore does not erase a completed business action.
The Pipecat adapter still owns interruption and response-frame cleanup.

Booking now speaks through typed availability/clarification/readback/receipt
outputs. Availability is rendered from the validated diary; an existing clear
slot choice still goes directly through `save_booking`. Arbitrary provider text
cannot announce a complimentary appointment or an unperformed write from this
step. Existing model-facing business tool names/signatures/descriptions are
preserved; `offer_times` and `booking_guidance` are additional output functions.
The public concierge remains generative.

## Verification

- [Offline](offline.txt): **all 14 modules ok**, with model requests disabled.
- [Ruff](ruff.txt), [ty](types.txt), [format](format.txt): pass.
- [Recovery](recovery.jsonl): **24/24**; [continuations](continuations.jsonl):
  **24/24**, three repetitions per scenario, real Gemini 3.5. These precede the
  final stale-phone-identity guard; that guard passed the full offline suite and
  five-scenario qualification. Split-phone replay uses scripted prefix
  interruption followed by real-model completion; it is not microphone testing.
- `tests.test_optimization`: zero-request approvals, stale/qualified consent,
  identity change, repeated agreement, input validation, write-receipt retention
  across external cancellation, pre-execution barge/generation fences, budget,
  suppressed unsupported speech and provider lifecycle.
- [Negative controls](negative-controls.txt): removing application advancement
  breaks the zero-request check; allowing booking text leaks a false complimentary
  appointment despite the earlier phrase guard. Earlier retention/queue/consent
  negative controls remain in their original evidence directories.
- [Profile](profile.txt): 200 offline approved-booking turns, with profiler overhead,
  p50 **0.745 ms**, p90 **0.863 ms**. No provider, audio or network is involved.
  Validation is retained; this does not justify history/schema caches or a claim
  about browser response time.

The first native preflight lacked required `NATIVE_CALL_ID`; every failure is in
`qualification.json.gz`. It is excluded from measurement. Supplying that synthetic
call identifier produced the retained `qualification-v2.json.gz`: POC 5/5; native
4/5, with one complaint-completion failure. The generated native source is unchanged.

## Matched measurement

The new 200-call cohort follows [PLAN.md](PLAN.md), with the original comparison
runner, scripts, business scoring, fixtures and native overlay. [snapshot.json](snapshot.json)
and `source.tar.gz` freeze the optimized implementation. Results are reported
separately from the earlier native and model cohorts. No pooling across changes.

| Scenario | POC / native passed (of 20) | POC turn p50 / p90 | Native turn p50 / p90 | Latency gate |
|---|---:|---:|---:|---|
| hours | 20 / 20 | 0.667 / 0.745s | 0.617 / 0.701s | not established |
| complaint | 20 / 14 | 0.555 / 1.431s | 1.229 / 1.496s | not established |
| complaint_verify | 20 / 6 | 0.544 / 0.785s | 1.601 / 3.570s | passed |
| booking | 20 / 20 | 0.699 / 1.309s | 1.213 / 1.686s | passed |
| move | 20 / 20 | 1.882 / 2.033s | 2.221 / 2.372s | passed |

All-call point estimates above include failures. **POC 100/100; native 80/100.**
The POC used **240 model requests**, native **519**, over 200 caller turns per arm.
The earlier frozen POC used 360 requests on the same scripts; that historical
difference is not a contemporaneous latency control. Native failures in this
cohort were incomplete phone readback and uncompleted complaint recording.
No source or native-build hashes changed during collection.

| Scenario | Successful matched pairs | p50 difference 95% CI | p90 difference 95% CI |
|---|---:|---:|---:|
| hours | 20 | -20 to +120 ms | -47 to +83 ms |
| complaint | 14 | -809 to -623 ms | -188 to +39 ms |
| complaint_verify | 6 | -771 to -544 ms | -2259 to -1668 ms |
| booking | 20 | -624 to -452 ms | -547 to -177 ms |
| move | 20 | -433 to -263 ms | -554 to -162 ms |

Differences are POC minus native. Booking, same-time moves and complaint through
verification pass both zero-regression confidence bounds with no worse observed
correctness. The verified-complaint median improves, but its p90 interval crosses
zero. Public-hours intervals cross zero and its point estimates are slower.
**The overall zero-regression gate is not closed.** In particular, the six
successful complaint-verification pairs give a limited conditional latency sample;
all-call metrics and failed speech remain available.

| Scenario | POC / native request-first-output p50 | POC / native first submitted speech p50 |
|---|---:|---:|
| hours | 0.536 / 0.483s | 0.543 / 0.487s |
| complaint | 0.599 / 0.505s | 0.555 / 0.903s |
| complaint_verify | 0.584 / 0.476s | 0.544 / 0.574s |
| booking | 0.566 / 0.469s | 0.555 / 0.644s |
| move | 0.580 / 0.510s | 0.658 / 0.597s |

First submitted speech includes filler. The same-time move finishes sooner,
but its first submitted speech has a slower point estimate; those are distinct
metrics. Zero-request application turns contribute their real turn durations;
they do not contribute fabricated zero TTFT observations. POC request-first-output
remains slower in these samples. The result demonstrates savings from fewer
model decisions, not faster individual model inference. Pool sharing, framework
preparation, rendered model inputs and transport are not causally separated here.

[results.json](results.json) includes all distributions and intervals;
[calls.json.gz](calls.json.gz) retains every scored call. Reproduce with
`python -m bench.optimization-evidence.analyse`.

The prior same-core Gemini comparison is [separate](../model-evidence/README.md):
3.1 completed 67/100 and 3.5 93/100 before this optimization. Transport resets
and deadline failures remain included. Those outcomes do not describe this
optimized source or support switching the selected model.

## What still closes the POC

The code work is implemented. Acceptance still needs the remaining microphone
matrix, matched useful-audio measurements and client playout. Text completion,
first submitted speech (which may be filler), provider first output and what a
caller actually hears are different measurements. Missing cache fields and
symmetric native/POC token capture remain unknown; no new cache/cost claim follows
from this cohort. Historical caller-to-bot/cache/payload measurements remain in
the [Phase 01 report](../poc-assessment-20260921.md).

The typed-flow justification is executable evidence for approval, identity,
legal routing and business receipts, plus fewer required provider decisions.
Native Pipecat already supplies audio, interruptions, routing and tool execution;
these are not new advantages of this implementation. Both systems still depend
on model judgment for intent and unfamiliar language. Finite scripts do not prove
universal conversational correctness.
