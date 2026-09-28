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
| `__init__.py` | the public boundary: `load`, `FlowSpec`, `StepFlow` (`compile`, `open`, `open(intents=...)` for a fresh process), `FlowCall`, `text_turn`, `TextReply`, `greet`, `Operation`, `ConfirmedOperation`, `BaseProposal`, `Intent`, `TurnBudget`. Imports no Pipecat, Langfuse, `.env` or provider |
| `state.py` | what every agency shares: `STRICT`, `VoiceState`, `CallContext` and its speech ledger (`Spoken.delivered`: delivered only by the segment's own transport mark or a text `acknowledge`, which is simulated delivery; an estimated `local` playback counts only in a call a launcher opened with `simulate_playout`; `mark_for`, `note_mark_sent`/`note_mark_returned`/`note_cleared`, `note_onset`, `answered_in_time`), `Intent` (one business action: opaque id, args, subject, `prepared`/`dispatched`/`unknown`/`succeeded`/`rejected`, `readbacks`; `intent_for`, `restore`, `unresolved`), `Evidence` (every validated receipt, append-only, with its resource `kind` and `version`; `effective`: each resource's latest successful change, keyed by kind and id and ordered by the application's version, never arrival; `note_version`/`is_latest`: a reconciled receipt below the service's current version is history), ending facts (`note_ending`), `Handoff`, `Question`, `Held` (what the caller could still take back, `hold`/`held`/`release`) |
| `graph.py` | `Node`, `Exit`, `LocalAction`, `Opening`, `route`, `moved_by`, `lent`, `model_clients` (yields one record per warm-up request, so a launcher counts them) |
| `flow.py` | `Flow`: the steps table as a `pydantic_graph` graph with each step's legal moves; it walks every turn and refuses an undeclared move; `render()` draws it |
| `policies.py` | `TurnBudget` (each further piece of a joined turn adds `per_piece` requests, up to twice the turn's own), the hooks (`one_action_per_reply`, `speak_by`, `request_clock`), `pick`, `schema_tool` |
| `history.py` | `paired` and `project`: what each provider request is actually sent |
| `locale.py` | `Locale`: the lines the turn engine says itself, in one language: holding lines, `goodbye`, `unsettled`, `gave_up` (a read-back past its unheard limit), `left_undone`, `pardon` (a rendered step's `wait` with nothing to ask), `restated` (a read-back confirmed by the caller saying its value again); `claims` is empty in core (which verbs claim a change is the application's: `locale: {base, claims, goodbye}` in the manifest); `ENGLISH`, `SPANISH`. A call picks one by class (`CallContext.default_locale`), per call (override the `locale` property), or by manifest (`locale:`) |
| `clock.py` | `Today`: the call's date, weekday, time and the labelled days ahead, read once per call from `CallContext.now` in its `timezone` (`calendar_days`), and sent to every step's model |
| `processor.py` | the Pipecat adapter: events in, `LLMTextFrame`s out, the turn span; `SegmentMark` after every segment, `TwilioMarks` (Pipecat's Twilio serializer with marks out, marks and `stop` in, clears recorded first), `LocalPlayout` (a development transport), `playout` (wires one of them into a bot; `simulate=True`, only from a launcher that names it, lets their estimates approve), `Ceiling` (the call ceiling its owner always stops), `record_ending` (the `call.ending` span). The only `core` module that imports Pipecat (the `voice` extra) |
| `proposals.py` | `Proposal` (structural), `BaseProposal`, `ProposalLedger`: the one question a call waits on, and when it can be answered |
| `operations.py` | `Operation`: one business action, `@final run` = validate, settle, authorise, find the intent and claim it (`CallContext._claimed`) before the first await, record it, mark it dispatched, execute, validate receipt, record; `outcome`: a receipt is `succeeded`, `rejected` (a declared terminal no) or `unknown` (pending, undeclared); after dispatch every exception, a plain `ModelRetry` included, leaves the intent unknown, never sent again unless `idempotent`; `Refused` is the application's explicit no; `resource`/`version` and `_current_version` (a reconciled success below the service's `current_version` is history); `needs_approval` (a read-back yes) and `changes` (it changes a resource) are separate; `subject`, `recap`; the `write` audit (intent, `proposal_args`, playback, sequence); `operation_token` (Faro's legacy records only) |
| `completion.py` | `Completion`, `FieldsPresent`, `ReceiptMatched`: a step is done only on evidence in the facts |
| `steps.py` | the manifest's step kinds, compiled by `StepFlow` into a nodes table. `CollectStep` and `ConfirmStep` are the plain form-flow (fields, an exact read-back). `ActionStep` runs one operation with no model, `returns: true` sending it back to the step that handed the caller in instead of onward. `ConversationStep` is the one-agent-per-node kind the salon builds on (`speech: rendered`: no model text at all; the agent chooses, and code says every word, with a `wait` output; `prose`, the default, is best effort): `fields` it may record, `choices` it may offer, `operations` it may carry out (each read back, then carried out on a later yes to that exact question), `exits` with a `purposes` map shown to the model, `requires`/`satisfied_by` for a prerequisite the graph interposes, `guarded` (clause-at-a-time speech), `quoted` (a field that must be the caller's own words), `opens_with` (an operation read back on arrival with no model), `asks` (on arrival, code asks for the first missing field in fixed words) and `reads_back` (an operation read back by code when a reply only recorded one of its fields, only when that is the one operation that could run; when the value recorded is the one the caller was just read back and heard in full, and every try this turn kept it, code says `Locale.restated` and carries it out: `core.graph.Restated`). A `goodbye` has no model words: core says the recaps the model pointed at (`recap: [ref]`, each a receipt that still stands, in the operation's `recap` words), the unsettled line if an intent is unknown, the `left_undone` line if the last read-back was never carried out, then the locale's goodbye; beside a waiting read-back it is sent back once. A read-back is replaced by `Locale.gave_up` after `READBACKS` (2) copies unheard on a line that has proven no delivery, or `CUT_READBACKS` (5) copies the caller talked over on a line that has. `AgentStep` is a hand-built step (legacy: Faro), whose module's `node(step)` function still uses `core.desk.AgentBuilder`. `End` can be taken back (`take_back`). `FlowState[S]` and `FlowCall[S]` are the session around an application's own state model (`update`, `collect`, `services`; `take_owed`: once the caller is known, the services' `owed(identity)` intents from calls that are over are restored and settled first); `receipt_for`, `ConfirmedOperation`, `FunctionOperation` (a declared typed function, run in a worker thread when it is plain: bindings, assignments, `succeeds`, `matches`, readback, reconcile by intent, receipt speech; a third parameter annotated `Intent` gets the intent) bind a manifest operation to the code that runs it. Every step's agent is built by `Agent.from_spec` from its persona's native `AgentSpec` |
| `flowspec.py` | flow folders: `FlowSpec` (YAML or JSON via `yaml.safe_load`, one spec class per step kind), `state:` naming the application's model and `identity:` its caller field, each step naming its prompt file and `code:` module inside the folder. The manifest (`agent.yaml`) also declares `operations:` (typed functions, checked against their signatures; `changes`, `subject`, `resource`, `version` (required with `subject` on a change), `rejects`, `idempotent`, `recap`), `agents:` (personas holding Pydantic AI's native `AgentSpec`), `entry`, `services`, `model`, `locale` and `app_version`; `FlowSpecError` listing every problem, before any model |
| `turn.py` | one caller turn as an event stream: `Turn`, `run_turn`, `take_turn`, `Barge`, the fixed lines, one holding line per caller turn, `remind` (the silence reminder, on Pipecat's `on_user_turn_idle`), the `speaks_for` and `reads_back` handling that keeps a node's own operation from being echoed or asked for twice. Imports no Pipecat |
| `observability.py` | Langfuse over OTel (the `tracing` extra); call `setup()` once, before any agent is built. Reads no `.env`: the launcher does |
| `classifiers.py` | legacy, Faro only until session 5. `Classifier`: one direct model request over an `Exchange` (question, answer, facts), the author's context and classes; `ClassSet` and `output_model` build classes from data |
| `decisions.py` | legacy, Faro only until session 5. Typed meanings of a reply (`Accept`, `SwitchTask`, ...); a business subclasses for its own |
| `readings.py` | legacy, Faro only until session 5. Narrow readings: `Reading` (one classifier, when it applies, its label), `classify`/`choose`, `ChoiceReading` (`picked`: only an id among its own options counts), the generic class sets (`CONFIRM`, `TAKE_BACK`, `STILL_AGREED`, `CLOSING`, `REQUEST`), `read` (run the ones that apply, side by side), `Acts` (answer, take back, pick, goodbye: the fixed order) |
| `conversation.py` | legacy, Faro only until session 5. A conversation's acts as classes a use case fills in: `ReadBack` (with its `done` line), `FixedChoice`, `Offer`, `HeardValue`, `TakeBack`, `HeldApproval`, `Detour`, `Closing`; `Conversation` builds their readings, runs them, and decides in core's order |
| `desk.py` | legacy, Faro only until session 5. `Desk`: what every step can do, for any use case (`hand_over_for`, `hang_up`, `end_conversation`, `clarify`, `switch_task`), built from a call type, a `Handoff`, the action names and an optional `Carry`; `AgentBuilder`, the hand-built step assembly the salon used until session 4, lives here with it |
| `interpreters.py` | legacy, Faro only until session 5. `Interpreter`: at most once per turn, cached on the call, budgeted, never published stale; `ClassifierInterpreter` |
| `tests/` | `test_classes`, `test_history`, `test_clock`, `clinic.py` (a clinic built on core's legacy classes, for a Faro-style use case) and `test_reuse` (that clinic through the real engine, scripted, with no salon code), and the public boundary: `clinic_flow/` (a clinic written as models and functions, five manifests: `agent.yaml` controlled, `one_agent.yaml`, `handoff.yaml`, `code_node.yaml`, `reads_back.yaml`), `test_public`, `test_authoring` (the authoring contract on the three main patterns), `test_session` (atomic state, owned fields, isolation, stale work, evidence), `test_nodes` (P1-P18: no reader runs on the public path, hostile calls are refused, `reads_back`, the silence reminder; `public_path_loads_no_reader` checks the legacy modules never import), `test_delivery` (marks, clears, onsets, silent segments), `test_sdk`, `test_package`, `test_intents` (I1-I10: an intent through cancellation, reconciliation, idempotent retry, a thread that commits late, restore, goodbye), `test_outcomes` (O1-O9: pending is unknown, declared no, one sender per intent, no resurrected effect, resource kinds, loader checks, owed intents), `test_speech` (S1-S7: a rendered step says no model words), and `ticket_flow/` with `test_tickets` (R1-R10: a Spanish support desk with no phone, date or slot, a SQLite backend, its write step rendered, fresh-process reconciliation after a killed worker, every name renamed with core untouched). No use case code. `defects.py` is outside the suite: known defects that fail on purpose, each with its repair session (`python -m tests.defects`, exit code = defects left) |

**`use-cases/salon-graph/`**, the salon on core.

| Path | Owns |
|---|---|
| `bot.py` | the Pipecat pipeline around the salon (`run_bot`, which `server.py` also runs per call): STT, VAD and smart-turn, TTS, transport, tracing worker, the greeting, `DIARY_PATH` (the shared diary file), `DELIVERY=simulated` (estimated playback may approve: the browser transport, Coval's mark-less simulator only), the silence reminder (`on_user_turn_idle` after `IDLE_SECONDS`, 20 s, calls `node.remind()`), `VAD_STOP_SECONDS` (0.3 s default), `MAX_CALL_SECONDS` for a simulated call (`core.processor.Ceiling`, stopped in a `finally` around the pipeline), `playout` wiring the Twilio marks, `record_ending` |
| `server.py` | the salon behind a bounded runner on one host: `uvicorn server:app --workers N`; `Admission` (SQLite `RUNNER_DB`: at most `MAX_CALLS` = 5 admitted or active over every worker, `<Reject reason="busy"/>` past it, one claim per CallSid, a dead worker's calls closed by a reaper), each worker's clients warmed once, its own drain on SIGTERM, loop-lag sampler, local-only `/ready`, `/stats`, `/calls`; `VOICE_DOUBLES=1` for the offline doubles; `CARRIER_NUMBER=1` offers the webhook's `From` |
| `salon/agent.yaml` | the manifest: `state:`, `identity:`, `entry`, `locale`, `model`, the `robin` persona; five declared operations (`lookup_customer`, `book_appointment`, `move_appointment`, `cancel_appointment`, `record_complaint`, each with `args`, `readback`, `reconcile`, `matches`, `succeeds`, `assign`, `say`; `subject`, `resource`, `version`, `rejects`, `idempotent` and `recap` on the four writes, `changes: false` on the lookup); `locale:` with the salon's own claim words and goodbye; four `conversation` steps (`concierge`, `verify`, `booking`, `complaints`), each with its `fields`, `choices`, `operations`, `exits`; `concierge`, `booking` and `complaints` `guarded`; `verify` with `opens_with`, `asks` and `reads_back`; `booking` with `reads_back` |
| `salon/app.py` | `FLOW` (`SPEC.compile()`, at import) and `open_call`: one session per call, the carrier's number prefetched (`prefetch_number`), the diary and clock passed in as services |
| `salon/functions.py` | the typed domain functions `agent.yaml` connects to: `Salon` (the call's services: the diary and its key; a write's diary token is its `Intent` id; `note_intent`, `current_version`, `owed`), each operation's function (`lookup_customer`, `book`, `move`, `cancel`, `record_complaint`), its readback and reconciliation (`find_booking`, `find_move`, `find_cancel`, `find_complaint`), `find_times`, `held_bookings` |
| `salon/models/` | `shapes` (`Phone`/`SubmittedPhone`, `Service`, `Verification`, ...), `receipts` (`CustomerLookup`, `Saved`, `Held`, `Slots`, what the diary may say), `state` (`SalonState`, the complaint-request word list `_ASKING_TO_COMPLAIN`, `prefetch_number`) |
| `salon/diary.py` | `Diary`: customers, bookings, complaints, write results and intents in one SQLite file every worker shares (rollback journal: this SQLite predates the WAL-reset fix), a trigger holding the one-chair capacity, a move as one `UPDATE`, `outcome_or_void` (a reconciled write with no result is fenced), revisions checked on move and cancel; offers in process memory, per call; and the oracle that says no |
| `salon/prompts/` | one Handlebars prompt per step, the shared voice and diary rules (`_voice.md`, `_diary.md`), and `knowledge/` with the salon's own documents |
| `evals/` | `doubles`, `load`, `faults`: the offline Stage C runner checks (scripted model, energy STT, paced TTS; fake Twilio phones on a real websocket; the 1-2-5 ramp; worker kills, a stale worker, overload, drain). `calls`: the live text checkpoint, four scripted calls (T1-T4) on the real engine, checked on the diary. Live, billable, except `calls --recheck`. `cases`: reviewed reply cases shared by the live and offline checks. `line` and `matrix`: the harness arm of the N1-N13 native comparison, offline, on the real pipeline. `paired`: session 4's classifier-vs-node-agent semantic check, the same file run once per arm in its own checkout |
| `tests/` | `test_graph` (legal moves, prerequisite interposition, every node's first request pinned in `golden/agents.json`), `test_business` (the acceptance matrix A-L), `test_walkthrough` (one booking call through every boundary), `test_continuation` (a turn joined over several caller pieces), `test_overlap` (timed barge-in and delivery replay), `test_state`, `test_diary`, `test_budget`, `test_traces`, `test_core` (direct engine and Pipecat adapter agree), `test_calls` (the live text checkpoint's own machinery, scripted), `test_marks` (M1-M6: marks through the real adapter, TTS base, FastAPI websocket transport and Twilio serializer over a fake phone), `test_recovery` (the C26pPHK6 family, one budget per joined turn that grows with its pieces, cut read-backs said again: B4.1-B4.6), `test_ceiling`, `test_store` (B1-B9: worker processes on one diary file, judged from the file), `test_runner` (A1-A5: admission), `test_audit_export` (the runtime's own spans judged by the real `load_report` command), `script.py` (the scripted provider every test drives the real flow with) |

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
| `concierge-tools/load_report.py` | judges Coval calls from Langfuse and the write records, with no Coval metric. Five answers per call, each pass, fail or unknown, never merged: outcome (receipts replayed by booking ID over the whole call: a move is the same appointment, a repeat is one, a receipt that contradicts its own write fails, a caller's number is required), authorization (label check and read-back arguments, `proposal_args`, by code; the answer's meaning by a reviewed label), delivery (only `Playback` marks bound to the proposal's segments, or a reviewed recording; any evidence it did not play fails), completion (each `Claim`, its action and appointment, against the replayed effects; a bare label can fail, never pass) and operational (an observed ending with its source; inferred is unknown; never a span's length); `missing` names what is unknown. A tool call is dropped only for its own `write` audit or when it only read itself back; any other stays, unknown. Reviews and facts come from `--evidence` (`Evidence`, strict: no coercion; each review naming `human` or `agent`). Diagnostics as before: speech, request chains, latency, endpointing. Exit 0 all pass, 1 any fail, 3 incomplete, 2 invalid input. `uv run --env-file ../../.env python load_report.py RUN_ID [RUN_ID ...] --out DIR [--evidence FILE] [--save EXPORT] [--call SIMULATION=CALL_SID]`, or `--saved EXPORT` offline. A phone-line simulation has no call SID from Coval: `coval_sim run` prints the bot's, for `--call` |
| `concierge-tools/coval_sim/` | the Coval release check: starts `bot.py -t twilio` behind a cloudflared tunnel and lets Coval call it. Needs network and keys, never runs in CI. One job per invocation (agent ID, test set, persona, `--max-simulations`), `plan` first; a run log refuses duplicates and uncertain relaunches. A Coval agent `voice-harness-<agency>-pipecat` calls `use-cases/<agency>`; `salon` calls salon-graph, and `graph` the unmute graph build (`GRAPH_BUILD_DIR`). A `-twilio` agent with a number is a real phone line: one `Campaign` owns the number (`serve --phone-number --campaign`, or a lone `run`), `run --host` never touches it, and it is given back once every call is over and only if unchanged; a phone-line job runs salon-graph's `server.py` and binds each simulation to its call by evidence (`correlate`, `--fixtures`). A bot with no `MAX_CALL_SECONDS` of its own (salon-pipecat-flows) is stopped by the runner 188 s after its call connects. `--port` gives a job its own bot and tunnel port, so jobs can run side by side. `--concurrency N` places a job's calls N at once in one run (a bot with its own limit only): most of a run is Coval dialling and scoring, which one call at a time pays per call. `--metrics` scores a run on named metric IDs instead of the agent's own (Coval refuses a launch with none). `--profile` names a speech profile env file (a validated `SpeechProfile`, every name required) the bot must run on, so load runs stay comparable; `--call-seconds` sets the call ceiling. `serve` starts one bot and tunnel that several jobs can share; `run --host <serve's host>` sends a job at it instead of starting its own bot, so jobs run side by side against one process. `--wait` bounds how long `run` waits for Coval before reporting a run still in progress, for a queue of one-call jobs |
| `concierge-tools/matrix/` | the N1-N13 native comparison: `scenarios.v2.json` (the rows run today; `scenarios.v1.json` kept as the pinned native reference), the checks each arm runs, the frozen N12 protocol, `run_checks.py` (one check in one member's own environment) and the driver, which writes `matrix.json`, `matrix.md` and `timing.json` |
| `concierge-tools/audio_timing.py` | who spoke when in a two-channel Coval recording: response latency, overlaps, barge-in stop latency |
| `faro-tools/bench.py` | Faro's benchmark cases live: scripted text calls on the real model, each call in its own process, one Langfuse session per call. Billable |

**The split that matters:** `core.turn` runs a turn and knows nothing about
how it reaches a person; `core.processor` is the Pipecat adapter over it.
Anything about *running* a turn goes in `turn`, anything about frames goes in
`processor`. salon-graph's `tests/test_core.py` asserts the two agree and that
no `core` module but the processor imports Pipecat.

`core` imports no use case. A use case imports `core` and nothing from another
use case; salon-prompt and salon-pipecat-flows import neither. In salon-graph:
`salon.models` imports nothing from `core` at all, plain Pydantic only;
`salon.functions` imports `core` for `OutcomeUnknownError` and reads the
diary and the models; `salon.app` imports `core` to load and compile
`agent.yaml` (`core.load(...).compile()`) and to open a call
(`flow.open(state, services=..., clock=...)`); `bot.py` wires the compiled
flow to Pipecat (`core.graph.model_clients`, `core.processor.PydanticAgentProcessor`,
`core.observability.setup`).

Application state is replaced wholesale, never field by field. `FlowCall.update(**fields)`
builds and validates one complete candidate from the current facts and these
changes, and commits it or changes nothing; `FlowCall.collect(**fields)` is
the model's own path into it, from a `ConversationStep`'s `collect` tool, and
also resets whatever rested on a field that changed. A typed function
declared under `operations:` takes the call's services as its first
parameter and a validated Pydantic argument model as its second
(`def book(services, args: Booking) -> Written`); core builds its arguments
from state fields by name (`binds`) and never from a model's words.

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
cd use-cases/concierge-tools && uv run python -m coval_sim plan --agency salon --agent <ID> --test-set <ID> --persona <ID> --max-simulations 1 --label <session> --profile <speech-profile.env>   # then `run`, billable
cd use-cases/concierge-tools && uv run python -m coval_sim serve --agency salon --profile <speech-profile.env>   # one bot and tunnel for several jobs
cd use-cases/concierge-tools && uv run python -m coval_sim run --agency salon --agent <ID> --test-set <ID> --persona <ID> --max-simulations 1 --label <session> --host <serve's host> --profile <same file>   # a job against that bot, billable
cd use-cases/salon-graph && uv run --env-file ../../.env python -m evals.calls   # live text checkpoint, billable
cd use-cases/concierge-tools && uv run ledger.py harness CA... -- graph trace:...   # cost of calls
cd use-cases/concierge-tools && uv run --env-file ../../.env python load_report.py <RUN_ID> [<RUN_ID> ...] --out /tmp/load [--evidence FILE]   # judges calls from Langfuse and write records, no Coval metric; exit 0 pass, 1 fail, 3 incomplete, 2 invalid input
cd use-cases/concierge-tools && uv run python -m matrix run      # N1-N13, both arms, offline; `time` for N12
cd use-cases/salon-graph && uv run python -m evals.load ramp --calls 1,2,5 --seconds 60 --workers 2 --out /tmp/load   # offline runner ramp
cd use-cases/salon-graph && uv run python -m evals.load faults --out /tmp/faults   # offline worker kills, stale worker, overload, drain
docs/research/reusable-core/session-2/package-proof.sh         # the wheel, installed outside the checkout
```

`uv run python -m tests` turns `ALLOW_MODEL_REQUESTS` off *before* any module
is imported, so a test that forgot to override an agent raises instead of
billing. One module alone needs a key in the environment:
`GOOGLE_API_KEY=x uv run python -m tests.test_graph`, from salon-graph.

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

1. **Prompts are compiled against the call type.** Every step's prompt is a
   Handlebars template, checked when `agent.yaml` is compiled (`salon.app`
   does this at import): a placeholder naming no field on `FlowCall[SalonState]`
   (`{{state.facts.x}}`, not `{{state.x}}`) raises `TemplateSchemaError`
   **before any call runs**, never mid-call. The salon's own prompts use no
   such placeholder today; what a step's agent is told about the application
   is written by code before every request (`core.steps.context`), not
   templated into the prompt file. `quote_caller: true` still appends
   `{{heard}}`, the caller's latest words, to every prompt.
2. **A new conversational step is two things kept in lockstep**: a prompt file
   in `salon/prompts/`, and its entry in `salon/agent.yaml` (`fields`,
   `choices`, `operations`, `exits`). A new operation it carries out also
   needs its typed function in `salon/functions.py`, declared under
   `operations:`. Miss one and it fails at compile, before any call runs:
   `FlowSpec` checks that every prompt file, operation, field and exit a step
   names is real.
3. **Anything a service returns is validated at the boundary.** Every `Diary`
   response is parsed into a `salon.models.receipts` model before a field is
   read. A return annotation is not validation.
4. **One action per model reply.** `one_action_per_reply` picks the single
   tool call in a response allowed to move the caller or write to the
   business; `claim_or_refuse` is how an action checks it holds that claim.
   The salon has no tool outside a declared operation that can write state at
   all, so there is nothing to scan for; `core/tests/test_nodes.py` and
   `use-cases/salon-graph/tests/test_business.py` drive a hostile scripted
   model to prove the gate holds at execution.
5. **Consent is application-owned.** A step's own agent reads the caller's
   reply; code decides whether that reading may authorise a write.
   `CallContext.heard` is what they said; `ProposalLedger` is the one
   question a call is waiting on and whether they heard it in full. Calling
   an operation again is not itself approval: `CallContext.approve` binds a
   yes to that exact, delivered question before `ConfirmedOperation` will run
   it, and on a step that can write, model speech goes out a clause at a time
   so a claim about a change nothing made is never said. Every business
   action is an `Intent` core issues before dispatch: after dispatch only a
   validated receipt or reconciliation settles it, and an unknown one is
   never sent again unless the backend dedups by its id (`idempotent`).
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
   heard in full only on evidence (`Spoken.delivered`): its own transport
   mark came back (Twilio's `mark`, sent after its last media chunk) with no
   clear sent while it was pending, or a text reader acknowledged it
   (`CallContext.acknowledge`, simulated delivery). An estimate (`local`:
   audio written, or time for it to play) delivers only in a call a launcher
   opened for simulated playout (`DELIVERY=simulated`). The aggregate
   `BotStoppedSpeakingFrame` decides turn-taking only. A question can be
   answered only by a reply that began after its latest copy played
   (`answered_in_time`: the voice detector's onset, less its window, on this
   process's `perf_counter`); no record, a queued copy, a cut or cleared one
   never can, and copies belong to a question by its serial, not its words.
   A bot with no `core.processor.playout` wiring can approve nothing. Without
   playback signals the engine errs towards a new turn. A runtime text report
   never cuts a segment whose audio already ran out, and a queued segment
   whose words had time to play counts as played for turn-taking only, never
   for approval. A joined turn's span says why (`continued_from`), and its
   pieces share one request tally.

## Known debt

No classifier or solver runs on the public manifest path any more: the active
step's own agent reads the caller's reply, and code decides whether that
reading may authorise a write (`ProposalLedger`, `CallContext.approve`,
`ConfirmedOperation`). `core/tests/test_nodes.py:public_path_loads_no_reader`
checks that compiling a manifest never imports the legacy classifier,
reading, conversation, interpreter, desk or decision modules. Faro alone
still runs on that legacy path, kept only until session 5.

The support desk's write step is `speech: rendered`, so nothing it says is
model prose. The salon's steps are still `prose`; moving them is a prompt
change that needs live checks (Stage D's decision). `tests.defects` keeps a
prose paraphrase claim visible as a known limit.

`Locale.claims` is the one place a word list still guards speech, and it is
the application's: the salon's English verbs in `salon/agent.yaml`, the
desk's Spanish ones in `core/tests/ticket_flow/agent.yaml`; core ships none.
It is checked against model text on a `guarded` step before it is said. It
is a best-effort guard on model prose, not a guarantee: what is guaranteed
is that receipts, recaps, goodbyes and fixed lines are rendered by code. `times_named` in
`salon/diary.py` still only steers which times a diary read prefers, and the
complaint-request word list in `salon/models/state.py`
(`_ASKING_TO_COMPLAIN`) is English only. Structured output validates shape,
not semantic accuracy; run the explicit live evaluation before making
accuracy or latency claims.

In one of two live text runs of the session-4 wording, the `verify` step's
first request answered "I'm sorry, I cannot do that right now" to an
ordinary request, with nothing unusual about it; the second run of the same
case passed (`docs/research/reusable-core/session-4/ledger.md`, live text
check `text-v2`). Seen once in two, not yet reproduced or explained.
