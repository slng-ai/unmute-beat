# Microphone review — 22 September 2026

The four supplied calls completed their intended business flows. Their saved
outcomes reconcile with the spoken confirmations: two new bookings, one move,
and three complaints, six writes in total. No duplicate writes, unresolved
business operations, exhausted runs, or apology-only complaint stalls were
observed. This is fresh microphone evidence for the optimized implementation,
not another transcript simulation.

The typical response remains approximately 1.15 seconds. The model is invoked
less often, and approved writes can complete without it. Tail latency has not
improved: p90 is 2.575 seconds, compared with 1.922 seconds in the earlier
Gemini 3.5 voice cohort. These are different conversations, not a controlled
experiment establishing a regression or improvement caused by one change.

## Outcomes and per-call measurements

All latency values below are seconds. Caller→bot is Pipecat's server-observed
latency from estimated user silence to `BotStartedSpeakingFrame`. It can end
on an acknowledgement or filler; it is not useful-answer completion or browser
playout. Quantiles use the existing nearest-rank reader.

| Trace | Reconciled outcome | Caller turns / model requests | Caller→bot median / worst | Request TTFT median |
|---|---|---:|---:|---:|
| [c19e1dfb](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=c19e1dfbff9b325d571bf0699c808a75) | Haircut booked Friday 10:00, moved to Monday 28th 10:00; complaint recorded, free-service request pending review | 10 / 16 | 1.152 / 3.393 | 0.654 |
| [f31fc563](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=f31fc563718f62c861a075f89992fbea) | Complaint initiated before verification; identity confirmed first, approved complaint recorded once | 5 / 4 | 1.779 / 2.575 | 0.541 |
| [b5721395](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=b5721395ca33a0bb31d7b5ce0f8e5b67) | Public pricing answered before verification; hair colour booked at 11:30 after approval | 8 / 10 | 1.081 / 2.193 | 0.683 |
| [2fe8f869](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=2fe8f86995edfbc1a437c3d2a16879b5) | Complaint initiated before verification; refund request recorded for manager review, no refund promised | 6 / 6 | 1.056 / 2.597 | 0.593 |

The pricing answers match the knowledge base: haircut €28, colour €75,
highlights €95 and roots €45. Every complaint has a readback, caller approval,
and one receipt. Every call has `unresolved=null` and ends after the caller
says they are finished. Each fresh fixture restarts identifiers, so CX0001 in
three separate calls is expected. The fixed salon clock is 21 September at
10:00 Madrid time; the “today” booking is therefore on the 21st by design.

There are **29 answered Pipecat caller turns**, **32 agent attempts**, and
**36 provider requests**, including three interrupted requests. All provider
requests have resolved turn ancestry. All 29 turns have caller→bot timing;
33 requests have TTFT and usage. Missing measurements on the interrupted
requests are not zeroes and are not filled in.

Three continuations restart successfully: the change from “the day after” to
Monday, “Uh” followed by “11. 30.”, and the greeting followed by the complaint.
The corrected Monday and selected 11:30 survive into the final business records.
This supports the interruption/history repair on real microphone input. It does
not specifically retest the earlier split-phone case: all four calls use the
same prefetched, seeded identity.

## Latency and efficiency

Historical rows are deduplicated cohorts from the existing
[measurement archive](../pipecat-repair-evidence/metrics.json). Different
models and implementation stages remain separate.

| Cohort | Caller→bot p50 / p90 (n) | Request TTFT p50 / p90 (n) | Requests / caller turn |
|---|---:|---:|---:|
| Earliest Gemini 3.1, 21 September | 1.456 / 2.773 (26) | 0.781 / 1.097 (45) | 49 / 29 = 1.69 |
| Earlier Gemini 3.5, 12:57–13:02 | 1.154 / 1.922 (29) | 0.626 / 0.764 (50) | 53 / 30 = 1.77 |
| Gemini 3.5, 17:36–17:40 | 1.149 / 2.394 (26) | 0.607 / 0.765 (47) | 56 / 30 = 1.87 |
| These four optimized Gemini 3.5 calls | **1.152 / 2.575 (29)** | **0.615 / 0.782 (33)** | **36 / 29 = 1.24** |

The median is effectively unchanged, not meaningfully faster. The current p90
is 34% above the earlier 3.5 sample and 7.5% above the 17:36 sample. With four
calls of different content, no statistical or causal parity claim follows.
Current requests per caller turn are about 30% below the earlier 3.5 sample;
different call composition contributes, so this percentage does not isolate
the optimization's effect.

The direct evidence for typed-flow savings is stronger than that historical
percentage: **four approval turns used zero model requests**, one booking and
three complaints. Their caller→bot times are 0.371, 0.413, 0.480 and 1.779 seconds.
The last includes a 1.397-second wait before the agent starts; the application
step itself emits its confirmation within 1 ms at recorded precision. The audit
also records three application-owned phone confirmations followed by model work
in the receiving step. These receipts are retained without fictitious model calls.

Other measured values:

- Agent-start→first submitted text: p50 0.683s, p90 1.025s, n=29. Includes filler
  and application confirmations; not client audio.
- TTS service TTFB: p50 0.126s, p90 0.204s, n=38 of 42 TTS observations, including
  greetings. STT service TTFB: p50 0.290s, p90 0.323s, n=36. STT TTFB is not final
  transcription latency, and these service distributions are not additive.
- Available canonical usage: 64,002 input tokens and 1,021 output tokens across
  33 requests. Only one request explicitly reports caching: 3,081 tokens. Missing
  cache fields are unknown; no cache-rate or total-cost conclusion is justified.
- 28 non-null tool outputs: median 85 bytes, p90 1,367, maximum 1,595. Two refused
  tools have null outputs, not zero-byte successful returns. These sizes provide
  no evidence that tool-output truncation or summarization is needed.

## What remains imperfect

**Turn-taking contributes materially to the slow tail.** Six of 29 turns wait
1.390–1.447 seconds between the final STT span ending and the answering agent
attempt starting; the other 23 wait 1–2 ms. Five of the six longest responses
have this gap. It is before Pydantic model execution and consistent with the
configured 1.5-second Smart Turn fallback. The traces do not record the analyzer's
decision, so attribution to that specific fallback is an inference, not a
measured decision. The slowest response combines a 1.390-second pre-agent wait
and 1.547 seconds to first text through complaint routing. This is the next
latency area to investigate; reducing the timeout blindly could reintroduce
premature turns.

**Two tool refusals recovered normally.** The model tried `save_booking` before
the application accepted consent: one move and the punctuated “11. 30.” slot
selection. The stored retry says nothing was saved and requires `ask_booking`.
Both runs then read back and complete after approval. These are guardrail
refusals shown as ERROR tool spans, not failed calls or provider outages. They
cost model work, and the time-format case adds a clarification turn. Longer
approval phrases (“That's right, great job”, “Yes, please, go ahead”) also use
the model fallback successfully instead of the narrower zero-request path.

**Minor conversational roughness remains.** The first complaint asks for details
already supplied, and its exact-word readback preserves the STT error “free rado”.
The colour offer says “on today”. None changes the verified result, but four
successful calls should not be described as perfect speech quality.

## POC conclusion

The tested microphone flows now support functional acceptance for public pricing,
verification-first booking/complaints, booking creation, same-time moves, complaint
approval/recording and the observed interrupted continuations. This is a material
advance beyond the previous text-only evidence.

The justification for typed flows is now visible in real calls: validated writes
and receipts, guarded consent, successful continuation, and removal of unnecessary
provider decisions on known approvals. The individual model-request latency is
approximately unchanged. Pipecat remains responsible for audio and turn-taking.

Overall latency parity with native Pipecat is still not closed. The existing
[matched transcript comparison](../optimization-evidence/README.md) remains the
evidence for native comparison: 100/100 POC versus 80/100 native outcomes, with
three scenario latency gates passed and two unproven. These four calls add no
matched native audio arm or measured browser playout. Split-phone, corrected or
withheld identity, cancellation, abandonment and interruption during a write
remain outside this particular microphone sample.

## Reproduction and provenance

Retrieved all 244 observations (93 / 38 / 66 / 47), one complete CLI page per
trace, no truncation. Reconciled each conversation root's session ID with the
four `bench/raw/live-20260922*.json` files. All calls share source hash
`23159b7732056e0550eb629776f5cddade33791906fd691d8f1459795f3e55c3`, matching the
current runtime files, and prompt hash `f4467e62…`. Runtime: Pydantic AI 2.46.0,
Gemini 3.5 Flash Lite, priority/MINIMAL; Soniox rt-v5 STT and Cartesia Sonic 3.5
TTS. Provider region is unpinned. No runtime or prompt changes were made for
this review; API credentials were loaded directly without being displayed.

[source.json.gz](source.json.gz) retains selected observation fields, timings,
usage, tool-output lengths, call outcomes, transcripts with the prefetched phone
redacted, and hashes of the raw retrievals and call records. Provider prompts and
credentials are omitted. Empty OTel arrays returned as `{"arrayValue": {}}` are
normalized to `[]` for the two request-metric fields: otherwise the old reader
mistakes the object key for a first-output category. This normalization changes
neither timing nor request counts. Interrupted attempts are counted using their
`status=interrupted`; the separate boolean is absent on those cancelled spans.

Rebuild [metrics.json](metrics.json) from the retained source:

```bash
uv run --no-project \
  --python /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.venv/bin/python \
  python -m bench.microphone-evidence-20260922.analyse
```

The analysis uses the existing tested trace reader and asserts unique observation
counts, complete retrieval, all 29 answered turns, 36 requests, four zero-model
turns, 33 TTFT measurements and six reconciled writes.

Review validation: analysis assertions passed; `all 14 modules ok`; Ruff and ty
both returned `All checks passed!`; analysis formatting and `git diff --check`
passed. No live model replay was needed for this read-only trace review.
