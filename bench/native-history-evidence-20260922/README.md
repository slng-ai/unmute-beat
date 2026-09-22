# Historical native Pipecat and the V1 case — 22 September 2026

**The combined evidence justifies continuing V1 development.** It supports a
shared conversation contract with application-owned authorization, validated
business results, explicit continuation and fewer unnecessary model decisions.
It does not establish that Pydantic AI is intrinsically faster than Pipecat,
that native Pipecat cannot implement those rules, or that the two-runtime
compiler proposal is already proven.

This review treats `UNMUTE_V1.md` and Phase 01 as proposals to assess, not
instructions to begin the other phases. Neither document nor the runtime was
modified. The evidence is an update to their product case.

## What the four historical calls contain

All four roots identify `concierge-salon-concierge-pipecat`. Their 207 observations
were retrieved in full: 54, 35, 54 and 64 respectively. They are two materially
different implementations/configurations, not one homogeneous baseline.

| Native call | Model label | Structure visible in tools | Observed business result |
|---|---|---|---|
| [21 September 12:19](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=787cfb6233eb03adda2fd399a02d23ec) | Gemini 3.1 Flash Lite | `book` flow; verification returns the booking node directly | Booking and move return success, but the move finishes before the requested readback receives a reply |
| [21 September 09:15](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=f1c4f999e516a7792dfdbed72fa8262d) | Gemini 3.1 Flash Lite | Same combined flow | Friday 15:00 booking follows readback and explicit approval |
| [16 September 14:51](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=b50d268c197cb9bb12693bccd76d487b) | GPT-5.6 Luna | Separate `verify_customer` and `manage_booking` tasks | Friday 09:00 booking, then Saturday 15:00 move, both with approval |
| [16 September 14:48](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=f24292e7ef17eb57497f11cadbb81821) | GPT-5.6 Luna | Separate tasks | Split phone retained; Thursday 09:00 booking and Friday 15:00 move report success |

These native records contain tool success responses and later diary reads, but
not the current POC's complete call-end business snapshot. They are not assigned
an equivalent four-call correctness score merely because the assistant announced
success. No historical call here exercises complaints.

## Measured performance

Values are seconds; nearest-rank quantiles, with observations pooled within each
named cohort only. No native and POC spans are mixed in one reader invocation.

| Cohort | Calls / caller turns | Caller→bot coverage | Caller→bot p50 / p90 | Actual model requests |
|---|---:|---:|---:|---:|
| Native, 16 September, GPT-5.6 Luna | 2 / 16 | 12 / 16 | 1.969 / 4.585 | 36; six lack turn ancestry |
| Native, 21 September, Gemini 3.1 | 2 / 12 | 10 / 12 | **1.076 / 1.769** | **26**, all resolved |
| Current typed POC, 22 September, Gemini 3.5 | 4 / 29 | 29 / 29 | **1.152 / 2.575** | **36**, all resolved |

Individual native call median/worst caller→bot measurements:

| Trace | Timed turns | Median / worst |
|---|---:|---:|
| 787cfb62 | 5 of 7 | 1.076 / 1.921 |
| f1c4f999 | 5 of 5 | 1.198 / 1.769 |
| b50d268c | 5 of 7 | 1.708 / 5.751 |
| f24292e7 | 7 of 9 | 2.892 / 4.585 |

The latest native Gemini sample is already responsive. Current median first-bot
latency is 76 ms higher and p90 is higher; these data do not demonstrate an audio
speed advantage over that implementation. The current POC is faster than the
older GPT sample on observed latency, but model, workflow and configuration
differences prevent attributing that improvement to the architecture.

The resolved native Gemini count is 26/12 = **2.17 requests per caller turn**,
versus 36/29 = **1.24** now: 43% fewer in these observed samples. The old sample
is booking-only and the current sample includes complaints and public questions,
so 43% is descriptive, not an isolated optimization effect. The GPT sample's
orphan requests remain explicit; apparent zero-request turns in that older trace
are not evidence of deterministic execution.

Native reported per-request TTFB over every request, including those without
turn ancestry, is 0.946 / 1.338s for GPT (n=36) and 0.550 / 0.692s for Gemini
(n=26). Current instrumented request TTFT is 0.615 / 0.782s (n=33 of 36).
These are different instrumentation paths; first-output type and interruption
coverage are incomplete in native. They do not suggest that individual model
inference became faster because of the typed core.

All calls report Soniox rt-v5 STT and Cartesia Sonic 3.5 TTS, which gives some
useful audio-stack continuity. Native model parameters differ, including a
4,096-token ceiling on the Gemini calls. Native source/prompt hashes, endpointing
configuration, tier, thinking settings and exact provider route are not established
by these exports. The newer fixed clock, validated diary, phone scope and
confirmation policies also differ. Missing timings are not assumed representative.
No symmetric cost or cache-saving claim follows.

## Concrete evidence for V1

### 1. Consent must be enforced by the application

The 12:19 native call contains this sequence, in UTC:

| Time | Observation |
|---|---|
| 12:20:13.309 | STT completes “Uh, the. 9:00 AM one.” |
| 12:20:14.141 | `save_booking` starts with `action=move`, `confirmed=true` |
| 12:20:14.143 | TTS receives “Shall I move your haircut appointment to Saturday at 9:00 AM?” |
| 12:20:17.331 | The tool span ends with `status=ok` |
| 12:20:18.883–19.415 | The caller's next utterance is “Yes, please.” |

The caller had chosen a time, but the native step's own instructions explicitly
require approval after its readback and distinguish selection from that approval.
The tool accepted the model-authored boolean before that reply. This is a failure
to enforce the implementation's declared consent policy, not evidence that the
caller never wanted an appointment. The exact database commit instant is not
visible, but the successful tool return is before the next utterance begins.

The current POC shows the complementary behavior: a premature move and a
punctuated-time booking attempt are refused, then complete after readback and
approval. Offline consent controls exercise forged approval, stale identity and
corrections as well. This strongly supports V1's core distinction: correctly
typed data is not sufficient authority to perform an action.

### 2. Known results need not make another model round trip

The historical booking saves are followed by a model request to voice the result.
For example, native morning `save_booking` starts at 09:16:39.674, followed by
a model request at 39.691 and confirmation TTS at 40.324.

In the current microphone sample, one booking approval and three complaint
approvals complete with **zero provider requests**, using the existing guarded
executors and retained receipts. Three reach the server bot-speaking marker in
0.371–0.480s; the fourth is delayed before agent startup. This directly demonstrates
the mechanism proposed in V1 §1.2 and §6. It is a removable decision, not an
assumption that every caller turn needs one fewer request.

### 3. The routing-hop claim needs an update

The September 16 traces demonstrate the older extra decision: a successful
`find_or_create_customer` is followed by another model request that selects
`manage_booking`. Those two routing requests take approximately 1.22 and 1.28s.

But both September 21 native traces return `manage_booking` directly from
`find_or_create_customer`. The native combined flow has already removed that
specific owner-routing decision. V1 should not claim this hop remains universal
in the current native implementation. Its benefit is a reusable declared
prerequisite/resume rule across workflows and adapters, rather than a special
salon sequence. The smaller native sequencing fix is demonstrably possible.

The latest native call still makes the same `find_slots` request twice for Friday
without an intervening caller correction. That is observed redundant model/tool
work, although the reader correctly does not label it a validation repair in
the absence of a refusal.

### 4. First acknowledgement can hide the useful-answer delay

In the two native Gemini calls, the final phone-confirmation STT precedes the
availability answer entering TTS by **4.615 and 4.946 seconds**. The early
“I'll check the diary” line makes the first-bot latency look much shorter.
For the current first call, the corresponding STT→availability-TTS interval is
**1.400 seconds**. These are illustrative trace intervals, not a matched audio
benchmark: they include different model decisions and speech scheduling and
do not measure client playback. They support measuring useful responses in
addition to first acknowledgements.

### 5. Preserve native strengths instead of claiming they are new

The September 16 native agent already retains a phone number spoken across two
STT fragments, and it accepts “Yes, go for it” on a booking. Current retention
and approval fixes restore necessary behavior; those abilities are not unique
advantages of typed flows. Pipecat continues to supply audio and interruption.

## Mapping to the supplied proposal

| Proposal / Phase 01 goal | Current support | What remains to establish |
|---|---|---|
| V1 §1.1, §2: valid values must also justify actions | Observed native premature save; current tracked approval refusals; candidate-state/service-result regression checks | Compiler-generated validation across supported authored schemas |
| V1 §1.2: remove avoidable decisions | Historical separate-task routing hop; current zero-request approved writes | Incremental benefit over already-combined native flows across broader scenarios |
| V1 §1.3, §6: own and inspect the model boundary | Actual request counts, typed outputs and deterministic receipts; interruption/history checks | Complete production observability and SDK/provider coverage |
| Phase 01 §2–6: state, diary, verification, flow and budgets | Implemented boundaries and 14 offline modules; current real microphone outcomes | Remaining adverse microphone cases and real external-service integration |
| Phase 01 §7: trustworthy measurements | Explicit turn ancestry, no STT/TTS request inflation, preserved missing values and failed work | Historical missing markers cannot be recreated; matched client audio remains open |
| Phase 01 §8: runtime-neutral loop | Shared core plus Pipecat adapter and equivalence tests | LiveKit adapter parity under the same cases |
| V1 §3, §5–6: one declaration on both runtimes | Feasible reference architecture and hand-written behavior | Compiler/emitter, authoring benefit and two-runtime parity are still proposed |
| V1 §6: better performance | Fewer provider decisions; responsive microphone sample; existing matched transcript results | Universal speedup, lower/equal audio p90, client playout and isolated framework overhead are unproven |

The [matched transcript study](../optimization-evidence/README.md) remains a
separate, stronger comparison for the measured implementation pair: POC 100/100
versus native 80/100; 240 versus 519 provider requests. Three scenario latency
gates pass and two remain unproven. Its native arm uses the documented shared
knowledge/business overlay; it is not an untouched historical build, and the
results do not isolate Pydantic from all the application changes.

## Recommended V1 wording and decision

> The Pipecat reference core now demonstrates validated business actions,
> application-enforced consent and continuation, and provider-free completion
> of known approved actions. Four recent microphone calls completed their
> intended outcomes, while historical native traces expose concrete prompt-only
> control failures and avoidable work. Typical first-response latency remains
> around one second, but an across-the-board audio latency advantage is not
> established. This evidence supports proceeding with the shared-core design;
> compiler generation and LiveKit parity remain the next proofs.

Update the proposal's opening status and §7 to include the completed Phase 01
implementation, current microphone evidence and matched transcript study. Update
§1.2 to distinguish the September 16 task layout from the already-improved
September 21 flow. Retain the proposal's caveat that native frameworks can host
the same controls, and its requirement that the shared core earn its maintenance
and authoring benefits.

**Decision:** proceed to the next V1 proof stage with a credible engineering and
efficiency case. Keep lower-or-equal latency as a measurable acceptance goal,
not as an already achieved general claim. These additional native calls make
the case more concrete; they do not require pretending every metric improved.

## Evidence and reproduction

[source.json.gz](source.json.gz) contains all observation identities/times,
selected metadata, usage, speech and tool evidence with phone numbers redacted.
The large returned node prompts/schemas are omitted and the node prompt hash is
retained. Raw retrieval hashes and completeness metadata identify the source.
[metrics.json](metrics.json) separates models and includes both turn-resolved
reader TTFT and all-request native reported TTFB, so orphan requests are visible.
The [current microphone archive](../microphone-evidence-20260922/README.md) is
referenced rather than copied or relabeled.

```bash
uv run --no-project \
  --python /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.venv/bin/python \
  python -m bench.native-history-evidence-20260922.analyse
```

The analysis asserts 207 unique observations, complete retrieval and the successful
native save ending before the caller's next utterance. It reuses the existing
trace reader. API credentials were loaded directly without being displayed.

Validation: analysis assertions passed; Ruff and ty both returned `All checks
passed!`; formatting and `git diff --check` passed. No runtime code changed.
