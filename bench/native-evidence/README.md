# Native reference alignment and verification repair

The POC defines the comparison contract. The selected reference is
[unmute salon-concierge](https://github.com/slng-ai/unmute/tree/2ddc7b5c9f15e307a470ff9013200bea1a3000e9/examples/salon-concierge),
compiled as **native Pipecat Workers/Flows**, with a local overlay. The original
repository, installed environments and running voice server were not modified.

**Latest scope:** preserve the POC and native reference; compare the closest
equivalent business scenarios and identify the typed flow's actual gains.
The frozen cohort below uses different model-visible tool schemas, so it is an
implementation comparison, not an isolation of framework overhead. The
[adoption assessment](ADOPTION.md) distinguishes existing native capabilities,
POC improvements and remaining evidence gaps. A subsequent shared-tool experiment
was removed from active source and is excluded from this comparison.
The subsequent booking-tool experiment was reverted and its results are excluded
([record](../booking-output-evidence/README.md)).
The [tool contract audit](TOOL_CONTRACT_AUDIT.md) retains the concrete schema and
behavior differences; only two tool names overlap, with different arguments.

## What is aligned

| Dimension | Native comparison arm |
|---|---|
| Public information | Exact POC `knowledge/services.md` in the concierge prompt |
| Complaint guidance | Exact `knowledge/complaint-guidance.json`; no free-redo recommendation |
| Business data | Imports the actual POC diary and phone validator through a small native wire adapter |
| Availability | Same hours, durations, grid, capacity, date resolution, time preferences and offer checks |
| Policy | Public questions stay public; verify before booking/complaints; phone readback and approval; complaint approval before recording; preserve bookings; no free-redo offer/write |
| Consent implementation | Native retains model-authored flags/speech; POC owns approval and typed outputs. This is an intended architecture difference, not a second policy. |
| Model | Gemini 3.5 Flash Lite, MINIMAL thinking, priority spillover header, Vertex Express Mode, region unpinned |
| Generation defaults | No explicit output cap, temperature, top-p, top-k or seed in either arm |
| Runtime | Shared installed Python 3.14 / Pipecat 1.11; compiler emits a 1.10 target, exercised on 1.11 |
| Call fixture | Same synthetic number, fresh diary and frozen Madrid clock; identical caller scripts |
| Audio configuration | Same SLNG STT/TTS model, voice and region; token streaming; VAD 0.2s; Smart Turn fallback 1.5s; no greeting mute |

`bench.native_reference` compiles the overlay. `config-check.txt` inspects the
**effective** native generation parameters, including framework defaults.
`adapter-check.txt` tests shared phone/availability/write/receipt behavior.
The missing Google support package is read from the already-installed native
environment after the POC's packages; nothing was installed. Module origins and
versions are recorded in the snapshots.

The transcript runner executes the native WorkerRunner, bus, LLM service and
FlowManager. It replaces audio with a text sink and injects scripted caller
messages. The POC arm uses its existing processor/core. These checks cannot
measure endpointing, caller audio gaps, useful audio or client playout.

## The stopped diagnostic cohort

The first cohort was stopped when effective-settings inspection found native
Pipecat's implicit **4,096-token output cap**, while the POC left it unset.
There are 126 completed calls and one operator-interrupted call retained in
`diagnostic-cohort/calls.json.gz`; none supports a matched latency claim.
The original plan, source snapshots, generated native code and stop reason are
retained alongside it. The cap is now explicitly unset.

The early native time-preference description also invited `same time` as a
literal preference, which the shared resolver cannot turn into a time. That
omitted 15:00 from the capped offer and caused refused moves. The adapter now
instructs the model to use the saved appointment time, matching the POC's intended
behavior. This was a comparison-adapter defect; those failures are not evidence
that native Pipecat is intrinsically worse.

The diagnostic runs also exposed a **real POC defect**. After routing to verify,
the model sometimes spoke its own phone readback instead of calling `ask_number`.
The caller heard a valid question, but no application approval record existed.
Their yes then triggered the actual structured readback, costing another turn.
One response even echoed the internal handoff instruction into speech. Inspection
confirmed that the role prompt was still present; this was not missing instructions.

Verification now uses typed output functions only, like complaints. `ask_number`
both speaks and establishes the question; `verification_guidance` handles missing
digits, country and corrections. Provider free text is suppressed before TTS.
The prompt stays short. Endpointing and streaming settings are unchanged.

`verification-negative-control.txt` demonstrates the regression: enabling free
model speech emits both an untracked question and the real readback. The current
boundary emits only the tracked question, and one caller yes approves it.

## Current evidence and closure limits

The measured source passed all 13 offline modules, Ruff and ty, plus **24/24
recovery and 24/24 continuation cases** after the verification change. The corrected
cohort then completed **200 calls**, 20 paired calls for each scenario. Source and
native build hashes were unchanged throughout collection. `MEASUREMENT.md` is
the declaration; `snapshot-v2.json`, `source-v2.tar.gz`, `calls.json.gz` and
`results.json` retain the exact code, inputs, failures and computed results.

| Scenario | POC passed | Native passed | POC full-turn p50 / p90 | Native full-turn p50 / p90 | Requests POC / native |
|---|---:|---:|---:|---:|---:|
| Public hours | 20/20 | 20/20 | .656 / .742s | .626 / .744s | 20 / 20 |
| Verified complaint | 20/20 | 17/20 | .876 / 1.418s | 1.188 / 1.414s | 60 / 80 |
| Complaint through verification | 20/20 | 9/20 | 1.224 / 1.437s | 1.328 / 3.342s | 100 / 186 |
| New booking | 20/20 | 18/20 | 1.360 / 1.883s | 1.148 / 1.589s | 120 / 142 |
| Same-time move | 20/20 | 18/20 | 1.913 / 2.096s | 2.193 / 2.427s | 60 / 81 |

These point estimates include **all calls**, including failures: 200 transcript
turns per arm, 360 POC requests versus 509 native requests. They are not audio
gaps. The fixed scripts do not adapt their replies to an incorrect question.

For latency inference, the predeclared paired bootstrap includes only pairs in
which both calls passed. Failures remain visible above. Intervals are 95%, 10,000
call-level resamples, seed 20260921, POC minus native:

| Scenario | Successful pairs | p50 difference interval | p90 difference interval |
|---|---:|---:|---:|
| Public hours | 20 | +.003 to +.094s | −.095 to +.148s |
| Verified complaint | 17 | −.398 to −.197s | −.142 to +.051s |
| Complaint through verification | 9 | −.099 to +.115s | −1.589 to −.331s |
| New booking | 18 | +.121 to +.270s | +.159 to +.389s |
| Same-time move | 18 | −.427 to −.203s | −.876 to +.260s |

**The overall no-regression latency gate did not pass.** Booking is slower on both
quantiles; hours has a modest positive median interval. Complaint and move medians
improve, but their p90 intervals cross zero. Complaint-through-verification improves
p90 while its median interval crosses zero. Do not turn these mixed results into
a general speed/parity claim.

Manual review supports the failure categories. Native calls omitted or shortened
the complete phone readback (12 calls), needed another complaint approval (five
calls), or passed literal `same time` instead of the saved time, then invented a
slot and reported 15:00 unavailable (two calls). One call has both phone and
complaint failures, yielding 18 distinct failed calls. The latter two failures
occurred after the preference description was corrected; the earlier adapter
defect remains excluded. No word-spelled phone number was mis-scored as missing.
These bounded semantic checks do not evaluate every nuance of conversational quality.

`results.json` also retains first submitted speech **including filler** and per-request
first output. Native first-output medians are lower in all five scenarios; tool
schemas and instruction formats differ by architecture, so no particular cause
is isolated. POC/native cache capture is not symmetric and no matched token-cost
conclusion is drawn.

After collection, a fresh microphone call exposed the unsupported phrase “Go for
it.” The [approval repair](../approval-evidence/README.md) is a later source change,
not part of this frozen cohort. The 100/100 result therefore does not cover that
phrase or imply universal acceptance. Current files may differ from the snapshot;
extract `source-v2.tar.gz` separately to reproduce the measured build. Do not rerun
the comparison into the existing raw destination.

Fresh voice now demonstrates complaint completion and booking, with the approval
loop separately reproduced and repaired. Full POC acceptance remains open for
the wider microphone matrix, matched audio, useful audio/client playout, and the
broader booking speech-prevention gate. `PLAN.md` remains the stopped diagnostic
cohort's declaration; its rows are not pooled into these results.

## Reproduce preparation and checks

Use the POC's interpreter/environment, with the existing native dependencies
path supplied to `bench.native_compare` where necessary:

```sh
python -m bench.native_reference /path/to/unmute
python -m bench.native_tools
python -m bench.native-evidence.check_score
python bench/native-evidence/check_config.py
python -m tests
ruff check .
ty check .
```

The environment-specific Google support path in `check_config.py` is an offline
probe for this machine. The comparison runner accepts `--native-dependencies`
explicitly. The generated package lives under ignored `bench/raw/`; source and
configuration hashes accompany retained evidence.
