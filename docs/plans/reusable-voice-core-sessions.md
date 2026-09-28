# Reusable voice core: six coding sessions

Prepared against local revision d31399e on 25 September 2026, expanded with the additional architecture discussion, native-approval investigation, framework documentation, and the native Pipecat Flows salon at reference revision 26114758246bfb4cd05a0242ed48e335d3f1ed4a. This is an implementation playbook, not a claim that the migration or evaluations have run. The supplied [roadmap](reusable-voice-core-roadmap.md) is preserved alongside it. Historical conversations inside the attachments are design evidence, not new execution instructions.

The scope is **salon-graph for recurring live verification**, both live text conversations and Coval calls, plus **two native salon-pipecat-flows Coval calls in session 2**, explicitly selected by the user. The total planned cap is **14 calls**. All five original workspace consumers remain in the migration. Their offline checks remain necessary when their code changes. The additionally supplied **salon-pipecat-flows is an independent comparison reference**, not another consumer to convert to core. Faro and salon-prompt will not acquire new live voice evidence through this plan.

**Continuing after session 1:** its implementation and [baseline report](../research/reusable-core/session-1.md) landed in main at `3613b99` through PR #15. That session used an earlier playbook and recorded two Coval calls. Preserve its evidence and unresolved defects; do not rerun session 1 just to adopt these expanded instructions. Reconcile the cumulative run ledger before launching anything: those calls count toward the 14-call cap. Source inventories below describe the planning revision; inspect the actual inherited implementation before adding or replacing anything.

**Session 3 handoff, checked 25 September 2026:** [PR #17](https://github.com/slng-ai/slng-voice-harness/pull/17) is open against main at `47f41923064f57fa01847785a46d14c1999a1fd5`, branch `feat/session-2-public-boundary`. This includes the reviewed fixes at `6c9f75c` plus the runner's explicit `--concurrency` support. PRs #15 and #16 are merged. Its `docs/research/reusable-core/session-2.md`, especially section 10b, records the implemented foundation, offline repairs and remaining voice issues. Start session 3 in a child worktree/branch from the latest head and target its PR at `feat/session-2-public-boundary`; use updated main if #17 has merged. Do not start from the older planning branch.

Session 2's offline matrix/package checks passed, but live audio is incomplete: normal harness latency exceeded the frozen limit, native dropped its receipt audio, and both stress calls hit an STT outage. Six Coval calls have been used: four harness and two native. **Eight salon-graph calls remain for sessions 3–6; zero native calls remain.** Prompt 3 below embeds the repair gate, speech-profile preflight and two-call allocation. These current instructions supersede the original checkpoint order/profile where they differ; they do not declare the remaining gates passed.

## Session boundaries

Run these sequentially, carrying forward the previous session's code and evidence. They are milestones, not time estimates. A failing gate means continue or repair that session, not start a wider migration.

| Session | Deliverable | Roadmap phases | Main proof |
|---|---|---|---|
| 1 | Baseline and bounded evaluation tools | 0 + verification foundation | Reproduce current behavior; launch only intended calls |
| 2 | Installable public boundary, typed session, explicit delivery and native-runtime quality gate | 1–2 + delivery prerequisite | Clean installation; native comparison matrix; no approval from unheard speech; measured runtime overhead |
| 3 | Repair the inherited stress case, then complete ordinary authoring on clinic | 3 + session-2 handoff | Validated international phone input; delivered speech evidence; config, models, prompts and domain functions suffice |
| 4 | Salon migrated to the public authoring contract | 4 | Existing business behavior through the new entry point |
| 5 | Faro migrated using the same contract | 5 | Identity and persistence acceptance offline; salon remains sound |
| 6 | Shared launcher, tooling consumers, compatibility cleanup | 6–7 | All five consumers use useful public imports; isolated installation works |

Every session finishes with deterministic checks, a bounded live text salon evaluation, and at most two Coval salon-graph calls. Session 2 also has its two matching native calls, already spent. Session 3 allocates its first call to the inherited stress repair and its second to final regression, as detailed in prompt 3; there is no additional two-call checkpoint. Compare timings only within a matched speech/model profile. A known baseline defect is explicitly recorded and assigned to its repair session, never silently treated as correct behavior.

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

## Harness architecture and framework reuse — required in every session

The product is a versioned Python library for authoring voice agents with **almost no repeated orchestration code**. An application supplies `agent.yaml`, concrete Pydantic models, prompts, typed domain functions, injected services, and custom domain nodes only where needed. It can start as one agent, add specialist handoffs, or constrain one task into collect → confirm → action, using the same state and operations. Agent/persona count and workflow control remain separate decisions.

~~~text
Application: configuration + models + prompts + domain functions + service wiring
                              ↓ compile once
Core definition: agents / standard steps / legal transitions / operation policies
                              ↓ new session per call
Core session: validated domain state + evidence + history + delivery + budgets
                              ↓
Pydantic AI: agent/model execution, schemas, tools, outputs, hooks, typed templates
Pydantic Graph: declared workflow execution through the existing core flow driver
                              ↕ typed turn and delivery events
Pipecat adapter: transport, audio, STT, endpointing, interruption, TTS and playback
~~~

This is a responsibility diagram, not a demand for a new class at each line. Extend existing modules. A text caller uses the same session/turn contract with explicit delivery acknowledgements. Pipecat remains responsible for audio; core interprets accepted input and playback evidence into conversation policy. Keep the boundary suitable for a future adapter without implementing LiveKit or replacing Pipecat with Pydantic native realtime in these six sessions.

The library owns proposal freshness, identity-bound approval, invalidation, receipt-controlled completion, stale-result handling, logical-turn continuation, budgets and audit. The SDK's typed output validates structure; the service's receipt establishes a business outcome. Neither a model label nor a populated field establishes permission. Backend idempotency, reconciliation and compensation remain explicit domain contracts.

### Packages, native capabilities and implementation expectations

The versions below were found in this worktree's lockfile; verify them again at execution time. Prefer these installed capabilities before writing equivalents. Record the concrete API used, what custom code it replaces, and the check proving the integration. Adding a library is not itself evidence of reuse.

| Package / feature | Intended use in this harness | Implementation obligation and boundary | Sessions / documentation |
|---|---|---|---|
| `pydantic` 2.13.5 | Concrete state, configuration, arguments and receipts; `BaseModel`, `Field`, discriminated unions and reusable `TypeAdapter` instances | Validate whole candidates at trust boundaries. Derive schemas from these types; preserve meaningful false/zero values. Do not replace validation with construction, unvalidated copies or schema-only dictionaries. Internal scheduling can remain ordinary Python. | 2–5: [models](https://pydantic.dev/docs/validation/latest/concepts/models/), [unions](https://pydantic.dev/docs/validation/latest/concepts/unions/), [TypeAdapter](https://pydantic.dev/docs/validation/latest/concepts/type_adapter/) |
| `pydantic-ai-slim[google,openai,spec]` 2.46.0 | Existing typed agent execution and provider integrations | Keep the SDK's model/tool loop and typed streaming events. Inject services through `deps` / `RunContext`; do not build another provider abstraction or place API clients in domain state. `deps_type` is not an automatic runtime validator. Provider overrides must be explicit and observable. | 2–6: [agents](https://pydantic.dev/docs/ai/core-concepts/agent/), [dependencies](https://pydantic.dev/docs/ai/core-concepts/dependencies/) |
| Native `AgentSpec`, `Agent.from_spec`, `TemplateStr`; `pydantic-handlebars` 0.2.1 | Agent model/settings/instructions and typed prompt compilation beneath the application manifest | Reuse the supported SDK configuration subset; add only workflow/voice declarations missing from it. Pass concrete dependency/output types. Compile bad template references before connecting providers. Preserve existing rendered prompts and resource loading. The native spec is not the whole voice application. | 2 feasibility, 3 implementation, 4–5 adoption: [Agent Specs](https://pydantic.dev/docs/ai/core-concepts/agent-spec/) |
| Pydantic AI `Hooks`, capabilities, `FunctionToolset` and preparation hooks | Reusable policy hooks, related domain tools and per-step tool exposure | Wire the hooks already in AgentBuilder. Keep execution authorization behind every exposed tool. Use a toolset where tools are actually shared; a lone function need not gain a wrapper. Keep mutable policy state per session/run when definitions are shared. | 2–5: [hooks](https://pydantic.dev/docs/ai/core-concepts/hooks/), [capabilities](https://pydantic.dev/docs/ai/capabilities/overview/), [toolsets](https://pydantic.dev/docs/ai/tools-toolsets/toolsets/) |
| On-demand capabilities | Optional specialist instruction/tool bundles for larger agents | First use deterministic step-level selection. Use native deferred loading only for a demonstrated optional bundle; measure any added discovery request. Approval, budgets, history and receipt guards are always active. Do not introduce a discovery step into every booking. | 3 decision, 4–5 only if useful: [on-demand capabilities](https://pydantic.dev/docs/ai/capabilities/on-demand/) |
| `ToolOutput`, output functions, native deferred tool requests/results | Structured decisions and application-rendered outcomes; optional pause/resume of ordinary function tools | Keep output finishers/direct actions for known workflows. Evaluate native deferral only for dynamic ordinary tools, under the same spoken-consent policy. No approval via corrected arguments that were never read back. Measure post-tool model continuations. | 2 quality checks, 3 integration decision: [outputs](https://pydantic.dev/docs/ai/core-concepts/output/), [deferred tools](https://pydantic.dev/docs/ai/tools-toolsets/deferred-tools/) |
| `pydantic-graph` 2.46.0 | Existing `GraphBuilder`, typed steps, decisions and legal transitions | Compile the current StepFlow into the existing graph driver. Session owns conversation position/evidence; graph state is not permission to bypass atomic updates. Do not add a second scheduler, or require separate personas for separate stages. | 2–5: [graph](https://pydantic.dev/docs/ai/graph/graph/), [GraphBuilder](https://pydantic.dev/docs/ai/graph/builder/) |
| `pipecat-ai` 1.11.0 and application `pipecat-slng` | Current pipelines, aggregators, turn strategies, frames, transport and speech services | Keep voice dependencies optional for core. Translate real interruption/playback signals at the adapter boundary. Preserve Pipecat's lifecycle and backpressure; application receipt speech must not trigger an unnecessary model continuation. Read the locked implementation before using current examples. | 1–2, 6: [interruptions](https://docs.pipecat.ai/pipecat/fundamentals/interruptions), [turn strategies](https://docs.pipecat.ai/api-reference/server/utilities/turn-management/user-turn-strategies.md) |
| Native `pipecat.flows` — `FlowConfig`, `Flow`, `FlowManager` | Independent benchmark of declarative authoring, transitions, tool schemas and response scheduling | The supplied native salon imports these from `pipecat-ai`; do not install the older separate `pipecat-flows` distribution by habit. Compare native behavior and its application-owned guarantees; do not run its scheduler underneath the existing core graph as a second engine. | 1 inventory, **2 quality gate**, 3 authoring comparison: [Flows](https://docs.pipecat.ai/pipecat/flows/introduction), [nodes/messages](https://docs.pipecat.ai/pipecat/flows/nodes-and-messages), [functions](https://docs.pipecat.ai/pipecat/flows/functions) |
| `pydantic-extra-types[phonenumbers]` 2.11.1 | Existing salon/Faro reusable phone value types | Keep format validation at the domain boundary; a valid phone is neither confirmed identity nor reliable recovery of misheard speech. A generic core does not need a mandatory phone dependency. | 4–5; inspect the existing domain models |
| PyYAML, `importlib.resources`, `uv_build` / uv | Application manifest, installed prompts/configuration and package distribution | Reuse safe loading and Pydantic validation; no executable expression language or custom templating engine. Build and load from outside the repository. Declare packages imported directly, including graph/template libraries when used directly. | 2–3, 5–6 |
| Langfuse 4.15.4 + OpenTelemetry + SDK instrumentation | Existing trace backend, request usage, operation and voice correlation | Configure explicitly at startup. Preserve one provider-request count and stable call/operation IDs. Pydantic AI can emit OTel without requiring Logfire; do not add another observability service to follow an example. | All: [SDK observability and OTel](https://pydantic.dev/docs/ai/integrations/logfire/) |
| Pydantic AI `FunctionModel`, `TestModel`, override and message-history APIs | Offline real-engine checks and cancellation/history inspection | Disable real requests before imports, retain completed messages after cancellation, and distinguish intentionally pending approval calls from abandoned calls. Existing assert-script runners remain. | All: [testing](https://pydantic.dev/docs/ai/guides/testing/), [history](https://pydantic.dev/docs/ai/core-concepts/message-history/) |

**Evaluate, do not install by default:** [Pydantic Evals](https://pydantic.dev/docs/ai/evals/evals/) may help if the existing small runner cannot manage the actual case/report needs; it must not replace the assert suites or imply voice simulation. [Pydantic AI Harness](https://pydantic.dev/docs/ai/harness/) is a separate capability library, not this project's voice core. Reuse a specific needed capability if it removes code after checking version/dependency implications; a coder/researcher harness, shell tools, autonomous planning and generic memory do not belong in the voice default. LiveKit, native speech-to-speech, durable workflow backends and Unmute code generation remain future integrations. Do not add LangChain.

### Native Pipecat reference and session 2 standard

The supplied reference is [salon-pipecat-flows](../../use-cases/salon-pipecat-flows/README.md), inspected at reference revision `26114758246bfb4cd05a0242ed48e335d3f1ed4a`. It is present in main alongside the session-1 work. Use that folder in the actual checkout or a pinned reference checkout. Record the exact source revision and file hashes. Never silently copy a different core from the reference checkout into the migration.

Read its `README.md`, `pyproject.toml`, `bot.py`, `flow.yaml`, `graph_salon/{__init__,flow,call,models,handlers,writes,speech}.py`, and `tests/test_graph_salon.py`. Its `Line` tests exercise real loaded functions/branch tables with a stand-in manager; other checks use real frames and FlowManager. Preserve this distinction when interpreting evidence. Passing handler tests does not prove a complete audio call.

The benchmark is the native build **including its application safeguards**, not bare Pipecat. Its code includes a consent wrapper, Pydantic schema repair, validated receipts, fixed speech, an empty-reply guard and a custom nonblocking speech action. Its own README identifies omissions including paused-input joining, held approval and complaint-excerpt enforcement. `SalonCall.approve` checks proposal kind, caller and turn but does not establish complete playback. Reproduce relevant cases before reporting a difference; these source observations are not new measured results.

### N1–N13 acceptance matrix

Use this matrix in `session-2.md`, with columns for native result, harness result, evidence, measured overhead and status. Results are **pass**, **fail**, **not exercised** or **not comparable**, never a composite score hiding a critical failure.

| ID | Required comparison | Harness acceptance |
|---|---|---|
| N1 | Invalid arguments, receipt shape/mismatch, unknown fields, atomic state | No backend dispatch on invalid input; no partial state or claimed success from invalid evidence. Schema sent to the model matches runtime constraints. |
| N2 | Missing/same-turn/wrong-identity approval, rejection, correction, replay | No unauthorized original write; one effect after delivered current approval. A native gap never relaxes this requirement. |
| N3 | Verification detour and resumed booking/read | Keep the request and return correctly. Known application continuation does not require another model to rediscover the task. |
| N4 | Offered choice, booking/move/cancel, complaint | Matching domain outcomes and truthful receipt speech for supported paths. No extra action or lost caller intent. |
| N5 | One action per reply, tool/request/transition budget | Reject the second conflicting action, enforce whole-turn bounds, and produce bounded recovery rather than loop or silence. |
| N6 | Model/tool failure, empty output, duplicate answer/receipt | Bounded handling with one truthful audible outcome; no duplicate completion or unbounded retry. Caller interruption is not an empty-model failure to retry. |
| N7 | Node transition while playback or an action-completion frame is interrupted/missing | No indefinite transition wait; next caller input is accepted and routed correctly. Exercise the native custom-say regression, not just a normal transition. |
| N8 | Cancellation before dispatch, during uncertain execution, after successful receipt | No pre-approval effect; completed effects retained; unknown remains unknown until reconciled; no automatic duplicate write. Check both semantic state and backend records. |
| N9 | Same input/output frame timeline; generated, partial, repeated and late playback | No unheard-readback approval; obsolete output stops; paused input survives; old frames cannot approve a new generation. Intentional stronger semantics are documented rather than forced to equal native behavior. |
| N10 | SDK context containing provider-specific messages / tool call-result pairs | No crashes, orphaned required results, leaked identity context or lost acknowledged result after cancellation. |
| N11 | Two concurrent sessions; startup/setup failure/disconnect | Isolated state/evidence and bounded cleanup. Shared compiled definitions/clients do not leak mutable call state. |
| N12 | Request topology, scheduling and local runtime overhead | No unexplained additional model round trip for code-decided continuation or receipt speech. Any classifier request is counted. Measure native and harness on the same deterministic trace before claiming an overhead improvement. |
| N13 | New application authoring and portable packaging | Session 2 proves the public typed state/session/operation slice without a custom application runtime subclass. Session 3 must complete the manifest/clinic proof, including reusable agents, handoffs and standard steps. Compare domain code separately from orchestration and configuration; do not hide code by moving it. |

Session 2 must pass its applicable runtime rows before the broader authoring migration. It should be at least as capable on common cases, explicitly stronger where required by delivery/evidence rules, and show no unexplained scheduling/request regression. N13's full declarative-authoring proof belongs to session 3 and remains visibly pending until then. A defect in the reference is evidence to describe, not behavior to reproduce or a reason to call the whole harness superior.

**Fair measurements:** the supplied native bot pins `gemini-3.1-flash-lite` and a `TURN_STOP_SECS=1.5` smart-turn fallback; its README contrasts these with the harness model and Pipecat's 3-second default. Resolve actual settings, provider endpoint, thinking, prompts/tools, diary data, VAD/STT/TTS, language and audio format for both arms. Keep original settings as a named product comparison. A controlled runtime comparison must use matched settings or explicitly exclude the unmatched component; never attribute different silence thresholds or model speeds to core.

For cheap runtime measurements, exercise equivalent real dispatch/frame paths with scripted model events and fake service completion, in isolated processes on the same machine. Warm up, run at least 20 paired repetitions in alternating order, record local dispatch-to-output, transition completion and interruption-to-output-stop separately, and retain request counts/raw samples. Use virtual time for ordering assertions and a monotonic wall clock for actual runtime cost. Freeze tolerances before the candidate measurement; the shared provisional threshold is an investigation trigger, not permission to declare equivalence. Do not count simulated provider delay as measured SDK speed. Missing/incomparable measurements keep the corresponding claim pending.

**Live comparison and cost — user selected:** 12 salon-graph calls across six sessions plus two native salon-pipecat-flows calls in session 2. Use the same normal-booking and pause/correction cases and personas for both arms: four calls at that checkpoint, **14 planned calls total**, at most 42 audio minutes if the 180-second ceiling is enforceable. This is not a currency quote. No extra native text/model suite or automatic reruns are bundled into the extension. Record the two exact target IDs, case/persona IDs and per-target allowances before launch; carry this authorization forward without asking again. Existing native recordings may add context only when their versions/settings match. Even the paired two-case checkpoint establishes sampled parity only, not statistical equivalence or general superiority; missing triggers or incomparable settings remain visible gaps.

Before session 1's reference calls, select and record one supported comparison profile for model/provider/thinking and audio/endpointing settings that both controllers can run. Use explicit evaluation overrides without silently changing production defaults, and preserve that salon-graph profile through all six checkpoints. If no shared profile works, run the two configured products with every difference recorded and keep runtime-speed/semantic-attribution claims unproven; do not spend extra calls searching for a favorable configuration. Report which production settings the chosen profile does and does not validate.

For the existing session-1 baseline, that opportunity has already passed: its report records Gemini 3.5 flash lite and a 1.6-second smart-turn fallback. Do not rewrite those observations or silently rerun the baseline to match the native build. Before session 2, record whether the paired checkpoint can retain those settings in both arms. If it cannot, keep the baseline comparison and the native comparison distinct, record any evaluation override, and limit claims to the components/settings actually compared within the assigned allowance.

## Verification contract — applies to every prompt below

### Working rules

- Read AGENTS.md, CLAUDE.md, the roadmap, the Harness architecture and framework reuse section, this contract, and the preceding session report. Inspect the current checkout; the planning revision is not a request to reset changes.
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

Use use-cases/salon-graph for every recurring text/voice checkpoint. The existing Coval convention maps voice-harness-salon-pipecat to that directory; discover and record its actual agent ID. Session 2 alone also permits two calls to the supplied native salon-pipecat-flows reference. Resolve its actual agent ID and launch path explicitly; never infer the native target from the existing salon alias or mix the two salon import packages.

Freeze fixtures, scenario versions, salon model/classifier, prompts, language, STT/TTS settings and personas across checkpoints. Each call starts with reset test diary data, never real customer bookings. Record intentional changes and invalidate affected comparisons.

**Live text checkpoint:** four salon dialogues: normal booking; correction/conditional acceptance; prerequisite detour and return; unknown-write recovery. Inject backend uncertainty through the test service boundary for the last case. One attempt per dialogue, maximum 12 caller turns per dialogue, at most 48 optional adaptive-caller model invocations across the checkpoint, and 100 target model invocations including classifiers across the checkpoint. Scripted caller turns require no caller model requests. Keep target per-turn budgets, cap adaptive caller output, record tokens, and configure a finite token/spend ceiling for the selected provider before running. Stop on explicit completion or budget exhaustion; exhaustion is incomplete evidence, not success. The live target and any adaptive caller incur model costs. Save transcripts for cheap scripted reproduction; rerunning the live target on saved caller text is still billable.

**Regular Coval checkpoint:** at most two salon-graph simulations, one iteration, one at a time, no mutations or automatic reruns. Session 2 repeats these same two cases once on native Pipecat under its separate two-call allowance:

1. **Normal booking:** identify the fixture caller, select an offered appointment, fully hear the readback, explicitly accept, and receive matching confirmation. Exactly one intended booking must exist.
2. **Pause and correction:** give identity/request in fragments separated by a pause; interrupt the appointment readback to correct it; accept only after hearing the corrected readback. No old/partial proposal may execute; exactly one final correct booking may exist. Record whether premature endpointing, agent-over-user speech and caller barge-in actually occurred.

Freeze these cases in session 1. Reuse a calm persona and a suitable interruptive persona with explicit IDs; do not launch a whole sweep. [Coval personas](https://docs.coval.ai/concepts/personas/overview) offer interruption settings, but configuration is not proof a call contained required overlap. Check recording and event trace. If a trigger did not occur, mark coverage **not exercised**, without automatically spending another call.

Budget: **2 salon-graph simulations × 6 sessions + 2 native simulations in session 2 = 14 maximum planned calls**, including the reference checkpoint. Propose a verified 180-second per-call ceiling: at most 42 audio minutes if enforced. This is not a currency quote and excludes model/TTS/telephony/evaluator charges. Verify account pricing for money estimates. If the service/transport cannot enforce the duration ceiling, report that before launch; polling timeout is not a call-duration cap. Paid retries consume a separately assigned allowance; unused allowance is not an instruction to launch more variants.

**Current allocation after session 2:** six calls used, eight harness calls remaining, reserved as at most two per session 3–6. Session 3's stress repair call consumes its first slot, not a separate allowance; prompt 3 defines its final slot. Failed and inconclusive calls still count. The historical 42-minute figure assumed a hard 180-second ceiling; session 2 recorded a native call at 188 seconds with shutdown grace. Keep actual audio duration and enforced grace in the ledger, rather than claiming the nominal bound was met.

At the original planning revision, coval_sim defaulted to all attached sets and could target several agents sharing a set. Session 1 has since added exact jobs, read-only planning, simulation limits and a run log; inspect and extend that implementation for session 2's native target instead of rebuilding it. Verify cases and expanded variants immediately before launch. Prefer two explicitly selected one-case sets/persona jobs, or verified exact-case filtering. One iteration does not mean one call. Do not copy the historical VkpkmRMV broad-set example, which described 18 calls.

Dependent repair/verification calls run sequentially and are reviewed before the next launch. The user permits independent, already-budgeted jobs in parallel using `coval_sim --port`, with separate ports, bot/tunnel processes, fixtures and logs. Ports alone are insufficient: `Runner.run` changes the Coval agent's callback URL, so do not overlap jobs on the same agent ID. Prefer useful offline work while one voice job runs; do not create extra calls or native jobs to fill parallel slots. Stop new paid launches on critical failure. Reconcile uncertain launch status by ID before retrying. Verify remote termination and clean up bot/tunnel processes; local polling expiry alone does not cancel a remote run.

Use Coval skills as needed: coval-resources for inventory; build-test-suite, design-persona and configure-metrics for the checkpoint; then **either** quick-eval **or** launch-run → watch-run → get-results. Do not launch twice by invoking both workflows. Use coval-compare-runs for matched checkpoints/native comparisons and coval-eval-audit when the evaluation's coverage or score is misleading; these do not authorize new runs. Use download-audio/trace diagnostics for missing evidence or failures. Accent, noise and broad adversarial sweeps are separate paid expansions. Carry selected IDs and authorization across sessions; only unresolved resources or changed spending scope need new input.

### Passing and stopping rules

- Applicable deterministic and replay checks pass. From session 2 onward, unknown/interrupted delivery cannot authorize consequential action. No unauthorized write, identity leakage, duplicate write, lost acknowledged business result, or unsupported completion claim.
- All four bounded live text cases meet deterministic business assertions. Inspect semantic failures against delivered transcript and classifier input/output. When interpretation changes, reuse salon's eval machinery for a small supported-language consent set within the recorded model allowance; do not launch its whole model comparison matrix.
- The assigned salon-graph Coval calls meet expected behaviors and have business/audit evidence; prompt 3 explicitly changes its case allocation. In session 2, record matched native outcomes independently; a native failure can demonstrate a difference but cannot excuse a harness failure. COMPLETED status or high composite score is insufficient. Match call IDs to receipts and audio. Trigger-dependent coverage passes only when the trigger occurred and behavior was correct. An earlier call does not validate later changes to the path it exercised.
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
Implement session 1 of the reusable voice core migration. This is an implementation task, not another architecture proposal. Read AGENTS.md, CLAUDE.md, README.md sections 7–10, docs/plans/reusable-voice-core-roadmap.md, and both Harness architecture and framework reuse and Verification contract in docs/plans/reusable-voice-core-sessions.md. Apply pydantic, python-dev, building-pydantic-ai-agents and ponytail:ponytail. Preserve the repository's assert-script tests and use uv, Ruff and ty.

HARNESS VISION AND FRAMEWORK READING

We are extracting repeated voice orchestration into one versioned library. Applications author configuration, Pydantic data, prompts and domain functions. Pydantic AI owns agent execution; Pydantic Graph owns the existing declared graph execution; Pipecat owns audio. Core connects them with evidence, approval, delivery and session policies. Your baseline must measure both reuse and conversational correctness, not just whether the bot responds.

Read these primary references against the locked SDK before writing evaluation adapters:
- https://pydantic.dev/docs/ai/guides/testing/ — reuse FunctionModel/TestModel and overrides for offline execution.
- https://pydantic.dev/docs/ai/core-concepts/message-history/ — retain the SDK's actual message/result boundaries when a turn is cancelled.
- https://docs.pipecat.ai/pipecat/flows/introduction and https://docs.pipecat.ai/pipecat/flows/nodes-and-messages — understand the independent native graph and its speak/wait behavior.
- https://docs.pipecat.ai/pipecat/fundamentals/interruptions — replay real frame/cancellation behavior rather than a second completed text turn.
Inventory the package/capability table from the shared architecture section. Do not install Pydantic Evals, Pydantic AI Harness or LangChain just to build the baseline runner.

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
- The supplied reference at use-cases/salon-pipecat-flows: README.md, bot.py, flow.yaml, graph_salon modules and tests/test_graph_salon.py. Pin its revision and inspect the native N1–N13 matrix in the shared architecture section. This is a separate reference, not a sixth migration consumer.

WORK IN THIS ORDER

1. Record the exact starting revision/dirty changes, Python and locked package versions, prompts, model/classifier configuration, voice settings and existing test commands. Inventory each consumer's dependencies and custom orchestration. Identify methods doing generic runtime work versus genuine domain work. Do not transfer old benchmark numbers or zero-model approval claims into this baseline.

2. Run all existing offline suites and per-member ty; capture real outputs and failures. Preserve golden prompt/tool-schema snapshots. Baseline defects must have a reproducible case and owner session. In particular, inspect ProposalLedger.last: absent matching speech currently may count as heard. Capture the desired “unknown delivery cannot authorize” regression without altering production semantics in this session or silently treating it as a passing safety guarantee. Keep that expected failing check separate from the baseline passing suite until session 2 repairs it.

   Establish the native reference offline in its own pinned environment, with no provider/audio network calls. Its salon namespace must never share a Python process/import path with the harness salon. Preserve its sibling-source wording checks against the correct pinned checkout. Reuse native scenarios as comparison inputs, but assert domain outcomes independently instead of treating native output as automatically correct. Record native model, provider/thinking and endpointing differences, which checks use real FlowManager/frame execution, and which use a stand-in manager. Search available local reports for reusable native run IDs; historical IDs in comments alone are not comparable evidence.

3. Extend existing interruption probes only where coverage is missing. Use the real processor and scripted model/classifier outputs, with clock control or synchronization barriers. Cover caller fragments around endpointing, output that has been generated but not played, partial readback, delayed old playback-stop events, and cancellation before/during/after a write. Record event order, input pieces, proposal identity, delivered segment IDs, backend effects and final history. Do not simulate concurrency by awaiting a whole answer before sending the interruption.

4. Implement a small salon text evaluation entry point in the appropriate existing evaluation/tooling location. Use the existing core turn runner and real salon definitions. Fixed caller scripts are the default. An existing Pydantic AI model may supply adaptive caller replies when needed; no LangChain or extra orchestration framework. A caller model may see only its role/goal and delivered conversation, not target state, tool internals or the expected result. Business assertions are independent of both models.

   Define four named, versioned cases:
   T1 normal booking: correct identified subject and offered slot, one matching booking receipt.
   T2 correction/conditional acceptance: “Yes, but make it Wednesday” must not book the Tuesday proposal; only a later acceptance of the revised delivered readback can book.
   T3 prerequisite detour: request booking before identity is established, resolve verification, then resume the intended booking without losing its requested details or treating verification consent as booking consent.
   T4 uncertain write: inject a backend outcome that is initially unknown; reconcile before retry, retain at most one actual booking, and never announce unproven success.

   Reset the test diary per case. Acknowledgements identify delivered text segments explicitly. A returned text string is not an audio playback event. Save input, delivered output, typed interpretation, operation attempt/result, final diary/state, request counts and timing. Enforce the contract's turn/request/token limits and no automatic retries. Its no-network self-check must prove that wrong outcomes and budget exhaustion are reported, not accepted.

5. Make the existing Coval runner support the narrow checkpoint before launching it. Read-only planning must resolve one exact agent ID and launch path per job, exact test cases/personas/metrics, iterations, mutations and the expanded simulation count. The default target is salon-graph; only session 2 has a separate explicit two-call native salon-pipecat-flows allowance. Reject ambiguous agents, accidental salon-prompt/Faro targets, native launches outside that allowance, empty selections that mean “all,” and counts above the per-checkpoint or global cap of 14. The existing test-set selection can match several agents; fix that path once instead of trusting names at the call site. Do not assume one iteration equals one simulation.

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

Write docs/research/reusable-core/session-1.md with baseline commands/results, exact reusable evaluation commands, frozen case IDs/configuration, actual run/trace IDs and spend, event coverage, numeric provisional timing budgets, and a method-to-responsibility migration inventory. Include a native-reference manifest, initial N1–N13 status, package/API versions and concrete reuse candidates. Freeze native comparison settings/tolerances before candidate measurements and record the user-selected 14-call plan: 12 salon-graph plus two native calls in session 2 only. Report core/model/backend/audio timings separately; two calls establish a smoke baseline, not p95 or reliability.

Session 2 may begin when the baseline is reproducible, evaluation selection is bounded and known defects are explicit. Missing credentials can leave live evidence pending while offline preparation finishes. Do not claim the baseline is fully verified or expand implementation into session 2.
~~~

## Prompt 2 — Establish the package and pass the native Pipecat quality gate

Copy this complete block after session 1's implementation and report are available.

~~~text
Implement session 2 of the reusable voice core migration. Read AGENTS.md, CLAUDE.md, the supplied roadmap, Harness architecture and framework reuse (including native matrix N1–N13), the Verification contract in docs/plans/reusable-voice-core-sessions.md, and session-1.md. Apply pydantic, python-dev, building-pydantic-ai-agents and ponytail:ponytail. Continue from the accepted code; do not reset to the planning revision.

HARNESS VISION AND FRAMEWORK READING

This must be a reusable conversation runtime that earns its abstraction cost against native Pipecat. Keep one Pydantic AI agent/tool loop and the existing Pydantic Graph driver, with Pipecat handling audio. Applications supply declarative data plus domain work, while core owns session/evidence/approval/delivery. A public facade hiding unchanged application runtime subclasses is insufficient.

Use the installed capabilities described by these references:
- https://pydantic.dev/docs/validation/latest/concepts/models/ and https://pydantic.dev/docs/validation/latest/concepts/type_adapter/ — validated whole-state replacement and reusable boundary adapters, not unvalidated copies.
- https://pydantic.dev/docs/validation/latest/concepts/unions/ — structural state variants where they remove behavior-heavy validators.
- https://pydantic.dev/docs/ai/core-concepts/dependencies/ — ordinary injected services and RunContext, separate from domain state.
- https://pydantic.dev/docs/ai/core-concepts/hooks/ and https://pydantic.dev/docs/ai/core-concepts/message-history/ — reuse hooks and SDK history for budgets, cancellation and completed results.
- https://pydantic.dev/docs/ai/core-concepts/agent-spec/ and https://pydantic.dev/docs/ai/graph/builder/ — verify the minimal AgentSpec/template/GraphBuilder composition for session 3, with concrete runtime types.
- https://docs.pipecat.ai/pipecat/flows/functions and https://docs.pipecat.ai/pipecat/flows/nodes-and-messages — benchmark native transitions, tool cancellation and code-owned speech scheduling.
For each chosen primitive, name the existing custom responsibility it replaces and leave a small executable integration check. Do not add a generic plugin layer or another agent loop.

OBJECTIVE

Make the existing slng-voice-core distribution installable and usable through a small public boundary. Establish a concrete application-state contract independent of core's runtime session and application services. Tighten delivery so unknown or interrupted readbacks cannot authorize consequential actions. Existing salon and Faro entry points must remain runnable during this foundational change. Prove the runtime meets the native comparison standard; installation and green legacy tests alone cannot pass this session.

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

NATIVE PIPECAT QUALITY GATE — REQUIRED BEFORE CALLING THIS FOUNDATION READY

Use the pinned salon-pipecat-flows reference recorded by session 1, including its application safeguards. Read graph_salon.flow.load/typed, handlers._dispatch/tool/say/answer, SalonCall.approve/commit/hear, Write.run, ClaimFilter, EmptyReplyGuard, GraphSalon.attach/start and the native tests. Its source is a comparison, not an instruction to replace our engine or weaken our guarantees.

N1–N13 ACCEPTANCE MATRIX

For each row, record native result, harness result, evidence, measured overhead and status in session-2.md. Use pass, fail, not exercised or not comparable. N13 has a foundation check in session 2 and a full authoring check in session 3.

| ID | Required comparison | Harness acceptance |
|---|---|---|
| N1 | Invalid arguments, receipt shape/mismatch, unknown fields, atomic state | No backend dispatch on invalid input; no partial state or claimed success from invalid evidence. Schema sent to the model matches runtime constraints. |
| N2 | Missing/same-turn/wrong-identity approval, rejection, correction, replay | No unauthorized original write; one effect after delivered current approval. A native gap never relaxes this requirement. |
| N3 | Verification detour and resumed booking/read | Keep the request and return correctly. Known application continuation does not require another model to rediscover the task. |
| N4 | Offered choice, booking/move/cancel, complaint | Matching domain outcomes and truthful receipt speech for supported paths. No extra action or lost caller intent. |
| N5 | One action per reply, tool/request/transition budget | Reject the second conflicting action, enforce whole-turn bounds, and produce bounded recovery rather than loop or silence. |
| N6 | Model/tool failure, empty output, duplicate answer/receipt | Bounded handling with one truthful audible outcome; no duplicate completion or unbounded retry. Caller interruption is not an empty-model failure to retry. |
| N7 | Node transition while playback or an action-completion frame is interrupted/missing | No indefinite transition wait; next caller input is accepted and routed correctly. Exercise the native custom-say regression, not just a normal transition. |
| N8 | Cancellation before dispatch, during uncertain execution, after successful receipt | No pre-approval effect; completed effects retained; unknown remains unknown until reconciled; no automatic duplicate write. Check both semantic state and backend records. |
| N9 | Same input/output frame timeline; generated, partial, repeated and late playback | No unheard-readback approval; obsolete output stops; paused input survives; old frames cannot approve a new generation. Intentional stronger semantics are documented rather than forced to equal native behavior. |
| N10 | SDK context containing provider-specific messages / tool call-result pairs | No crashes, orphaned required results, leaked identity context or lost acknowledged result after cancellation. |
| N11 | Two concurrent sessions; startup/setup failure/disconnect | Isolated state/evidence and bounded cleanup. Shared compiled definitions/clients do not leak mutable call state. |
| N12 | Request topology, scheduling and local runtime overhead | No unexplained additional model round trip for code-decided continuation or receipt speech. Any classifier request is counted. Measure native and harness on the same deterministic trace before claiming an overhead improvement. |
| N13 | New application authoring and portable packaging | Session 2 proves the public typed state/session/operation slice without a custom application runtime subclass. Session 3 must complete the manifest/clinic proof, including reusable agents, handoffs and standard steps. Compare domain code separately from orchestration and configuration; do not hide code by moving it. |

1. Make N1–N13 an executable comparison/report, reusing existing assert scripts and frame-test utilities. Keep the driver small: one versioned scenario description and separate adapters/processes for the actual native and harness paths. Do not compare two newly written fake engines. Assert diary effects, validated receipts, transition results, delivery records and model-request topology. Mocking interpretation to yes tests authorization, not whether a real model correctly understands consent.

2. Require every applicable common functional case to pass on the harness. Reproduce native failure/unsupported behavior where practical, then document the harness's stronger required contract: interrupted/unknown readback cannot approve, paused utterances remain available, stale work cannot change current routing, and completed writes survive cancellation. Keep zero unauthorized or duplicate writes as a hard gate. Never average these away with latency or a composite evaluation score.

3. Exercise the specific native lessons: a provider-specific context record must not crash a tool; an enum must reach the model schema; a second answer must not speak the receipt again; code-spoken failure/receipt must not invite a model to invent success; empty output must recover within the budget; a node change must not wait forever for an ActionFinishedFrame dropped during barge-in. Use bounded synchronization and actual processor/manager paths, not just direct handler calls. Reuse native facilities that solve a problem in the locked version; retain only the necessary shared workaround when they do not.

4. Compare scheduling and overhead using the shared matched-measurement protocol: warmup, at least 20 paired offline repetitions, common fake provider/service timings, isolated environments and raw samples. Count every request including classifiers, retries and any continuation after a function result. For code-decided transitions and receipt-rendered confirmations, assert no avoidable extra model round trip. Record the costs of required policy checks separately. Investigate a breach of the predeclared tolerance; do not change the tolerance after seeing the candidate. A faster unsafe path is a failure.

5. Prove reuse now with a minimal public Python/definition slice for typed state, injected service, readback, guarded operation and receipt delivery, using the clinic or equivalent existing fixture. No custom application CallContext, approval ledger, history manager or speech filter. Validate a typed native AgentSpec/template integration and use the existing graph execution rather than writing substitutes. Compare the authored orchestration inventory with the native SalonCall/tool wrapper/speech guards. Do not claim the full manifest experience is complete here: session 3 must finish N13 with the external clinic, agent/handoff/typed-flow variants and no core edits.

6. Run the two salon-graph voice cases and the user-selected two additional native calls for those same cases/personas. Launch sequentially in a recorded paired order, such as normal harness → normal native → stress harness → stress native; review each result and stop new launches on critical failure. Session 2 has four calls maximum; the complete plan has 14. No additional native calls are permitted in later sessions. Keep the native reference unchanged and use the comparison profile frozen by session 1, with explicit overrides in isolated launches. Different model/endpointing settings must not yield a runtime-speed claim. Correlate business effects, requests, audio intervals and interruption coverage for each pair. Two paired cases support only a sampled result; missing evidence remains pending without automatic reruns.

If a required runtime behavior is missing, repair it at its shared owner and rerun the affected checks within this session. If measurements or overlap evidence are missing, record the unresolved gate and continue useful offline work; do not label the harness “as good as native” without its evidence. A failure tied to a fundamental API decision must be resolved before expanding the authoring surface in session 3.

PACKAGE PROOF

Use uv to build the core wheel. In a fresh temporary project outside the checkout, install that artifact and only declared dependencies. Clear inherited project paths and keys. Assert the imported module resolves to the installed artifact, not workspace source. Validate a small definition and run an offline scenario. Separately test voice/tracing extras and meaningful missing-extra diagnostics. Record exact commands/artifact hashes and dependency sets. uv sync in the shared workspace is not isolation evidence.

Run common Ruff/format/core/salon commands plus ty/tests in every affected consumer, including temporary compatibility adapters and the pinned native comparison checks. Then run session 1's four live-text cases and the paired session-2 Coval checkpoint: at most TWO frozen salon-graph calls and TWO matching native calls. These are the same calls specified in the quality gate, not another launch. Keep all evaluation identities/settings fixed; do not tune models or endpointing to hide a regression.

DELIVER AND STOP

Save session-2.md, a concrete public import/session example, the ownership table, the SDK-reuse decisions and each remaining compatibility consumer. Include the completed N1–N13 comparison with source/test/trace evidence, package/settings hashes, request topology, raw paired timings and remaining gaps. Report foundation correctness, native common-case parity, stronger delivery guarantees, runtime overhead and live audio evidence separately. Gate: clean isolated installation, concrete state on the engine, atomic/ownership/isolation checks, repaired unknown-delivery invariant, preserved current apps, applicable native runtime rows passing and documented live results. N13's full authoring proof is due in session 3. Leave absent live evidence pending; make only dimension-specific supported comparisons. Do not yet migrate the full salon/Faro authoring or redesign the solver.
~~~

## Prompt 3 — Repair the inherited voice issues and complete the ordinary authoring contract

Copy this complete block into the session-3 worktree. It includes the session-2 handoff; no separate pasted handoff is required. Keep the shared architecture, N1–N13 matrix and verification sections available in the repository.

~~~text
Implement session 3 of the reusable voice core migration: first repair and check the inherited interruptive-speaker problems, then complete the ordinary authoring contract. This prompt incorporates the user's session-2 handoff and overrides older checkpoint order/profile instructions where they conflict. Apply pydantic, python-dev, building-pydantic-ai-agents and ponytail:ponytail. Keep assert-script tests, uv, Ruff and ty. No LangChain dependency; scripted/Pydantic AI text conversations use the existing core runner. All paid text/voice evaluation targets salon-graph only.

START FROM THE CORRECT PARENT AND STACK THE PR

The user is starting from feat/session-2-public-boundary and moving this task to a worktree. PR https://github.com/slng-ai/slng-voice-harness/pull/17 is open against main; the latest checked head is 47f41923064f57fa01847785a46d14c1999a1fd5, which contains the reviewed fixes at 6c9f75ce495a41be89116566b09409a96eef2843. Sessions 1 and 1b/resources (#15 and #16) are already merged. Session 3 must inherit the latest #17 head, not start from the old baseline or planning branch.

1. Inspect git status, branch, HEAD, worktree list and current remote PR #17 status before editing. Preserve existing user changes. If #17 remains open, fetch its current head and verify the session-3 checkout contains that parent and 6c9f75c (or inspect an explicitly rewritten equivalent). If the worktree already has a session-3 branch, keep it; otherwise create a feat/ child branch there. Never commit session-3 work onto the parent branch or reset another worktree.
2. While #17 is open, the session-3 PR base is feat/session-2-public-boundary. Record its exact parent SHA and inspect the parent-to-child diff so the PR shows session-3 changes only. If #17 is merged before starting, use updated main containing its changes; a squash merge need not contain the old SHA as an ancestor. If it merges during this session, transplant only the child commits onto updated main and retarget to main, verifying no parent changes are duplicated. Do not rewrite the parent's history.
3. Read AGENTS.md, CLAUDE.md, README.md, docs/plans/reusable-voice-core-roadmap.md, and docs/plans/reusable-voice-core-sessions.md (Harness architecture and framework reuse, N1–N13 acceptance matrix, Verification contract). Then read docs/research/reusable-core/session-1.md and docs/research/reusable-core/session-2.md in full, especially sections 8, 9, 10, 10b, 12 and 13, and the evidence they link. Recover missing files from #17 rather than repeating evaluations. If the repository still has the old prompt 3, incorporate this updated block into the plan and record the changed allocation/profile in session-3.md.
4. A new worktree does not inherit another worktree's untracked .env or uncommitted documentation. Use the existing approved credentials source through process environment without printing/committing keys or changing another running session's environment. Verify imports and bot paths resolve to THIS checkout. Opening update: actual parent/child SHAs, inherited evidence, unresolved gates and remaining call allowance.

INHERITED RESULTS: PRESERVE THE FIXES, DO NOT REBUILD THEM

At 6c9f75c, core.load -> compile -> open -> core.text_turn with explicit segment acknowledgement is public; FlowState[S]/FlowCall[S], atomic collect/update, Evidence/receipt_for, delivery serials and optional voice/tracing extras are implemented. core/tests/clinic_flow, test_public, test_session, test_delivery, test_sdk and test_package cover the foundation. The session-2 report records clean offline checks and an isolated wheel installation. Reuse that API.

Session 2 already repaired these problems offline; no later Coval calls verified the repairs:
- HeardValue.turns and CallContext.heard_since join a number across separate turns. Salon uses turns=3. The exact sequence '+34 671' -> the bot's request for the rest is fully heard -> '273 454' now reaches read-back. The regression fails when turns=1.
- Offer._snapshot refuses a choice reading against an offer whose speech never played. This prevents a repeated identity confirmation from silently selecting 9 AM.
- _missed tells the next turn when reply audio never started, as well as when it was cut. Generated speech or TTS completion is still not proof of delivery.

Keep those regressions and their failure sensitivity. Do not move these completed fixes back into session 4.

Evidence limits to carry forward:
- Offline native matrix and N12 pass under their recorded protocol; N13 is only the public-boundary foundation, to be completed here. Native move/cancel and disconnect coverage remain partial. A warm hand-over measurement was slower on the harness outside the frozen protocol; retain that finding.
- Live text T1/T2/T4 passed. T3.v2 still loses the requested 2 PM across the identity detour and offers other Thursday times; session 4 owns that existing defect. Preserve its failing evidence and do not weaken it to claim four passes. If this session changes the shared mechanism responsible, repair and verify it here. T4.v2 unknown-write reconciliation already passed; retain it while adapting operations.
- Normal harness voice booked correctly but breached the frozen audio-latency budget: median 2.46 s versus 2.29 s. Native booked but its receipt audio was cut off. Both stress calls hit a simultaneous SLNG STT outage, and barge-in during the appointment read-back was not exercised. Thus live parity and stress acceptance are incomplete, even though the judge returned scores.
- Historical runs: normal harness BChXtqzgWq5HxzxP69aoKo; normal native XMnnft82s6MitKPjiiQpJz; stress harness RXcDFLJXHnN7dEddsLyptv; stress native ZTKVhxqTAhRAekBhG9jAsC. Use their existing recordings/traces; no native rerun is authorized.

PHASE A — REPAIR THE STRESS PATH BEFORE THE AUTHORING REFACTOR

A1. Validate international phone input through the existing Pydantic phone types.

Inspect salon/tools/identity.py, salon/models/shapes.py, their callers, state/receipt types, prompts and fixtures. Removing _SPANISH_RUN alone is insufficient: SubmittedPhone/Phone currently use PhoneNumberValidator(supported_regions=['ES']), Spanish length diagnostics, and Spanish-only guidance; _readback assumes a three-character country prefix. Use the existing pydantic-extra-types/phonenumbers dependency to support valid international numbers, normalize to E.164, and render the confirmed number without corrupting country codes or digits. Keep the distinction between an absent state value and a required submitted number. Never invent a country code, accept malformed/incomplete input, or look up an empty number. Ask for missing country information.

Reuse HeardValue.turns/CallContext.heard_since rather than creating a second phone accumulator. Handle pieces across premature endpoints and across genuinely separate turns. A correction/restart replaces the relevant old candidate; do not concatenate an old full number, unrelated appointment digits or another identity into the new number. Keep bounded per-session evidence and exact-number read-back before lookup. A valid number is not consent, and a valid unknown customer is not malformed input.

Verify with small offline cases using library-validated fixtures: the exact '+34 671'/'273 454' sequence, a non-Spanish number, one- and three-digit country calling codes, a corrected number, missing country information, invalid/too-long input, replayed yes and interrupted/unheard read-back. Assert the normalized value AND lookup count/arguments: zero before the matching delivered read-back and acceptance, exactly one afterward. Review affected model-facing descriptions/snapshots intentionally. Keep native reference runtime code frozen; document the changed phone-support profile rather than silently editing the comparator.

A2. Change the speech launch profile, with a compatibility check before spending a Coval call.

User-selected target settings for both salon-graph and salon-pipecat-flows:
- SLNG_STT_MODEL=deepgram/nova:3-en, hosted in the EU.
- SLNG_TTS_MODEL=slng/fish/tts:s2.1-pro, hosted in eu-north or eu-west.
- SLNG_BASE_URL set to the verified regional host; the existing Pipecat services use this one host for BOTH STT and TTS. Use configuration/environment overrides; do not edit the native runtime or hard-code the switch into core.

Inspect the locked pipecat-slng implementation and SLNG's current model/region/voice catalogue. Verify the exact STT identifier: installed SDK documentation names slng/deepgram/nova:3-en, while the handoff requests deepgram/nova:3-en; the bridge inserts the supplied model in the URL without normalizing this prefix. Resolve this from supported routing evidence, record any necessary correction, and do not silently select a different model or region. An EU hostname alone is not proof the chosen model processes audio in the EU. Verify both models work through the SDK's /v1/bridges/unmute endpoints; native-provider WebSocket examples use different protocols.

Verify a Fish-compatible SLNG_TTS_VOICE; do not carry over the old Cartesia UUID blindly. Check language, output encoding/sample rate, streaming and cancellation settings against the actual transport. Both bots already read the model/base-URL/voice environment variables. Use a reusable, secret-free launch-profile record plus process-local overrides, preserving the native source digest. Preflight uses read-only catalogue/configuration evidence and offline SDK checks; the allocated harness stress call supplies actual audio evidence, not an unbudgeted probe loop. Any native construction/configuration check is offline: no native Coval or billable native provider probe remains in scope. If the requested EU model/voice combination cannot be verified, report that exact live prerequisite and continue useful offline work without claiming it is configured successfully.

Read https://docs.slng.ai/llms.txt for the current official Pipecat integration, region and Fish S2.1 Pro documentation. The Nova English reference is https://docs.slng.ai/api-reference/stt/deepgram-nova-3/nova-3-english-ws ; at preparation its published region list did not establish EU availability, so deployment availability must be checked. Do not infer availability from a model name.

Give the resulting profile a version/hash and record resolved model IDs, region evidence, voice, SDK/lock versions, transport, VAD/endpointing and unchanged LLM settings. This replaces the old speech profile, so there is no matched end-to-end latency comparison with sessions 1/2. Preserve those measurements and the unresolved latency finding. Freeze a stated product/provisional timing target before the new measurement; do not widen the old threshold after seeing results. Offline N12 runtime comparisons remain useful when their inputs/settings are unchanged.

A3. Evaluate persistent interruption/missed-speech context.

Inspect core history projection, turn instructions and the existing _missed path. Consider retaining a compact factual missed-speech note beyond the next turn only if a focused scenario shows the existing delivered-history view is insufficient. This is a model-input change, not cleanup. If implemented, retain actual heard content and valid tool-call/result pairs, avoid duplicate notes or retaining unbounded transcripts, and never promote a queued/cut segment to delivered. Prove with offline projected requests that a later turn knows the caller missed an offer/receipt, recovers the correct content and does not repeat the backend write. If the existing mechanism already meets this requirement, keep it and record the evidence. Make this decision before the stress call so that call checks the final chosen behavior.

A4. Run offline checks, then the one allocated stress repair call.

Run Ruff/format, core and salon ty/assert suites, affected consumer suites, delivery/history/continuation/recovery checks and applicable native matrix rows. Use the existing concierge-tools matrix runner and package-proof script, not replacement harnesses. Preserve the N1–N13 criteria: a failure requires a minimal regression and a shared fix when the defect belongs to core. No red acceptance check becomes green by lowering its expected result.

Read the relevant Coval skills: coval-resources and coval-eval-audit for existing resources/evidence, then EITHER quick-eval OR launch-run -> watch-run -> get-results. Use coval-compare-runs and audio/trace analysis where applicable. Reuse the existing sets/personas/metrics; do not trigger broad accent/noise/adversarial suites or both launch workflows.

The user has already authorized this bounded check; do not ask again for the same scope. Reconcile the run ledger first. Expected salon-graph target: agent JxGgSPA55DcFcDcvbKAUQM, agency salon. Verify the current target mapping and exact expanded case count before launch. First run the read-only plan from use-cases/concierge-tools:

uv run python -m coval_sim plan --agency salon --agent JxGgSPA55DcFcDcvbKAUQM --test-set K3Xwqi7g --persona VSruLZYYyz3w7fkdUaheDR --iterations 1 --max-simulations 1 --concurrency 1 --label session-3-stress-repair --port 7861 --log ../../docs/research/reusable-core/session-3/coval-runs.jsonl

Use an available port and the verified process environment. When preflight passes, run the same job with 'run' in place of 'plan', once. Preserve its intent/run ID; never change labels or delete log entries to bypass an uncertain launch. Verify the existing call ceiling and shutdown grace; polling timeout alone is not a call-duration limit.

Audit the actual audio plus STT/playback/operation traces: the split number is joined and read back without repeated requests for already supplied digits; no choice is inferred from an unheard offer; a cut/unplayed read-back cannot authorize; corrected intent reaches one matching backend effect; an unheard receipt is recovered without executing again. Check whether premature endpointing, agent-over-user speech and read-back barge-in actually happened. If absent, mark that trigger not exercised. STT/TTS outage, missing recording, elapsed run completion or a high judge score cannot be counted as a pass.

CALL BUDGET AND PARALLEL WORK

Six of the global 14 calls are already spent. Eight salon-graph calls remain for sessions 3–6, at most two per session. Zero native calls remain. Failed, inconclusive and accidentally expanded simulations count too; reconcile any uncertain launch before assigning the balance.

Session 3 call 1 is the stress repair above. Reserve call 2 for the final authoring revision: use the same stress case if authoring changes affect its core/voice path; otherwise use normal set kQ8b6nMZ/persona MUFJdYAHdBHU6UbRRu4ykM. Record that choice before launch. These are the session's two calls, not a prerequisite call plus a fresh two-call checkpoint. A final stress repeat uses the explicitly reserved second slot; there are no automatic retries. Any unexercised normal/stress trigger remains a visible coverage gap. Keep six calls reserved for sessions 4–6. Do not spend an unused slot merely to fill the allowance.

The observed 9–10 minutes per Coval run mostly followed the call: in one run the bot was ready around 7 s, Coval dialled after 29 s, audio lasted 86 s, and total completion was 535 s. This does not prove every future delay is on Coval; record stages separately. Use the wait for independent offline checks, documentation or fixture work, keeping the tested source/profile fixed until evidence is captured.

Independent already-authorized jobs may overlap using distinct --port values, separate bot/tunnel/fixture/log resources and separate Coval agent IDs. Runner.run updates each agent's callback URL, so two concurrent launches on JxGgSPA55DcFcDcvbKAUQM can redirect each other's calls even with different ports. Do not run this session's two dependent checks in parallel, create extra agents/calls to fill idle time, or launch a native job. If an independent job's isolation cannot be established, run sequentially. Concurrent provider outages in session 2 did not prove their cause.

Commit 47f4192 also adds --concurrency N for several calls within ONE planned run, distinct from --port for separate jobs. It requires a bot with its own per-call limit and marks concurrent latency as incomparable to sequential latency. This session's repair and final checks depend on different code stages, so keep --concurrency 1 and --max-simulations 1 for each; the new option does not expand the allowance.

Review Phase A before broader authoring changes. Repair critical consent/delivery failures first. External-service failure or absent triggers leave live acceptance pending; finish useful independent offline work without pretending the stress gate passed or exhausting later sessions' allowance. Carry the separate implementation/offline/live statuses into the report.

HARNESS VISION AND FRAMEWORK READING

Read Harness architecture and framework reuse in docs/plans/reusable-voice-core-sessions.md. Implement its ordinary authoring contract by composing existing packages: AgentSpec for agent configuration, concrete Pydantic types for runtime validation, typed templates for prompts, SDK tools/hooks for execution policy, and the existing Pydantic Graph driver for declared workflows. Core adds voice/session/evidence semantics; applications add only domain work. The same domain tool must remain usable in one agent, a specialist handoff and a controlled flow.

Read these references and translate each applicable feature into a real implementation decision:
- https://pydantic.dev/docs/ai/core-concepts/agent-spec/ — compose model/settings/instructions/capabilities with concrete deps_type/output_type; avoid duplicating the native agent schema.
- https://pydantic.dev/docs/ai/tools-toolsets/toolsets/ and https://pydantic.dev/docs/ai/core-concepts/hooks/ — reusable function toolsets and existing prepare_tools/prepare_output_tools policies.
- https://pydantic.dev/docs/ai/capabilities/overview/ and https://pydantic.dev/docs/ai/capabilities/on-demand/ — use supported capability composition; defer optional specialist bundles only when useful, with all authorization guards always active.
- https://pydantic.dev/docs/ai/core-concepts/output/ and https://pydantic.dev/docs/ai/tools-toolsets/deferred-tools/ — preserve output-function/direct completion and the conditional native-approval decision.
- https://pydantic.dev/docs/ai/graph/builder/ — compile legal routes through the existing typed graph, not another scheduler.
- https://docs.pipecat.ai/pipecat/flows/introduction — use the native authoring experience as the reference for N13: declarations and plain handlers, with our shared session guarantees replacing its repeated application guards.
Use installed APIs, not unverified current examples. Document a concrete mismatch before writing custom code for a feature that the SDK already supplies.

OBJECTIVE

An ordinary new application must consist of agent.yaml, fields-only Pydantic models, prompts, typed domain/backend functions and a thin entry point. It must not supply a CallContext subclass, custom confirmation interpreter, Conversation/Desk/AgentBuilder assembly, or one Operation subclass per ordinary function. Prove this with the existing clinic fixture and one salon identity/readback/action slice.

READ AND EXTEND

Start in core/src/core/__init__.py and the session-2 public boundary, then flowspec.py (FlowSpec, resolver, diagnostics and compile), steps.py (standard kinds, ConfirmedOperation, AgentBuilder and StepFlow), completion.py, graph.py, flow.py, operations.py, policies.py, readings.py, interpreters.py, conversation.py and decisions.py. Trace the new core/tests/clinic_flow, test_public.py and test_sdk.py as well as the older clinic.py/test_reuse.py. Preserve the existing pydantic_graph execution and core.turn engine; do not implement another scheduler.

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

Start with native Pydantic AI AgentSpec for the supported model/settings/instructions/capabilities subset and pass the concrete runtime types into construction. The session-2 experiment must show how it composes with existing AgentBuilder hooks and typed templates. Retain only workflow/voice-specific fields in the outer manifest. If the locked API cannot express a required behavior, show the smallest failing example and keep a narrowly scoped existing construction path for that gap; do not invent another complete agent spec. Native deps_schema/template checking does not validate domain state at runtime; output_schema alone does not replace actual output/receipt Pydantic validation. Existing AgentBuilder already has prepare_tools/prepare_output_tools hooks: connect declarations to them, rather than inventing duplicate preparation.

Group genuinely reused ordinary tools with native FunctionToolset where appropriate. Keep output functions distinct from ordinary toolsets so receipt speech still ends the run without another model request. Use current Hooks/capability composition for shared policies instead of per-application subclasses. State how optional specialist instructions are selected; a deterministic step switch is preferable to an extra discovery request for a known booking stage. If demonstrating on-demand loading, assert optional content is absent before loading, present afterward, and that execution guards apply in both states. Do not install the separate Pydantic AI Harness library unless a concrete required capability demonstrably removes custom work and its dependency change is authorized.

GUARDED OPERATIONS AND SPEECH

Adapt typed ordinary functions into the existing shared Operation boundary. Both model-selected tools/output finishers and application-selected actions must perform the same sequence:
validate full arguments → settle unresolved previous outcome → check current preconditions and exact approval → consume one authorized attempt → call backend → validate and match receipt → atomically apply allowed assignments/invalidation → record audit → render outcome.

Keep one flow-changing/business-mutating action per model reply, and a shared turn-wide budget across classifications, direct actions, tools and graph hops. A direct transition must recheck authorization. Tool visibility is not authorization; direct calls with stale or forbidden inputs must still fail.

Resolve session 2's open turn-policy question explicitly: a doubly empty structured reply used more requests than native because of SDK output retries. Reuse SDK retry/usage controls if a bounded policy can reduce that work while preserving valid structured outputs and one truthful recovery. Record actual classifier/step/retry counts and any tradeoff; do not widen the N6 bound or optimize by weakening validation.

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
- Complete N13: show the same clinic's domain functions/state reused across all three agency patterns, no application session/interpreter subclasses, no repeated approval/speech/budget plumbing, typed prompt failures caught at startup and no core edits to add the application. Include an author-file inventory against the native reference, separating domain logic, configuration and shared runtime work.
- SDK reuse is executable: native spec/template construction, concrete output validation, tool preparation and graph transitions each have a relevant check. Re-run affected N1–N12 cases; a smaller manifest must not regress the native-runtime gate.

FINAL VERIFICATION AND HANDOFF

Run the shared Ruff/format/core/salon commands and ty/assert suites in every affected consumer. Re-run the existing N1–N13 matrix, completing N13 and preserving recorded native coverage gaps. Repeat N12's frozen timing protocol only if the changed execution path warrants it; separate engine overhead from real speech timing. Build/install the wheel outside the checkout using the session-2 proof as the starting point; assert public imports, packaged prompts/manifests, core-only optional-import isolation and clinic/salon-slice execution without sibling-source imports.

Run the same four live-text salon cases once against the final code, using explicit segment acknowledgements: at most 12 caller turns each, 100 target requests total and, only if needed, 48 adaptive caller requests. Preserve the recorded finite token/spend limits; Phase A probes or additional model checks count toward the same session allowance. T3 remains an explicitly owned session-4 failure if untouched, not an accepted behavior or a fabricated four-case pass. Other new failures must be diagnosed and fixed. Use offline replay for iteration before considering any further paid request.

After cheap checks pass, run only the remaining, preassigned second Coval call described above. If it was already consumed during a necessary repair, there is no fresh final allowance: record exactly which later changes lack live evidence. Clinic proves the new author API offline; salon voice calls prove only the shared paths they actually execute. Do not claim the old salon entry point has validated the full manifest migration, or that one successful call closes all overlap/latency questions.

Save docs/research/reusable-core/session-3.md with parent/child SHAs, dependency/lock and speech-profile hashes, actual manifest schema/public imports, valid minimal example, invalid-definition examples, operation/interpretation evidence and authored orchestration versus domain-code inventory. Link exact commands/results, request/token counts, run/simulation/trace IDs, audio and receipts, spent/remaining calls, observed/missing interruption triggers, and the status of every inherited finding. Keep implementation, deterministic, semantic and voice verdicts separate. Show the updated N1–N13 status and concrete outstanding work, not just an aggregate score.

Gate: ordinary authoring works on clinic and the real salon slice with one engine and one consent authority, without losing the session-2 delivery/receipt guarantees. Fix defects found in this scope; do not merely list them. Provider outages and unobserved triggers can remain honestly pending within the fixed budget. Stop before the full salon/Faro migration. Commit the session-3 changes and open a reviewable PR stacked on feat/session-2-public-boundary while #17 remains open, otherwise against updated main. Attach/link the PR and report its base, exact tested revision and remaining gates.
~~~

## Prompt 4 — Migrate salon-graph through the public contract

Copy this complete block after the clinic and salon slice prove the authoring contract.

~~~text
Implement session 4 of the reusable voice core migration. Read AGENTS.md, CLAUDE.md, the roadmap, the Verification contract, and session-3.md with its implemented public schema/example. Apply pydantic, python-dev, building-pydantic-ai-agents and ponytail:ponytail. Preserve assert-script tests. Live target remains ONLY salon-graph.

HARNESS VISION AND FRAMEWORK READING

Read Harness architecture and framework reuse in docs/plans/reusable-voice-core-sessions.md. Salon is the first complete production adoption: its configuration references concrete domain types/functions, while core composes AgentSpec, typed templates, SDK hooks/tools and the existing graph. Do not recreate native salon-pipecat-flows' application consent/speech wrappers in a new salon runtime class.

Use https://pydantic.dev/docs/ai/core-concepts/agent-spec/ for the native agent/template subset, https://pydantic.dev/docs/ai/tools-toolsets/toolsets/ for actual shared tool bundles, https://pydantic.dev/docs/ai/core-concepts/output/ for code-rendered transaction completion, and https://pydantic.dev/docs/ai/graph/builder/ for declared legal transitions. The public contract from session 3 supplies these features; application authors must not repeat their construction. Preserve the native comparison matrix from session 2 and N13 authoring evidence from session 3. A domain-specific function is allowed; a copied generic guard is a migration failure.

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

Re-run the native comparison rows affected by migrated booking, consent, receipt speech or routing, using the same isolated reference and scenario versions. Check request counts on matched application continuations: changing configuration loading must not add a hidden planning/classifier/agent cascade. Update the orchestration inventory to show which native-style application wrappers are now supplied once by core.

Then run the same four bounded live-text cases and at most TWO frozen Coval salon simulations. Prove the entry point actually loaded the migrated manifest and public session, using its recorded version/hash. Inspect audio plus proposal/delivery/operation evidence. Compare rendered prompt changes and provider request counts explicitly; natural-language approval may still require a classifier request. Do not claim zero-model approval from historical numbers.

DELIVER AND STOP

Write session-4.md with each old orchestration responsibility and its new declaration/core owner, retained domain functions, prompt diffs, business-matrix results, real evaluation evidence and remaining compatibility. Gate: real salon behavior runs through the documented API without custom runtime/consent assembly, critical invariants pass, and live evidence is present or accurately pending. Do not widen this session into Faro or launcher extraction.
~~~

## Prompt 5 — Migrate Faro's lifecycle, grading and persistence

Copy this complete block after the salon proves the public contract in a real application.

~~~text
Implement session 5 of the reusable voice core migration. Read AGENTS.md, CLAUDE.md, the roadmap, Verification contract and session-4.md. Apply pydantic, python-dev, building-pydantic-ai-agents and ponytail:ponytail. Keep all live text/Coval verification on salon-graph. Do not launch Faro's live bench.py or any Faro Coval run.

HARNESS VISION AND FRAMEWORK READING

Read Harness architecture and framework reuse in docs/plans/reusable-voice-core-sessions.md. Faro proves the package generalizes beyond booking: one companion persona can serve several typed stages, with learning and persistence expressed as domain functions. It uses the same compiled definitions, session, guarded operations and delivery evidence as salon.

Use https://pydantic.dev/docs/validation/latest/concepts/unions/ and https://pydantic.dev/docs/validation/latest/concepts/type_adapter/ for structural state/value validation; https://pydantic.dev/docs/ai/core-concepts/dependencies/ for Store/clock injection; https://pydantic.dev/docs/ai/core-concepts/agent-spec/ for compiled prompts/configuration; and https://pydantic.dev/docs/ai/graph/builder/ for explicit learning stages. Retain the existing pydantic-extra-types PhoneNumberValidator and SQLite implementation. The grader may use typed Pydantic AI output through the shared invocation boundary; its rubric remains Faro code. Do not add generic agent memory or a durable-workflow package to replace acknowledged domain storage.

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

For shared-core changes required by Faro, re-run affected N1–N12 comparison cases and the external clinic without changing its domain definition. The same service-injection, validation and capability hooks must work for both applications; no conditional core behavior keyed on the application name. Preserve N13 by listing Faro's remaining custom code and why it is domain work.

DELIVER AND STOP

Save session-5.md with phase-to-step and method-to-owner mappings, the final authoring files, full L01–L16/additional check results, persisted-state evidence, resource-install proof and salon regression results. State explicitly: “Faro migrated and checked offline; not newly validated with live models or voice.”

Gate: the same public contract supports Faro without custom generic runtime assembly; identity, grading, persistence and resume checks pass; shared salon behavior is preserved. Do not claim horizontal scalability or change the curriculum/model/persona as cleanup.
~~~

## Prompt 6 — Finish the launcher, tooling consumers and release proof

Copy this complete block after salon and Faro use the public authoring contract.

~~~text
Implement session 6 of the reusable voice core migration. Read AGENTS.md, CLAUDE.md, the roadmap, Verification contract and session-5.md. Apply pydantic, python-dev, building-pydantic-ai-agents and ponytail:ponytail. Live text and voice evaluation remain salon-graph only, using the final assigned two-call Coval checkpoint.

HARNESS VISION AND FRAMEWORK READING

Read Harness architecture and framework reuse in docs/plans/reusable-voice-core-sessions.md. Finish a versioned, transport-independent conversation core with an optional Pipecat boundary and optional existing tracing. Consumers import useful public capabilities; a future compiler can emit thin definitions for the same package. This session must not introduce a second agent scheduler or fold the independent native comparison into core.

Read https://docs.pipecat.ai/pipecat/fundamentals/interruptions and https://docs.pipecat.ai/pipecat/flows/nodes-and-messages for lifecycle, speak/wait scheduling and interruption behavior. Use https://pydantic.dev/docs/ai/integrations/logfire/ for the SDK's OpenTelemetry integration without requiring Logfire, and https://pydantic.dev/docs/ai/core-concepts/message-history/ for public message/turn boundaries in tooling. Keep Langfuse as the existing backend. Reuse native pipeline/aggregator/transport facilities and stdlib resource loading rather than wrapping every service behind a new plugin abstraction. Check the relevant native N1–N13 evidence after extraction.

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

Script-only consumers need an isolated dependency environment plus only that member's own sources. Do not add packaging scaffolding where it serves no distribution/import need. Never put the salon packages, including the independent native reference, in the same environment; their shared import name can conceal a wrong import. Do not rely on editable workspace installs, PYTHONPATH/sys.path injection, sibling bot/tests modules or a repository cwd. Record resolved module/resource locations and wheel hashes.

Run root Ruff and formatting checks, per-member ty, every offline assert suite and the new tool smoke checks. Re-run the external clinic authored without core modifications. Confirm import boundaries allow only the implemented adapter/launcher modules to import Pipecat, and no core module imports any use case.

Exercise two interleaved sessions with shared compiled definitions; cancel one and verify the other retains independent state/proposals/history. This proves isolation, not capacity. Check launcher startup, successful shutdown and failing setup cleanup with fake services for all three applications.

Re-run N1–N13 on the final package where applicable, especially N6–N12 after launcher changes. Confirm the native reference remains independent and record any reference drift. Finish the package/capability adoption table with actual imports, locked versions, removed custom machinery and tests. Update CLAUDE.md/README boundaries to match the implemented extras/launcher/public API. No claim of greater reusability is complete without the installed external clinic and both domain migrations using the same API.

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
| Cost-conscious semantic and voice verification | Every session | Four bounded text cases; two salon-graph voice calls per session plus two native calls in session 2 only; real coverage |
| Documented SDK/package reuse and minimal custom runtime | 2–3; adoption 4–6 | AgentSpec/templates, typed validation, hooks/toolsets and graph integration checks; versioned adoption table |
| At least native-quality runtime and simpler application authoring | 2 runtime; 3 authoring; regression 4–6 | N1–N13 with independent native source, matched cases, actual evidence and explicit gaps |
| Fair native comparison without uncontrolled paid expansion | 1–2 | Settings/revision manifest; paired offline measurements; user-selected 14-call cap with two native calls in session 2 only |
| No invented performance or scalability claims | Every session | Raw timing/request/spend evidence, sample counts and explicit limits |

## Using the prompts

Paste one complete numbered block per coding session, with the roadmap and this complete document available in the checkout. The numbered prompts depend on shared sections: the Harness architecture and framework reuse section and Verification contract are required reading. The N1–N13 matrix is also embedded in prompt 2 so copying that block includes its criteria.

Before starting a session, verify:

- The checkout contains the previous session's implementation commits and the latest planning-document changes. A new worktree from an older base does not inherit another worktree's uncommitted files.
- Both docs/plans/reusable-voice-core-roadmap.md and docs/plans/reusable-voice-core-sessions.md are readable. Use this repository copy rather than a stale pasted excerpt.
- The preceding docs/research/reusable-core/session-N.md report and its referenced evidence are available. Carry forward actual failures, pending gates, scenario versions and exact run IDs.
- The requested skills are available and AGENTS.md/CLAUDE.md have been read. Existing credentials remain in the runtime configuration; never paste secrets into a session prompt.
- Session 2 can read the independent native reference and knows each job's target, allowed cases and remaining cumulative budget. Existing launches count even if their result failed or was inconclusive; reconcile uncertain launches before any retry.
- For session 3, use the complete updated prompt above: parent PR #17 at `47f4192` (or its verified successor/merged main), section 10b of its report, and eight remaining harness calls with no native allowance. Copy the updated block before moving if these documentation edits are not yet committed to your starting branch. The first stress repair is part of session 3's two calls.

Each session's opening update should identify its starting revision, the reports/resources it found, unresolved predecessor findings and its assigned evaluation allowance. Missing context is a handoff gap to recover from files/artifacts, not a reason to repeat paid evaluations. If requirements have changed since an earlier checkpoint, record the change and resulting comparison limits.

Carry implementation and evidence forward. If a milestone needs another context, continue that milestone from its report; do not remove checks to fit or start the later migrations from an unmodified baseline. Session 3 proves ordinary authoring; session 6 completes adoption across the repository.
