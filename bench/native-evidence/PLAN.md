# Matched transcript comparison — declared before collection

The POC is the target contract. The user-selected reference is unmute's
salon-concierge at `2ddc7b5c9f15e307a470ff9013200bea1a3000e9`, compiled to
native Pipecat Workers/Flows with simplified prompts. `bench.native_reference`
reproduces the isolated overlay; the source repository is untouched.

## Inputs held equal

- Exact `knowledge/services.md` and `knowledge/complaint-guidance.json` contents.
  Neither complaint arm receives the contradictory free-redo recommendation in
  the original refunds document. No retrieval or embeddings in either arm.
- The POC's actual diary, slot grid/capacity, date resolver, phone validator,
  customer identity, complaint records and business receipts. Native tools adapt
  their existing wire interface to these services. No second diary fixture.
- Public questions without verification; identification before personal work;
  phone readback and approval; booking on clear selection; cancellation readback;
  complaint readback/approval before recording; no offered or booked free redos.
  The native implementation retains its model-authored consent flags and speech.
  The POC retains its application-owned approval and typed output functions.
- Same Python environment, Pipecat 1.11.0, Gemini 3.5 Flash Lite, MINIMAL thinking,
  Vertex Express Mode with priority spillover header, no region pin. The native
  compiler targets 1.10.0; qualification exercises its output on 1.11.0.
- Fresh fixture per call; clock 2026-09-21 10:00 Europe/Madrid; synthetic Spanish
  number. The move fixture holds one Friday 15:00 haircut in both arms.
- The exact same scripted caller utterances from `bench.native_compare.SCENARIOS`.
  Fixed replies do not adapt to an erroneous agent question; that is a failure.

Existing `google.api_core` support is read from the already-installed native
environment **after** the POC's packages on `sys.path`. No packages are installed
or modified. Record the actual module origins and versions in the snapshot.

The generated audio configuration is also aligned to the POC: same SLNG STT/TTS,
voice, region, token streaming, VAD 0.2s, Smart Turn fallback 1.5s, and no greeting
mute. **This experiment does not execute audio.** No conclusion about those
settings, endpointing, transport output or client playout follows from text runs.

## Sample and order

Twenty paired calls for **each** of five scenarios: public hours, verified
complaint, complaint through verification, new booking through verification,
and an existing same-time move. Total: 100 pairs / 200 calls. POC/native order
alternates by repetition. Calls run sequentially, never concurrently. Startup
imports and fixture setup are outside turn timing. Retain every failed call,
timeout and emitted text; do not tune prompts during this cohort.

Prior diagnostic qualifications are separate and excluded. They exposed missing
phone readback and unavailable-offer behavior in the native arm. The score now
checks phone readback as well as writes, dates/times and receipts. Automated
checks are bounded, not a complete semantic judge; review the saved speech.

## Decision rule and metrics

Report correctness per scenario out of all 20 runs in each arm. Report latency
only with its successful-pair denominator, alongside failures. Use ordinary
median and nearest-rank p90 for full transcript-turn duration, first speech
submission (including filler) and per-request first output. Report requests per
turn. First useful audio, caller gap and endpointing errors are **not measured**.

Resample matched calls, keeping each call's turns together, 10,000 times with a
fixed random seed. Report percentile 95% intervals for POC-minus-native p50/p90
full-turn differences. A zero-regression latency claim requires both upper
bounds <= 0 and no worse observed correctness; uncertainty crossing zero means
inconclusive. This decision applies only to transcript processing, never voice
responsiveness or full POC acceptance. No larger follow-up is authorized by a
bad result alone; declare a new sample before any additional measurement.

Freeze source/configuration hashes before the first measured call and verify
them after collection. Changes invalidate pooling; keep separate cohorts.
