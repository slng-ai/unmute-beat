# Portable typed conversation flows

Research and proposed direction, 2026-09-23. Reviewed checkout: `452698d`.
This is a recommendation, not an implemented configuration API or an accepted migration decision.
The plan that follows from it is [`docs/plans/portable-typed-flows.md`](../plans/portable-typed-flows.md).

**Recommendation.** Keep application-owned workflow state and guarded execution. Make the portable unit a step with a completion contract, and make classification optional at individual decision points. The same format can describe a ten-step procedure, a branching service flow, or one conversational Q&A step. A step does not require its own agent or a model request.

The current salon implementation is evidence that parts of this work. It does not yet establish general portability or classifier accuracy across domains.

**What the three calls show.** I retrieved all observations for the three supplied traces through the Langfuse CLI's observations API, scoped to 13:15–13:25 UTC. The responses contained 52, 68, and 40 observations, with no continuation cursors. Counts below use the harness's turn observations; TTS generations and nested agent spans are not counted as additional caller turns.

| Trace | Relevant observation | Implication |
|---|---|---|
| [Booking then complaint](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=af9fd9737e3a38a5d6ebad43a79ea0a6) | Phone acceptance, complaint acceptance, and goodbye each completed with one classifier request. A separate classifier attempt was interrupted after about 0.15 s. | Narrow decisions can replace a conversational request. Cancellation must remain a first-class outcome. |
| [Complaint](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=a3ea3389250b51efa52aeb2e3f702688) | Phone acceptance took about 0.95 s, then a complaint-agent run asked what happened. First text arrived at about 1.94 s. The classifier returned `accept`. | A correct classification can still be followed by another request because the next step needs to speak. |
| [Booking](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=ab9d60c23024a73a29ff9cc5f2fc082e) | Phone acceptance and goodbye used classification. Choosing an offered time used the booking agent and existing authorization code. | Even this one use case already mixes interpretation strategies. |

Across these calls: 23 harness turn observations, 25 recorded model requests, nine classifier attempts, eight completed classifications, and seven completed classifier turns with no additional conversational request. Completed classifier durations were 0.860–1.127 s, with a sample median of 0.967 s. These are request durations, not caller-to-audible-response latency. Eight results are insufficient for a meaningful production latency or semantic accuracy claim.

The complaint call's `classifier_fallback=true` is misleading if read as an interpretation failure. In [the affected turn](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=a3ea3389250b51efa52aeb2e3f702688&observation=e1ece7569413d197), classification accepted the phone confirmation. `Turn._hops` subsequently overwrites the last classifier request's fallback flag when the next node has no local action (`core/turn.py:334`). Separate unresolved interpretation, refused action, and ordinary downstream conversation before comparing designs.

Several transcripts contain apparent recognition errors, such as “circuit” in a haircut complaint. The traces show the text the application received; they do not establish what was actually spoken. Moving to a different classifier does not by itself solve that input-quality problem.

**The reusable contract.** A flow owns its current step, collected facts, pending proposal, and allowed outcomes. Each step specifies:

| Part | Meaning |
|---|---|
| Purpose | The information or business outcome this step needs. |
| Input contract | Typed fields, allowed choices, or an exact proposal awaiting an answer. |
| Available operations | Registered tools/actions and knowledge available here. |
| Completion evidence | Validated fields, an approved current proposal, or a matching successful backend receipt. |
| Continuation | The next step after completion; explicit branches where needed. |
| Other outcomes | Stay and clarify, revise, cancel, fail, or hand off. A failure is not completion. |

The model interprets language and proposes data. The runtime validates the result and decides whether completion evidence exists. The backend remains the authority on whether an action actually happened.

This separation has established precedents. Anthropic distinguishes workflows with code-defined paths from agents that choose their own process, and recommends the simplest adequate approach. Rasa separates language understanding from business flows and conversational interruptions. These support the architectural direction, not a claim that this implementation is already production-ready. [Anthropic](https://www.anthropic.com/engineering/building-effective-agents), [Rasa dialogue management](https://rasa.com/docs/learn/concepts/dialogue-management/).

**Ten required steps.** For an illustrative access-request process, the completion contracts could be:

| Step | Complete only when |
|---|---|
| 1. Identify requester | The required identity check returns a matching result. |
| 2. Collect team | A supported team is selected. |
| 3. Collect system | A supported system is selected. |
| 4. Collect access level | A supported level for that system is selected. |
| 5. Collect reason | The configured required information is present. |
| 6. Collect start date | A date passes the configured constraints. |
| 7. Collect end date | It passes the date and duration constraints. |
| 8. Check eligibility | The policy service returns an eligible result. |
| 9. Confirm request | The caller approves this exact current proposal. |
| 10. Submit request | A matching receipt confirms submission. |

Advancement from step 4 to step 5 is ordinary code once step 4's contract holds. No classifier is needed to decide which step comes next. Submission is not the same as approval or provisioning; the receipt must state which outcome occurred.

Distinguish two authoring policies: a step may be satisfied by information already supplied, or it must explicitly be performed at its position in the sequence. For example, a caller can provide two ordinary fields in one sentence, but cannot pre-approve a proposal that has not yet been presented. Rasa exposes a similar distinction for collection and explicit re-asking. [Rasa flow steps](https://rasa.com/docs/reference/primitives/flow-steps).

When a caller revises an upstream fact, invalidate dependent checks and approvals. When they ask a side question, retain the unfinished business step and return to it. Before accepting consent after such a detour, re-present the current proposal. Cancellation and escalation are explicit exits, never successful completion of skipped mandatory steps.

**One Q&A node.** Use the same runtime with a single conversational step, knowledge instructions/retrieval, and any allowed tools. Each answer finishes a turn; the node remains active for the next question. No separate classifier is required for ordinary questions. Call termination can still use a narrow decision if the channel needs one. Business actions introduced later inherit their own guards.

**Where a classifier belongs.** “Understanding input” is always necessary for natural language; a separate model request is only one implementation.

| Situation | Default approach |
|---|---|
| Known next step after validated completion | Deterministic transition. |
| Backend success/failure | Validate and match the receipt. |
| Form, button, or DTMF input | Parse and validate the explicit input. |
| Ordinary fact collection | Extract typed values within the step's existing model request. |
| Open Q&A | Conversational agent with appropriate knowledge/tools. |
| Exact proposal confirmation or contextual goodbye | Narrow interpretation is a useful candidate; application guards still decide. |
| Choice among presented options | Typed selection constrained to current option IDs, then membership/freshness checks. |

Keep the current separate interpreter for salon approvals as the baseline. Its small context, lack of business tools, and per-turn caching are useful properties. Do not make its four salon-specific output fields the universal schema. A confirmation, a selection, and a collection step should each request the data they need.

A separate classifier is not an independent proof of consent. The current classifier and agents use the same underlying model. Identity, freshness, and proposal guards prevent several classes of misuse, but a false `accept` for a valid current proposal can still pass those guards. Schema validity does not establish semantic truth. Likewise, phone readback proves agreement with the number; it does not by itself prove control of that number or stronger identity assurance required by another domain.

For a decision that needs language generation after every answer, putting a classifier first can add latency consistently. Compare that with one structured agent request whose tool executor still checks the same evidence. Conversely, when a classifier can finish the decision and the next output is known, the current direct-request path is sensible. Pydantic AI supports both output functions and direct model requests. [Output functions](https://pydantic.dev/docs/ai/core-concepts/output/#output-functions), [direct requests](https://pydantic.dev/docs/ai/core-concepts/direct/).

**Configuration users can manage.** Offer four initial step types: collect, confirm/select, action, and conversation. The editor should show a short ordered list by default, with branches exposed when needed. Users choose fields, questions, available operations, completion conditions, and failure outcomes. Classifier model selection belongs in an advanced execution policy, not in the basic setup journey.

Persist one versioned JSON/YAML representation. Use Pydantic models with a `kind` discriminator for step configuration, and ordinary existing runtime objects after validation. Reject unknown fields, missing destinations, unknown handlers, invalid field references, and action paths that bypass required completion evidence before publishing. Restrict conditions to supported operators or registered checks; user configuration must not execute arbitrary Python. Pydantic's discriminated unions fit this boundary. [Pydantic unions](https://pydantic.dev/docs/validation/latest/concepts/unions/#discriminated-unions).

The following is a **proposed authoring sketch**, not an API currently supported by this repository:

```yaml
version: 1
id: service_request
steps:
  - id: details
    kind: collect
    fields:
      description: {type: string, min_length: 1}
    question: What would you like us to help with?

  - id: approval
    kind: confirm
    proposal: create_service_request
    on_reject: cancelled
    on_revise: details

  - id: submit
    kind: action
    handler: create_service_request
    approval: approval
    on_error: failed
```

In this proposed format, completion advances to the next listed step. Collect stays until its fields validate. Confirm advances only on acceptance of the bound proposal; conditional or unclear answers stay and clarify. The registered `create_service_request` operation supplies the typed arguments, proposal readback, execution, and receipt validation. The action consumes approval for those same arguments. `cancelled` and `failed` are terminal outcomes; an unknown backend outcome stays pending reconciliation instead of being reported as a definite failure. Reaching the end after a matching successful receipt completes the flow.

The Q&A configuration uses the same envelope:

```yaml
version: 1
id: help
steps:
  - id: answer
    kind: conversation
    knowledge: help_center
    instructions: Answer from the supplied knowledge; ask when details are missing.
```

Here `help_center` is a registered knowledge source. Conversation steps stay active across answers until an explicit end or handoff. These examples describe the desired user experience; implementing them still requires a loader and runtime mapping. A new external business integration still requires a connector or developer-provided handler. Configuration cannot invent correct backend behavior.

**What to reuse.** Pydantic AI already provides `Agent.from_spec`/`from_file` for model, instructions, capabilities, and schemas. Use that for the agent portion; it is not a business-flow engine or a substitute for completion guards. The installed package has these entry points. The checkout locks Pydantic 2.13.5, Pydantic AI/Graph 2.46.0, and Langfuse 4.15.4. No dependency upgrade is needed merely to design this layer. [Agent specs](https://pydantic.dev/docs/ai/core-concepts/agent-spec/).

Pipecat also has a declarative `FlowConfig` for nodes, tool names, and transitions, including branches on tool results. Its implementation is present in the local environment. Its manager expects a Pipecat LLM service, whereas this application puts Pydantic AI behind its own processor. Adopting it would be an orchestration integration/migration, not loading the existing table unchanged. Evaluate it if owning orchestration in Pipecat becomes the product direction; do not layer a second flow manager over the current one just to obtain YAML. [Pipecat configuration](https://reference-server.pipecat.ai/en/stable/api/pipecat.flows.config.html).

Pydantic Graph is another candidate when branching and resumable control flow become the hard part. Its documentation explicitly advises considering simpler programmatic handoffs first. For this project, first map the small public configuration onto existing `Node`, `Exit`, `Opening`, and guarded executors. Choose one orchestration owner; replace the current engine only if a concrete second use case demonstrates a meaningful reduction in custom logic. [Pydantic Graph](https://pydantic.dev/docs/ai/graph/graph/), [programmatic handoffs](https://pydantic.dev/docs/ai/guides/multi-agent-applications/).

**Current portability limits.** The generic classifier transport is already reusable. The reusable engine already handles budgets, cancellation, history, and optional local advancement. The remaining domain wiring is substantive:

- `salon.agents.build` knows salon tool names and which named agents use structured speech. `advance` maps salon question labels to salon executors.
- `Asked` combines phone, booking, complaint, and closing fields in one model. Pending decision ownership and cache validation live on `SalonCall`.
- Completion is distributed among domain finishers and write receipts; `Node.requires` checks entry prerequisites, not a general step-completion contract.
- `route` and the checkpoint support one outstanding prerequisite. Nested flows are deliberately excluded by current graph tests. A flat ten-step sequence does not itself require nesting.
- Slot selection, complaint detection, and some policy responses retain language-specific heuristics.
- The diary is in memory. The code inspected does not provide a durable, multi-worker business store or a durable call-resume mechanism.

**Scalability.** Compile and validate a flow once per published version, share immutable agent/model configuration, and keep state and pending decisions per call. Pin each active call to its flow version. Only supply the active step's relevant tools and context; specialist capabilities do not need to be loaded on every turn. Preserve a single shared turn budget and one executor for each action regardless of whether the model or deterministic path requested it.

For multiple workers, use backend-enforced idempotency and concurrency constraints, plus persistent session state if reconnect/restart recovery is a requirement. A ten-step in-call workflow does not by itself justify a durable workflow platform. Calls spanning restarts or long external waits are a separate execution requirement. There is no concurrent-load measurement in these three traces.

**How to decide empirically.** Prove the contract on three examples before freezing the user format: the current salon, one genuinely different ordered ten-step procedure, and a one-node Q&A assistant. Compare the current separate classifier, a constrained single-agent interpretation path with equivalent executor guards, and deterministic parsing where input permits it. Run every candidate on the same cases and full conversations.

Measure false acceptance of unapproved proposals, skipped mandatory steps, successful completion, unnecessary clarification, unresolved interpretation, downstream requests, and time to audible response. Include corrections, conditional answers, side questions, topic switches, interrupted readbacks, stale options, duplicate requests, and unknown write outcomes. Report semantic classification, policy authorization, backend outcome, and audio delivery separately. Human-reviewed labels are needed for semantic accuracy; three calls and the existing 15-case live evaluation cannot establish cross-domain quality. Langfuse recommends deriving evaluation priorities from reviewed observations. [Error analysis](https://langfuse.com/guides/cookbook/error-analysis-llm-applications).

The public configuration should follow validation of these contracts on those examples. The state-solver proposal below is the next experiment to resolve how language interpretation fits that configuration. Runtime code, prompts, live datasets, and dependencies were not changed during this research; no billable model benchmark or production experiment was run.

**Follow-up: replace narrow classification with a focused state solver.** This is feasible and is a better hypothesis for correction and flow-switching turns. The current classifier is already an LLM request. A typed flow alone does not reduce model calls; savings require that its interpretation replace work otherwise done by a conversational agent. Broader structured output can combine interpretation, proposed field changes, a permitted transition, and a short reply in that same request.

Rasa provides a close precedent: its dialogue-understanding LLM proposes commands for starting/cancelling flows and setting/correcting fields; the dialogue manager executes the defined business logic. This supports the separation of responsibilities, not an accuracy or latency claim about this project. Adding a generated short reply to our command result is our proposed extension. [Rasa dialogue understanding](https://rasa.com/docs/learn/concepts/dialogue-understanding/).

The request should receive the active step, relevant collected values, the current proposal and its delivery status, relevant conversation history, and the allowed destinations with their purpose and entry requirements. A correction such as “the last three digits are 123” needs the earlier number. A switch to complaints needs awareness that complaints exists and what information it requires. Compile this view from the flow definition; users should not maintain a second classifier prompt describing the same flow.

Use one typed decision with variant-specific arguments, rather than three independently writable fields for arbitrary state, step and speech. For example, this is an illustrative output for an already verified caller abandoning an uncommitted booking:

```json
{
  "decision": {
    "kind": "switch_flow",
    "target": "complaints",
    "current_flow": "abandon",
    "complaint_details": null
  },
  "reply": "Okay, we'll stop the booking process. What happened?"
}
```

The executor invalidates the booking proposal, marks that task abandoned, applies the legal transition, and releases the reply. Any complaint details already supplied travel with the decision, so it does not unnecessarily ask the caller to repeat them. One transition may make several related state changes atomically; it is still one application action. A request to pause booking uses different semantics and preserves its resumable context. Abandoning a draft does not cancel an existing appointment. An already-started write with an unknown outcome must be reconciled before claiming that nothing was booked.

For a corrected phone number, the decision contains the new candidate value. Application code validates it and uses the existing `readback_number` executor. It replaces the pending phone question and invalidates affected identity/approval state. The candidate is not a verified identity; confirmation and lookup remain necessary before verified state can be set. This needs one model request for the correction turn, not a second request to interpret the same correction. Ambiguous digits get a clarification instead of invented values.

The model must not write fields such as `customer_verified`, successful write receipts, or completion flags. It proposes a correction, acceptance, abandonment or other permitted action; code applies the corresponding transition. Pydantic validates structure and field constraints, while executors validate current identity, proposal, prerequisites and backend outcomes. Structured output does not guarantee correct interpretation. [Gemini structured output validation](https://ai.google.dev/gemini-api/docs/structured-output#best-practices).

**The concrete implementation boundary.** Reuse `Classifier[InputT, OutputT]` in `core/classifiers.py`: it already performs one native structured request with no model repair loop. Replace the salon's narrow input/output models and dispatch mapping with richer turn context and decision variants. Reuse existing action executors rather than giving a model direct `call.commit` access. Pydantic AI output functions are another supported way to validate arguments and terminate a run without sending the result back to the model, but changing transport is unnecessary for this experiment. [Pydantic AI output functions](https://pydantic.dev/docs/ai/core-concepts/output/#output-functions).

Two runtime details are essential:

- `Turn._hops` currently follows a `Handoff` by running the destination node. A solver that changes the step and supplies a sufficient reply needs a supported transition that ends the current turn, sharing the same routing, prerequisite, checkpoint and interruption handling. Otherwise the destination agent still adds a request. Deterministic entry questions can also finish a transition without a model request.
- `SalonCall.pending_question()` excludes interrupted readbacks. That is correct for consent, but `advance()` currently uses that eligibility to decide whether to classify. The solver must also interpret corrections and topic switches when the proposal was interrupted. Supply the interrupted proposal as context with acceptance disabled; do not make it eligible for authorization.

Finish parsing and validating the decision before committing state or speaking. Discard results from a superseded turn. Keep confirmations and number readbacks application-rendered, and produce success statements from actual backend receipts. A generated short acknowledgement can accompany a validated transition, but cannot predict whether a future backend operation succeeds.

**Expected request counts, not benchmark results.** These paths follow from the current control flow; actual counts can vary with openings, refusals, repairs and interruptions.

| Turn | Current representative path | Proposed path |
|---|---|---|
| Clear acceptance with a deterministic continuation | Classifier → executor: 1 request | Solver → executor: 1 request |
| Full phone correction after a delivered readback | Classifier → verification agent: 2 | Solver → validated readback: 1 |
| Switch from booking to complaints after a delivered proposal | Classifier → booking handoff → complaint opening: potentially 3 | Solver → transition and reply: 1 |
| Switch during an interrupted readback | Classifier can be skipped; booking and complaints may still each run | Solver → transition and reply: 1 |
| Ordinary Q&A | Conversational request: 1 | Conversational request: 1; no mandatory preliminary solver |

An operation whose newly fetched result requires further model reasoning may still need another request. A fixed receipt or deterministic next question does not. Model retries also increase request counts; a strict one-request path must use a deterministic safe clarification when validation fails instead of silently retrying.

Fewer sequential requests can reduce latency, but the richer solver has more context and output tokens and waits for complete validated output before speaking. A one-request acceptance could become slower. The traces' completed classifier median of 0.967 seconds is a baseline for the existing classifier, not a forecast for the solver. Compare end-of-user-speech to first audible response, request counts, token use and task success on the same cases and model settings.

**Smallest useful experiment.** Extend the existing interpretation path with phone corrections, abandon-and-switch, short clarifications and current acceptance behavior. Exercise it as the primary interpreter for those controlled steps, including interrupted proposals; do not prepend it to every ordinary agent response. Compare against the current path on reviewed cases covering conditional agreement, partial corrections, mixed intents, missing prerequisites, stale results and write races. Assert that mandatory checks remain unskippable and measure whether destination-agent requests disappear. Keep interpretation, authorized state changes and backend outcomes separately visible in Langfuse. Rasa's dialogue-understanding tests similarly evaluate expected commands and report latency/token usage; they are useful precedent for the evaluation shape. [Dialogue-understanding tests](https://rasa.com/docs/reference/testing/e2e/dialogue-understanding-tests).

**Validation performed.** Both illustrative YAML blocks parse successfully; they are not executable flow definitions yet. Repository checks ran through `uv run --no-sync` using the existing project environment. Actual final output:

```text
ruff check .
All checks passed!

ty check .
All checks passed!

python -m tests
all 13 modules ok
```
