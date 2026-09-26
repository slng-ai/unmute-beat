# Session 4 independent review: keep node agents, repair the execution guarantees

Reviewed 26 September 2026. This is a review and a plan, not a release approval. No new Coval calls, model evaluations, remote prompt changes, or runtime fixes were made during this review.

## Decision

Keep the one-agent-per-node direction. The implementation is simpler, keeps domain decisions with the node that owns them, and has useful successful voice examples. The evidence does **not** yet establish production stability, safety across arbitrary applications, or capacity for 50 calls on a server. Insert a stability phase before migrating Faro. Concentrate on delivery evidence, uncertain business operations, truthful completion, and a trustworthy evaluator; another classifier or orchestration framework would not resolve these faults.

The next phase is specified in [the stability plan and copy-paste prompt](../../plans/reusable-voice-core-stability.md). It retains the existing Pydantic AI, Pydantic Graph, Pipecat and Langfuse stack.

## What was inspected

- [PR #19](https://github.com/slng-ai/slng-voice-harness/pull/19), open at `9df9928ac16d312c7c848032e60b650e4820ea95`, on `feat/session-4-node-agents-cc`, based on `feat/session-3-authoring` at `074a6c2`. The runtime files inspected are from that head. Its two commits after `83d4be8` only change documentation; the reported final voice build and reviewed runtime therefore agree.
- The session-4 report, ledger, all local aggregate call reports, native diary implementation, core delivery/operation/closing paths, bot lifecycle, and evaluator code.
- Fresh, read-only Coval transcripts and Langfuse observations for **12 deliberately selected cases**: known failures, two harness outcome controls, native consent failures and an apparent native success, an unusually long native span, and an inconclusive load call. Coval responses succeeded; all 12 Langfuse queries returned HTTP 200 with `truncated: false`.
- Three small offline probes against the actual PR environment reproduced the operation and closing defects below. Model requests were disabled. The probes are included at the end of this document.
- Current primary documentation on transport acknowledgements, Python cancellation, production Pipecat hosting, idempotency and validation.

This was a targeted engineering review, not a new human annotation study or an independent audit of all 138 calls. Recordings were not replayed or remeasured here. Transcript wording and trace events support the findings below; acoustic quality, overlap and latency claims still require recording review. Raw exports and credentials are not added to the repository.

## Corrections to the supplied report

| Report claim | What the inspected evidence supports |
|---|---|
| 140 calls tested | The ledger records 140 allowed and **138 placed: 120 harness, 18 native**. Two native interruptive calls were never placed. Budget, launch, connected, completed and evaluated counts must be separate. |
| Two native double bookings | Both named calls contain a `book` followed by a **`move` of the same `BK0001`**, with `additional: false` and a `moved` receipt. The native diary replaces the entry under that ID. The evidence contradicts “Friday and Saturday both booked”; there is no independent final backend snapshot. The premature authorizations remain real defects. |
| Native interruptive success is 1/8 | This is not a reliable end-to-end success rate. The count penalizes two receipts even when they represent a move. Conversely, the apparent single-booking success accepts identity on the fragment `that's`, which the count misses. Rescore outcome, authorization and delivery separately. Do not replace 1/8 with an invented corrected rate. |
| 227 writes prove safety | They prove that the current audit found earlier matching question labels. It missed the first consent fault. It does not independently establish delivered audio, valid acceptance, exact subject/arguments or complete instrumentation. Native `confirm_customer` is missing from its recognized consent-action map. |
| Normal and interruptive accuracy are comparable to native | Normal harness 38/40 pools two revisions; the latest runtime has no fresh normal gate. Native has only 10 normal and 8 interruptive calls. The reported harness totals, 67/69 and 42/50, cover 119 of 120 calls and omit the early critical case from those accuracy denominators. Preserve every attempt and its separate safety outcome. |
| All N1–N13 checks passed | Functional harness rows in `matrix-stress-prep/matrix.md` pass, including N13 at 16/16. Native N9 fails and native N13 is not exercised. N12 is a separate timing command: the available `matrix-fixed/timing.json` has `passed: false`, `trace_within_tolerance: false`, despite passing the 50 ms harness-turn ceiling. Do not call the entire comparison green. |
| A native call ended at the 300 s limit | `iPFgi5qq` has a Langfuse conversation span of **1,105.081 s**. The report derives duration from this span and labels any duration above the limit as “ended by the limit.” That does not establish actual connected or billed audio duration, nor the termination cause. Check transport/provider lifecycle events. |
| No cross-call leakage, because every first booking is BK0001 | A fresh diary per call makes that outcome expected. It does not test shared-backend contention, worker isolation, or preservation after process death. |
| Faster and similar cost | The reported timings and input-token counts are useful observations of those configurations. Models, VAD, concurrency and some speech profiles differ; pooling turns weights longer calls more heavily. Input tokens alone omit output, cache pricing, warm-up and some cancelled requests. Neither architecture-only speedup nor cost parity is established. |

## Concrete trace findings

The selected controls illustrate why business completion and conversation quality must be separate: `SB4GGMzU` eventually booked, after an unnecessary transfer claim and wait; `VuhAwZ3e` booked after repeated read-backs. A booking receipt alone hides that friction.

| Case | Observation | Implication |
|---|---|---|
| `fymMyWTiyz6eqfK3RwRTRX` | Lookup audit is `ok` on a repeated phone fragment; the report itself identifies an unheard read-back and false audit pass. | Keep this incident as a regression for both execution and evaluator. |
| `cYMMixAchYqsXAAFpLUccQ` | Transcript says the haircut is booked; trace has only lookup evidence. | The known false goodbye was real. The current broad receipt exception still has another route around the fix. |
| `cBrx4NhRQXYyTgSfY8zmhA` | Verification claims it will connect the caller to someone, then ends without a booking. | The deterministic missing-number opening addresses a concrete failure; preserve its replay. |
| `C26pPHK6Fkjz57N9bmZupD` | Saturday selection includes an early “please book”; only the start of a read-back appears, more answers arrive, then goodbye. Trace contains lookup but no booking. | Safe non-execution, unsuccessful task. Add bounded recovery and a truthful pending-outcome goodbye; do not turn an early yes into consent. |
| `YzkhPWRpDFUKuEmBWNmfrA` | Friday booking authorized on a time-selection phrase; later same booking ID moved to Saturday. | Consent defect, not demonstrated duplicate final appointment. |
| `3CyVWZmpnotfM4nfdru2e2` | Friday booking authorized on STT text `silence underscore tool`; later same booking moved. | Strong negative case for consent evaluation and simulator-contamination classification. |
| `kwUbc4PRZhe4Absq4yxScy` | Saturday booking receipt exists, but identity write used only the fragment `that's`. | “Correct final booking” does not imply all required authorizations were valid. |
| `8opr2oigPcynN6vEv6nRUd` | Identity accepted while caller asks the agent whether the number is correct; later technical failure closes the call. | Keep semantic authorization and service availability as separate failure labels. |
| `Khs7tVuSX4b8Pwghtxz3jX` | Retrieved Coval transcript is empty; Langfuse has observations and a ~180 s conversation span. | Incomplete quality evidence. Retain it in the operational denominator, never as a pass. |

Direct links to the decisive examples: [last harness failure](https://app.coval.dev/slng-ltd-1787040174713992753/workspace/01M09YA4S1SC1Q8AHKHP2R9A0E/simulated/runs/B6c7CSHw32Lt6PqxRR9yCZ/results/C26pPHK6Fkjz57N9bmZupD), [its Langfuse session](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/sessions/CA31386078e3904c3fa89a08bc4b9d75f1), [native tool-noise write](https://app.coval.dev/slng-ltd-1787040174713992753/workspace/01M09YA4S1SC1Q8AHKHP2R9A0E/simulated/runs/5YWfZDoPQihTXbfiY3wJWs/results/3CyVWZmpnotfM4nfdru2e2), [its Langfuse session](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/sessions/CA4fb153ba57994918a9ca45d71f732d61).

## Architecture findings, ordered by consequence

### 1. Unknown writes can become retryable after cancellation — high priority

In PR #19, `core/operations.py:100` validates and authorizes, executes, records evidence and then records/speaks the result. Its broad exception branch marks an attempt failed. `FunctionOperation._execute` in `core/steps.py:345` registers an unknown attempt only for the explicit `OutcomeUnknownError` exception. A backend that commits and then loses its response through cancellation can leave no unknown entry. The offline probe reproduces this; it is a reusable-core contract gap, not evidence that the synchronous salon diary duplicated a live booking.

`FunctionOperation._before` at `core/steps.py:327` also removes the unknown entry **before** validating the reconciliation receipt. A malformed result loses the guard; the second probe reproduces that. This path also needs the same subject/argument checks as an ordinary receipt. A mismatched or malformed response after dispatch cannot justify the existing assurance “Nothing was done.”

Repair the existing operation boundary. Distinguish confirmed rejection, confirmed success and unknown effect. Persist a stable operation intent before dispatch when restart survival is required; use backend idempotency and reconciliation. Keep cancellation propagation and bounded cleanup. An audio interruption cancels speech, not a committed business fact. A new intentional operation needs a new intent identity even if its arguments equal an earlier operation's.

Python documents cancellation inside `asyncio.timeout()` and requires cleanup without swallowing cancellation. Retry safety depends on the service's idempotency contract, not on wrapping a coroutine in a retry loop. [Python cancellation](https://docs.python.org/3/library/asyncio-task.html#task-cancellation), [AWS idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/).

### 2. Playback timing is still promoted to delivery evidence — high priority

`core/state.py:613` marks all queued segments as playing when aggregate bot audio starts. `note_drained` at line 627 can infer queued-segment completion from text length at 15 characters/second and then records `heard_by='signal'`. That is not a segment-specific transport acknowledgement. The first queued-read-back fix narrows one race, but the general inference remains.

Carry segment/generation identity through the existing Pipecat adapter and acknowledge actual emitted media. For Twilio, send a mark after the associated audio and track clear/interruption generations. **Twilio also returns pending marks when audio is cleared**; those acknowledgements cannot authorize an unheard proposal. An acknowledgement proves transport playout under that protocol, not human understanding. Unknown delivery must remain unknown. [Twilio mark and clear semantics](https://www.twilio.com/docs/voice/media-streams/websocket-messages).

Reuse installed serializer/frame extension points. The inspected Pipecat 1.11 serializer sends media and clear but does not supply the needed segment mark mapping. Keep Twilio details in the adapter; core consumes delivery events. Do not build a second audio engine or assume a local `BotStoppedSpeakingFrame` proves remote playback.

### 3. Receipt presence is too broad a permission to claim completion — high priority

`_unbacked_goodbye` in `core/steps.py:515` permits any goodbye once **any** evidence belongs to an operation on the current node. The third probe shows a lookup receipt permits “Your haircut is booked for Friday.” Failed-status receipts and subsequent cancellations also need explicit treatment. The source docstring already acknowledges the lookup shortcut.

Make consequential completion/closing statements derive from a successful, correctly bound and still-effective receipt, or a known pending/failed outcome. Use the existing localized receipt speech and typed finishing paths. Ordinary conversation can remain generative. Regex claim detection is not a universal truth guarantee across paraphrases and languages; another classifier is not the proposed fix.

### 4. Safety needs a bounded recovery path, not stronger prompting alone — high priority

The final stress failure preserves the booking guard but does not complete the task. Define progress around a proposal version and delivery outcome. A replacement invalidates stale answers; a tool result stays with the booking node; early acceptance never substitutes for hearing the exact proposal. After a bounded number of unsuccessful delivery/clarification attempts, say what is known and end or offer a real supported handoff. Do not pretend a transfer exists. If a caller leaves with no booking, say so; if an effect is unknown, say it is unknown.

Count retries across fragmented turns, not just model requests inside one turn. Interruptions must remain possible during read-back. Delaying endpointing globally or suppressing interruption may mask the incident while harming the voice experience.

### 5. The host and backend are still development infrastructure — high priority for the 50-call target

`salon-graph/bot.py` creates per-call audio services and enters `model_clients(..., warm=True)` per call. `core/graph.py` excludes warm-up requests from tracing and only cancels its warm-up tasks during cleanup. The default diary and unknown-operation records live in process memory; evaluation often gives each call a separate diary. Neither survives a killed worker or arbitrates capacity between workers.

Compile immutable flow definitions once per worker, own SDK-client lifecycle there, and keep mutable sessions isolated. Use a bounded worker arrangement, admission control, deadlines, graceful drain and measured cleanup. Keep booking storage in the application service. Inspect Faro's existing SQLite/WAL pattern before adding storage infrastructure; for one server, stdlib SQLite with atomic domain transactions is a reasonable first implementation, subject to the actual contention test. Multi-host HA is outside this phase.

Pipecat explicitly distinguishes its development runner from production hosting: the development runner lacks production concurrency and lifecycle protections. Use its existing production patterns and installed server components rather than inventing a scheduler platform. [Pipecat production guidance](https://docs.pipecat.ai/pipecat/deployment/running-bots-in-production).

A killed worker loses its live media connections. Success means unaffected workers continue, lost sessions terminate predictably, and durable effects reconcile without duplicate writes. Seamless audio migration is not promised.

## Budget and evidence decision

The user's latest choices authorize planning **up to 20 targeted repair calls**, followed by **20 normal + 20 interruptive candidate calls**. Native currently has **10 normal + 8 interruptive** placed calls, so top-ups are **10 + 12**, subject to eligibility. That is **up to 82 new Coval calls**, not 82 required calls. Historical failures remain in the baseline; exclude only for predeclared evidence/configuration reasons, not poor performance.

If old native configuration/case versions cannot be established or the evaluator cannot recover essential evidence, do not silently count them as comparable and do not quietly add replacement calls. Report the affected metric/cohort as incomplete and quantify the additional allowance needed. New versus historical native results must also remain visible separately.

The server-capacity experiment is separate. Fifty actual concurrent sessions cannot be inferred from a 40-call quality sample or a CLI concurrency setting. Prove cheap replay load first, then bound a 50-session real-provider media run on the intended server, and inject worker failure. Label simulated carrier transport honestly. This consumes provider resources even if Coval is not used.

## Reproducible offline probes

Run the following with the **PR #19 environment** (`9df9928`), not this older planning checkout. They assert the observed defects, so their assertions should stop passing after repair. Convert them into regressions asserting the corrected behavior during implementation. They use an in-memory fake backend and disable model requests; `needs_approval=False` isolates the post-authorization execution path.

```python
import asyncio
from pydantic import BaseModel, ValidationError
from pydantic_ai import models
from core.steps import FlowCall, FlowState, FunctionOperation, _unbacked_goodbye

models.ALLOW_MODEL_REQUESTS = False


class Args(BaseModel):
    value: str = 'sample'


class Receipt(BaseModel):
    status: str


def fresh():
    return FlowCall(state=FlowState(agent='booking', facts=Args()))


async def main():
    committed = []

    async def commit_then_cancel(services, args):
        committed.append(args.value)
        raise asyncio.CancelledError()

    class Probe(FunctionOperation):
        name = 'probe'
        args_type = Args
        receipt_type = Receipt
        needs_approval = False
        function = staticmethod(commit_then_cancel)

    op, call = Probe(), fresh()
    try:
        await op.run(call, Args())
    except asyncio.CancelledError:
        pass
    assert committed == ['sample'] and not call._unknown

    class ReconcileProbe(Probe):
        reconcile = staticmethod(lambda services, args: {'wrong': 'receipt'})

    op, call = ReconcileProbe(), fresh()
    token = op._token(call, Args())
    call._unknown[token] = (op, Args())
    try:
        await op._before(call, Args())
    except ValidationError:
        pass
    else:
        raise AssertionError('Invalid receipt unexpectedly accepted')
    assert token not in call._unknown

    call = fresh()
    claim = 'Your haircut is booked for Friday.'
    assert _unbacked_goodbye(call, claim, frozenset({'lookup'}))
    call.note_evidence('lookup', {}, {'status': 'found'})
    assert not _unbacked_goodbye(call, claim, frozenset({'lookup'}))
    print('All three reported defects reproduced; no network requests.')


asyncio.run(main())
```

## Retrieval inventory

| Selection | Coval simulation | Langfuse session |
|---|---|---|
| Early consent | `fymMyWTiyz6eqfK3RwRTRX` | `CA09f5c0958d564d9782dc10947e8e421c` |
| Gate 1 failure | `cYMMixAchYqsXAAFpLUccQ` | `CAc55c9058f9db48d6950588b0f260b38e` |
| Gate 1 rerun failure | `cBrx4NhRQXYyTgSfY8zmhA` | `CAb558bd3cf85447a480e30745a9bb1430` |
| Gate 2 failure | `C26pPHK6Fkjz57N9bmZupD` | `CA31386078e3904c3fa89a08bc4b9d75f1` |
| Normal outcome control | `SB4GGMzURZEKaYgDf2RkdU` | `CA41a566420bc24d5dafd3cb64023ea737` |
| Interruptive outcome control | `VuhAwZ3etdM8ERrZWMndxh` | `CA941c5b40321245599d78e7175d9d55c6` |
| Native premature selection | `YzkhPWRpDFUKuEmBWNmfrA` | `CAa24472f088d44304815811b516268390` |
| Native tool noise | `3CyVWZmpnotfM4nfdru2e2` | `CA4fb153ba57994918a9ca45d71f732d61` |
| Native outcome control | `kwUbc4PRZhe4Absq4yxScy` | `CA3bf0451600f34218a5c98af34a484aef` |
| Native stopped | `8opr2oigPcynN6vEv6nRUd` | `CA8ab6402bae99492987ab790a3b2c1417` |
| Native long span | `iPFgi5qqowDhHxEE66H85W` | `CAecf6261f1e8f408cb3305e9b31fb21cf` |
| Inconclusive load | `Khs7tVuSX4b8Pwghtxz3jX` | `CAa4df9080030e4c0ebad7456396636c26` |
