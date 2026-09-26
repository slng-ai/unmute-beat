# Session 4b, stage B: runtime repairs

Stage B repairs the runtime so that the evidence Stage A's evaluator needs now exists, and the guarantees behind it hold. Every business action is now an `Intent` that survives cancellation, a lost answer and a fresh process. A goodbye can recap only a receipt that still stands. A read-back counts as heard only when its own Twilio mark comes back before the caller starts to answer. Recovery is bounded. And the real `load_report` command passes on the runtime's own spans.

This report is kept up to date as the stage goes. The sections marked "open" are not finished.

## Checkpoint

| Item | Value |
|---|---|
| Parent | PR #20, branch `feat/session-4b-evaluator`, handoff `c07891afdaa4987d4d3cc91e1f054fc02fb805ea` |
| This branch | `feat/session-4b-runtime` (renamed from `worktree-session-4b-runtime`, which Claude Code created; the name was unused locally and on the remote) |
| PR #20 at start | open, head `c07891a`, base `feat/session-4-node-agents-cc` (checked 26 September 2026) |
| Tested source SHA | open |
| Stage | B1 to B5 implemented and offline-proven; text checkpoint and targeted Coval open |

## Effective configuration

Installed with `uv sync --locked --all-packages`. The lockfile did not change.

| Package | Version |
|---|---|
| pydantic | 2.13.5 |
| pydantic-ai-slim | 2.46.0 |
| pydantic-graph | 2.46.0 |
| pipecat-ai | 1.11.0 |
| langfuse | 4.15.4 |
| Python | 3.14.5 |

- Speech profile: `docs/research/reusable-core/session-3/speech-profile.v1.env`, unchanged. The values are `SLNG_BASE_URL=eu-west.api.slng.ai`, `SLNG_STT_MODEL=deepgram/nova:3`, `SLNG_STT_LANGUAGE=en`, `SLNG_TTS_MODEL=slng/fish/tts:s2.1-pro`, `SLNG_TTS_VOICE=16cabdb7f8d240569aff36c9e480d783` and `SLNG_TTS_LANGUAGE=en`.
- The root `.env` holds no speech-profile names, so `bot.py`'s `load_dotenv(override=True)` cannot override the profile. This was checked by listing the names only, never the values.
- The salon's model is `google-cloud:gemini-3.5-flash-lite` (`salon/agent.yaml`), unchanged.

## What changed, by stage

```mermaid
flowchart LR
    R[Read-back] -->|own mark back| H[Heard]
    H -->|reply began after it| Y[Yes binds]
    Y --> I[Intent dispatched]
    I -->|validated receipt| S[Succeeded]
    I -->|lost, cancelled, bad answer| U[Unknown]
    U -->|reconcile by intent| S
    S --> E[Effective receipt]
    E --> G[Goodbye recap]
```

1. A read-back is heard only when its own transport mark comes back and nothing cleared it (B3).
2. A yes binds only to a copy that played out before the reply began (B3).
3. Core makes the intent before anything is sent, and marks it dispatched before the await (B1).
4. A validated receipt settles it. Anything else after dispatch leaves it unknown (B1).
5. An unknown intent is reconciled by its id, and never sent again unless the backend dedups by that id (B1).
6. The latest successful change of each resource is the effective receipt (B2).
7. A goodbye can recap only an effective receipt, in the application's words (B2).

### B1: intent identity, cancellation, reconciliation

| Rule | Where |
|---|---|
| `Intent` (strict, frozen): opaque `uuid4` id, operation, canonical args JSON, subject, status, `readbacks` | `core/src/core/state.py` |
| Made when a read-back is authored (`FunctionOperation.proposal_for`), or on the first dispatch of an operation that needs no approval. Same operation, args and subject while open is the same intent. After it succeeds or is refused, the same args make a new one | `CallContext.intent_for` |
| Marked `dispatched` synchronously before `await _execute`. After dispatch, `CancelledError`, `TimeoutError`, a lost connection, a receipt that does not validate and a `matches` mismatch all leave it `unknown`. `CancelledError` is re-raised; nothing is shielded | `Operation.run`, `_lost` |
| Only a validated receipt settles it: `succeeded`, or `rejected` when it fails `succeeds` (an authoritative no) | `Operation._settled` |
| Reconciliation clears `unknown` only after the answer validates, matches the arguments and says an outcome. `None` is not a refusal | `FunctionOperation.reconcile_intent` |
| An unknown intent is sent again only as itself, and only when the backend dedups by its id (`idempotent: true`) or it is a read (`changes: false`). While one intent of an operation is unknown for a caller, a different one is refused | `Operation._may_dispatch` |
| A success whose recording fails (state projection, speech) is kept and recorded again from its receipt, never re-sent | `CallContext._unpresented`, `present_again` |
| A plain domain function runs in a worker thread (off the audio loop). A thread that commits after its awaiting task was cancelled leaves the intent unknown, and reconciliation finds the effect | `FunctionOperation._call`, test I4 |
| Optional durable record: a service's `note_intent(intent)` is awaited, bounded to 2 s, before dispatch; a failure means known not dispatched. `StepFlow.open(intents=...)` restores intents in a fresh process; an id reused for other args is refused | `Operation._note_intent`, `CallContext.restore` |
| After the caller said goodbye, no new change starts; reconciliation still runs | `CallContext.note_departure` |

Manifest keys, all checked by the loader: `changes`, `subject` (must be a receipt field), `idempotent` (requires the `Intent` parameter), `recap`. A function or `reconcile` may take a third parameter, which must be annotated `core.Intent`.

The salon's diary token is now the intent id. The per-call `writes` counter, which a fresh process could not rebuild, is gone.

**Limit.** The salon's `Diary` is in memory. Its intents last only as long as the process. Exactly-once holds only against a backend that dedups by the intent id. The support desk's SQLite backend shows the durable pattern. Stage C owns shared, durable capacity.

### B2: receipt-backed completion speech

- **Effective receipts.** `CallContext.effective()` keeps each resource's latest successful change (`subject:`), for the caller on the line. A move replaces the booking it moved, a cancellation replaces the booking, and a reopening replaces the resolution. A lookup (`changes: false`) and a validated refusal never stand.
- **Goodbye.** The goodbye has no model words: `goodbye(recap: list[str])`. Core says, in order:
  1. each recap, only for a reference listed under "Can recap" in the step context, rendered by the operation's `recap:` template
  2. `Locale.unsettled` if any intent is unknown
  3. `Locale.left_undone` if the last read-back's intent was never dispatched
  4. the locale's goodbye

  A reference nobody issued is refused. A goodbye beside a waiting read-back is sent back once, so a yes is carried out first.
- **Claim words.** Core's `Locale.claims` is now empty, and `recounts` is removed. The salon carries its English verbs unchanged in `locale: {base: english, claims: [...]}`. The desk carries its own Spanish ones.
- **Correction lines.** "Nothing has been changed yet" (`withheld`, `unbacked`) is never said while an intent is unknown. The unsettled line is said instead (`CallContext.correction`).
- **Receipt replay.** A cut receipt is said again only while its receipt stands. Replaying it never re-sends.
- **Other domain words.** "is supplied by the salon" (`policies.py`) became "the application". The goodbye instruction naming bookings is gone with the words.
- **Confirm steps.** `ConfirmStep.answer` now uses the application's claim check too.

**Every route to TTS**

| Route | What decides the words | Guarantee |
|---|---|---|
| Read-back (`Prompted`) | the application's `readback` function | structural |
| Receipt (`say:`) and its replay | the application's template on the validated receipt; replayed only while it stands | structural |
| Goodbye | recaps (`recap:` on an effective receipt) and fixed locale lines | structural |
| Fixed lines: holding, `unsettled`, `gave_up`, `left_undone`, stumble, exhausted, reminder, idle goodbye, `asks:` | core's locale or the manifest | structural |
| `End` step `say:` | the application's template on the facts | structural |
| Streamed model text on a `guarded` step | the model, one clause at a time, checked against the application's claim words | best effort: a semantic behaviour to evaluate |
| Streamed model text on other steps, `ConfirmStep.answer`, `Handoff.reply` | the model; the last two claim-checked | semantic behaviour to evaluate |

Delivery is measured on Twilio marks. Clause gating buffers only what it did before this stage.

### B3: segment delivery and answer ordering

Verified in the installed source:

- `TTSService` sends every non-system downstream frame through its serialization queue (`pipecat/services/tts_service.py:904-911`). So a frame pushed after a `TTSSpeakFrame` comes out only after that utterance's Started, Audio and Stopped frames (`_audio_context_task_handler`).
- The output transport writes a data frame from its audio queue, after the audio chunks ahead of it, and flushes the tail on `TTSStoppedFrame` (`pipecat/transports/base_output.py:396, 657-678, 841`).
- Pipecat's `TwilioFrameSerializer` sends no marks and drops inbound `mark` events (`pipecat/serializers/twilio.py:276-314`).

What core does (no Pipecat):

- **Mark names.** `mark_for(segment)` names a mark `<segment id>~<clears so far>`.
- **Delivered.** A segment is delivered only when its own mark comes back (`note_mark_returned`), with no clear sent while it was pending (`note_cleared`, called before the clear goes out). The segment must also have had media before the mark (`note_mark_sent(media=0)` means silent), and synthesis must not have failed (`note_speech_failed`).
- **Ignored marks.** Duplicate, unrelated and stale marks change nothing. A clear does not unplay a segment whose mark already came back.
- **Answered in time.** A question is answerable only if its latest copy played out before the earliest caller onset after it (`answered_in_time`). The onset is the voice detector's report less its own `start_secs`. Both times are this process's `time.perf_counter`, taken where each report arrives. With marks and no onset, the answer's start is unknown and it does not count.
- **Diagnostics only.** `BotStoppedSpeakingFrame` and the text-length estimate decide turn-taking only.
- **Text runs.** A text acknowledgement is simulated delivery. It counts for a text run and is recorded apart (`simulated_delivery`), never as playback.

What the adapter does (`core/src/core/processor.py`):

- `SegmentMark` (an `OutputTransportMessageFrame`) goes right after each fixed line, and after each model segment when it closes. For streamed model text the TTS holds it until the response's audio is out, so the mark is late, never early.
- `TwilioMarks` wraps the runner's serializer. It sends `{"event":"mark",...}` out, reads `mark` and `stop` back, counts media chunks between marks, and records a clear before it is sent.
- `LocalPlayout` serves a development transport. It records `local`, never a telephone mark.
- `playout(transport, call)` wires one of the two into a bot. Both bots use it.

### B4: bounded recovery and cleanup

- **The `C26pPHK6` family.** Each read-back copy the caller did not hear in full before answering counts once against its intent. The count survives joined pieces and reissued questions. After `READBACKS = 2`, the caller hears `Locale.gave_up` instead of a third read-back. The early yes never books. A goodbye after it says `left_undone`.
- **Joined turns.** A turn joined over pieces spends one request tally (`CallContext._turn_usage`).
- **Holding timer.** The holding-line timer is cancelled and awaited, bounded. The old code already cancelled it; this is hardening, not a repaired defect.
- **Call ceiling.** `core.processor.Ceiling` is started on connect and stopped by its owner in a `finally` around the pipeline. The old timer was cancelled only on a client disconnect.
- **Ending facts.** `note_ending` records each fact apart: `intent_to_close` (not observed), `ceiling`, `media_disconnect`, `pipeline_end`, and Twilio `stop` (`media_stop`, observed). `record_ending` emits a `call.ending` span. Carrier closure is `unknown` without a carrier report. An `EndFrame` or a failed REST hang-up is not one.
- **Native build.** Its unbounded model wait is untouched and remains a baseline limit.

### B5: audits the evaluator can read

The `write.audit` gains these fields: `intent` (also `operation`), `proposal_args`, `needs_approval`, `changes`, `call`, `generation`, `sequence`, `playback` and `simulated_delivery`.

- **`proposal_args`** are taken from the question's `value`, recorded when it was asked. They are never copied from the executed arguments.
- **`playback`** is in the evaluator's `Playback` shape. Each mark is `played` or `cleared` (a silent or failed segment counts as `cleared`), with `answered_at`, all in UTC wall time from one monotonic origin per call.
- **Reconciliation** is a `write` audit marked `reconciled`, under the same intent.

The evaluator has one narrow extension. A reconciliation settles only an earlier unknown write of the same action and intent, and is judged through that write. A `reconciled` flag with no such write excuses nothing (regression `a_reconciliation_settles_only_its_own_intent`).

`use-cases/salon-graph/tests/test_audit_export.py` does the end-to-end check:

1. It drives a salon booking through the real processor, a Pipecat TTS base, `FastAPIWebsocketTransport` and `TwilioFrameSerializer` (wired by `playout`), over a fake phone.
2. It exports every emitted span with the OpenTelemetry in-memory exporter.
3. It runs the real `load_report.py --saved ... --evidence ...` in concierge-tools' folder.

| Export | Exit |
|---|---:|
| complete evidence | 0 |
| `proposal_args` removed | 3 |
| a mark after the answer began | 1 |
| `playback` removed | 3 |
| marks bound to another proposal | 3 |

The evidence file holds the case, a review of the caller's words and the agent's claims, and the ending. The test writes it from its own script. The runtime's consent check is not the judge. The ticket desk's audit is the same envelope, and names its own backend effect (R10).

## Generic versus application

| Core owns | The application owns |
|---|---|
| Intent identity, status, restore, the retry guard | Whether its backend dedups by the intent (`idempotent`), its durable intent record (`note_intent`), its tables |
| Proposal serials, question copies, delivery by mark, answer timing | The read-back words (`readback`) |
| Effective receipts by declared resource | Which receipt field is the resource (`subject`), what success looks like (`succeeds`, `matches`) |
| Goodbye composition, fixed-line routing | Receipt and recap words (`say`, `recap`), claim verbs (`locale.claims`), the goodbye line |
| Bounded recovery (`READBACKS`), turn budgets, the ceiling timer's lifecycle | The call ceiling's length, the domain validation, the diary's calendar rules |

The domain audit of `core/src/core` found these executable assumptions, all repaired:

- `_unbacked_goodbye` and its any-receipt exemption, now removed;
- the goodbye instruction naming bookings, now removed;
- `Locale._CLAIMS_EN`, `_CLAIMS_ES` and `recounts`, now the application's;
- `policies.py`'s "supplied by the salon".

Docstring examples naming bookings remain as examples only.

## Reuse: three applications, one engine

- **Clinic** (`core/tests/clinic_flow/`). It gains `subject` and `changes`. `test_intents` (I1 to I10) runs the generic rules on a declared operation.
- **Salon.** Every stage's tests run on it.
- **Support desk** (`core/tests/ticket_flow/`, `test_tickets` R1 to R10). It has an account, a ticket and a revision, with no phone, date, appointment or slot. It speaks Spanish, with its own claims, receipts, recaps and goodbye. It has an informational `faq` step with no identity and no write. Its backend is stdlib SQLite with WAL and `BEGIN IMMEDIATE`: the intents and effects commit in one transaction, keyed by intent id. The checks cover:
  - a stale revision, which is an authoritative refusal;
  - an answer about another ticket, and a lost answer, both reconciled by intent;
  - resolve then reopen, where only the reopen can be recapped;
  - a withheld claim after a lookup;
  - a fresh process that reconciles after the worker was killed with `os._exit`, after its commit (found, not re-sent) and before it (sent again as the same intent, once);
  - every field and operation renamed with core untouched (R8).

## Before and after

`python -m tests.defects` at the parent (`defects-before.txt`) and now (`defects-after.txt`):

```
before: KNOWN DEFECT (session 4b stage B1): a write cancelled after its commit is sent again
        KNOWN DEFECT (session 4b stage B1): an invalid reconciliation answer clears the retry guard
        KNOWN DEFECT (session 4b stage B2): a lookup receipt backs a goodbye that claims a booking
after:  fixed: (all three)
```

Each repair was then turned off, one at a time, in a scratch copy of HEAD (`git archive`, so the worktree was untouched). Its regression failed every time (`session-4b-runtime/mutations.md`, harness `mutations.py`):

| Repair turned off | Regression | With | Without |
|---|---|---:|---:|
| B1 a cancellation after dispatch is recorded as a refusal | core `test_intents` | 0 | 1 |
| B1 an invalid reconciliation answer clears the guard | core `test_intents` | 0 | 1 |
| B1 a read is recorded as an intent to reconcile | core `test_tickets` | 0 | 1 |
| B2 a later change does not supersede an earlier receipt | core `test_tickets` | 0 | 1 |
| B2 a lookup counts as a standing change | core `test_tickets` | 0 | 1 |
| B2 a withheld clause cuts the step's previous fixed line | core `test_tickets` | 0 | 1 |
| B3 the aggregate stop signal delivers | core `test_delivery` | 0 | 1 |
| B3 a clear does not void pending marks | salon `test_marks` | 0 | 1 |
| B3 the reply may begin before the mark | salon `test_marks` | 0 | 1 |
| B3 a mark with no media before it delivers | core `test_delivery` | 0 | 1 |
| B4 read-backs are not bounded | salon `test_recovery` | 0 | 1 |
| B4 each joined piece gets a fresh budget | salon `test_recovery` | 0 | 1 |
| B4 the ceiling owner does not cancel the timer | salon `test_ceiling` | 0 | 1 |
| B5 a reconciled flag alone excuses a write | concierge-tools `test_load_report` | 0 | 1 |

Two regressions did not tell the difference at first, and were fixed.

- **M6's late caller.** It spoke less than the VAD window after the read-back was queued, so the "no onset after the copy" rule decided it. It now reaches the timing comparison.
- **M3's silent segment.** It was also caught by Pipecat's own zero-audio error. It now has a direct core check.

Faults found on the way, each repaired with a regression:

- a withheld clause cut the step's previous fixed line, so the receipt was said again next turn (R6);
- a read was recorded as an intent to reconcile (R7);
- reconciliation in a fresh session looked for the intent under the caller now on the line (R7);
- the walkthrough test wrote into session 4's historical `booking-context.md`. It now writes this stage's copy, with the random recap references replaced by `<ref>`.

## Verification

Open. Final commands and results are filled in at the end.

## Performance

All numbers come from one machine (Apple silicon, macOS), with the same scripted models, cases and warmups.

**N12, the frozen timing gate.** This is `python -m matrix time`: 3 warmups, 20 repetitions, delays 0 and 0.05 s. The candidate and the parent `c07891a` ran back to back with nothing else running (`session-4b-runtime/timing/candidate.json` and `parent-c07891a.json`). The rule is native + max(5 ms, 25%) per turn and for the trace, with a 50 ms ceiling on any harness turn.

| Delay 0 | Turn 1 | Turn 2 | Turn 3 | Turn 4 | Turn 5 | Trace | Largest harness turn |
|---|---:|---:|---:|---:|---:|---:|---:|
| native | 7.0 | 8.2 | 30.4 | 6.3 | 6.7 | 58.5 | - |
| harness, candidate | 11.9 | 17.7 | 46.0 | 17.1 | 16.3 | 107.3 | 48.8 |
| harness, parent | 12.6 | 17.2 | 45.7 | 16.7 | 15.7 | 107.0 | 47.6 |

All values are milliseconds, the median of the last output.

- The verdict is **failed at both commits**, for the same reason. Turns 2 to 5 and the trace are outside tolerance. The 50 ms ceiling holds.
- Stage B moved the trace by 0.3 ms. Model requests are identical: 7 agent, 0 classifier.
- The gap to native, about 10 ms per turn, was profiled with `cProfile` over the same trace. It sits in the event loop's scheduling of the Pipecat pipeline and Pydantic AI's run machinery, not in core's own code. That profile's harness CPU is about 4 to 8 ms per turn.
- **N12 remains an unresolved gate**, as it was before Stage B. Closing it needs a change in how a turn is scheduled, not a repair in this stage. The thresholds were not changed.

**Core overhead** (`session-4b-runtime/bench.py`, run from each tree's `use-cases/salon-graph`, 20 runs after 3 warmups; `timing/bench-*.json`):

| | Parent `c07891a` | Candidate |
|---|---:|---:|
| Cold: interpreter start to compiled salon flow | 0.498 s | 0.499 s |
| Turn 1 (collect, options): wall / CPU | 10.06 / 7.72 ms | 9.97 / 7.64 ms |
| Turn 2 (the read-back): wall / CPU | 4.84 / 3.75 ms | 5.23 / 4.02 ms |
| Turn 3 (the booking): wall / CPU | 5.28 / 4.13 ms | 5.43 / 4.27 ms |
| Model requests per turn | 3, 1, 1 | 3, 1, 1 |
| Instructions per request | 6,510 / 6,484 / 6,631 B | same |
| History on the booking turn | 38,165 B | 38,567 B (+402 B) |

**Zero added model calls.**

- There is no consent classifier, completion judge, retry planner or summarizer.
- Reconciliation and receipt replay run with no model (`LocalAction`).
- A goodbye beside a waiting read-back costs one retry, and only in that case.
- A recap given as its words instead of its reference now costs none (seen once in text-1 T2, repaired).
- A plain domain function now runs in a worker thread, which keeps a database or HTTP call off the audio loop. That costs a thread hop per write, under 0.4 ms here.

**Not measured in Stage B.** These need a live voice run or Stage C: endpointing, STT, TTS, playback acknowledgement latency on a real network, time to useful speech, and interruption stop latency. The targeted Coval call reports what it can below. A narrow cohort cannot establish the 2.7 s p90 target.

## Live checks

Open.

## Budgets

These live in `session-4b-runtime/budget.json`. Every billable action updates that file.

| Pool | Allowed | Spent |
|---|---:|---:|
| Text model requests | 100 | 0 |
| Text tokens | 250,000 | 0 |
| Coval repair calls | 20 | 0 |

Reserved for later stages, untouched: 20 final normal, 20 final interruptive, 10 native normal and 12 native interruptive calls.

## Coval resources checked (26 September 2026, read-only)

| Resource | ID | Content |
|---|---|---|
| Candidate agent | `JxGgSPA55DcFcDcvbKAUQM` | exists |
| Stress set | `K3Xwqi7g` | one case, `GRdmreeb7SP4G8yegtMntC`: the number in two pieces, then Friday changed to Saturday at the booking read-back (content digest `d65c8691d89fcdcf`) |
| Stress persona | `VSruLZYYyz3w7fkdUaheDR` | Interruptive Speaker, `interruption_rate: HIGH`, voice `john` (digest `f62a73e16243e27a`) |
| Normal set | `kQ8b6nMZ` | one case, `M5Z8LjVyQZPuhSubmt26w7` (digest `8aab485fa62115f7`) |
| Normal persona | `MUFJdYAHdBHU6UbRRu4ykM` | Standard Customer, `interruption_rate: NONE` (digest `8f33bd6f967ed2e2`) |

Both sets hold exactly one case, so `coval_sim`'s whole-set launch places one call per iteration. Coval CLI 0.7.3 supports `--test-cases`, but it is not needed here. `coval runs launch --help` was read with `COVAL_API_KEY` unset.
