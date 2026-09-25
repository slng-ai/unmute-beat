# CLAUDE.md

A Pydantic AI agency standing where Pipecat's LLM service would. Pipecat owns
transport, VAD, turn taking, STT and TTS; the agents own the conversation.

```
transport.input() -> SlngSTT -> user_aggregator -> PydanticAgentProcessor -> SlngTTS -> transport.output()
```

`README.md` is the long version: how to run it and every design choice with
its supporting link. Read it before changing how a turn works.

## Layout

A uv workspace. The root `pyproject.toml` lists the members and holds the ruff
config; there is one `uv.lock` and one `.venv` at the root, and one `.env`.
Every member has its own `pyproject.toml`. Paths in each table are relative to
that member's folder.

**`core/`**, the package `slng-voice-core`. The code is in `core/src/core/`.

| Path | Owns |
|---|---|
| `__init__.py` | the public boundary: `load`, `FlowSpec`, `StepFlow` (`compile`, `open`), `FlowCall`, `text_turn`, `TextReply`, `greet`, `Operation`, `ConfirmedOperation`, `BaseProposal`, `TurnBudget`. Imports no Pipecat, Langfuse, `.env` or provider |
| `state.py` | what every agency shares: `STRICT`, `VoiceState`, `CallContext` and its speech ledger (`Spoken.delivered`: delivered, interrupted or unknown; `acknowledge` for text), `Evidence` (every validated receipt, append-only), `Handoff`, `Question`, `Held` (what the caller could still take back, `hold`/`held`/`release`) |
| `graph.py` | `Node`, `Exit`, `LocalAction`, `Opening`, `route`, `moved_by`, `lent`, `model_clients` |
| `flow.py` | `Flow`: the steps table as a `pydantic_graph` graph with each step's legal moves; it walks every turn and refuses an undeclared move; `render()` draws it |
| `policies.py` | `TurnBudget`, the hooks (`one_action_per_reply`, `speak_by`, `request_clock`), `pick`, `schema_tool` |
| `history.py` | `paired` and `project`: what each provider request is actually sent |
| `turn.py` | one caller turn as an event stream: `Turn`, `run_turn`, `take_turn`, `Barge`, the fixed lines. Imports no Pipecat |
| `locale.py` | `Locale`: the lines the turn engine says itself, in one language, holding lines included; `ENGLISH`, `SPANISH`. A call picks one by class (`CallContext.default_locale`) or per call (override the `locale` property) |
| `clock.py` | `Today`: the call's date, weekday, time and the labelled days ahead, read once per call from `CallContext.now` in its `timezone` (`calendar_days`), and sent to every step's model |
| `processor.py` | the Pipecat adapter: events in, `LLMTextFrame`s out, the turn span. The only `core` module that imports Pipecat (the `voice` extra) |
| `classifiers.py` | `Classifier`: one direct model request over an `Exchange` (question, answer, facts), the author's context and classes; `ClassSet` and `output_model` build classes from data |
| `decisions.py` | typed meanings of a reply (`Accept`, `SwitchTask`, ...); a business subclasses for its own |
| `proposals.py` | `Proposal` (structural), `BaseProposal`, `ProposalLedger`: the one question a call waits on, and when it can be answered |
| `operations.py` | `Operation`: one business action, `@final run` = validate, settle, authorise, execute, validate receipt, record; `needs_approval` says whether a read-back yes is required; `operation_token` |
| `completion.py` | `Completion`, `FieldsPresent`, `ReceiptMatched`: a step is done only on evidence in the facts |
| `steps.py` | `CollectStep`, `ConfirmStep`, `ActionStep`, `ConversationStep`, `AgentStep` (a hand-built step) and `AgentBuilder` (builds its agent the same way for every use case), `End`, compiled by `StepFlow` into a nodes table; `FlowState[S]` and `FlowCall[S]`, the session around an application's own state model (`update`, `collect`, `services`), `receipt_for`, `ConfirmedOperation` |
| `flowspec.py` | flow folders: `FlowSpec` (YAML or JSON via `yaml.safe_load`, one spec class per step kind), `state:` naming the application's model and `identity:` its caller field, each step naming its prompt file and `code:` module inside the folder; `FlowSpecError` listing every problem |
| `readings.py` | narrow readings: `Reading` (one classifier, when it applies, its label), `classify`/`choose`, `ChoiceReading` (`picked`: only an id among its own options counts), the generic class sets (`CONFIRM`, `TAKE_BACK`, `STILL_AGREED`, `CLOSING`, `REQUEST`), `read` (run the ones that apply, side by side), `Acts` (answer, take back, pick, goodbye: the fixed order) |
| `conversation.py` | a conversation's acts as classes a use case fills in: `ReadBack` (with its `done` line), `FixedChoice`, `Offer`, `HeardValue`, `TakeBack`, `HeldApproval`, `Detour`, `Closing`; `Conversation` builds their readings, runs them, and decides in core's order |
| `desk.py` | `Desk`: what every step can do, for any use case (`hand_over_for`, `hang_up`, `end_conversation`, `clarify`, `switch_task`), built from a call type, a `Handoff`, the action names and an optional `Carry` |
| `interpreters.py` | `Interpreter`: at most once per turn, cached on the call, budgeted, never published stale; `ClassifierInterpreter` |
| `observability.py` | Langfuse over OTel (the `tracing` extra); call `setup()` once, before any agent is built. Reads no `.env`: the launcher does |
| `tests/` | `test_classes`, `test_history`, `test_reuse`, `test_clock`, `clinic.py` (a clinic built on core's classes), and the public boundary: `clinic_flow/` (a clinic written on `import core` alone), `test_public`, `test_session` (atomic state, owned fields, isolation, stale work, evidence), `test_delivery`, `test_sdk`, `test_package`. No use case code. `defects.py` is outside the suite: known defects that fail on purpose, each with its repair session (`python -m tests.defects`, exit code = defects left) |

**`use-cases/salon-graph/`**, the salon on core.

| Path | Owns |
|---|---|
| `bot.py` | the Pipecat pipeline around the salon: STT, VAD and smart-turn, TTS, transport, tracing worker, greeting |
| `salon/models/` | `shapes` (Phone, Date, Time, Id), `consent` (`Asked`, `ConsentInput`, `ReplyClassification`), `receipts` (what the diary may say), `state` (`SalonState`, `SalonCall`, `open_call`) |
| `salon/tools/` | one module per step (`identity`, `booking`, `complaints`, `desk`), the write boundary (`writes`), `Ask`, and `policy` (`ACTIONS`, `SPEAK_BY`). Each write is an `Operation`: `LOOKUP`, `WRITE_BOOKING`, `WRITE_COMPLAINT` |
| `salon/diary.py` | `Diary`: customers, slots, offers, bookings, complaints, in memory, and the oracle that says no |
| `salon/flow.yaml` | the salon as four `agent` steps: each one's prompt, module, exits and requirements |
| `salon/steps/` | one module per step, each giving `node(step)`: its tools, outputs and openings |
| `salon/nodes.py` | `BUILDER` (core's `AgentBuilder`, given the salon's information) and `build()`, `advance()`, `decided()`, `solve()`, the guards |
| `salon/instructions.py` | loads `flow.yaml` as `SPEC`; `instructions(name)` is a step's compiled prompt; `GREETING`; `COMPLAINT_GUIDANCE` |
| `salon/classification.py` | `SALON`, the salon's conversation: core's acts given the salon's information, which builds its narrow readings (`CONFIRMATION`, `APPOINTMENT_CHOICE`, `CHOSEN_OFFER`, `LAST_CHANGE`, `RETAINED_APPROVAL`, `CONVERSATION`, `DETOUR`) and decides on them; `reading_of`, the merged view the guards read; `SOLVER` (`SALON_INTERPRETER=solver`); `classify_reply`. Tools reach it through the call (`SalonCall.read_reply`), never by import |
| `salon/agents.py` | the `AGENTS` table, compiled from `flow.yaml`; re-exports what tests import |
| `salon/prompts/` | one Handlebars prompt per step, the shared voice and diary rules, and `knowledge/` with the salon's own documents |
| `evals/` | the classifier evaluations (`cases`, `compare`, `consent`, `focused`, `readings`, `report`, `rules`, `solver`), and `calls`: the live text checkpoint, four scripted calls (T1-T4) on the real engine, checked on the diary. Live, billable, except the rule floor and `calls --recheck`. `line` and `matrix`: the harness arm of the N1-N13 native comparison, offline, on the real pipeline |
| `tests/` | fifteen assert-script modules, `classifier_model.py`, and `golden/` (every request the salon sends, pinned) |

**`use-cases/salon-prompt/`**, the same salon as one prompt on Pipecat's own Gemini service: the baseline. No core, no Pydantic AI.

| Path | Owns |
|---|---|
| `bot.py` | the same pipeline, with `GoogleLLMService` where salon-graph has `PydanticAgentProcessor` |
| `salon/baseline.py` | the one prompt, the tools on the same `Diary`, the greeting |
| `salon/diary.py`, `salon/models/shapes.py`, `salon/prompts/` | copies of salon-graph's. Change both, or the builds stop being comparable |
| `utils/observability.py` | core's copy without the Pydantic AI hook |

**`use-cases/salon-pipecat-flows/`**, the same salon on declarative Pipecat flows: the harness's guarantees as plain application code. No core, no Pydantic AI.

| Path | Owns |
|---|---|
| `bot.py` | the same pipeline, with `GoogleLLMService`, `ClaimFilter` and `EmptyReplyGuard` where salon-graph has `PydanticAgentProcessor`; `gemini-3.1-flash-lite`; smart-turn's silence fallback at 1.5 s |
| `flow.yaml`, `prompts/*.yaml` | a Pipecat `FlowConfig`: the nodes, a branch table on `next` for every tool, and the `say` action (never `tts_say`, whose wait a barge-in freezes) |
| `graph_salon/` | `handlers` (direct functions, `@tool(ArgsModel)`), `flow` (loads and checks `flow.yaml`, `typed` gives each tool its Pydantic schema), `models`, `call` (consent guards, turn, budget), `writes` (`Write.run`), `speech` (fixed lines, `ClaimFilter`, `EmptyReplyGuard`) |
| `salon/diary.py`, `salon/models/`, `salon/prompts/` | copies of salon-graph's; `receipts.py` writes out `STRICT`, so nothing imports `core`. Change both, or the builds stop being comparable |
| `utils/observability.py` | salon-prompt's copy |
| `tests/` | `test_graph_salon`: every guarantee offline, a real `FlowManager` entering every node, and salon-graph's fixed lines read from its source as text |
| `evals/` | `line` and `matrix`: the native arm of the N1-N13 comparison, this build on its real pipeline with a scripted Gemini at the network boundary. Drives the reference; changes none of it |

**`use-cases/faro-language-learning/`**, Faro on core.

| Path | Owns |
|---|---|
| `bot.py` | the same pipeline as salon-graph's, with Faro's greeting (a function of the call), `close_call` on disconnect, and its own speech languages |
| `faro/` | a flow folder (`flow.yaml`, `voice.md`, `prompts/`, `steps/`) and a package, so faro-tools can import it. `curriculum` (the versioned placement bank, lessons, level rule, fixed lines), `models`, `store` (SQLite: WAL, `synchronous=FULL`, one worker thread; `fixture`, `backup`, `bench`), `state` (`FaroState`, `FaroCall`), `operations` (one `Operation` per write), `conversation` (`FARO`, `BUILDER`, and Faro's own readings: `GRADE` records every exercise attempt, `ONBOARD` saves setup answers, both by code), `steps/companion.py` (the tools, gated by phase; the agent coaches and never grades) |
| `tests/` | `test_faro`: the acceptance cases offline, on a real SQLite file |

**`use-cases/concierge-tools/`** and **`use-cases/faro-tools/`**, tools that import no build.

| Path | Owns |
|---|---|
| `concierge-tools/ledger.py` | reads any build's Langfuse traces (harness, one-prompt baseline, graph build, LiveKit) into one ledger: requests, tokens counted once, calls in a row, latency, and each write with the question it answered (`write` spans from `core/operations.py`) |
| `concierge-tools/coval_sim/` | the Coval release check: starts `bot.py -t twilio` behind a cloudflared tunnel and lets Coval call it. Needs network and keys, never runs in CI. One job per invocation (agent ID, test set, persona, `--max-simulations`), `plan` first; a run log refuses duplicates and uncertain relaunches. A Coval agent `voice-harness-<agency>-pipecat` calls `use-cases/<agency>`; `salon` calls salon-graph, and `graph` the unmute graph build (`GRAPH_BUILD_DIR`). A bot with no `MAX_CALL_SECONDS` of its own (salon-pipecat-flows) is stopped by the runner 188 s after its call connects. `--port` gives a job its own bot and tunnel port, so jobs can run side by side |
| `concierge-tools/matrix/` | the N1-N13 native comparison: `scenarios.v1.json` (the rows, the checks each arm runs, the pinned native digest, the frozen N12 protocol), `run_checks.py` (one check in one member's own environment) and the driver, which writes `matrix.json`, `matrix.md` and `timing.json` |
| `concierge-tools/audio_timing.py` | who spoke when in a two-channel Coval recording: response latency, overlaps, barge-in stop latency |
| `faro-tools/bench.py` | Faro's benchmark cases live: scripted text calls on the real model, each call in its own process, one Langfuse session per call. Billable |

**The split that matters:** `core.turn` runs a turn and knows nothing about
how it reaches a person; `core.processor` is the Pipecat adapter over it.
Anything about *running* a turn goes in `turn`, anything about frames goes in
`processor`. salon-graph's `tests/test_core.py` asserts the two agree and that
no `core` module but the processor imports Pipecat.

`core` imports no use case. A use case imports `core` and nothing from another
use case; salon-prompt and salon-pipecat-flows import neither. In salon-graph: `salon.models` imports
`core.state`; `salon.tools` imports models, the diary and `core.policies`, and
reads a reply through the call; `salon.classification` imports the tools its
acts run; `salon.agents` imports everything; `bot.py` wires it to Pipecat.

State is replaced wholesale via `call.commit(**updates)`: one validated
candidate or nothing, never field-by-field assignment. Tools live with the step
they belong to and bind to `RunContext[SalonCall]`. The model-facing tool has a
`ctx` first parameter; the executor it shares with `advance()` takes `call`.

## Commands

Everything that builds an agent needs a model key at import: the root `.env`,
which each `bot.py` finds by walking up, or the placeholder `python -m tests`
sets. Run a member's commands from its folder, or with `uv run --directory`.

```bash
uv sync --all-packages                                   # the whole workspace, from the root
cd use-cases/salon-graph && uv run bot.py                # voice, http://localhost:7860
uv run python -m tests                                   # this folder's modules, offline, no .env
uv run ruff format . && uv run ruff check .              # from the root: every member
for d in core use-cases/*; do uv run --directory $d ty check .; done
cd use-cases/concierge-tools && uv run python -m coval_sim plan --agency salon --agent <ID> --test-set <ID> --persona <ID> --max-simulations 1 --label <session>   # then `run`, billable
cd use-cases/salon-graph && uv run --env-file ../../.env python -m evals.calls   # live text checkpoint, billable
cd use-cases/concierge-tools && uv run ledger.py harness CA... -- graph trace:...   # cost of calls
cd use-cases/concierge-tools && uv run python -m matrix run      # N1-N13, both arms, offline; `time` for N12
docs/research/reusable-core/session-2/package-proof.sh         # the wheel, installed outside the checkout
```

`uv run python -m tests` turns `ALLOW_MODEL_REQUESTS` off *before* any module
is imported, so a test that forgot to override an agent raises instead of
billing. One module alone needs a key in the environment:
`GOOGLE_API_KEY=x uv run python -m tests.test_salon`, from salon-graph.

ty runs per member: each member's `pyproject.toml` has a `[tool.ty]` table, so
ty takes that folder as its root. Without one, ty walks up to the next
pyproject that has one.

Before calling anything done: `ruff check`, `ty check` and `python -m tests`
all clean, in every member you touched. Paste the real output; don't round up.

## Conventions

- **Every comment is a docstring.** Rationale goes on the function, class,
  module or attribute it is about; a note on a field is a string literal on
  the line after it. The only `#` lines are tool directives (`noqa`,
  `ty: ignore`, `fmt:`). A deliberate shortcut is a `ponytail:` line inside a
  docstring, naming its ceiling and upgrade path.
- **Tool docstrings are prompt text.** The model reads them. Editing one is a
  prompt change, not a style fix; that is why `D205`, `D210`, `D415` and `D417`
  are off. Rationale for a tool goes on its executor or its module.
- **Prompts are moved, not edited, without a live check.** `salon/prompts/*.md` are
  the versions the POC was measured on.
- Single quotes, 110 columns, `ruff format` decides. `E501` is off.
- No pytest. Tests are assert scripts run as modules, and stay that way.
- No new dependency without asking. Stdlib first.

## Seven things that bite

1. **Prompts are compiled against state.** `salon/prompts/*.md` are Handlebars
   templates typed against `SalonCall`, so a `{{state.x}}` naming no field
   raises `TemplateSchemaError` **at import**, never mid-call. Add the field or
   fix the prompt.
2. **A new step is four edits in lockstep**: a prompt in `salon/prompts/`, a
   module in `salon/steps/` (with its tools in `salon/tools/`), an entry in
   `salon/flow.yaml`, and a name in `AgentName`. Miss one and it fails at
   import: the flow file checks its prompt and module exist and fit.
3. **Anything a service returns is validated at the boundary.** Every `Diary`
   response is parsed into a `salon.models.receipts` model before a field is
   read. A return annotation is not validation.
4. **One action per model reply.** `one_action_per_reply` picks the single
   tool call in a response allowed to move the caller or write to the
   business; `claim_or_refuse` is how an action checks it holds that claim.
   `tests/test_flow.py` scans the tools package for anything that commits
   state and is not an action.
5. **Consent is application-owned.** A model interprets replies; application guards authorize
   the exact action, identity and fresh proposal. `CallContext.heard` is what they said;
   `SalonCall._asked` is the one question they are answering, put to them by an
   `Ask` output whose words this code chose. Every consequential action checks
   the two against each other, and on a step that can write, model speech goes
   out a clause at a time so a claim about a change nothing made is never said.
6. **A cancelled turn still heard something.** The engine retains accepted
   input and completed tool results from the SDK's `new_messages()` boundary
   after stream teardown, even when Pipecat cancels the task. Never slice by
   the original history length. The processor carries input that never entered
   a run to the front of the next turn.
7. **A caller turn lasts until an answer has played out to them.** When the
   caller stops is the runtime's endpointing, and it stops on every pause. The
   harness accumulates: a caller who cuts in before any answer played to its
   end is still talking, so the piece continues the same turn
   (`CallContext.continues_turn`): same turn number, `heard` joined, the
   question ledger rewound, speech kept as far as it was heard. A segment is
   heard in full only on evidence (`Spoken.delivered`): its audio ran out
   (`BotStoppedSpeakingFrame`) before the caller cut in, or a text reader
   acknowledged it (`CallContext.acknowledge`). Only a delivered copy of a
   question can be answered; no record, a queued copy or a cut one never can,
   and copies belong to a question by its serial, not its words. Without
   playback signals the engine errs towards a new turn. A runtime text report
   never cuts a segment whose audio already ran out, and a queued segment
   whose words had time to play counts as played for turn-taking only, never
   for approval. A joined turn's span says why (`continued_from`).

## Known debt

No word list authorises a write any more: consent, move/add, closing, the
pick of a time on the table (`chosen_offer`) and taking back a booking
(`last_change`) are all read by the typed classifier and checked by
application guards. `times_named` in `salon/diary.py` still only steers which
times a diary read prefers. The complaint word list in
`salon/tools/complaints.py` and the unbacked-claim patterns in
`salon/models/state.py` remain language-specific heuristics. Structured output
validates shape, not semantic accuracy; run the explicit live evaluation
before making accuracy or latency claims.

The shared confirm reading (`CONFIRM`, Langfuse `harness-reading/confirm`
v3) reads "Yes, that's right. Oh, and can we make it Saturday instead?" to a
phone read-back as `revise` (Coval run hdbogMqB, AJ4t). Three rewordings
(v5, v6, v7, the last with the proposal's parts as input) fixed it and made
a yes followed by a price question read as accept, so none shipped. The path
after an accept is built and tested (`ReadBack.done`, a silent action
followed by the step's model).
