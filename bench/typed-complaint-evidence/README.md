# Typed complaint repair — 21 September 2026

**Historical repair stage.** The [current assessment](../poc-assessment-20260921.md)
adds typed verification, completed native Pipecat comparison, fresh voice evidence
and the subsequent approval-phrase repair. Results below identify this earlier source.

Result at this stage: **13/13 offline modules, 24/24 Gemini recovery cases and 24/24
Gemini continuation cases** after the receiving-agent handoff status change.
These are text/adapter checks, not microphone acceptance or native-runtime parity.
The goal remains open while the native comparison and audio gates are completed.

## What the new call actually did

[Trace 051b9e…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=051b9e25ee1c3ff97f136e4a3911f834)
was fetched through the Langfuse CLI: 180 observations, four pages, no truncation.
The local call record `bench/raw/live-20260921T193818Z.json` matches the preceding
verification-first source/prompt hashes. This was the expected build, not an old server.

1. At 19:36:33 UTC, `ask_complaint` invented both a requested “redo” and the claim
   that the faulty haircut was on Saturday. Saturday was the **upcoming** booking.
2. At 19:36:43–45, complaints routed to concierge and back, then offered to book
   a redo at no charge. The injected salon document explicitly said a redo was
   the first thing to offer; the role's actual authority did not support this.
3. Caller agreement to that offer sent the conversation into booking. The attempt
   at 19:37:13 used eight model requests and exhausted its four-run routing budget.
4. Eventually CX0001 was recorded, still with the invented Saturday/redo wording.
   Three real writes occurred: Friday booking, Saturday move, complaint. The
   upcoming Saturday 10:00 booking remained; no free-service conversion existed.

This was a policy/authority and model-output problem, not missing STT or failed TTS.
Typed strings ensured valid shapes but did not establish factual provenance or
restrict what ordinary streamed text could promise. The previous acknowledgement
word list could not prevent it. `trace-outcome.json`, `trace-observations.json.gz`
and `trace-metrics.json` retain the supporting evidence without contact fields.

## Changes

- Complaints uses Pydantic AI output functions without a plain-text alternative.
  The core's `Node.model_speech=False` keeps provider preambles out of TTS, even
  when text accompanies a valid tool call. Other agents keep their streaming path.
- Six small knowledge-base entries supply questions and policy guidance. They can
  explain review/refund/product/follow-up options; none offers or books a free redo.
  The original salon policy remains on disk. The front-desk guidance reflects the
  narrower authority requested by the user.
- Readbacks extract caller wording. Both fields must occur in accepted caller
  speech, allowing punctuation/case differences. Assistant history and booking
  records cannot provide readback facts. A request for a free visit is retained
  only when the caller said it, and is explicitly pending review.
- Approval, identity checks, corrections, one write/receipt and original booking
  preservation remain application-owned. Complaint goodbye has no model-authored
  speech parameter. Successful guidance/readback calls also defeat a paired handoff.
- The complaint prompt is a short ordered workflow. The apology vocabulary and
  complaint `speak_by` fallback to ordinary text were removed.
- A separate recovery failure exposed the receiving booking agent saying “I've
  handed you over” after the graph successfully entered it. Ordinary handoffs now
  inject an application-owned ownership status using the existing return-status
  mechanism. This avoids duplicating the caller's message or adding a request.

The extractive readback is a deliberate tradeoff: it can sound less polished when
STT fragments a sentence. It proves source text, not semantic completeness, and the
caller must still approve it. The new bounded guidance is less stylistically free
than ordinary model speech. Fresh audio is needed to judge its spoken quality.

## Actual checks

| Evidence | Result |
|---|---|
| `before/offline.txt` | Pre-edit offline baseline: 13 modules |
| `offline.txt`, `ruff.txt`, `format.txt`, `types.txt` | Current offline/style/type checks |
| `negative-controls.txt` | Removing source validation or allowing model speech fails the new regression checks |
| `typed-complaint-recovery-20260921T195052Z.jsonl` | 23/24; same-time move failed |
| `typed-complaint-continuations-20260921T195147Z.jsonl` | 15/15; initial complaint/phone cohort |
| `typed-complaint-final-recovery-20260921T195449Z.jsonl` | 23/24; failure protocol captured the post-handoff stall |
| `typed-complaint-final-20260921T195415Z.jsonl` | 24/24; adds policy, correction and abandonment cases |
| `typed-handoff-recovery-20260921T195627Z.jsonl` | **24/24** after handoff status |
| `typed-handoff-continuations-20260921T195631Z.jsonl` | **24/24** after handoff status |

All scenarios have three repetitions, Gemini 3.5 Flash Lite, priority tier,
MINIMAL thinking, fresh in-memory fixtures and a frozen Madrid clock. The latest
continuation suite includes three phone cases and 21 complaint cases. Scripted
prefix cancellations precede the real-model phone completion. The offline suite
separately runs Pipecat's actual interruption queue. No new dependency was added.

Negative checks inspect emitted speech frames, not just final output or business
state. They include an invented readback fact/remedy, plain-text refusal/repair,
a false free-service preamble beside a valid tool, phone fragments, verification
order, correction, abandonment and duplicate receipt protection.

`results.json` preserves **all six runs**, including failures. `snapshot.json`
identifies the final tested source, prompts, knowledge, lock and scenario scripts.
The knowledge JSON is included in process prompt provenance.

## Timing and comparison limits

The new **pre-repair** voice call had 13 caller turns, 16 attempts (12 answered,
three interrupted, one exhausted), and 43 model requests. Caller-to-bot server
markers: p50 **1.757s**, p90 **2.637s**, n=11. Request TTFT: p50 **0.628s**,
p90 **0.728s**, n=40. The model's first output remained quick; repeated routing
requests accounted for additional waiting and incorrect work.

The earlier Gemini 3.5 reference remains p50/p90 **1.154/1.922s**, and the four
17:36–17:40 voice calls remain **1.149/2.394s**. These are separate deduplicated
cohorts described in `../poc-status-20260921.md`, not matched experiments.

Latest transcript-run **full turn processing**, not caller audio latency:
recovery median/p90 **0.607/1.937s** over 45 turns and 67 requests; continuation
median/p90 **0.571/1.227s** over 72 non-scripted turns and 97 requests. Ordinary
median and nearest-rank p90 are labeled separately. Do not compare these values
directly with voice gaps or claim a latency win from them.

The new voice trace reported cache fields on 11/43 requests (43,834 reported
cached tokens within 160,472 canonical input tokens). Missing fields remain
unknown. Tool outputs were at most 1,504 characters, so output truncation is
still unsupported by the evidence. Client playout and first useful audio remain
unmeasured.

## Goal audit / remaining work

| Requirement | Current evidence / remaining condition |
|---|---|
| Baseline, SDK and effective-input contracts | Existing Phase 01 snapshots plus current request/schema checks. Final source identified by hashes; detached dirty checkout retained. |
| State, identity, diary and consent | Offline modules pass; identity-before-service and one authorized record covered. Contact confirmation still does not prove number ownership. |
| Caller fragments, cancellation and history | Existing core/adapter and native Pipecat queue regressions pass; phone replay 3/3. Fresh microphone acceptance remains open. |
| Complaint recording and restricted authority | 21/21 final complaint replays; code owns guidance, readback provenance and receipts. No free-redo suggestion path in complaint guidance. |
| Flow and shared budgets | Request snapshots include real handoff ownership; 24/24 recovery sample passes. Three repetitions cannot prove universal model reliability. |
| Speech safety | Complaint speech is constrained. Booking still uses the existing clause/claim detector; this does not prove the completion plan's broader arbitrary-false-speech prevention contract. |
| Measurement and responsiveness | Historical trace reconciliation retained. No matched native result or client-playout result yet. |
| Native reference requested by user | `slng-ai/unmute`, `examples/salon-concierge`, main **2ddc7b5c9f15e307a470ff9013200bea1a3000e9**. Verified against remote main. Unmute 0.5.6 compiled its Pipecat arm successfully in `/tmp/salon-native-reference`. Prompt/policy alignment and paired measurements are next. |

The original goal remains the full POC comparison, not just these passing tests.
The native example has both targets; the comparison will use **Pipecat**, with
simplified prompts. Its current defaults (3.1 model, Pipecat 1.10, endpointing,
retrieval tools, consent and complaint entry policy) must be aligned or explicitly
excluded before a matched claim. No shared checkout was modified and no bot was
restarted during this repair.

## Recheck

```sh
uv run python -m tests
uv run --env-file /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.env python -m bench.recovery --repeats 3
uv run --env-file /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.env python -m bench.continuations --repeats 3
```
