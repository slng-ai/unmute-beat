# Reusable voice core: six coding sessions

Prepared against local revision d31399e on 25 September 2026, expanded with the additional architecture discussion and native-approval investigation. This is an implementation playbook, not a claim that the migration or evaluations have run. The supplied [roadmap](reusable-voice-core-roadmap.md) is preserved alongside it.

The user's final scope is **salon-graph only for live verification**, both live text conversations and Coval calls. All five workspace consumers remain in the migration. Their offline checks remain necessary when their code changes. Faro and salon-prompt will not acquire new live voice evidence through this plan.

## Session boundaries

Run these sequentially, carrying forward the previous session's code and evidence. They are milestones, not time estimates. A failing gate means continue or repair that session, not start a wider migration.

| Session | Deliverable | Roadmap phases | Main proof |
|---|---|---|---|
| 1 | Baseline and bounded evaluation tools | 0 + verification foundation | Reproduce current behavior; launch only intended calls |
| 2 | Installable public boundary, typed session, explicit delivery | 1–2 + delivery prerequisite | Clean installation; atomic state; no approval from unheard speech |
| 3 | Complete ordinary authoring on the clinic fixture | 3 | Config, data models, prompts and domain functions suffice |
| 4 | Salon migrated to the public authoring contract | 4 | Existing business behavior through the new entry point |
| 5 | Faro migrated using the same contract | 5 | Identity and persistence acceptance offline; salon remains sound |
| 6 | Shared launcher, tooling consumers, compatibility cleanup | 6–7 | All five consumers use useful public imports; isolated installation works |

Every session finishes with deterministic checks, a bounded live text salon evaluation, and a two-call Coval salon checkpoint. Session 1 records the reference result; sessions 2–6 compare against it and the previous accepted checkpoint. A known baseline defect is explicitly recorded and assigned to its repair session, never silently treated as correct behavior.

## Decisions carried into the implementation briefs

These prompts turn the additional notes into implementation requirements. They do not restart the older inheritance-based plan, add a compiler, or move the agent to a different voice runtime.

| Decision | Concrete meaning for the coding sessions |
|---|---|
| Almost zero new orchestration | Standard use cases do not subclass CallContext or assemble Conversation, Desk, AgentBuilder, interpreters or approval ledgers. Domain functions and specialized grading/learning nodes remain legitimate code. |
| State is data | The application provides concrete Pydantic declarations; the library owns session bookkeeping; clients and clocks are injected. Moving FaroCall methods into another custom runtime class does not meet the goal. |
| Validation and evidence differ | A valid phone or an object named VerifiedIdentity is not authorization. Shape checks, evidence provenance and backend permissions all have distinct checks. |
| Corrections invalidate eligibility | A changed identity/slot/plan invalidates dependent approval and completion. Historical evidence of an actual external effect remains available for reconciliation. |
| Receipt controls transactional speech | An attempted action, a saves counter or an unrelated success cannot justify a completion claim. Free-form coaching has a weaker wording guarantee. |
| Interpretation has one public contract | Common narrow readings are derived by the library; solver selection is not ordinary author configuration. A domain grader remains domain-owned. |
| SDK features are building blocks | Reuse existing hooks/templates/output functions. Native agent configuration and deferred tools do not replace the application's state, completion and spoken-consent semantics. |
| Authoring reuse and capacity differ | Compile definitions once and isolate sessions. Keep SQLite-worker and process-local coordination limits explicit; do not add distributed infrastructure without a deployment requirement. |

The old notes reference a 559a checkout, examples/faro, examples/help and examples/access. The active code is the current core/use-cases workspace. Faro is under use-cases/faro-language-learning, salon is under use-cases/salon-graph, and the independent reuse fixture is core/tests/clinic.py. Do not resurrect removed examples or act on historical claims that an experiment already passed.

### Source map for the implementation agents

These are reading paths, not a requirement to edit every listed file.

| Area | Current sources | How to use them |
|---|---|---|
| Existing state and delivery | [state.py](../../core/src/core/state.py), [proposals.py](../../core/src/core/proposals.py), [history.py](../../core/src/core/history.py) | Preserve atomic replacement and cancellation history; repair absent-delivery approval; retain business evidence while fencing stale routing. |
| Authoring and completion | [flowspec.py](../../core/src/core/flowspec.py), [steps.py](../../core/src/core/steps.py), [completion.py](../../core/src/core/completion.py) | Extend the existing loader/standard steps; replace aggregate facts-dictionary authoring with the concrete model. |
| Actions and interpretation | [operations.py](../../core/src/core/operations.py), [readings.py](../../core/src/core/readings.py), [interpreters.py](../../core/src/core/interpreters.py), [conversation.py](../../core/src/core/conversation.py) | Reuse guarded execution, relevant readings, caches and common conversation behavior. |
| Salon adoption | [state](../../use-cases/salon-graph/salon/models/state.py), [classification](../../use-cases/salon-graph/salon/classification.py), [nodes](../../use-cases/salon-graph/salon/nodes.py), [writes](../../use-cases/salon-graph/salon/tools/writes.py) | Replace runtime assembly while preserving salon-specific facts, proposals and service behavior. |
| Faro adoption | [state](../../use-cases/faro-language-learning/faro/state.py), [conversation](../../use-cases/faro-language-learning/faro/conversation.py), [operations](../../use-cases/faro-language-learning/faro/operations.py), [acceptance tests](../../use-cases/faro-language-learning/tests/test_faro.py) | Separate storage/context/grading from generic session behavior; preserve all named acceptance cases. |
| Evaluation | [Coval runner](../../use-cases/concierge-tools/coval_sim/__main__.py), [ledger](../../use-cases/concierge-tools/ledger.py), [Faro text benchmark pattern](../../use-cases/faro-tools/bench.py) | Reuse bounded calling/traces and real-engine text driving; do not run the Faro benchmark under the current live scope. |
| Historical design/evidence | [portable-flow plan](portable-typed-flows.md), [research](../research/portable-typed-flows.md), [POC evidence](../research/poc-evidence-2026-09-24.md) | Retain behavior and measurement lessons. The supplied newer roadmap supersedes the old requirement for application runtime subclasses. |

Framework references sharpen particular decisions:

- [Pydantic model behavior](https://pydantic.dev/docs/validation/latest/concepts/models/): test complete candidate validation and nested mutation rather than assuming an outer frozen model is deeply immutable.
- [Pydantic AI Agent Specs](https://pydantic.dev/docs/ai/core-concepts/agent-spec/): reuse supported agent construction/template features where useful; schema-only declarations do not replace concrete runtime validation.
- [Output functions](https://pydantic.dev/docs/ai/core-concepts/output/#output-functions): the existing finishing path can return application-controlled results without another model response.
- [Deferred tools](https://pydantic.dev/docs/ai/tools-toolsets/deferred-tools/): ordinary function-tool approval is a separate execution pattern. The additional source investigation identified pending-history, cancellation, argument-override and post-tool-request constraints; session 3 specifies the conditional compatibility checks.

Check the installed lockfile and corresponding SDK source before using those APIs. Current documentation is guidance, and the prior investigation was not a runtime integration test.

## Verification contract — applies to every prompt below

### Working rules

- Read AGENTS.md, CLAUDE.md, the roadmap, this contract, and the preceding session report. Inspect the current checkout; the planning revision is not a request to reset changes.
- Use the requested pydantic, python-dev, building-pydantic-ai-agents, and ponytail:ponytail skills. The roadmap's explicit instruction to preserve assert-script tests overrides the generic Python skill's pytest recommendation. Use uv, Ruff and ty; do not migrate the test framework.
- Extend the existing engine. Keep this workspace's Python 3.14 support, distribution name slng-voice-core, and import namespace core. Verify SDK APIs against the installed lockfile before copying newer documentation examples.
- Use Pydantic for definition/input/state/receipt boundaries and existing classes or stdlib dataclasses for internal scheduling and test events. No generic plugin registry, expression language, persistence framework, second voice adapter, or second conversation engine.
- Reuse the existing core text runner and Pydantic AI for conversation evaluation. Prefer scripted caller turns; use a small Pydantic AI caller only where adaptive responses are necessary. No additional orchestration dependency. Keep existing Langfuse tracing.
- Preserve prompts, tool descriptions, model settings, curriculum and domain behavior unless explicitly required. Review prompt snapshot diffs; do not regenerate snapshots simply to make checks pass.
- Keep applications runnable after each session. Temporary compatibility must translate into the same engine, have a named consumer, and disappear once that consumer migrates.

### Four kinds of evidence

| Layer | What runs | What it proves | What it cannot prove |
|---|---|---|---|
| A: deterministic | Real engine with scripted models, fake services or temporary SQLite | State, guards, routing, receipts, budgets, persistence and history | Real model interpretation quality |
| B: timed event replay | Existing processor/engine driven by timestamped input and playback events | Continuations, cancellation, stale events, delivery accounting | Real VAD/STT/TTS accuracy or network latency |
| C: live text simulation | Scripted or Pydantic AI caller driving salon's real models through the core text runner | Semantic behavior on sampled dialogues | Audible overlap, endpoint detection quality or audio latency |
| D: Coval voice | Actual salon Pipecat pipeline over the existing tunnel/transport | Sampled end-to-end voice behavior and observed overlap | Statistical reliability or other applications' live behavior |

In layer A, disable real model requests before imports and override every relevant model, including classifiers and any simulated caller. Pydantic AI's [testing documentation](https://pydantic.dev/docs/ai/guides/testing/) describes FunctionModel, TestModel and ALLOW_MODEL_REQUESTS; the repository already uses this approach. That flag does not block STT, TTS or arbitrary HTTP: stub those boundaries too and avoid loading real credentials into offline checks.

Layer C needs only a bounded async loop around the existing core text runner, following the repository's scripted live benchmark pattern. Caller turns can be scripted, with an existing Pydantic AI model supplying adaptive replies only where needed. An adaptive caller sees its goal and the conversation actually delivered to it, not private state or expected answers. Keep deterministic assertions on diary results and operation audits outside the caller. Its assertion that the task succeeded is not evidence. Langfuse records traces and evaluation evidence; it does not play the caller.

Text delivery must be explicit: acknowledge identified output segments when the text consumer receives them. Partial and absent acknowledgements stay partial/unknown. Do not equate take_turn() returning, model generation ending, or TTS finishing generation with audio playback completion.

### Existing checks and commands

Extend the closest existing check rather than introducing a testing platform:

- core/tests/test_reuse.py and core/tests/clinic.py: independent use-case reuse.
- use-cases/salon-graph/tests/test_core.py: direct engine / Pipecat equivalence and import boundaries.
- Salon's test_continuation.py: split utterances and playback-aware turn continuation.
- Salon's test_consent.py, test_recovery.py, test_replay.py, test_budget.py, test_state.py, test_flow.py, and prompt snapshots.
- use-cases/faro-language-learning/tests/test_faro.py: temporary-database acceptance.
- use-cases/concierge-tools/tests/test_coval_sim.py and test_ledger.py: tools without network calls.

From the workspace root, record each exit code and actual output:

~~~bash
uv run ruff check .
uv run ruff format --check .
uv run --directory core ty check .
uv run --directory core python -m tests
uv run --directory use-cases/salon-graph ty check .
uv run --directory use-cases/salon-graph python -m tests
~~~

For other changed members, also run uv run --directory <member> ty check . and their python -m tests suites when present. Session 1 inventories every member; session 6 checks every member. Faro-tools has no suite at the planning revision: give changed execution logic a small offline assert check and register its command. Do not run its live bench.py as an offline smoke test.

### Interruption and endpointing coverage

Keep the replay driver small and in tests/evaluation tooling. Drive the real processor using existing events and control time/order with a fake monotonic clock or explicit synchronization barriers. Do not simulate interruption by sending a second message after awaiting a complete first response.

| Case | Required event arrangement | Required result |
|---|---|---|
| Short pause | Caller resumes before the configured stop strategy ends the turn | No premature response; final input contains both fragments |
| Premature endpoint | Endpoint fires on a pause; caller resumes before an answer finishes playing | Continue logical turn, retain fragments, prevent stale interpretation from acting |
| Agent talks over caller | Output starts after a false endpoint while caller is speaking/resumes | Stop obsolete output, retain input, do not mark output fully delivered |
| Caller barges into readback | Playback starts, only a prefix plays, then interruption | No approval/write from unfinished readback; correct and read back again |
| Generation without playback | Entire readback is generated but playback is absent/buffered | Delivery unknown; later yes cannot authorize it |
| Partial then complete repeat | First copy interrupted; identified later copy fully plays | Acceptance binds to delivered current proposal, once |
| Delayed/stale events | Late transcript, classifier result, playback stop or tool response from superseded generation | No wrong-generation approval/route/speech update; retain completed business evidence |
| Cancellation around a write | Interrupt before dispatch, during uncertain execution, and after successful receipt | No unapproved dispatch; reconcile uncertainty; completed write survives without duplication |
| Real new turn | Caller speaks after response fully plays | Advance turn and age/invalidate proposals correctly |

Use actual timing configuration as the reference. Test pauses below, near and above its thresholds, with delayed STT and delayed playback independently. Smart-turn is not just a silence timer: synthetic endpoint events test the engine response; recorded PCM through the installed detector can test the detector offline where feasible. Do not copy a detector algorithm into the simulator. Consult [Pipecat's turn strategy documentation](https://docs.pipecat.ai/api-reference/server/utilities/turn-management/user-turn-strategies.md), checking APIs against the installed version.

Use overlapping input/output timelines, not an LLM's textual instruction to “interrupt,” to prove an event occurred. Replay has exact ordering assertions; real audio has measurement uncertainty. Missing timestamps never become zero latency.

### Bounded live checks

Use only use-cases/salon-graph. The existing Coval convention maps voice-harness-salon-pipecat to that directory; discover and record its actual agent ID.

Freeze fixtures, scenario versions, salon model/classifier, prompts, language, STT/TTS settings and personas across checkpoints. Each call starts with reset test diary data, never real customer bookings. Record intentional changes and invalidate affected comparisons.

**Live text checkpoint:** four salon dialogues: normal booking; correction/conditional acceptance; prerequisite detour and return; unknown-write recovery. Inject backend uncertainty through the test service boundary for the last case. One attempt per dialogue, maximum 12 caller turns per dialogue, at most 48 optional adaptive-caller model invocations across the checkpoint, and 100 target model invocations including classifiers across the checkpoint. Scripted caller turns require no caller model requests. Keep target per-turn budgets, cap adaptive caller output, record tokens, and configure a finite token/spend ceiling for the selected provider before running. Stop on explicit completion or budget exhaustion; exhaustion is incomplete evidence, not success. The live target and any adaptive caller incur model costs. Save transcripts for cheap scripted reproduction; rerunning the live target on saved caller text is still billable.

**Coval checkpoint:** at most two simulations, one iteration, one at a time, no mutations or automatic reruns:

1. **Normal booking:** identify the fixture caller, select an offered appointment, fully hear the readback, explicitly accept, and receive matching confirmation. Exactly one intended booking must exist.
2. **Pause and correction:** give identity/request in fragments separated by a pause; interrupt the appointment readback to correct it; accept only after hearing the corrected readback. No old/partial proposal may execute; exactly one final correct booking may exist. Record whether premature endpointing, agent-over-user speech and caller barge-in actually occurred.

Freeze these cases in session 1. Reuse a calm persona and a suitable interruptive persona with explicit IDs; do not launch a whole sweep. [Coval personas](https://docs.coval.ai/concepts/personas/overview) offer interruption settings, but configuration is not proof a call contained required overlap. Check recording and event trace. If a trigger did not occur, mark coverage **not exercised**, without automatically spending another call.

Budget: **2 simulations × 6 sessions = 12 maximum planned calls**, including the reference checkpoint. Propose a verified 180-second per-call ceiling: at most 36 audio minutes if enforced. This is not a currency quote and excludes model/TTS/telephony/evaluator charges. Verify account pricing for money estimates. If the service/transport cannot enforce the duration ceiling, report that before launch; polling timeout is not a call-duration cap. Paid retries consume a separately assigned allowance.

The current coval_sim run defaults to all attached sets, can target several agents sharing a set, and uses one persona per invocation. It does **not** yet implement this narrow gate. Session 1 adds exact target selection, a read-only launch plan and a simulation-count limit. Verify cases and expanded variants immediately before launch. Prefer two explicitly selected one-case sets/persona jobs, or verified exact-case filtering. One iteration does not mean one call. Do not copy the documented VkpkmRMV example: its README describes 18 calls.

Launch sequentially and review the first result before launching the second. Stop new paid launches on critical failure. Reconcile uncertain launch status by ID before retrying. Verify remote termination and clean up bot/tunnel processes; local polling expiry alone does not cancel a remote run.

Use Coval skills as needed: coval-resources for inventory; build-test-suite, design-persona and configure-metrics for the checkpoint; then **either** quick-eval **or** launch-run → watch-run → get-results. Do not launch twice by invoking both workflows. Use download-audio/trace diagnostics for missing evidence or failures. Accent, noise and broad adversarial sweeps are separate paid expansions. Carry selected IDs and authorization across sessions; only unresolved resources or changed spending scope need new input.

### Passing and stopping rules

- Applicable deterministic and replay checks pass. From session 2 onward, unknown/interrupted delivery cannot authorize consequential action. No unauthorized write, identity leakage, duplicate write, lost acknowledged business result, or unsupported completion claim.
- All four bounded live text cases meet deterministic business assertions. Inspect semantic failures against delivered transcript and classifier input/output. When interpretation changes, reuse salon's eval machinery for a small supported-language consent set within the recorded model allowance; do not launch its whole model comparison matrix.
- Both Coval calls meet expected behaviors and have business/audit evidence. COMPLETED status or high composite score is insufficient. Match call IDs to receipts and audio. Trigger-dependent coverage passes only when the trigger occurred and behavior was correct.
- Record caller acoustic speech end, runtime endpoint, transcript availability, model/classifier durations and requests, backend duration, first audible output, interrupting speech start and last audible obsolete output. Use one monotonic clock for local differences and document external audio alignment/error. Endpoint delay = endpoint minus acoustic end; response latency = first audible output minus acoustic end; barge-in stop latency = last obsolete output minus interrupting speech start. Keep these separate from text/token latency.
- Freeze numeric timing budgets in session 1, using product limits or explicitly provisional limits. A matched live median response/stop latency rise above max(200 ms, 20% of baseline) is a provisional investigation trigger, not statistical proof. Do not widen limits after seeing regression. Report sample count and raw ranges; two calls cannot substantiate p95, reliability or capacity. Negative response latency can indicate agent-over-user speech; do not clamp it away.
- Missing credentials, unavailable required metrics, absent triggers or exhausted budget mean **incomplete**, not passed. Finish useful offline work, leave live gates pending, and do not claim full verification. Session 1 may record existing defects as baseline debt. Critical approval/delivery debt is due in session 2; all critical debt must be resolved for final acceptance.

### Session handoff

Keep a compact tracked report at docs/research/reusable-core/session-N.md. Save machine outputs/transcripts in an appropriate artifact directory and link durable sanitized evidence. Record:

1. Start/end revisions or exact dirty-tree patch identity, lockfile hash, API changes and compatibility still needed.
2. Commands, exit codes, check counts and failures/skips without rounding up.
3. Scenario/persona/model/voice versions, target/run/simulation/trace IDs and package hashes where applicable.
4. Expected versus actual business outcomes, delivery/replay coverage, raw timing/request/token observations, actual reported cost and unavailable cost components.
5. Separate implementation, deterministic, semantic and voice statuses, plus next-session prerequisites.

Export evidence before removing temporary runtime directories. Do not commit keys, real caller data or sensitive raw traces. Keep reports small; do not build a reporting service.

## Prompt 1 — Establish the baseline and make evaluation repeatable

Copy this complete block into the first coding session.

~~~text
Implement session 1 of the reusable voice core migration. This is an implementation task, not another architecture proposal. Read AGENTS.md, CLAUDE.md, README.md sections 7–10, docs/plans/reusable-voice-core-roadmap.md, and the Verification contract in docs/plans/reusable-voice-core-sessions.md. Apply pydantic, python-dev, building-pydantic-ai-agents and ponytail:ponytail. Preserve the repository's assert-script tests and use uv, Ruff and ty.

OBJECTIVE AND BOUNDARY

Give later sessions a reproducible starting point and a small evaluator that drives the real salon engine. Do not start the production state/API migration yet. The distribution already exists; the workspace is already split. Live text and Coval verification target ONLY use-cases/salon-graph. Read other projects to understand reuse and run their existing offline suites, but do not launch their live benchmarks.

READ THE EXISTING IMPLEMENTATION FIRST

- core/src/core/turn.py: run_turn, take_turn, Barge, TurnEvent and cancellation retention.
- core/src/core/processor.py: Pipecat input, interruption and playback integration.
- core/src/core/state.py and proposals.py: commit, logical turns, generation fencing, speech ledger and pending readbacks.
- use-cases/salon-graph/tests/test_core.py, test_continuation.py, test_consent.py, test_recovery.py, test_replay.py, test_budget.py, and tests/__main__.py.
- use-cases/salon-graph/bot.py: actual STT/TTS/VAD setup, DIARY_PER_CALL, playback callbacks, call IDs and disconnect handling.
- use-cases/concierge-tools/coval_sim/{__main__,coval,models,runtime}.py and its tests.
- use-cases/concierge-tools/ledger.py, plus use-cases/faro-tools/bench.py for its scripted caller/real-target/trace pattern. Reuse the pattern; do not import Faro into the salon evaluator.

WORK IN THIS ORDER

1. Record the exact starting revision/dirty changes, Python and locked package versions, prompts, model/classifier configuration, voice settings and existing test commands. Inventory each consumer's dependencies and custom orchestration. Identify methods doing generic runtime work versus genuine domain work. Do not transfer old benchmark numbers or zero-model approval claims into this baseline.

2. Run all existing offline suites and per-member ty; capture real outputs and failures. Preserve golden prompt/tool-schema snapshots. Baseline defects must have a reproducible case and owner session. In particular, inspect ProposalLedger.last: absent matching speech currently may count as heard. Capture the desired “unknown delivery cannot authorize” regression without altering production semantics in this session or silently treating it as a passing safety guarantee. Keep that expected failing check separate from the baseline passing suite until session 2 repairs it.

3. Extend existing interruption probes only where coverage is missing. Use the real processor and scripted model/classifier outputs, with clock control or synchronization barriers. Cover caller fragments around endpointing, output that has been generated but not played, partial readback, delayed old playback-stop events, and cancellation before/during/after a write. Record event order, input pieces, proposal identity, delivered segment IDs, backend effects and final history. Do not simulate concurrency by awaiting a whole answer before sending the interruption.

4. Implement a small salon text evaluation entry point in the appropriate existing evaluation/tooling location. Use the existing core turn runner and real salon definitions. Fixed caller scripts are the default. An existing Pydantic AI model may supply adaptive caller replies when needed; no LangChain or extra orchestration framework. A caller model may see only its role/goal and delivered conversation, not target state, tool internals or the expected result. Business assertions are independent of both models.

   Define four named, versioned cases:
   T1 normal booking: correct identified subject and offered slot, one matching booking receipt.
   T2 correction/conditional acceptance: “Yes, but make it Wednesday” must not book the Tuesday proposal; only a later acceptance of the revised delivered readback can book.
   T3 prerequisite detour: request booking before identity is established, resolve verification, then resume the intended booking without losing its requested details or treating verification consent as booking consent.
   T4 uncertain write: inject a backend outcome that is initially unknown; reconcile before retry, retain at most one actual booking, and never announce unproven success.

   Reset the test diary per case. Acknowledgements identify delivered text segments explicitly. A returned text string is not an audio playback event. Save input, delivered output, typed interpretation, operation attempt/result, final diary/state, request counts and timing. Enforce the contract's turn/request/token limits and no automatic retries. Its no-network self-check must prove that wrong outcomes and budget exhaustion are reported, not accepted.

5. Make the existing Coval runner support the narrow checkpoint before launching it. Read-only planning must resolve one exact salon agent ID, exact test cases/personas/metrics, iterations, mutations and the expanded simulation count. Reject ambiguous agents, accidental baseline/Faro targets, empty selections that mean “all,” and counts above the assigned allowance. The existing test-set selection can match several agents; fix that path once instead of trusting names at the call site. Do not assume one iteration equals one simulation.

   Reuse two explicit one-case jobs if that is simpler than adding sampling. Launch one call, collect/review its result, then launch the next. Preserve the existing tunnel and bot lifecycle. Record the run ID immediately; an uncertain response must be reconciled before considering a retry. A local wait timeout neither proves failure nor cancels the remote call.

6. Use the relevant Coval skills: coval-resources, build-test-suite, design-persona, configure-metrics, and one launch/watch/results workflow. Reuse existing IDs and fixtures. Choose only the normal-booking and pause/correction cases from the shared contract. Keep one iteration and no mutations. Check the actual supported duration limit and current pricing before stating a cost estimate. Do not run the documented broad example set or an accent/noise/persona sweep.

VERIFICATION

- Offline runner tests reject wrong target, oversized jobs, duplicate jobs and uncertain launch retries without making network calls.
- Text evaluator fails when its diary/receipt assertion is deliberately wrong and stops at its limits.
- Ordinary no-network tests disable every actual model, including classifiers; all STT/TTS/HTTP boundaries are stubbed.
- The event replay exposes which input/output intervals overlap and which readback was heard. A scenario name containing “interruption” is insufficient.
- All existing baseline behavior is recorded without prompt/model tuning.

Run from the root:
uv run ruff check .
uv run ruff format --check .
uv run --directory core ty check .
uv run --directory core python -m tests
uv run --directory use-cases/salon-graph ty check .
uv run --directory use-cases/salon-graph python -m tests
uv run --directory use-cases/concierge-tools ty check .
uv run --directory use-cases/concierge-tools python -m tests
Also run the existing Faro and salon-prompt offline suites and ty for every remaining member. Faro-tools/bench.py is billable and is not a test command.

After cheap checks, run the four bounded live-text cases and at most TWO Coval simulations against salon-graph. Capture audio plus correlated execution evidence. If the stress call did not actually produce a requested overlap, mark that coverage not exercised. Do not spend another call automatically.

DELIVER AND STOP

Write docs/research/reusable-core/session-1.md with baseline commands/results, exact reusable evaluation commands, frozen case IDs/configuration, actual run/trace IDs and spend, event coverage, numeric provisional timing budgets, and a method-to-responsibility migration inventory. Report core/model/backend/audio timings separately; two calls establish a smoke baseline, not p95 or reliability.

Session 2 may begin when the baseline is reproducible, evaluation selection is bounded and known defects are explicit. Missing credentials can leave live evidence pending while offline preparation finishes. Do not claim the baseline is fully verified or expand implementation into session 2.
~~~

## Prompt 2 — Establish the package, state/session boundary and delivery contract

Copy this complete block after session 1's implementation and report are available.

~~~text
Implement session 2 of the reusable voice core migration. Read AGENTS.md, CLAUDE.md, the supplied roadmap, the Verification contract in docs/plans/reusable-voice-core-sessions.md, and session-1.md. Apply pydantic, python-dev, building-pydantic-ai-agents and ponytail:ponytail. Continue from the accepted code; do not reset to the planning revision.

OBJECTIVE

Make the existing slng-voice-core distribution installable and usable through a small public boundary. Establish a concrete application-state contract independent of core's runtime session and application services. Tighten delivery so unknown or interrupted readbacks cannot authorize consequential actions. Existing salon and Faro entry points must remain runnable during this foundational change.

STARTING POINTS

Read core/pyproject.toml, core/src/core/__init__.py, state.py, proposals.py, history.py, turn.py, processor.py, steps.py, observability.py and their callers. Read core/tests/clinic.py, test_reuse.py, salon's test_state/test_core/test_continuation/test_consent/test_recovery modules, and the application state classes before deciding what to move.

The current __init__.py is empty, core has required voice/tracing dependencies, FlowState has an unrestricted facts dictionary, and CallContext/state subclasses carry application-specific runtime behavior. Reuse the existing engine and commit logic. A new top-level facade alone does not complete this work.

IMPLEMENTATION

1. Build the existing wheel, then identify the minimal documented entry points needed to validate/load a definition, create a session with dependencies, drive a text turn and report delivery. Names are an implementation decision; settle them once and document them. Preserve deep imports temporarily for named unmigrated consumers.

2. Move optional voice and tracing dependencies into extras within the same distribution. Importing the core facade and validating definitions must not load .env, instantiate provider clients, require API keys, initialize a tracer or import Pipecat. Resolve provider clients at the explicit runtime boundary. Keep environment handling in launch/application setup. Declare direct dependencies genuinely imported by the core rather than relying on a tracing/voice extra's transitive packages.

3. Separate three objects:
   - Application state: the supplied concrete Pydantic model with fields, nested models, defaults, enums, literals and built-in constraints.
   - Core session: active step/pending destination, logical turn and generation, message/speech history, proposals and approval evidence, interpretations, budget accounting and operation evidence.
   - Dependencies: explicitly constructed diary/store/API clients, clock and services with documented shared or per-call lifetimes.

   A new use case supplies its model without subclassing CallContext. The engine may use an internal context/wrapper; applications must not implement lifecycle hooks to access normal behavior. Preserve current applications via adapters until their migration sessions.

4. Make both standard and custom steps use the exact supplied application state type. A dict of individually validated facts is insufficient. Keep approved field access for prompt context, including nullable/discriminated variants, without exposing secrets/service objects to templates. Do not treat Pydantic AI deps_type as runtime validation.

5. Preserve one atomic update boundary. Construct and validate the whole candidate before replacing state; reject unknown fields and invalid nested values without changing the original. Do not use unvalidated model_copy(update=...), model_construct or sequential field assignment as public update paths. Prevent caller-owned mutable input containers from mutating committed state later. A frozen outer model alone does not solve nested mutation.

   Split ownership: model collection writes only fields declared collectible; it cannot write verification, approval, completion or receipt evidence. A well-shaped “verified” model supplied by an LLM is still untrusted. Evidence is established only by the guarded result path. Complex domain invariants belong in named domain execution/commit checks, not validators with storage or conversational behavior.

6. Preserve invalidation and stale-work semantics while moving runtime fields out of application state. Corrections invalidate dependent selections, derived completion and approval eligibility. A stale model/classifier cannot route the current call. A real completed business write from an older generation remains recorded for reconciliation even though it must not overwrite current routing or another identity's visible state. Do not erase the audit record of an actual booking merely because the caller changed topic.

7. Repair delivery at the shared speech/proposal boundary. Trace ask/pending/last, note_said/note_heard/note_reached/note_drained, note_spoken and processor playback callers. Record delivered, interrupted and unknown explicitly. Approval references the exact operation/arguments, identity, relevant state/proposal revision and delivery record. Distinguish multiple identical wordings by segment/proposal identity, not text equality alone. A full repeat of the current readback can qualify; a late event for an older copy cannot qualify a new proposal.

   Explicitly acknowledge text delivery in tests/text adapters. Neither run completion nor TTS generation completion proves audible delivery. Keep accepted caller input and completed tool results from the SDK's new_messages boundary after cancellation teardown. Preserve logical turn continuation when the caller resumes before the answer has played out.

REQUIRED NEGATIVE AND POSITIVE CHECKS

- Invalid multi-field or nested update: original state/value graph unchanged.
- Attempt to set a protected field through collection: rejected before backend dispatch.
- Mutating an object passed into commit: cannot silently alter saved state.
- Two interleaved sessions sharing one compiled definition: distinct proposals, caches, histories and mutable state.
- Generated readback with no acknowledgement + yes: no write.
- Partial readback + yes: no write.
- Interrupted first copy, fully delivered current repeat + yes: exactly one permitted attempt.
- Identity/argument revision, stale proposal, unrelated yes, conditional yes or replay: no authorization of the original action.
- Late playback-stop or interpretation from an old generation: no false delivery or stale action.
- Cancellation after a matching successful receipt: retain the effect/evidence; do not execute it twice.
- All old direct-core/Pipecat parity and split-utterance tests retain their business assertions.

PACKAGE PROOF

Use uv to build the core wheel. In a fresh temporary project outside the checkout, install that artifact and only declared dependencies. Clear inherited project paths and keys. Assert the imported module resolves to the installed artifact, not workspace source. Validate a small definition and run an offline scenario. Separately test voice/tracing extras and meaningful missing-extra diagnostics. Record exact commands/artifact hashes and dependency sets. uv sync in the shared workspace is not isolation evidence.

Run common Ruff/format/core/salon commands plus ty/tests in every affected consumer, including temporary compatibility adapters. Then run session 1's four live-text cases and at most TWO frozen Coval salon calls. Keep all evaluation identities/settings fixed; do not tune models or endpointing to hide a regression.

DELIVER AND STOP

Save session-2.md, a concrete public import/session example, the ownership table, and each remaining compatibility consumer. Gate: clean isolated installation, concrete state on the engine, atomic/ownership/isolation checks, repaired unknown-delivery invariant, preserved current apps and documented live results. Leave absent live evidence pending. Do not yet migrate the full salon/Faro authoring or redesign the solver.
~~~

## Prompt 3 — Complete the ordinary authoring contract

Copy this complete block after the package/session boundary is verified.

~~~text
Implement session 3 of the reusable voice core migration. Read AGENTS.md, CLAUDE.md, docs/plans/reusable-voice-core-roadmap.md, the Verification contract, and session-2.md. Apply pydantic, python-dev, building-pydantic-ai-agents and ponytail:ponytail. Live evaluation remains salon-graph only with the same bounded allowance.

OBJECTIVE

An ordinary new application must consist of agent.yaml, fields-only Pydantic models, prompts, typed domain/backend functions and a thin entry point. It must not supply a CallContext subclass, custom confirmation interpreter, Conversation/Desk/AgentBuilder assembly, or one Operation subclass per ordinary function. Prove this with the existing clinic fixture and one salon identity/readback/action slice.

READ AND EXTEND

Start in core/src/core/flowspec.py (FlowSpec, resolver, diagnostics and compile), steps.py (standard kinds, ConfirmedOperation, AgentBuilder and StepFlow), completion.py, graph.py, flow.py, operations.py, policies.py, readings.py, interpreters.py, conversation.py and decisions.py. Trace core/tests/clinic.py and test_reuse.py. Preserve the existing pydantic_graph execution and core.turn engine; do not implement another scheduler.

DEFINE THE MINIMUM MANIFEST

Use agent.yaml as the public application manifest. Evolve FlowSpec and translate old flow.yaml into the same compiled definition while consumers still need it. Do not maintain two parsers with independent behavior or require duplicate files.

Express the following concepts, selecting final field names in this session:
- Application ID/version, concrete state reference, entry step and dependency-factory reference.
- Model defaults/overrides and locale binding. Speech-provider configuration belongs to the adapter side.
- Reusable agent/persona instructions and explicitly exposed tools.
- Standard collect, confirm, action, conversation and end steps; a custom domain-node extension.
- Explicit transitions, prerequisites, their satisfier and return destination.
- Operation function, input bindings, permissions/approval policy, argument and receipt models, allowed assignments, completion/failure/unknown outcomes, readback and receipt speech.
- Choice source and stable offered identifiers; invalidation dependencies on actual domain facts.

Python types/signatures are the schema source of truth. YAML connects them; it does not repeat field type declarations. State-to-argument and receipt-to-state bindings must be explicit and checked for compatibility. For example, binding a confirmed subject and selected slot to booking arguments should not ask the LLM to recreate either one. A receipt with an options field is not implicitly an offer to the caller.

Use a small closed set of predicates: field presence, trusted identity/evidence, receipt status and allowed choice; unusual business rules use named typed functions. No arbitrary expressions, embedded Python, prompt-text dependency inference or chains of generic callbacks. Specify what “present” means for None, empty values, False and zero. Reuse valid existing semantics and test any necessary change. Refuse unresolved/ambiguous prerequisite providers or unsupported prerequisite cycles rather than guessing a route.

STARTUP VALIDATION

Before any provider connection, diagnose unsupported version, unknown fields/kinds, duplicate IDs, missing modules/resources, bad prompt references, incompatible bindings, undeclared edges, illegal protected-field assignments and missing approval paths. Include the application/step/reference in errors. Standard and custom nodes receive the same concrete state. Resources load from the installed package, not relative to the caller's cwd.

Consider native Pydantic AI AgentSpec for supported model/settings/template construction if it actually removes custom plumbing. Do not wrap it merely to add another layer. Native deps_schema/template checking does not validate domain state at runtime; output_schema alone does not replace actual output/receipt Pydantic validation. Existing AgentBuilder already has prepare_tools/prepare_output_tools hooks: connect declarations to them, rather than inventing duplicate preparation.

GUARDED OPERATIONS AND SPEECH

Adapt typed ordinary functions into the existing shared Operation boundary. Both model-selected tools/output finishers and application-selected actions must perform the same sequence:
validate full arguments → settle unresolved previous outcome → check current preconditions and exact approval → consume one authorized attempt → call backend → validate and match receipt → atomically apply allowed assignments/invalidation → record audit → render outcome.

Keep one flow-changing/business-mutating action per model reply, and a shared turn-wide budget across classifications, direct actions, tools and graph hops. A direct transition must recheck authorization. Tool visibility is not authorization; direct calls with stale or forbidden inputs must still fail.

Successful speech requires a successful receipt for that operation, subject and arguments. A failed receipt, generic saves counter, acted_this_turn flag, or unrelated successful action cannot justify “booked/saved/activated.” An action attempt is not success. Transactional steps use application-rendered speech; free-form coaching/conversation remains possible with its stated weaker wording guarantee. Do not expose partial tool arguments as executable operations.

Unknown outcome goes to reconciliation with the same stable operation identity before a retry. Backend idempotency is an explicit service contract, not a wrapper guarantee across crashes. Undo calls a real compensating operation with its own permission and receipt. Invalidating current completion eligibility does not erase an external effect or historical receipt.

INTERPRETATION

Derive standard narrow readings from the active declaration: readback approval, offered choice, revision/withdrawal, clarification and closing. Their result proposes meaning, never executable actions or arbitrary state patches.

Execution order is: known application event can continue directly; pending-question reply uses the relevant reading; unresolved ordinary speech goes to the conversational step; domain reasoning goes to its declared domain node. Do not create a mandatory classifier → solver → agent chain.

Return accept/reject/revise/conditional/unclear as appropriate. Failed or ambiguous interpretation grants no approval. Cache only for the same utterance, proposal and relevant context; stale asynchronous results are discarded. Interpret choices only among actually offered IDs. Keep standard decision types still used by confirmation. Keep the solver experimental and outside normal author configuration.

PRESERVE THE NATIVE-APPROVAL DECISION

For known confirm → action flows, keep direct guarded execution and output functions. Do not replace them merely to use requires_approval. Native deferred tools apply to a dynamically proposed ordinary function tool when that pattern is actually needed.

If implementing that optional path because a real consumer requires it, first add a focused offline compatibility experiment against the locked SDK: original pending call survives history projection; resume is bound to its call ID; denial/replay/batched proposals cannot bypass one-action policy; cancellation while awaiting approval is safe; corrected arguments require a new readback rather than ToolApproved overrides; count the model continuation after a normal tool result. Use the same consent authority. Otherwise document the decision and defer this integration; it is not a prerequisite for this session.

PROVE THE AUTHORING EXPERIENCE

Rewrite the independent clinic fixture around the public loader. A clinic author supplies state/argument/receipt models, appointment functions and prompts. Offer two explicit IDs, read back a selected appointment, accept/reject/revise it, book it, and compensate if asked. No application runtime subclass/assembly.

Preserve composability with small offline variants of the same fixture: one conversational agent, declared specialist handoff, and a controlled collect/confirm/action sequence. One persona may serve several steps; do not force one personality per node. Guards and receipts remain identical at every agency level. Do not resurrect removed help/access projects for this proof.

Then exercise one salon identity → readback → approved action path with its real types/domain functions using the same public definition. This catches a clinic-only abstraction before session 4 migrates everything.

ACCEPTANCE

- Bad definitions fail before model initialization with actionable diagnostics.
- Multi-field collection updates only allowed fields; protected fields and invalid nested data are rejected.
- Changing an upstream identity/selection invalidates downstream approval/completion and routes to the required evidence.
- Direct and model-selected paths refuse identical unauthorized/malformed/unknown-outcome cases.
- A matching delivered proposal and acceptance produce one backend effect and matching confirmation.
- Unrelated/malformed receipts never complete a step or unlock success speech.
- Ordinary conversation without a pending decision does not pay unnecessary confirmation requests.
- Ordinary clinic steps require no custom Python step module, runtime subclass or core edit.
- Installed-package clinic and salon slice run without sibling-source imports or cwd assumptions.

Run common checks, affected-member checks, installed-package/resource proof, frozen four-case live-text salon evaluation and at most TWO Coval salon calls. Clinic is offline proof of the new API; salon voice calls verify shared-engine regression. Do not claim voice has exercised an API path that the call did not actually use.

Save session-3.md with the actual manifest schema/public imports, valid minimal example, invalid-definition examples, operation/interpretation evidence and measured authored orchestration versus domain code. Gate: complete ordinary authoring works on clinic and the real salon slice with one engine and one consent authority. Stop before full application migration.
~~~

## Prompt 4 — Migrate salon-graph through the public contract

Copy this complete block after the clinic and salon slice prove the authoring contract.

~~~text
Implement session 4 of the reusable voice core migration. Read AGENTS.md, CLAUDE.md, the roadmap, the Verification contract, and session-3.md with its implemented public schema/example. Apply pydantic, python-dev, building-pydantic-ai-agents and ponytail:ponytail. Preserve assert-script tests. Live target remains ONLY salon-graph.

OBJECTIVE

Make the production salon definition consume the public authoring/session API. Remove repeated runtime assembly while preserving booking, moving/cancelling appointments, complaints, identity, corrections, detours and recovery. A smaller file is not success if it hides the same machinery under a renamed SalonRuntime class.

TRACE THE COMPLETE SALON PATH

Read use-cases/salon-graph/salon/flow.yaml, agents.py, nodes.py, instructions.py, classification.py, models/state.py, models/consent.py, models/receipts.py, steps/*.py, tools/*.py, diary.py and bot.py.

Currently flow.yaml describes four hand-built agent steps: concierge, verify, booking and complaints. nodes.py assembles AgentBuilder and decides between narrow readings and solver. SalonConversation merges common readings; SalonCall owns application-specific proposal wrappers, offers, held complaint consent, read_reply and speech-claim checks. tools/writes.py centralizes business writes. Understand every caller before editing these boundaries.

MOVE RESPONSIBILITIES DELIBERATELY

- Keep SalonState and argument/receipt/value types as domain data declarations. Replace its behavioral identity validator with the shared guarded state/result contract plus domain invariants where needed.
- Inject Diary and clock through dependencies instead of a custom call object.
- Use manifest identity/evidence/locale bindings rather than SalonCall.identity and standard lifecycle overrides.
- Declare explicit offer source/IDs, common confirmation/correction/closing policies, and standard readbacks instead of call-owned table/proposal/interpretation assembly.
- Preserve real salon meanings such as move versus add, complaint summary, diary rules and how ambiguous appointment references are resolved. Those may remain typed domain functions or specialized nodes.
- Replace SALON/DESK/BUILDER assembly and per-step builders with public declarations where behavior is standard. Keep a custom step only for actual domain reasoning, with documented inputs/results; it must not rebuild core consent or execution.
- Keep the salon import package and use-cases/salon-graph runnable location. Do not import salon-prompt or another use case's private runtime.

MIGRATE IN REVIEWABLE SLICES

1. Move the proven identity/readback/action slice fully onto the public entry point. Confirm that collected phone digits do not create verified identity. Existing customer/verified_phone evidence must remain bound to the same subject and be invalidated on corrections.
2. Port booking inquiry, offered-slot choice, add versus move, booking confirmation, cancellation/undo and uncertain-write recovery. Bind arguments from state/evidence instead of asking the model to reconstruct trusted identifiers.
3. Port complaints: retain the caller's actual description, exact current summary and approval; preserve verification detours and retained-approval rules. A changed identity, changed complaint or stale proposal cannot inherit the old permission.
4. Port concierge handoffs, prerequisite return, side questions and closing. Keep the caller's unfinished goal when verification interrupts it. Validate borrowed entry tools/via transitions again after execution, not only when schemas are prepared.
5. Remove redundant call methods and assembly only after their production callers and test helpers use the public contract. Leave legitimate diary/service logic in the salon.

PROMPTS AND SPEECH

Move wording without silently improving it. Render prompts/tool schemas at representative unverified/verified/booking/complaint states and compare the existing golden snapshots. If context paths change, adapt the binding while preserving rendered meaning. Treat tool docstrings as prompts. Record and evaluate any unavoidable semantic wording change within the bounded salon model allowance; do not regenerate all snapshots to hide it.

Replace claims about completed transactions with receipt-rendered outputs. For every removed regex or word-list guard, identify the behavior it protected and add its replacement regression first. Explicitly test:
- “Nothing was saved” after failure remains an allowed truthful response.
- “Your complaint is saved” after only a booking succeeded is denied.
- A completed operation cannot authorize a success claim about different arguments/identity.
- Equivalent unsupported success claims in supported languages cannot escape a transactional step's constrained output policy.

Do not claim all free-form concierge prose is mechanically true. Keep conversational flexibility and control the consequential completion outputs. Never replace the writes counter with acted_this_turn as success evidence.

BUSINESS ACCEPTANCE MATRIX

Use fixed diary time and existing synthetic identities/slots; assert backend records and receipts, not just response strings.

A. Booking before identity: verification interposes; successful verification returns to the intended booking; no booking has happened yet.
B. Pick an offered slot: the runtime reads back its exact operation/arguments; the pick alone does not authorize booking.
C. Delivered current readback + clear acceptance: exactly one intended booking and matching receipt speech.
D. Reject/condition/correct/unrelated yes: zero original writes; a correction produces a fresh readback.
E. Move existing appointment: the original identity/booking is checked, one move occurs, and an unrelated second appointment is not created.
F. Cancel/undo: execute the guarded compensating operation and validate its receipt; no in-memory-only undo.
G. Complaint: preserve quoted description and approved summary through permitted detours; never file a changed summary on old approval.
H. Timeout/unknown result: reconcile with stable operation identity before retry; neither false success nor a duplicate booking.
I. Interrupt identity or transaction readback: pending permission cannot be earned from unheard words.
J. Interrupt after successful backend receipt: booking/complaint remains recorded; the next turn can explain the true outcome without repeating the effect.
K. Superseded classifier/model result: cannot move the current route or authorize a stale action.
L. Multi-action model reply/direct-action continuation: one-action and whole-turn budgets still apply.

Keep the existing test_graph, test_salon, test_consent, test_classifiers, test_replay, test_solver, test_traces, test_state, test_diary, test_flow, test_budget, test_recovery, test_continuation, test_optimization and test_core assertions where behavior remains intended. Preserve shared decision types even if experimental solver assembly is reduced. Do not make solver selection part of the ordinary author's API.

VERIFICATION AND LIVE CHECKPOINT

Run:
uv run ruff check .
uv run ruff format --check .
uv run --directory core ty check .
uv run --directory core python -m tests
uv run --directory use-cases/salon-graph ty check .
uv run --directory use-cases/salon-graph python -m tests
Run all affected tooling/compatibility checks, public-entry clinic regression and an isolated salon consumer/resource check.

Then run the same four bounded live-text cases and at most TWO frozen Coval salon simulations. Prove the entry point actually loaded the migrated manifest and public session, using its recorded version/hash. Inspect audio plus proposal/delivery/operation evidence. Compare rendered prompt changes and provider request counts explicitly; natural-language approval may still require a classifier request. Do not claim zero-model approval from historical numbers.

DELIVER AND STOP

Write session-4.md with each old orchestration responsibility and its new declaration/core owner, retained domain functions, prompt diffs, business-matrix results, real evaluation evidence and remaining compatibility. Gate: real salon behavior runs through the documented API without custom runtime/consent assembly, critical invariants pass, and live evidence is present or accurately pending. Do not widen this session into Faro or launcher extraction.
~~~

## Prompt 5 — Migrate Faro's lifecycle, grading and persistence

Copy this complete block after the salon proves the public contract in a real application.

~~~text
Implement session 5 of the reusable voice core migration. Read AGENTS.md, CLAUDE.md, the roadmap, Verification contract and session-4.md. Apply pydantic, python-dev, building-pydantic-ai-agents and ponytail:ponytail. Keep all live text/Coval verification on salon-graph. Do not launch Faro's live bench.py or any Faro Coval run.

OBJECTIVE

Prove the same contract supports a stateful learning application. Faro should author data, lifecycle declarations, prompts, grading/curriculum functions and persistence operations. It must not need FaroCall, a replacement runtime subclass, its own consent engine or repeated phase-based agent assembly.

READ THE REAL RESPONSIBILITIES

Start in use-cases/faro-language-learning/faro/state.py, models.py, operations.py, conversation.py, flow.yaml, steps/companion.py, curriculum.py, store.py, __init__.py, its bot.py and tests/test_faro.py. Read faro-tools/bench.py only to preserve consumer compatibility; it is billable.

Faro's current flow has one companion agent. Its phases and _in/_when tool gates live in Python. FaroCall owns store access, private learner/plan/draft/session records, load, write_brief, locale/identity, lifecycle save counters, coaching-assistance tracking and save-claim regexes. Deleting that class by moving every method into another orchestration object would fail the authoring goal.

USE THIS RESPONSIBILITY MAP

- Learner/language/plan/exercise/checkpoint data → plain concrete Pydantic state/result models.
- SQLite Store, clock and service lifecycle → injected dependencies owned by application wiring.
- load() → validated domain result projection plus the shared atomic commit path. Do not mutate private domain records before a failing state candidate is validated.
- write_brief() presentation → prompt templates. Curriculum lookups and derived educational facts inside it → named domain context functions returning typed data.
- locale/identity selection → public bindings; preserve explanation-language switching independently of target language.
- Proposals, consumed approvals, current step, history, receipts, turn counters and generic save-speech checks → core session/policies.
- Placement grading, assisted-answer tracking, curriculum progression and review items → domain node behavior using shared execution/budget/staleness infrastructure.

DECLARE THE LIFECYCLE ONCE

Use the same companion persona across the following permission/completion stages. Exact step IDs are your implementation choice; document the mapping from current phases.

1. Identify: caller ID is an untrusted hint; read back the number and establish current-call evidence before looking up or exposing learner data.
2. Sign-up: an unknown learner must accept account creation; declining leaves no learner row. A phone's presence is not sign-up permission.
3. Onboarding: collect/validate supported target language, learning goal and session length; save an acknowledged checkpoint. Preserve current supported-language behavior.
4. Assessment: a domain grader evaluates placement evidence and persists each meaningful attempt under existing rules. The conversational model coaches but does not award grades or mark completion.
5. Plan proposal/approval: build a draft from validated domain evidence; read back its specific version/content; activate only on fresh delivered approval. Decline/revision leaves the currently agreed plan unchanged.
6. Lessons: use a reusable domain lesson node/loop, not a node per exercise. Preserve current exercise, attempts, assisted answers, review list and progression evidence.
7. Ending/resume: goodbye and disconnect finalize/checkpoint through domain persistence operations and clean up once. A future call resumes from acknowledged storage after identity is re-established.

Derive ordinary tool exposure from these stages while retaining checks inside executors. Calling a hidden tool directly must still fail when its prerequisites do not hold. Agent/persona identity and workflow stage are separate choices.

STATE AND IDENTITY

Replace _nothing_before_confirmation with a structural representation such as unidentified versus identity-established data, plus runtime gates on Store lookup and prompt exposure. Shape alone is insufficient: constructing an “identified” object or collecting a phone cannot grant the transition. A changed identity removes the prior learner's visible facts, proposals and eligible receipts and requires new evidence. Historical audit data can remain without entering the new learner's prompt.

Keep existing reusable PhoneNumberValidator/value-type behavior. Format validation does not recover digits the caller never supplied and does not prove confirmation. Do not infer the target/explanation language from a phone number. Fields-only state can use validated reusable value types; it cannot contain storage, prompt-building or conversational lifecycle behavior.

GRADING, SPEECH AND STORAGE

Keep GRADE/ONBOARD domain schemas, prompts and interpretations with Faro as appropriate. Use the runtime's shared model invocation accounting, cache validity and stale-generation checks. Preserve the distinction between no attempt, misheard/uncertain speech, incorrect answer and assisted correct answer. A goodbye during an exercise is not a graded attempt.

Preserve the current assistance-tracking semantics with explicit domain inputs/events; do not lose them when removing note_model_text overrides. If changing when help counts from generated speech to delivered speech, treat it as a separately justified behavior change with regression evidence, not an incidental move.

Replace _CLAIM/saves-based completion checks with confirmations from exact acknowledged receipts. “Nothing was saved” after failure is valid; “your plan is activated” after an unrelated checkpoint save is not. Free-form coaching remains allowed under its weaker wording guarantee. Remove each old guard only after the replacement negative case fails without its protection.

Keep Store, curriculum and backend idempotency/reconciliation domain-owned. Preserve SQLite transaction/durability settings, checkpoint conflict checks, process-restart behavior and operation identities. Do not add a universal persistence abstraction. Document the existing single-worker and process-local active-learner limits; neither becomes distributed through packaging.

OFFLINE ACCEPTANCE, USING REAL TEMPORARY SQLITE

Map every existing requirement to its named check in tests/test_faro.py:
- L01–L02: seeded identity is disclosed only after acceptance; wrong, malformed, foreign and corrected numbers reveal nothing.
- L03–L05: sign-up/onboarding/placement work; unsupported language is handled; refusal, conditional acceptance and revised plan do not activate the original.
- L06–L07: restart resumes onboarding/assessment/plan and the correct lesson exercise; replay does not duplicate progress.
- L08–L09: recap does not mutate learning; reassessment keeps the agreed plan until the revised draft is accepted.
- L10–L11: honest grading evidence controls progression; interrupted plan readback cannot activate.
- L12–L13: goodbye/disconnect finalize once; stale checkpoint and half-failed transaction do not publish partial state.
- L14–L16: memory belongs to confirmed identity; unearned completion is blocked; fixture reset and normal restart have distinct behavior.
- Preserve additional checks for killed-process durability of an acknowledged answer, phase/tool visibility, assisted answers, code-owned onboarding and language switches.

Use scripted Pydantic AI models, real temporary database files and process kill/reopen checks where already present. Spy on Store lookup to prove no pre-approval read, not merely no spoken disclosure. Assert persisted records after interruption/restart, not just current in-memory state.

PACKAGE AND REGRESSION CHECKS

Package Faro prompts, voice.md, curriculum/configuration and manifest as resources. Load from an unrelated cwd in a clean environment with built Faro/core artifacts. Put databases/backups in explicit writable runtime locations, never site-packages.

Run common core/salon commands, Faro ty and python -m tests, package-resource checks and offline compatibility checks for faro-tools if imports change. Then run ONLY the frozen salon four-case live-text checkpoint and at most TWO salon Coval calls to detect shared-core regressions. Faro's own evidence in this session is offline.

DELIVER AND STOP

Save session-5.md with phase-to-step and method-to-owner mappings, the final authoring files, full L01–L16/additional check results, persisted-state evidence, resource-install proof and salon regression results. State explicitly: “Faro migrated and checked offline; not newly validated with live models or voice.”

Gate: the same public contract supports Faro without custom generic runtime assembly; identity, grading, persistence and resume checks pass; shared salon behavior is preserved. Do not claim horizontal scalability or change the curriculum/model/persona as cleanup.
~~~

## Prompt 6 — Finish the launcher, tooling consumers and release proof

Copy this complete block after salon and Faro use the public authoring contract.

~~~text
Implement session 6 of the reusable voice core migration. Read AGENTS.md, CLAUDE.md, the roadmap, Verification contract and session-5.md. Apply pydantic, python-dev, building-pydantic-ai-agents and ponytail:ponytail. Live text and voice evaluation remain salon-graph only, using the final assigned two-call Coval checkpoint.

OBJECTIVE

Complete useful adoption in all five consumers, demonstrate installation outside the workspace and remove replaced compatibility. This is the release-readiness milestone, not authorization to publish or deploy.

READ THE CONSUMER BOUNDARIES

Compare the three bot.py files in salon-graph, salon-prompt and faro-language-learning. Read core.processor/core.observability/model_clients; salon-prompt/salon/baseline.py and utils/observability.py; concierge-tools/coval_sim and ledger.py; faro-tools/bench.py; every member pyproject.toml and each suite runner.

1. EXTRACT THE SHARED LAUNCHER

Move repeated transport creation, speech-service setup, VAD/turn strategies, pipeline worker/tracing setup and common lifecycle handling into the optional core voice boundary. Reuse installed Pipecat facilities. Accept the controller and genuine application-specific dependencies/callbacks rather than branching on application names.

Preserve the actual pipeline ordering, audio configuration, token aggregation, greeting behavior for web/phone clients, session/call correlation IDs and disconnect draining. For core-backed controllers, preserve playback-run metadata and the linkage among response start, assistant playback start/stop, delivered segments and interruption. A simplified launcher that stops emitting delivery evidence is a correctness regression even if the bot answers.

Keep provider/environment initialization explicit. Shared clients have bounded, documented lifetimes; session data stays private. Cleanup closes service/model/store resources and workers/tunnels once. Preserve the existing bounded finish-current-turn behavior without claiming a hung external write has failed simply because shutdown timed out.

Give salon/Faro thin entry points: load application definition, construct services, supply domain-specific greeting/finalization where needed, invoke launcher. No repeated core engine/agent assembly.

2. MIGRATE THE BASELINE WITHOUT CHANGING ITS IDENTITY

salon-prompt must declare and use core's launcher/tracing infrastructure, while its conversational controller remains Pipecat's native GoogleLLMService and its existing baseline prompt/tools. It must not acquire PydanticAgentProcessor, harness approval or graph routing through a default.

Record the controller class and rendered prompt/tool schemas before/after. Test launcher wiring with fakes and native-service construction without network. Compare relevant transport/model/voice settings. Keep equivalent salon diary/prompt domain assets explicit through comparison/versioning; do not move salon business code into core. A later controller replacement would be a separately named benchmark variant.

3. FINISH TOOLING CONSUMERS

concierge-tools: consume the smallest versioned public event/audit/result and launch metadata contracts needed to run calls and read new traces. Reuse existing fields where adequate. Preserve the ability to interpret historical traces through explicit old-format handling. A newly named operation must appear in reports without adding its name to a task-specific allowlist.

Represent enough evidence to correlate application/version, session/turn/generation, operation identity/name, proposal/delivery reference, attempt status and validated result. Keep unknown distinct from failed and succeeded. Do not put raw sensitive inputs in new default telemetry merely for reporting convenience. Retain zero-model turns, interrupted requests and missing measurements; count provider requests/tokens once, avoiding duplicate parent/child span totals.

faro-tools: declare slng-voice-core directly because it uses the public text/session interface, while retaining faro-language-learning for domain fixtures/assertions. Replace private call._say/_end/_proposals or internal turn-driving setup with public session/delivery operations. Preserve case scripts, isolation and domain assertions. Check with scripted models and temporary SQLite; do not run the billable Faro benchmark.

Every consumer must have a useful documented core import. An unused import or transitive dependency does not satisfy adoption.

4. REMOVE COMPATIBILITY AND DOCUMENT THE REAL API

Use repository searches to identify remaining old loader/deep-import/runtime-subclass callers. Remove only replaced paths whose consumers have migrated. End with one definition compilation and one execution path. Do not delete shared decision/history/receipt behavior just because an experimental solver used it.

Document an ordinary authoring example, state/session/dependency ownership, each standard step, custom domain-node extension, explicit choice/invalidation rules, approval and receipt speech, resource loading, extras, text delivery, launcher usage and migration from flow.yaml. Include valid and invalid configuration examples that run against the actual implemented schema.

Keep native deferred approval scoped to a concrete ordinary-function-tool requirement if one emerged; otherwise leave its compatibility decision documented. LiveKit parity, a Go/YAML generator, broad plugin discovery, distributed persistence and solver redesign remain separate work.

5. PROVE INSTALLATION AND ALL FIVE CONSUMERS

Build the core distribution and packaged application resources. Use separate clean temporary projects outside the checkout:
- core alone: facade import/definition validation/offline scenario with no keys or voice/tracing extras.
- core voice extra and tracing extra: expected imports and setup behavior, without hidden base-package dependency leakage.
- salon-graph: own application definition/resources and public core dependency.
- Faro: installed resources plus writable external database and offline lifecycle/resume.
- salon-prompt: native controller with useful shared infrastructure imports.
- concierge-tools: offline Coval/ledger tests and generic new-format operation reporting.
- faro-tools: declared Faro/core dependencies and scripted text adapter smoke.

Script-only consumers need an isolated dependency environment plus only that member's own sources. Do not add packaging scaffolding where it serves no distribution/import need. Never put the two salon packages in the same environment; their shared import name can conceal a wrong import. Do not rely on editable workspace installs, PYTHONPATH/sys.path injection, sibling bot/tests modules or a repository cwd. Record resolved module/resource locations and wheel hashes.

Run root Ruff and formatting checks, per-member ty, every offline assert suite and the new tool smoke checks. Re-run the external clinic authored without core modifications. Confirm import boundaries allow only the implemented adapter/launcher modules to import Pipecat, and no core module imports any use case.

Exercise two interleaved sessions with shared compiled definitions; cancel one and verify the other retains independent state/proposals/history. This proves isolation, not capacity. Check launcher startup, successful shutdown and failing setup cleanup with fake services for all three applications.

FINAL LIVE CHECKPOINT

Only after cheap checks pass, run the frozen four-case real-model text evaluation on salon and at most TWO salon Coval simulations. Use the extracted launcher and migrated public API. Compare with both session 1 and the last accepted checkpoint. Record any settings change that prevents a matched comparison.

Review actual audio and operation evidence for the normal booking and pause/correction cases. Confirm real overlap occurred before marking that coverage passed. Missing triggers/metrics or exhausted budget remain pending; do not silently spend more calls. Report audio latency separately from text/model/backend timing and do not infer p95 from two calls.

DELIVER AND ACCEPT

Write session-6.md mapping every requirement in the document's coverage table and the roadmap release checklist to a passing check/artifact or an explicit gap. Include useful core imports for all five consumers; package/resource proof; removed compatibility; remaining domain code; public API examples; changed prompt/settings history; total live calls, requests/tokens and known actual spend.

The final claim may be “reusable package adoption complete with salon live verification” only when its required evidence exists. Keep Faro and baseline live-validation limits explicit. Do not claim exactly-once backend effects across crashes, horizontal capacity or untested adapter parity. Do not publish packages, deploy, or create another platform as part of this session.
~~~

## Requirement-to-session coverage

Use this as the final audit index. The session reports supply actual evidence; this table is not evidence of completion.

| Requirement from the roadmap and additional notes | Owner session | Required evidence |
|---|---|---|
| Reuse the existing packaged engine and current workspace | 1–2 | Inventory, unchanged runnable consumers, one execution path |
| Plain application state, core session, injected services | 2; adoption 4–5 | Ownership map; no new application runtime subclasses |
| Concrete aggregate state and atomic validated updates | 2–3 | Invalid/nested/unknown-field rollback and protected-field rejection |
| Explicit invalidation and preservation of real external effects | 2–4 | Corrections clear eligibility, receipts/audit survive |
| Complete ordinary manifest and early diagnostics | 3 | Valid/invalid definitions and installed external clinic |
| Standard steps without custom assembly | 3–5 | Authored code inventory and real use-case acceptance |
| Flexible agent, handoff and typed-flow composition | 3–4 | Small fixture variants and declared salon handoffs |
| One operation boundary for direct and model-selected actions | 3–5 | Identical guard, budget, receipt and recovery checks |
| Exact proposal/identity/freshness/delivery-bound approval | 2–5 | Negative consent matrix and one-shot authorized attempt |
| Shared narrow interpretation; no required solver pipeline | 3–5 | Scoped reading requests, cache/stale checks, domain grader retained |
| Native SDK features used only where they reduce needed work | 3 | Version-checked AgentSpec/deferred-tool decision; conditional experiment |
| Completion speech derived from exact receipts | 3–5 | Failed/unrelated receipts and misleading claim negative cases |
| Cancellation, logical turns and delayed playback | 1–2; regression throughout | Timed replay plus observed salon Coval audio |
| Faro lifecycle, grading, memory and durable resume | 5 | L01–L16, additional assistance/durability checks, temporary SQLite |
| All five consumers have useful public dependencies/imports | 6 | Per-consumer import/command evidence |
| Preserve native salon baseline and domain equivalence | 6 | Controller/prompt/tool/fixture comparison offline |
| Packaged resources and dependency isolation | 2, 5, 6 | External clean-environment artifact checks |
| Generic audit reporting and historical trace compatibility | 1, 6 | New operation-name fixture; old trace fixtures; no double counting |
| Cost-conscious semantic and voice verification | Every session | Four bounded text cases; at most two salon voice calls; real coverage |
| No invented performance or scalability claims | Every session | Raw timing/request/spend evidence, sample counts and explicit limits |

## Using the prompts

Paste one complete numbered block per coding session, with the roadmap and this document available in the checkout. Each block names the starting files, intended behavior, work sequence, concrete acceptance checks and output report. The shared Verification contract supplies the frozen evaluation details.

Carry implementation and evidence forward. If a milestone needs another context, continue that milestone from its report; do not remove checks to fit or start the later migrations from an unmodified baseline. Session 3 proves ordinary authoring; session 6 completes adoption across the repository.
