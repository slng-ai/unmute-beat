# Plan: portable typed conversation flows

Written 2026-09-23 against `452698d`. Based on
[`docs/research/portable-typed-flows.md`](../research/portable-typed-flows.md).

This is a plan, not a decision to migrate. Each phase ends with a check. If a
check fails, we stop and rethink before starting the next phase.

## The goal in one paragraph

Any business should be able to describe a phone flow as a short list of typed
steps (collect, confirm, action, conversation). The app, not the model,
decides when a step is done and what comes next. The model only reads what the
caller said and proposes data. Pydantic graph owns the flow; our turn engine
owns the voice work. Everything reusable is a class in `core` that a business
(the salon today) subclasses or fills in. The salon keeps working the whole
way through, and we prove the idea on two more very different flows before
anyone writes a flow file.

## What we keep, no matter what

- State changes go through `call.commit(...)` only. The model never sets
  `customer_verified`, receipts, or "done" flags.
- One action per model reply (`one_action_per_reply`).
- Consent is owned by the app: the words of a question come from code, and
  every write checks the answer against that exact question.
- Success is only said after a real backend receipt.
- `core` imports nothing from `salon`. Only `core/processor.py` imports
  Pipecat.
- No new dependency without asking. No pytest. Every comment is a docstring.
- Prompts in `prompts/*.md` and tool docstrings are not edited without a live
  check.
- **One flow owner: pydantic graph** (`pydantic_graph` 2.46.0, already locked
  because Pydantic AI runs on it). We do not add Pipecat Flows, and we do not
  keep our own step-routing next to the graph once the trial passes.

## Order of work

| Phase | What | Changes behaviour? | Model spend |
|---|---|---|---|
| 0 | Honest measurement | No | None |
| 1 | Test cases and a baseline | No | One live eval run |
| 2 | State solver experiment (salon, behind a switch) | Only when switched on | Live eval runs |
| 3 | Compare and pick the default interpreter | No | None |
| 4 | Reusable base classes in `core`, salon ported onto them | No | None |
| 5 | Pydantic graph trial: the salon as a graph | No | None |
| 6 | Two more example flows | New code only | Live eval runs |
| 7 | Flow files and publish checks | New code only | None |

Phases 0 and 1 are small and safe and can start now. Phase 3 only decides
which interpreter is the default. Because interpreters are pluggable classes
(see below), phases 4 to 7 go ahead whichever one wins.

---

## The class design

This is the target shape. Each phase below says which of these classes it
adds. The rules behind it:

- **Small base classes, clear jobs.** One class, one responsibility. A base
  class holds the rule every subclass must follow; a subclass only fills in
  what differs.
- **At most two levels of inheritance**: `core` base, then a kind (for example
  `ConfirmStep`), then the business (for example the salon's step). Anything
  deeper is done by composition: a step *has* an interpreter and operations,
  it is not *a kind of* interpreter.
- **Guards cannot be skipped by a subclass.** The public method that runs the
  checks is a template method marked `@typing.final` (checked by `ty`), and
  subclasses override only the protected hook inside it (for example
  `Operation.run` is final and calls `self._execute`).
- **Config is frozen and shared, call state is per call.** Steps, operations,
  interpreters and flows are immutable and built once per flow version.
  Everything that changes during a call lives on the `CallContext` subclass.
- **Match the repo's style**: PEP 695 generics (as in
  `class Classifier[InputT: BaseModel, OutputT: BaseModel]`), Pydantic models
  for anything validated, frozen dataclasses for plain config, `Protocol` only
  where a structural type is enough (as `Question` is today).
- **Tool docstrings are prompt text**, so model-facing tool functions stay as
  thin functions with their current docstrings; they call the class that does
  the work.

### Where each class lives

| Module | Classes | Job |
|---|---|---|
| `core/proposals.py` | `Proposal`, `ProposalLedger` | What we asked the caller and are waiting on; who it was asked to; whether it was heard in full |
| `core/decisions.py` | `Decision` and its variants, `DecisionResult` | What the caller's reply means, as typed data |
| `core/interpreters.py` | `Interpreter` (base), `ClassifierInterpreter`, `RuleInterpreter` | Turning the caller's words into a `Decision` |
| `core/operations.py` | `Operation` (base), `Receipt` | One business action, with approval and receipt checks built in |
| `core/completion.py` | `Completion` (base), `FieldsPresent`, `ProposalApproved`, `ReceiptMatched` | "This step's job is done", backed by evidence |
| `core/steps.py` | `Outcome` types, `Step` (base), `CollectStep`, `ConfirmStep`, `ActionStep`, `ConversationStep`, `AgentStep` | One step of a flow |
| `core/flow.py` | `Flow`, `FlowRunner`, `Registry` | The steps as a pydantic graph, and one caller turn through it |
| `core/flowspec.py` | `FlowSpec`, `StepSpec` and one subclass per kind | Flow files (phase 7) |

`core/classifiers.py` (`Classifier`), `core/turn.py` and
`core/processor.py` stay. `core/graph.py` shrinks and is removed at the end of
phase 5 if the trial passes. The layout table in `CLAUDE.md` is updated in the
same commit as each new module.

### The classes in a bit more detail

**`Proposal`** (Pydantic model, frozen, strict). Fields every question has:
`owner`, `turn`, `spoken`, `value`, `delivered`. Methods: `is_current(call)`
(right turn, right owner) and `can_be_accepted(call)` (current *and*
delivered). The salon subclasses it per kind instead of one model with every
field: `PhoneProposal`, `BookingProposal`, `MoveOrAddProposal`,
`ComplaintProposal`, `ClosingProposal`. Replaces today's `Asked`.

**`ProposalLedger`** (owned by `CallContext`). `ask(proposal)`,
`pending(kind) -> P | None` (only acceptable ones), `last() -> Proposal | None`
(includes interrupted, for the interpreter to read), `consume()`. Moves the
logic now in `SalonCall.ask`, `pending_question`, `pending` and `answered`
into `core`.

**`Decision`** (Pydantic models, one per variant, joined by a `kind`
discriminator). Generic variants in `core`: `Accept`, `Reject`, `Revise`,
`Clarify(question)`, `SwitchTask(target, current, details)`, `EndCall`,
`NoDecision`. A business adds its own by subclassing: the salon adds
`CorrectPhone(digits)` and `ChooseAppointment(choice)`. `DecisionResult[D]`
holds the decision and an optional short reply.

**`Interpreter[InputT, DecisionT]`** (abstract). The public
`interpret(call, usage, limits)` is final and does the parts every interpreter
must do: at most once per turn, cache the answer, drop it if the turn moved on
or was barged, count the request, and turn a failure into `NoDecision`. That
is the logic in `classify_reply` today. Subclasses fill in two hooks:
`_input(call) -> InputT` and `_decide(input) -> DecisionT`.
- `ClassifierInterpreter` uses a `Classifier` (composition) for one
  structured model request.
- `RuleInterpreter` parses without a model: digits, yes/no words, DTMF. It is
  the floor in phase 3 and the choice for button or keypad input.
- The salon's interpreters (`SalonConsentInterpreter` for today's four-label
  classifier, `SalonSolver` for phase 2) are subclasses that only build the
  input and name the decision type.

**`Operation[ArgsT, ReceiptT]`** (abstract). One business action. Class
attributes: `name`, `args_type`, `receipt_type`, `needs_approval`. Public
`run(call, args)` is final and does, in order: claim the one action per reply,
check the approved proposal matches these exact args (when
`needs_approval`), call `_execute`, validate the result into `receipt_type`,
and return the receipt. Subclasses fill in `_execute(call, args)` and
`proposal(args) -> Proposal` (the words to read back). Salon subclasses:
`LookupCustomer`, `WriteBooking`, `WriteComplaint`. Today's executor
functions (`lookup_customer`, `write_booking`, `write_complaint`) become
one-line calls to these, so the model-facing tools do not change.

**`Completion`** (abstract). `name`, `is_met(call) -> bool`, and
`depends_on: frozenset[str]` (the facts whose change clears it). Concrete:
`FieldsPresent(*fields)`, `ProposalApproved(kind)`,
`ReceiptMatched(operation)`. When an earlier fact changes, the ledger clears
every completion and approval that depends on it.

**`Outcome`** (frozen dataclasses): `Completed`, `Stay`, `MoveTo(to, reply)`,
`Ended`. Every step returns one. The graph routes on the outcome **type**
with `GraphBuilder.match(...)`, so a new step kind never needs a new routing
rule.

**`Step[C]`** (abstract, frozen). Fields: `id`, `completes`, `operations`,
`interpreter` (optional), `openings`, `then` (next step id). Public
`run(ctx) -> Outcome` is final: interpret (if the step has an interpreter),
act, check completions, else speak. Subclasses fill in the hooks:
- `CollectStep`: asks for typed fields, done when they validate.
- `ConfirmStep`: reads back an operation's proposal, done on `Accept` of
  that exact proposal; `Reject` / `Revise` go to their configured steps.
- `ActionStep`: runs one `Operation`, done on a matching receipt; an unknown
  result stays pending, never "failed".
- `ConversationStep`: answers from knowledge, stays active every turn.
- `AgentStep`: wraps one of today's Pydantic AI agents and the existing turn
  engine. This is how the salon's four agents move onto the graph without
  being rewritten.

**`Flow[C]`** (frozen). `id`, `version`, `steps`, `start`.
`build() -> pydantic_graph.Graph` registers each step's bound `run` with
`GraphBuilder.step(call=step.run, node_id=step.id)` and adds a decision per
step that matches on `Outcome` types. `render()` returns the Mermaid diagram
(`Graph.render()`). `validate()` runs `build(validate_graph_structure=True)`
plus our own checks (phase 7).

**`FlowRunner`**. Runs one caller turn: `Graph.iter()`, then
`override_next([GraphTaskRequest(current_step, ...)])` to start at the step
the caller is in, then `next()` step by step. Between steps it checks barge-in,
staleness and the turn budget, the same checks `Turn._hops` does today. It
stops on `Stay` or `Ended`. Pydantic graph 2.46.0 has no save-and-resume, so
the current step lives on the call state and each turn is a fresh short run.

**`Registry`**. Named `Operation`s, `Interpreter`s, knowledge sources and
checks. A flow (code or file) can only refer to things by name through it.

---

## Phase 0: honest measurement

**Why.** Today one trace flag lies. `core/turn.py:334-335` sets
`call.requests[-1].fallback = action is None` after the next step has no
opening. So a classifier that correctly said `accept` is logged as a
fallback. We cannot compare designs while the numbers mix up "did not
understand", "understood but was refused", and "understood, then the next
step had to talk".

**Changes.**

1. `core/state.py`, `Request`: keep `fallback` meaning only "the classifier
   request failed or returned nothing usable" (set in
   `salon/classification.py`). Add
   `outcome: Literal['', 'action', 'refused', 'none'] = ''` and `labels`
   (the validated answer as JSON).
2. `core/turn.py` `_hops`: stop writing `fallback`. Write `outcome` instead,
   only on the hop that made the request: `action` when the chosen action ran
   and returned, `refused` when it raised `ModelRetry`, `none` when the
   interpretation chose nothing and the step carried on.
3. `core/processor.py:321`: feed `classifier_fallback` from the real fallback
   only. Add `classifier_outcome`.
4. Put the interpretation labels (not the caller's words) on the span, so a
   trace shows three separate things: what the model understood, what the app
   allowed, and what the backend said.

**Tests.** Extend `tests/test_classifiers.py::decisions_and_fallback` with the
case from the complaint trace: `accept` on a phone readback,
`confirm_customer` runs, the next step has no opening. Assert `fallback` is
false and `outcome` is `action`. Add a refused case and a failed-request case.
Update `tests/test_traces.py` for the new attribute.

**Done when.** `ruff check`, `ty check`, `python -m tests` are clean, and a
local voice call shows correct flags in Langfuse.

---

## Phase 1: test cases and a baseline

**Why.** Eight classifier results and a 15-case eval cannot tell us if a new
design is better. We need the same cases run against every candidate.

**Changes.**

1. New `evals/cases.py`: a `Case` Pydantic model (step, pending proposal,
   delivered or not, prior speech, caller text, expected decision, expected
   state change, expected request count) and a list of about 60, grouped by
   kind:
   - clear yes / no, in English and Spanish;
   - conditional yes ("yes, if it's under twenty");
   - yes plus a question;
   - full and partial phone corrections ("the last three are 1 2 3");
   - switch task mid-flow (booking to complaint, with and without details);
   - interrupted readback, then a correction or a switch;
   - stale proposal (answer two turns late);
   - mixed intents ("yes, and also I want to complain");
   - goodbye vs "no thanks" vs "no, but...".
   Labels are written and reviewed by a person, seeded from real Langfuse
   traces where we can.
2. `evals/consent.py`: read cases from `evals/cases.py`. Report per case kind:
   semantic accuracy, false accept, request count, request time. Stays opt-in
   and billable only when run on purpose.
3. An offline replay in `tests/` that runs every case through `take_turn`
   with a `FunctionModel` returning the labelled answer. It checks the **app
   side** (guards, state changes, request counts) with no network, and will
   catch a design that skips a mandatory step even when the model is right.
   Written against the `Interpreter` interface from the start, so phases 2
   and 3 reuse it unchanged.

**Done when.** One live baseline run of the current classifier is saved under
`evals/results/` and the offline replay passes on the current code.

---

## Phase 2: the state solver experiment

**Why.** One structured request that both understands the reply and proposes
the next move, so a correction or a task switch costs one request instead of
two or three:

| Turn | Now | Goal |
|---|---|---|
| Clear yes | 1 | 1 |
| Phone correction after readback | 2 | 1 |
| Booking to complaint after a proposal | up to 3 | 1 |
| Switch during an interrupted readback | 2 | 1 |
| Ordinary question | 1 | 1 (no solver in front) |

It runs only where there is a pending question (delivered or interrupted),
never in front of every agent reply. It sits behind a switch in
`salon/classification.py`, so today's path stays the default.

This phase is where the first reusable classes appear, built small and only
as far as the experiment needs: `Decision` and its variants, `Interpreter`,
`ClassifierInterpreter`, and `SalonSolver`. Today's classifier is wrapped as
`SalonConsentInterpreter` so both run through the same interface.

### 2a. Input and decisions

- `SolverInput` (frozen, `extra='forbid'`): caller text, last thing we said,
  active step, owner phone, the last proposal with `delivered`, the allowed
  destinations (name, one line of purpose, what they need, built from
  `AGENTS` and the step's exits so it cannot drift), and the relevant facts
  (for example the number read back, so "last three are 123" can be applied).
- Decisions: `Accept`, `Reject`, `CorrectPhone(digits)`,
  `ChooseAppointment(choice)`, `SwitchTask(target, current, details)`,
  `Clarify(question)`, `EndCall`, `NoDecision`, joined by
  `Field(discriminator='kind')`.
- `SolverResult = DecisionResult[SalonDecision]`: the decision plus an
  optional short reply, only spoken for `SwitchTask` and `Clarify`. Readbacks
  and confirmations stay worded by code.
- Check early that Gemini's native schema accepts the union. If it does not,
  use one flat model with a `kind` enum and optional fields, checked by a
  `model_validator`, and keep the same `Decision` classes on our side.

### 2b. Dispatch

A `SalonSolver.dispatch(call, result) -> LocalAction | None` maps each
decision to an **existing** executor. No new write paths.

- `Accept` → the same branches `advance()` has today.
- `CorrectPhone` → validate with the phone shape, then
  `readback_number(call, digits)`. It replaces the pending phone question and
  clears identity-based approval. Unclear digits → `Clarify`.
- `ChooseAppointment` → the existing `move_or_add` branch.
- `SwitchTask` → a new `switch_task` executor in `salon/tools/desk.py`: check
  the target is a legal exit now, drop the pending proposal, keep complaint
  details, commit the move with `route()` so prerequisites and checkpoints
  still apply. It never cancels an existing appointment, and refuses while a
  write with an unknown result is in flight. Added to `ACTIONS`.
- `EndCall` → `end_conversation`.
- `Clarify` → speak the reply, keep the question pending.
- `NoDecision` or a failed request → the agent runs, as today.

The base `Interpreter.interpret` already turns `Accept` on an interrupted or
stale proposal into `NoDecision`, drops results from a turn that moved on,
and caches per turn.

### 2c. A move that ends the turn (`core/turn.py`, `core/state.py`)

Today a `Handoff` always runs the next step's agent in the same turn. Add
`ends_turn: bool = False` to `Handoff`. `_hops` routes and commits exactly as
for a normal handoff (same `route()`, checkpoint, barge and staleness
checks), then speaks the reply and returns instead of looping. If the new
step has an opening for this entry, the opening wins: it is code-worded and
free. In phase 5 this becomes the `MoveTo(to, reply)` outcome.

### 2d. Interrupted proposals

`pending_question()` hides interrupted readbacks, which is right for consent.
The solver still needs to see them to handle "no wait, it's 123". Add
`SalonCall.last_proposal()` (becomes `ProposalLedger.last()` in phase 4).
Authorization still uses `pending_question()` only.

### 2e. Tests (offline)

- All phase 1 cases through both interpreters with a `FunctionModel`.
- Must-never tests: accept on an interrupted or stale proposal does not
  write; `SwitchTask` to a step whose prerequisite is missing goes through
  `verify`; `CorrectPhone` never sets `customer_verified`; leaving booking
  never cancels a booking; a barge during the solver request changes nothing.
- A request count test per row of the table above.
- `tests/test_core.py` still passes.

**Done when.** The offline suite is clean with the switch on and off, and one
live solver run is saved next to the baseline.

---

## Phase 3: compare and pick the default interpreter

Run the same cases on `SalonConsentInterpreter`, `SalonSolver`, and a
`RuleInterpreter` floor. Report each on its own line:

- false accept of a proposal that was not approved (must be zero on the
  reviewed set);
- mandatory steps skipped (must be zero);
- task success over full scripted conversations;
- unneeded "could you say that again";
- requests and tokens per turn;
- time from end of caller speech to first audio, from local voice calls.

The solver becomes the default if it has no more false accepts than the
baseline, no skipped steps, and clearly fewer requests on correction and
switch turns without slowing plain "yes" turns by more than about 150 ms at
the median. Otherwise the current classifier stays the default. Either way
the other one stays available as a class. Write the real numbers into this
file.

### Status (2026-09-23)

Built: `evals/compare.py` runs the rule floor offline and, with `--live`, the
classifier and the solver on the same 53 cases, and prints one line per
interpreter plus accuracy per kind of reply. `--out` saves the full reports.
**Not yet run live**: this container has no Gemini key, and the run is
billable, so it waits for someone to run
`uv run --env-file .env python -m evals.compare --live --out evals/results/<date>.json`.
Until then `SALON_INTERPRETER` defaults to the classifier.

What is known offline, with every reading assumed correct (projected from
the control flow, not measured):

| | Projected requests over the 53 cases |
|---|---|
| Classifier | 88 |
| Solver | 75 |
| Rule floor (no model; 0.45 accuracy, 0 false accepts) | 44 |

One cost the plan did not predict: when the caller talks over a proposal and
says nothing the solver can act on ("yes" to a readback they did not hear in
full), the solver path costs one request more than today, because today's
classifier is skipped for an interrupted proposal. Three of the 53 cases.
`tests/test_solver.py` pins this, so a change that makes it worse fails.

---

## Phase 4: reusable base classes in `core`

No change in how the salon behaves. This is a move into classes, not a
rewrite.

**Add:** `core/proposals.py`, `core/operations.py`, `core/completion.py`,
and finish `core/decisions.py` and `core/interpreters.py` from phase 2.

**Port the salon:**

- `Asked` → `Proposal` subclasses. `SalonCall`'s question logic moves into
  `ProposalLedger` on `CallContext`.
- `lookup_customer`, `write_booking`, `write_complaint` → `Operation`
  subclasses. The functions stay as one-line wrappers so tools and their
  docstrings do not change.
- `classify_reply` and `advance()` → the interpreter's final `interpret` plus
  a `dispatch` method.
- The hard-coded names in `salon.agents.build` (`'complaints'`,
  `'record_complaint'`, `structured_speech = name in (...)`) become fields on
  a `StepConfig`, so `build` has no salon words left and can move to `core`.

**Tests.**

- Every existing test passes unchanged.
- New tests for each base class using a tiny fake business defined inside the
  test, never the salon, so `core` is proven reusable on its own.
- A test that fails if a subclass overrides a `@final` method (belt and
  braces for `ty`).
- `tests/test_flow.py` keeps scanning for anything that commits state and is
  not an action, now including `Operation` subclasses.

### Status (2026-09-23)

Done, with every existing test unchanged and passing:

- `core/proposals.py`: `ProposalLedger` now owns the one pending question
  for every call (`ask`, `pending_question`, `pending`, `last_proposal`,
  `answered` live on `CallContext`); `SalonCall` only adds its identity and
  the complaint side effect.
- `core/operations.py`: `Operation` with a `@final run`. The salon's three
  writes are `LookupCustomer`, `WriteBooking`, `WriteComplaint`; the old
  functions are one-line wrappers, so tools and their docstrings are the same.
  `tests/test_flow.py`'s write scan now follows `INSTANCE.run(...)` into the
  class's methods.
- `salon.agents.build` names no step or tool any more: `structured_speech`,
  `offered_when` and `stand_ins` are arguments set in `AGENTS`.
- `tests/test_classes.py` proves the base classes on a made-up library desk
  with no salon import, and that no subclass anywhere overrides a `@final`
  method.

Changed from the plan, and why:

- `Proposal` is a structural `Protocol`, and `Asked` stays one model instead
  of five subclasses. Splitting it would reorder the fields of the JSON the
  classifier is sent, which is a prompt change that needs a live check.
  `BaseProposal` is the ready-made model for a new business.
- `Completion` moves to phase 5, where the first step that uses it is built.
- `build` stays in `salon/agents.py` until phase 5 gives it a generic home
  (`AgentStep`).

---

## Phase 5: pydantic graph trial

**Why.** Pydantic graph can own "which step, is it done, where next"
instead of our own `Node`, `Exit`, `route`, `moved_by` and the step-to-step
part of `Turn._hops`.

What we checked in the installed code (`pydantic_graph` 2.46.0):

- `GraphBuilder` builds a graph at runtime from steps and decisions; a step
  can be any async callable, including a bound method such as `step.run`.
- `Graph.iter()` + `GraphRun.next()` run one step at a time, and
  `override_next()` can start at any node or end early.
- `build(validate_graph_structure=True)` checks the graph's structure.
- `Graph.render()` gives a Mermaid diagram.
- Runs create tracing spans on their own.
- There is **no** save-and-resume, so each caller turn is one short run
  starting at the current step, and the current step lives on the call state.

**Add:** `core/steps.py` (`Outcome` types, `Step` and its kinds, `AgentStep`)
and `core/flow.py` (`Flow`, `FlowRunner`, `Registry`).

**Port the salon:** its four agents become four `AgentStep`s in one `Flow`.
Prerequisites (`requires` / `satisfied_by`) and the one-deep checkpoint become
graph decisions plus the checkpoint on the call. `FlowRunner` replaces the
step loop in `_hops`; the per-step voice work in `_speak` stays in
`core/turn.py`.

**Pass marks (all three):**

1. All test modules pass without changing what they assert.
2. Barge-in still works: a turn cancelled mid-step leaves state as today,
   and retained input still carries to the next turn.
3. `core/flow.py` + `core/steps.py` + what is left of the hop logic is
   smaller than `core/graph.py` + today's `_hops`.

If all three pass, delete `core/graph.py` and the old hop loop. If not, keep
the phase 4 classes, drop the graph, and write down why.

---

## Phase 6: prove it on two very different flows

A design proven on one business is not proven. Add two small example packages
beside `salon/`, each only subclassing and filling in `core` classes, each
with an in-memory fake backend and offline tests:

1. **`examples/access/`**: the ten-step access request (identify, team,
   system, level, reason, start, end, eligibility check, confirm, submit),
   built from `CollectStep`, `ConfirmStep` and `ActionStep`. Checks: two
   fields in one sentence fill two steps; approval before the proposal was
   read out is ignored; changing the system clears the level and eligibility
   (`Completion.depends_on`); submit needs a matching receipt that says
   "submitted", not "approved"; an unknown backend result stays pending.
2. **`examples/help/`**: one `ConversationStep` with a knowledge folder and no
   interpreter. Checks: each answer ends the turn; the step stays active; zero
   classifier requests on ordinary questions; goodbye still ends the call.

`bot.py` stays the only runnable entry point; pick the flow with an env var
(default `salon`).

**Done when.** All three flows run on the same `core` with no `core` change
made just for one of them. Anything special-cased is listed here as debt.

---

## Phase 7: flow files and publish checks

**Format.** JSON first, since Pydantic reads it with no new dependency. YAML
is nicer to write and `pyyaml` is already in `uv.lock` as an indirect
dependency, but making it direct needs a yes from the team first.

**Add `core/flowspec.py`:**

- `FlowSpec(version: Literal[1], id: str, steps: list[StepSpec])`, strict (no
  unknown fields).
- `StepSpec` is a discriminated union on `kind`: `CollectSpec`,
  `ConfirmSpec`, `ActionSpec`, `ConversationSpec`. Each has one method,
  `to_step(registry) -> Step`, so compiling is polymorphic, not a long
  if-chain, and a new step kind is one new pair of classes.
- `FlowSpec.compile(registry) -> Flow`. Use `Agent.from_spec` only for the
  agent part (model, instructions), never for flow logic.

**Publish checks** (`Flow.validate()`, raise before a flow can be used):

- unknown step kind, unknown field, duplicate step id;
- a destination that does not exist;
- an operation, interpreter, knowledge source or check that is not in the
  `Registry`;
- a field reference that no earlier step collects;
- an `ActionSpec` whose operation `needs_approval` with no `ConfirmSpec` for
  it before it;
- any path from start to an action that skips a required step;
- plus pydantic graph's own structure check.

**Runtime.** Compile once per published version and share it. Each call pins
the version it started on.

**Tests.** Write the salon, access and help flows as spec files; assert each
behaves the same as the code-built flow on the phase 1 and phase 6 cases. One
test per publish check, with a broken spec.

---

## Out of scope for this plan

- Pipecat Flows as the engine.
- Durable storage, multi-worker locks, and resuming a call after a restart.
  Needed for production, but a separate project. Backend writes should be
  idempotent when we get there.
- A visual flow editor. `Flow.render()` gives a diagram to start from.
- Fixing speech-to-text mistakes. A better interpreter does not fix bad input.
- Replacing the remaining language heuristics (`times_named`, complaint word
  list). Tracked as known debt.

## Risks and how we handle them

| Risk | What we do |
|---|---|
| The solver says "accept" when the caller did not agree | Zero false accepts on the reviewed set is a hard gate; `Operation.run` still checks identity, freshness and the exact proposal |
| A bigger request is slower on plain "yes" turns | Measure per case kind; interpreters are pluggable per step |
| Gemini rejects the union schema | Flat model plus a validator on the wire; same `Decision` classes in code |
| A generated reply says something that did not happen | Replies only on `SwitchTask` and `Clarify`; success lines come from receipts; the guarded clause check still runs |
| Too many classes, too little gain | Two levels of inheritance at most; phase 5 pass mark 3 (less code) and phase 6 gate (no special cases) |
| A subclass skips a guard | Guards live in `@final` template methods; a test checks nothing overrides them |
| Pydantic graph cannot handle barge-in cleanly | Phase 5 pass mark 2; fall back to the phase 4 classes on our own loop |
| Prompt drift | Prompts and tool docstrings are moved, not edited; any wording change gets a live check |

## Check before every push

```bash
uv run ruff format . && uv run ruff check . && uv run ty check . && uv run python -m tests
```

Paste the real output in the PR.
