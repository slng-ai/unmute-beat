# Plan: portable typed conversation flows

Written 2026-09-23 against `452698d`. Based on
[`docs/research/portable-typed-flows.md`](../research/portable-typed-flows.md).

This is a plan, not a decision to migrate. Each phase ends with a check. If a
check fails, we stop and rethink before starting the next phase.

## The goal in one paragraph

Any business should be able to describe a phone flow as a short list of typed
steps (collect, confirm, action, conversation). The app, not the model,
decides when a step is done and what comes next. The model only reads what the
caller said and proposes data. The salon stays working the whole way through,
and we prove the idea on two more very different flows before we let anyone
write a flow file.

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
- Prompts in `prompts/*.md` are not edited without a live check.
- One orchestration owner. We do **not** add Pipecat Flows or
  `pydantic-graph` on top of the current engine.

## Order of work

| Phase | What | Changes behaviour? | Model spend |
|---|---|---|---|
| 0 | Honest measurement | No | None |
| 1 | Test cases and a baseline | No | One live eval run |
| 2 | State solver experiment (salon, behind a switch) | Only when switched on | Live eval runs |
| 3 | Compare and decide | No | None |
| 4 | Generic step contract in `core`, salon ported to it | No | None |
| 5 | Two more example flows | New code only | Live eval runs |
| 6 | Flow file loader and publish checks | New code only | None |

Phases 0 and 1 are small and safe and can start now. Phase 2 is the real
question. Phases 4 to 6 only happen if phase 3 says yes.

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
   `salon/classification.py`). Add one field for what happened next, for
   example `outcome: Literal['', 'action', 'refused', 'conversation'] = ''`.
2. `core/turn.py` `_hops`: stop writing `fallback`. Write `outcome` instead:
   `action` when a `LocalAction` ran and returned, `refused` when it raised
   `ModelRetry`, `conversation` when no action applied and an agent ran.
3. `core/processor.py:321`: keep the `classifier_fallback` span attribute
   but feed it from the real fallback only. Add `classifier_outcome`.
4. Add the interpretation result itself to the span (the labels, not the
   caller's words), so a trace shows three separate things: what the model
   understood, what the app allowed, and what the backend said.

**Tests.** Extend `tests/test_classifiers.py::decisions_and_fallback` with the
case from the complaint trace: classifier says `accept` on a phone readback,
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

1. New file `evals/cases.py`: a typed list of cases (a Pydantic model per
   case: step, pending proposal, whether it was interrupted, prior speech,
   caller text, expected decision, expected state change, expected request
   count). Start with about 60, grouped by kind:
   - clear yes / no, in English and Spanish;
   - conditional yes ("yes, if it's under twenty");
   - yes plus a question;
   - full and partial phone corrections ("the last three are 1 2 3");
   - switch task mid-flow (booking to complaint, with and without details);
   - interrupted readback, then a correction or a switch;
   - stale proposal (answer two turns late);
   - mixed intents ("yes, and also I want to complain");
   - goodbye vs "no thanks" vs "no, but...".
   Labels are written and reviewed by a person. Seed them from real
   Langfuse traces where we can, not only made-up lines.
2. `evals/consent.py`: read cases from `evals/cases.py` instead of its own
   inline list. Report, per case kind: semantic accuracy, false accept,
   request count, and request time. Keep it opt-in and billable only when
   run on purpose.
3. An offline replay in `tests/` that runs every case through
   `take_turn` with a `FunctionModel` returning the labelled answer. This
   checks the **app side** (guards, state changes, request counts) with no
   network. It will catch a design that skips a mandatory step even when the
   model is right.

**Done when.** One live baseline run of the current classifier is saved (as a
JSON file under `evals/results/`, git-ignored or committed, our choice) and
the offline replay passes for the current code. That baseline is the number
phase 3 compares against.

---

## Phase 2: the state solver experiment

**Why.** The research's best idea: one structured request that both
understands the reply and proposes the next move, so a correction or a task
switch costs one request instead of two or three. Today:

| Turn | Now | Goal |
|---|---|---|
| Clear yes | 1 | 1 |
| Phone correction after readback | 2 | 1 |
| Booking to complaint after a proposal | up to 3 | 1 |
| Switch during an interrupted readback | 2 | 1 |
| Ordinary question | 1 | 1 (no solver in front) |

It is only used where there is a pending question (delivered or
interrupted). It is **not** put in front of every agent reply.

It sits behind a switch (a module constant or env var in
`salon/classification.py`), so the current path stays the default and both
can run on the same cases.

### 2a. Input and output models (`salon/models/consent.py`)

- `SolverInput` (frozen, `extra='forbid'`): caller text, last thing we said,
  active step, owner phone, the pending proposal with a `delivered: bool`
  (false when interrupted), a small list of allowed destinations (name, one
  line of purpose, what they need), and the relevant facts (for example the
  number read back, so "last three are 123" can be applied).
- `Decision`: a discriminated union on `kind`, one variant per legal move,
  each with only the fields it needs:
  - `accept` (no fields) — only valid when the proposal was delivered;
  - `reject`;
  - `correct_phone` with `digits: str` (a candidate, not a verified number);
  - `choose_appointment` with `choice: Literal['move', 'add']`;
  - `switch_task` with `target`, `current: Literal['abandon', 'pause']`,
    and optional `details`;
  - `clarify` with a short question;
  - `end_call`;
  - `none` (nothing to decide; let the agent talk).
- `SolverResult`: `decision: Decision` and an optional short `reply` that is
  only spoken for moves that do not need a fixed line (`switch_task`,
  `clarify`). Readbacks and confirmations stay worded by code.
- Build the destination list from `AGENTS` and the step's exits, not by hand,
  so it cannot drift from the graph.

Use Pydantic's `Field(discriminator='kind')`. `Classifier` already wants an
object at the top level, which `SolverResult` is. Check early that the Gemini
native schema accepts the union (`oneOf` with a `const` tag); if it does not,
fall back to one flat model with a `kind` enum and optional fields, validated
by a `model_validator`.

### 2b. Dispatch (`salon/agents.py`, new `solve()` next to `advance()`)

Map each decision to an **existing** executor. No new write paths.

- `accept` → the same branches `advance()` has today
  (`lookup_customer`, `write_booking`, `write_complaint`).
- `correct_phone` → validate with the existing phone shape, then
  `readback_number(call, digits)`. This replaces the pending phone question
  and clears any identity-based approval. Unclear digits → `clarify`.
- `choose_appointment` → existing `move_or_add` branch.
- `switch_task` → a new executor `switch_task(call, target, current, details)`
  in `salon/tools/desk.py` that: checks `target` is a legal exit now,
  drops the pending proposal, keeps complaint details if given, commits the
  step change with `route()` so prerequisites and checkpoints still apply,
  and returns the new result type from 2c. It never cancels an existing
  appointment. If a write is in flight with an unknown result, it refuses and
  says so.
- `end_call` → `end_conversation` as today.
- `clarify` → speak the reply, keep the question pending.
- `none` / failed request → fall through to the agent, same as today.

Rules the dispatcher checks, not the model:

- `accept` on an interrupted or stale proposal is turned into `none`.
- The result is dropped if the turn moved on (`call.is_current(generation)`),
  as `classify_reply` does now.
- Cache per turn, like `classify_reply`, so a tool retry cannot ask twice.
- `switch_task` goes in `ACTIONS` so `one_action_per_reply` and
  `tests/test_flow.py` see it.

### 2c. A move that ends the turn (`core/turn.py`, `core/state.py`)

Today a `Handoff` always runs the next step's agent in the same turn. That
extra request is exactly what we want to remove.

- Add a small result type in `core/state.py`, for example
  `Transition(to, because, reply)`, or a flag on `Handoff`
  (`ends_turn: bool = False`). Pick whichever needs fewer special cases in
  `_hops`; lean toward the flag so routing code is shared.
- In `_hops`: route and commit exactly as for a normal handoff (same
  `route()`, checkpoint, `call.commit(agent=..., pending=...)`, barge and
  staleness checks). Then, instead of looping, speak the reply and return.
- If the new step has an opening for this entry, prefer the opening over the
  generated reply: it is code-worded and costs nothing.

This belongs in `core` because any flow will want it. Keep it free of salon
words.

### 2d. Interrupted proposals

`SalonCall.pending_question()` hides interrupted readbacks, which is right for
consent. The solver still needs to see them to handle "no wait, it's 123".
Add `SalonCall.last_proposal()` that returns the question plus a `delivered`
flag. The solver gets that. Authorization still uses `pending_question()`
only, so an interrupted proposal can never be accepted.

### 2e. Tests (offline, no network)

- All phase 1 cases through `solve()` with a `FunctionModel`.
- Must-never tests: accept on interrupted proposal does not write; accept on
  stale proposal does not write; `switch_task` to a step whose prerequisite is
  missing goes through `verify`; `correct_phone` never sets
  `customer_verified`; switching away from booking never cancels a booking;
  a barge during the solver request leaves state untouched.
- Request count tests for each row of the table above.
- `tests/test_core.py` still passes (no Pipecat import outside the
  processor; `turn` and `processor` agree).

**Done when.** Offline suite is clean with the switch on and off, and one
live eval run of the solver is saved next to the baseline.

---

## Phase 3: compare and decide

Run the same cases on: (a) current classifier, (b) state solver, and
(c) plain parsing where the input allows it (digits, yes/no words), as a
floor.

Measure and report each on its own line, never merged:

- false accept of a proposal that was not approved (must be zero on the
  reviewed set);
- mandatory steps skipped (must be zero);
- task success over full scripted conversations;
- unneeded "could you say that again";
- requests per turn and tokens per turn;
- time from end of caller speech to first audio, from local voice calls with
  Langfuse, not just request time.

**Go** if the solver has no more false accepts than the baseline, no skipped
steps, and clearly fewer requests on correction and switch turns without
slowing plain "yes" turns by more than about 150 ms at the median. Otherwise
keep the current classifier, keep phase 0 and phase 1, and write down why.

Write the result into this file (or a short follow-up note) with the real
numbers.

---

## Phase 4: a generic step contract in `core`

Only after a "go". No change in how the salon behaves; this is a move, not a
rewrite.

**Add to `core/graph.py`** (or a new `core/flow.py` if `graph.py` grows too
big):

- `Completion[C]`: a named check over the call that says "this step's job is
  done", backed by evidence (validated fields, an approved current proposal,
  or a matching receipt). A plain callable with a name, like `Exit.guard`.
- `Node.completes: tuple[Completion, ...]` and `Node.then: str` (next step
  once complete). `_hops` checks completion after an action and moves on in
  code. No model decides "what's next" once a step is done.
- `Node.solver`: the optional interpreter from phase 2, generalised: a
  `Classifier` plus a function that builds its input from the call and a
  dispatch table from decision kind to `LocalAction`. Replaces the salon-only
  `advance` field; `advance` stays as an alias until the salon is moved over.
- A generic `Proposal` base (owner, turn, spoken, value, delivered) in
  `core/state.py`. The salon's `Asked` becomes a subclass per kind (phone,
  booking, complaint, closing) instead of one model with every field.
  Pending-question tracking moves from `SalonCall` into `CallContext`.

**Move out of `salon.agents.build`:**

- the hard-coded names (`'complaints'`, `'record_complaint'`,
  `structured_speech = name in (...)`) become fields on the step
  (`structured_speech: bool`, and a per-output "offered when" check);
- `build` itself becomes a generic `core` builder that takes a registry of
  tools and outputs; the salon passes its own.

**Tests.** Every existing test passes unchanged. Add `tests/test_graph.py`
checks: a step with completions and `then` advances in code; a failed action
is never completion; changing an earlier fact clears later completions and
approvals.

**Known limit we keep.** One pending prerequisite (`route` and
`Checkpoint`). A flat ten-step list does not need nesting. Nested flows stay
out of scope.

---

## Phase 5: prove it on two very different flows

A contract proven on one domain is not proven. Add two small example
packages beside `salon/`, each with an in-memory fake backend and its own
offline tests:

1. **`examples/access/`**: the ten-step access request from the research
   (identify, team, system, level, reason, start, end, eligibility check,
   confirm, submit). Checks: two fields in one sentence fill two steps;
   approval before the proposal was read out is ignored; changing the system
   clears the level and the eligibility result; submit needs a matching
   receipt, and the receipt says "submitted", not "approved"; an unknown
   backend result stays pending, not "failed".
2. **`examples/help/`**: one conversation step with a knowledge folder and no
   solver. Checks: each answer ends the turn; the step stays active; zero
   classifier requests on ordinary questions; goodbye still ends the call.

`bot.py` stays the only runnable entry point. Pick the flow with an env var
(default `salon`).

**Done when.** All three flows run on the same `core` with no `core` change
made just for one of them. Anything that had to be special-cased is listed
here as debt.

---

## Phase 6: flow files and publish checks

Only after phase 5.

**Format.** JSON first, since `pydantic` reads it with no new dependency.
YAML is nicer to write, and `pyyaml` is already in `uv.lock` as an indirect
dependency, but making it a direct dependency needs a yes from the team first.

**New `core/flowspec.py`:**

- `FlowSpec(version: Literal[1], id: str, steps: list[Step])` with
  `model_config = STRICT` (no unknown fields).
- `Step` is a discriminated union on `kind`:
  `CollectStep`, `ConfirmStep`, `ActionStep`, `ConversationStep`, each with
  `id`, and only the fields that kind needs (`fields` and `question`;
  `proposal`, `on_reject`, `on_revise`; `handler`, `approval`, `on_error`;
  `knowledge`, `instructions`).
- A `Registry` the developer fills in code: handlers (typed args, readback
  text, executor, receipt model), knowledge sources, and named checks.
  Config can only **name** these. It never runs Python.
- `compile(spec, registry) -> dict[str, Node]`: turns the spec into the same
  `Node` / `Exit` / `Opening` / `Completion` objects phase 4 made. Use
  `Agent.from_spec` only for the agent part (model, instructions), never for
  flow logic.

**Publish checks** (raise before a flow can be used, with a clear message):

- unknown step kind, unknown field, duplicate step id;
- `on_*` or `then` pointing at a step that does not exist;
- a handler, knowledge source or check that is not registered;
- a field reference that does not exist on an earlier step;
- an action with no confirm step before it, when its handler says it needs
  approval;
- a path from start to an action that skips a required step.

**Runtime.** Compile once per published version and share it. Each call pins
the version it started on. State stays per call.

**Tests.** Write the salon, access and help flows as spec files; assert each
compiles to nodes that behave the same as the hand-written ones on the phase 1
and phase 5 cases. One test per publish check, with a broken spec.

---

## Out of scope for this plan

- Adopting Pipecat Flows or `pydantic-graph` as the engine. Revisit only if a
  second real product shows it would remove a lot of our own code.
- Durable storage, multi-worker locks, and resuming a call after a restart.
  Needed for production, but a separate project. Backend writes should still
  be idempotent when we get there.
- A visual flow editor. The spec format should be easy to show as a short
  list, but building the editor comes later.
- Fixing speech-to-text mistakes. A better interpreter does not fix bad input.
- Replacing the remaining language heuristics (`times_named`, complaint word
  list). Tracked as known debt; the solver may make some of them unneeded.

## Risks and how we handle them

| Risk | What we do |
|---|---|
| The solver says "accept" when the caller did not agree | Zero false accepts on the reviewed set is a hard gate; guards still check identity, freshness and the exact proposal |
| A bigger request is slower on plain "yes" turns | Measure per case kind; keep the old path for plain confirms if needed |
| Gemini rejects the union schema | Flat model with a `kind` enum plus a validator (2a) |
| A generated reply says something that did not happen | Replies only on `switch_task` and `clarify`; success lines come from receipts; the guarded clause check in `_hops` still runs |
| The generic layer grows faster than it helps | Phase 5 gate: if the two new flows need `core` hacks, stop and simplify |
| Prompt drift | Prompts are moved, not edited; any wording change gets a live check |

## Check before every push

```bash
uv run ruff format . && uv run ruff check . && uv run ty check . && uv run python -m tests
```

Paste the real output in the PR.
