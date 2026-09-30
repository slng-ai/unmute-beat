# CLAUDE.md

A Pydantic AI agency standing where Pipecat's LLM service would. Pipecat owns
transport, VAD, turn taking, STT and TTS; the agents own the conversation.

```
transport.input() -> SlngSTT -> user_aggregator -> PydanticAgentProcessor -> SlngTTS -> transport.output()
```

[README.md](README.md) introduces the package and how to run it. The
[architecture guide](docs/architecture/README.md) explains the current public
path from the voice stack through state, routing and recovery, with source
and test links. Read it before changing how a turn works.

## Layout

A uv workspace. The root `pyproject.toml` lists the members and holds the ruff
config; there is one `uv.lock` and one `.venv` at the root, and one `.env`.
Every member has its own `pyproject.toml`. Paths in each table are relative to
that member's folder.

**`core/`**, the package `slng-voice-core`. The code is in `core/src/core/`.

| Path | Owns |
|---|---|
| `__init__.py` | the public boundary: `load`, `FlowSpec`, `StepFlow` (`compile`, `open`, `open(intents=...)` for a fresh process), `FlowCall`, `text_turn`, `TextReply`, `greet`, `Operation`, `ConfirmedOperation`, `BaseProposal`, `Intent`, `TurnBudget`, `Lookup`, `Write`, `Receipt`, `Rejection`, `Subject`, `Version`, `Phone`, `NotPast`, `Quoted`. Imports no Pipecat, Langfuse, `.env` or provider |
| `state.py` | what every agency shares: `STRICT`, `VoiceState`, `CallContext` and its speech ledger (`Spoken.delivered`: delivered only by the segment's own transport mark or a text `acknowledge`, which is simulated delivery; an estimated `local` playback counts only in a call a launcher opened with `simulate_playout`; `mark_for`, `note_mark_sent`/`note_mark_returned`/`note_cleared`, `note_onset`, `answered_in_time`), `Intent` (one business action: opaque id, args, subject, `prepared`/`dispatched`/`unknown`/`succeeded`/`rejected`/`withdrawn`, `readbacks`; `intent_for`, `restore`, `unresolved`), `Evidence` (every validated receipt, append-only, with its resource `kind` and `version`; `effective`: each resource's latest successful change, keyed by kind and id and ordered by the application's version, never arrival; `note_version`/`is_latest`: a reconciled receipt below the service's current version is history), ending facts (`note_ending`), `Handoff` (`ends_turn` with no `reply` ends the turn unless the destination opens with something; status `returned` ends a consultation), `VoiceState.consulting` (the step a specialist hands the caller back to, kept apart from the prerequisite slot `pending`), `Question`; `validation_context` (what an application's validators read whenever core validates its state: `today`, `now`, and in a flow the previous state as `before`, the caller's words this turn as `heard`, and everything they said on the call as `said`); `unheard_receipt` (said again at most `RECEIPT_REPEATS`, 1, times, never marked delivered for it); `Hearing` (`CallContext.hearing`: `heard`, `early`, `cut`, `failed`, `unconfirmed`; missing evidence is `unconfirmed`, never "did not hear"); `delivery_record` and `note_confirmation` (a `confirmation` span for a read-back that was blocked or withdrawn, with the transport and each copy's evidence); `withdraw` (a read-back past its limit: its intent `withdrawn`, terminal, found again by the same arguments, nothing left pending; its audit reason is the hearing of the last copy, never inferred from which retry count ran out); `last_asked_left_undone` (a read-back never carried out and never turned down: only the node agent's `decline` turns one down, `CallContext.decline`, bound to the pending question and spent with it; a joined turn that rewinds drops it; a question in reply leaves it waiting) |
| `graph.py` | `Node`, `Exit` (a destination), `LocalAction`, `Opening` (on arrival by a hand-over only), `Restated` (code carries out an operation: a read-back confirmed by its value said again, or a `carries_out` rule), `route`, `model_clients` (yields one record per warm-up request, so a launcher counts them) |
| `flow.py` | `Flow`: the steps table as a `pydantic_graph` graph with each step's legal moves; it walks every turn and refuses an undeclared move (a consulted step may move back to each step that consults it); `render()` draws it. `Nodes`: a compiled steps table (a `dict`) that owns its graph, built once per compile and collected with it; no process-wide cache (`Flow.of` builds one per use for a table made by hand) |
| `policies.py` | `TurnBudget` (each further piece of a joined turn adds `per_piece` requests, up to twice the turn's own), the hooks (`one_action_per_reply`, `speak_by`, `request_clock`; `into=` registers each on a step's one `Hooks`: the SDK dispatches every capability separately), `pick` |
| `history.py` | `paired` and `project`: what each provider request is actually sent. A spoken line whose run left no response stays in the turn it was said: before the next turn's caller words, or joined to the response that called the tools whose results follow. `conversation`: `context: {history: messages}`, earlier caller turns as what was said and heard, their tool exchanges left out, except a `load_capability` pair the step registers (so a loaded capability is not loaded twice). `History` (`context: {history: {scope: node|conversation, turns: N}}`): whole caller turns by the step id that handled them (kept messages carry `metadata['step']`, `Spoken.step`; a turn a consultation handled for a step, `metadata['for']`, is that step's too), this run's own tool exchange only, no earlier request's `instructions`; the turn that asked for the step (`CallContext._arrived`), its own pending question and later turns, and its capability loads kept past `turns` (`window`, logged). `Scope`: a scoped step's view (visible facts, own offers, own question, own intents and receipts through visible fields only). `Payload`: one request at the model boundary as canonical JSON (instructions, history, tools, outputs), for pins, byte counts (`sizes`; `results`, the tool results inside history, apart so pins keep) and canary searches |
| `locale.py` | `Locale`: the lines the turn engine says itself, in one language: holding lines, `goodbye`, `unsettled`, `gave_up` (said once, as a read-back past its limit is withdrawn), `withdrawn` (that read-back asked for again), `unconfirmable` (said instead of the greeting on a line that reports no playback), `left_undone`, `pardon` (a rendered step's `wait` with nothing to ask), `declined` (said by `wait` after a `decline`, never `pardon`), `restated` (a read-back confirmed by the caller saying its value again); `refusals` (the caller's words for a refused value, by its validator's error type: `note_refusal` suggests them to a prose step's model and a rendered step's `wait` says them; `locale: {base, goodbye, refusals}` in the manifest); `ENGLISH`, `SPANISH`. No claim word lists: the claim patterns were retired. A flow picks one in its manifest (`locale:`), or per call from a state field (`language:` names it, `locales:` maps its values), so a bilingual application's fixed lines follow the caller |
| `clock.py` | `Today`: the call's date, weekday, time and the labelled days ahead, read once per call from `CallContext.now` in its `timezone` (`calendar_days`), and sent to every step's model. `CallClock`: the manifest's `current_datetime: {enabled, timezone, calendar_days}`, copied into each call by `StepFlow.open`, which reads the clock once (`enabled: false` keeps the date out of prompts; validators still read it) |
| `values.py` | the value types an application would otherwise write itself: `Receipt` (core's `STRICT` plus `extra='forbid'`), `Rejection` (an authoritative no), the `Subject`/`Version` markers on receipt fields, `NotPast` (a `date` the call's clock has not left behind), `Phone` (libphonenumber through pydantic-extra-types, a plain-string schema, `.spoken`, `from_carrier`), `Quoted` (the caller's own words, from the validation context's `said`). Each validator raises `PydanticCustomError` with a stable type, which `Locale.refusals` words for the caller |
| `actions.py` | operations as classes: `Action[S]` is the argument model and the action (`run`, `readback`, `reconcile` methods; `say`, `recap`, `route`, `idempotent`, `matches`, `assign` class variables), `Lookup` and `Write` its two kinds. Arguments bind by name, or by `validation_alias`/`AliasPath` to another or a nested state field (validated by name only, so a missing source is missing, never a same-named fallback); a discriminated union return is the outcome (`Rejection` members are a no); `flowspec._declared_class` adapts it into a `FunctionOperation` |
| `processor.py` | the Pipecat adapter: events in, `LLMTextFrame`s out, the turn span; `SegmentMark` after every segment, `TwilioMarks` (Pipecat's Twilio serializer with marks out, marks and `stop` in, clears recorded first), `WebRTCPlayout` (the browser: a segment is heard once the page reports that its audio element played the stream without a break from before the segment's `start` and its played RTP timestamp passed the bound, both read from the audio sender, `start` when the segment's first audio left and the bound when its last did; reports are `PlayoutReport` (`since`: where the unbroken run began), validated and checked again; an interruption voids only the marks still queued behind the dropped audio (`note_cleared(sent=False)`), since a browser has no clear; a segment with no audio is never bounded; `capability` comes from the page's `client-ready`, read through `_About`, so a malformed `about` is a client that reports nothing), `serve_client` (core's page at `/voice/`, `/` sent there), `reports_playback(wiring, call)` (a launcher's check before its greeting: True only on a Twilio stream with marks, a browser client that said it reports, or a call opened for simulation), `LocalPlayout` (a transport with no marks, or a browser call a launcher opened for simulation), `playout` (wires one of them into a bot and names the transport, `CallContext.note_transport`; `simulate=True`, only from a launcher that names it, lets estimates approve), `Ceiling` (the call ceiling its owner always stops), `record_ending` (the `call.ending` span). The only `core` module that imports Pipecat (the `voice` extra) |
| `proposals.py` | `Proposal` (structural), `BaseProposal`, `ProposalLedger`: the one question a call waits on, and when it can be answered |
| `operations.py` | `Operation`: one business action, `@final run` = validate, settle, authorise, find the intent and claim it (`CallContext._claimed`) before the first await, record it, mark it dispatched, execute, validate receipt, record; `outcome`: a receipt is `succeeded`, `rejected` (a declared terminal no) or `unknown` (pending, undeclared); after dispatch every exception, a plain `ModelRetry` included, leaves the intent unknown, never sent again unless `idempotent`; `Refused` is the application's explicit no; `resource`/`version` and `_current_version` (a reconciled success below the service's `current_version` is history; asked before any state changes, and a check that fails, times out or answers no version leaves the intent unknown, never a made-up version); `needs_approval` (a read-back yes) and `changes` (it changes a resource) are separate; `confirms_by_restating` (`restated: confirms`, a lookup only); `route` (the receipt field naming the step a success moves the caller to); `subject`, `recap`; the `write` audit (intent, `proposal_args`, playback, sequence) |
| `completion.py` | `Completion`, `FieldsPresent`, `ReceiptMatched`: a step is done only on evidence in the facts |
| `steps.py` | the manifest's step kinds, compiled by `StepFlow` into a nodes table. `CollectStep` and `ConfirmStep` are the plain form-flow (fields, an exact read-back). `ActionStep` runs one operation with no model, `returns: true` sending it back to the step that handed the caller in instead of onward. `ConversationStep` is the one-agent-per-node kind the salon builds on (`speech: rendered`: no model text at all; the agent chooses, and code says every word, with a `wait` output that says, in order, a read-back the reply calls for, a refusal then this turn's offer (a choice function's `say`), `withheld` after an authoritative no, the first missing `asks:` field, the offer looked up by code, or `pardon`; a rendered step may offer choices, never be `guarded`; `prose`, the default, is best effort; `speech: structured`, opt-in in version 1 and the default in version 2, is `core.structured`): `fields` it may record, `choices` it may offer, `operations` it may carry out (each read back, then carried out on a later yes to that exact question), `exits` with a `purposes` map shown to the model, `requires`/`satisfied_by` for a prerequisite the graph interposes, `quoted` (a field that must be the caller's own words), `opens_with` (an operation read back on arrival with no model), `asks` (on arrival, code asks for the first missing field in fixed words) and `reads_back` (an operation read back by code when a reply only recorded one of its fields, only when that is the one operation that could run; when the value recorded is the one the caller was just read back and heard in full, every try this turn kept it, and the operation says `restated: confirms`, code says `Locale.restated` and carries it out: `core.graph.Restated`; for any other operation the same value is only a record, and the step's agent reads the reply), and `refusals` (a rendered step's words for a value `collect` refused, said by `wait`). A waiting read-back whose words change for the same value (the caller switched language) is put again in the new words. `carries_out` (an approval-free operation code runs after a reply that only records one of its fields; never offered to the model, so it runs only on what this reply recorded; a refusal goes back to the step's agent in the same turn) and `routes` (the steps its operations' receipts may move the caller to, `route:`: code's moves, never `hand_over`'s, and they win over a prerequisite's return). A change is a change of validated meaning (`FlowCall.collect` validates with the call's context first). A pick counts on the offer for the state the update ends in: `ConversationStep._record` commits the rest, has the choice function make each offer that no longer stands (and one for a refused pick), records a refused pick found on it, and `collect` returns the offers it made, so a rendered step has no `options` tool (session 4g). A choice's offer and pick are taken away only by a change to the fields it rests on (`after:`, which may name another choice, and the reset follows the chain; every non-choice field when it declares none) or by a known caller changed to another; a value the state already holds is not a new pick, and a pick whose offer the same update took away is refused. A refused change takes back the picks it was built from. A `goodbye` has no model words: core says the recaps the model pointed at (`recap: [ref]`, each a receipt that still stands, in the operation's `recap` words), the unsettled line if an intent is unknown, the `left_undone` line if the last read-back was never carried out, then the locale's goodbye; beside a waiting read-back it is sent back once. A read-back is replaced by `Locale.gave_up` after `READBACKS` (2) copies unconfirmed on a line that has proven no delivery, or `CUT_READBACKS` (5) copies the caller talked over on a line that has: its intent is withdrawn (`CallContext.withdraw`), the same arguments asked again hear `Locale.withdrawn` with no read-back, and a corrected value is a new intent with its own allowance. `StepFlow.confirms`: some operation needs a heard yes; `StepFlow.greeting(call, words)`: the words, then the entry step's `opens_with` read-back or first `asks` line, as the question the first reply answers. An `End` with outcome `completed` is required only when a collect, confirm or action step has no `then`. A conversation step with an operation that needs approval has `decline` (a function tool: the agent reads the reply as a no to the pending read-back; it runs beside `wait` or `goodbye` in one response), and one whose operations say receipts has `say_again` (the newest standing receipt in its `say` words, `StepFlow.receipt_words`, beside the `recaps` and `goodbye_words` methods). Offers are `Offers`, one `StandingOffer` per choice: ids and words together, said only by the step and on the turn that made them. After a successful change, a choice that names a resource of that kind (bound to an operation's `subject`) is offered again by its own function, quietly (`StepFlow.resource_choices`); a field a receipt set rests on that receipt's arguments (`_assigned_by`); the fields a reply recorded are cleared when each piece of a turn starts (`FlowCall.turn_started`). `End` can be taken back (`take_back`). `FlowState[S]` and `FlowCall[S]` are the session around an application's own state model (`update`, `collect`, `services`; `take_owed`: once the caller is known, the services' `owed(identity)` intents from calls that are over are restored and settled first); `receipt_for`, `ConfirmedOperation`, `FunctionOperation` (a declared typed function, run in a worker thread when it is plain: bindings, assignments, `succeeds`, `matches`, readback, reconcile by intent, receipt speech; a third parameter annotated `Intent` gets the intent) bind a manifest operation to the code that runs it. Every step's agent is built by `Agent.from_spec` from its persona's native `AgentSpec`. A conversation step also has `history` and `facts` (`context:`: what of earlier turns and which state fields its agent sees), `capabilities` (native Pydantic AI capabilities, on-demand ones among them), and `consults`/`consulted` (a specialist it asks; the specialist's `done` returns the caller with no parent request, and `FlowCall.reissue_waiting` puts the read-back that was waiting again, fresh, while its intent is only prepared) |
| `structured.py` | `speech: structured` (opt-in in a version 1 manifest, the default for a version 2 conversation step; `prose` and `rendered` unchanged): every answer and action is one typed terminal output carrying the caller's update, no text output and no `collect`. `Decision`: the update type (`patch_type`: each field with the state's own type, constraints and description, no default; absent keeps, null clears only what may be None, False/0/'' are values) and `apply` (whole or not at all, through `StepFlow.record(atomic=True)`: validators with `before`/`heard`/clock, whole-state rules, resets, picks on an offer that stands; one call per response may carry it, `patch_or_refuse`; a value the state's own types refuse in the arguments is sent back with the locale's words for the caller, as `collect`'s is, when the locale has words for it: `steps._refusal_words`). Outputs: `reply(say, update, decline)` (said by code as model speech once committed: one request), `record(update)` (only where code carries out, a restated value confirms, or a `reads_back` field the operation's update cannot carry, such as a language that changes only the read-back's words: code speaks), operations and `hand_over` with the update (an action takes only the fields it rests on), `goodbye(recap, decline)`, `done(answer, handback)` (never blank, said once; a handback continues to the step that consulted, on its legal edge, with the caller's own words in its history). Ordinary tools: `Read` (a pure read: no read-back, `changes: false`, no `route`, no `say`; `Operation.run`, then `{status: found|refused|absent|failed, result}` back to the same agent; the same read for the same turn and state is not sent again) and `Offering` (`options(update)`: a choice function only where no offer stands; while every offer stands `update` is required). A response with a read answers and acts on nothing beside it (`one_action_per_reply(reads=...)`) |
| `flowspec.py` | flow folders: `FlowSpec` (YAML or JSON through `_Manifest`, a `yaml.SafeLoader` that refuses a key given twice, one spec class per step kind; `version: 1` lists the steps, `version: 2` keys them by id and `_normalized` turns that into the same list: a step with no `kind` is a `conversation`, one with no `speech` `structured`, the flow context no facts and `{scope: node}`, every conversation step scoped, a writable field must be in its step's facts, and each version 2 problem is a `FlowSpecError` naming its step; a persona `spec.output_schema` is refused in both), `state:` naming the application's model and `identity:` its caller field, each step naming its prompt file and `code:` module inside the folder. The manifest (`agent.yaml`) also declares `operations:` (typed functions, checked against their signatures; `changes`, `subject`, `resource`, `version` (required with `subject` on a change), `rejects`, `idempotent`, `recap`), `restated: confirms`, refused on anything but a lookup read back, `route:`, a `Literal` of step ids checked at load), `choices:` as `{field: ref}` or `{field: {function, after: [fields]}}` (`ChoiceSpec`), `refusals:` on a rendered step, `carries_out:`, `agents:` (personas holding Pydantic AI's native `AgentSpec`), `entry`, `services`, `model`, `locale`, `language` with `locales`, and `app_version`; `FlowSpecError` listing every problem, before any model. An operation may instead be one line, `name: file.py:Class` (a `core.Lookup` or `core.Write`, `_declared_class`); a step may name `context:`, `capabilities:` and `consults:`; the flow `context:` and `current_datetime:`. `ContextSpec`: `facts` are whole top-level fields (a path, index or wildcard is refused); in v1 a step's context replaces the flow's whole; a `History` object at either level opts the step into the scoped view, where each member the step sets replaces the flow's (`under`) and its prompt compiles against that view (`ViewTemplate`: `{heard, state: {facts}}` only, so a hidden field, `services` or any other call attribute fails at load). `speech: structured` on a conversation step (`core.structured`); a pure read there needs no `say:`. Five step kinds: `collect`, `confirm`, `action`, `conversation`, `end`. There is no hand-built step and no application call class |
| `turn.py` | one caller turn as an event stream: `Turn`, `run_turn`, `take_turn`, `Barge`, the fixed lines, one holding line per caller turn (only for a tool or action still running after `hold_after`, never for a request count), `remind` (the silence reminder, on Pipecat's `on_user_turn_idle`), an unheard receipt said again first and then the step's agent reading the rest of the turn (`LocalAction.answers=False`; `wait` and `say_again` add nothing after it, `CallContext.receipt_said_again`), the `speaks_for` and `reads_back` handling that keeps a node's own operation from being echoed or asked for twice. Imports no Pipecat |
| `client/` | core's browser page, package data served by `serve_client`: `index.html` (Start, Hang up, the bot's `<audio>`, as Pipecat's vanilla examples do), `playout.js` (core's only addition: declares the capability with `setAboutClient`, watches the audio element and the receiver, reports `playout`), `sdk.js` (generated by `core/client/` from `@pipecat-ai/client-js` 1.13.1 and `@pipecat-ai/small-webrtc-transport` 1.10.8, unmodified; do not edit). `core/client/` (outside `src/`, not in the wheel) is its build: `package.json`, `package-lock.json`, `sdk.js` (the re-exports) |
| `observability.py` | Langfuse over OTel (the `tracing` extra); call `setup()` once, before any agent is built. Reads no `.env`: the launcher does. Each model request's tokens go to Langfuse once, as `usage_details` (`_Usage`): Pydantic AI's `gen_ai.usage.details.*` are a breakdown of the counts, which Langfuse would otherwise add to the total (`docs/research/reusable-core/token-accounting.md`) |
| `tests/` | `test_classes`, `test_history`, `test_clock`, `test_values` (the value types, the manifest clock, caller ID, refusal words), `test_actions` (class operations on `clinic_flow/classes.yaml`: one line each, the same guarantees, refused at load when they cannot be inferred), `test_context` (history and facts per step, a capability kept, consultations on `clinic_flow/consult.yaml`; v1 requests pinned whole in `golden/context.json` (`BLESS_PAYLOADS=1`); legacy `collect` frozen; the scoped view on `clinic_flow/scoped.yaml`: inheritance, load refusals, canaries in every automatic source, free text sent as said, node history by id, anchors past `turns: 1`, two calls on one flow, a trusted capability callback), `test_v2` (manifest version 2: every refusal named by its step, the short defaults and an explicit key kept, a step's context member by member, `context: none` keeping the turn and its own question, and `clinic_flow/structured.v2.yaml` and `scoped.v2.yaml`, version 2 twins that normalize to their version 1 files and send the same requests; `test_context`'s scoped checks and all of `test_structured` also run on the twins), `test_structured` (`speech: structured` on `clinic_flow/structured.yaml`: a local answer in one request, update semantics and atomic refusals, a read with new values in two, labelled dead ends for repeats and refetches, read results found/refused/absent/failed/cancelled, one decision per response, consent, consultations with a blank answer, a handback and a wrong or missing summary, a stale read-back not put again, partial output and cut words), `test_engine` (E1-E7: the turn engine's own promises on the public clinic: stall retry, holding lines, a late report, the date, errors), and the public boundary: `clinic_flow/` (a clinic written as models and functions, six manifests: `agent.yaml` controlled, `one_agent.yaml`, `handoff.yaml`, `code_node.yaml`, `reads_back.yaml`, `restated.yaml` (a lookup whose value said again confirms it)), `test_public`, `test_authoring` (the authoring contract on the three main patterns), `test_session` (atomic state, owned fields, isolation, stale work, evidence; a discarded flow is collected, a call carries on while other flows come and go, the same routing with another prompt shares nothing, one graph per compile), `test_nodes` (P1-P18: the retired reader modules are gone and never imported, hostile calls are refused, `reads_back`, the silence reminder), `test_delivery` (marks, clears, onsets, silent segments), `test_sdk` (one `Hooks` per step agent), `test_package`, `test_intents` (I1-I15: an intent through cancellation, reconciliation, idempotent retry, a thread that commits late, restore, goodbye, a cancel or failure during the current-version check, a repair said only to its caller), `test_outcomes` (O1-O9: pending is unknown, declared no, one sender per intent, no resurrected effect, resource kinds, loader checks, owed intents), `test_speech` (S1-S8: a rendered step says no model words; on a prose step, a claim no standing change backs is corrected), and `ticket_flow/` with `test_tickets` (R1-R14: a Spanish support desk with no phone, date or slot, a SQLite backend, its write step rendered, fresh-process reconciliation after a killed worker, every name renamed with core untouched, `choices.yaml`: two choices resting on different fields; `chained.yaml`: a choice resting on a choice; `rendered_choices.yaml`: a rendered step offering choices in the desk's words; a heard no is not left undone; R15: an offer's words go with its ids; R16: only `decline` turns a read-back down, and a receipt-set field rests on the receipt that set it; R17: `say_again`; R18: a changed resource stays on offer; R19: a read-back with no playback evidence is withdrawn once and stays withdrawn, and the caller can confirm, correct, ask or leave; R20: missing marks on a line that proved delivery are withdrawn as `unconfirmed`, never as a cut; R21: a pick counts on the offer for the final state; R22: a change asked for over a cut receipt is read in the same turn; R24: the desk as `speech: structured`, in `ticket_flow/v2.yaml`, manifest version 2; R25: `v2.yaml` keeps R7's fresh-process recovery, R8's renaming and R16's question-is-no-yes), `test_flow_rules` (F1-F7 on `ticket_flow/routed.yaml`: a language per call, a lookup carried out by code whose receipt moves the caller, a read-back put again in a new language, a greeting that reads back, a validator reading `heard`, every rule checked at load), `test_webrtc` (W1-W10: Pipecat's real SmallWebRTC transport and an aiortc peer acting as core's page, on the desk: heard only by the playout report, never by the mark's arrival; an output stopped and started again approves nothing that began before; forged, stale, wrong-call, late-started, silent segments count for nothing; an interruption cuts only what was still to send; a yes resolves once; a malformed `client-ready` reports nothing; `reports_playback` is truthful per transport), `client_check.py` (outside the suite: core's page, Pipecat's own client with `playout.js`, in headless Chrome, C1-C8: paused, paused midway and resumed, detached, blocked autoplay and interrupted read-backs are never heard). No use case code. `defects.py` is outside the suite: known defects that fail on purpose, each with its repair session (`python -m tests.defects`, exit code = defects left) |

**`use-cases/salon-graph/`**, the salon on core.

| Path | Owns |
|---|---|
| `bot.py` | the Pipecat pipeline around the salon (`run_bot`, which `server.py` also runs per call): STT, VAD and smart-turn, TTS, transport, tracing worker, the greeting, `DIARY_PATH` (the shared diary file), `DELIVERY` (`marks`, the default: a read-back is heard only by the transport's own report, a Twilio mark or the browser page's playout report; `simulated`: estimated playback may approve, for Coval's mark-less simulator only), `serve_client` before the runner's `main()`, and before the greeting (on `client-ready`, or on a phone line's connect) a line that can never confirm (`reports_playback`) hears `Locale.unconfirmable` and the call ends, the silence reminder (`on_user_turn_idle` after `IDLE_SECONDS`, 20 s, calls `node.remind()`), `VAD_STOP_SECONDS` (0.3 s default), `MAX_CALL_SECONDS` for a simulated call (`core.processor.Ceiling`, stopped in a `finally` around the pipeline), `playout` wiring the Twilio marks, `record_ending`, `latency_log` (Pipecat's own per-turn breakdown in the bot log: endpointing, transcription, agent step, synthesis) |
| `server.py` | the salon behind a bounded runner on one host: `uvicorn server:app --workers N`; `Admission` (SQLite `RUNNER_DB`: at most `MAX_CALLS` = 5 admitted or active over every worker, `<Reject reason="busy"/>` past it, one claim per CallSid, a dead worker's calls closed by a reaper), each worker's clients warmed once, its own drain on SIGTERM, loop-lag sampler, local-only `/ready`, `/stats`, `/calls`; `VOICE_DOUBLES=1` for the offline doubles; `CARRIER_NUMBER=1` offers the webhook's `From`; `live_counts` (live per-call objects, matched along the MRO: a call is a `FlowCall[SalonState]`), `MEDIA_HOSTS` (several runners sharing `RUNNER_DB` and `DIARY_PATH`: the webhook places consecutive calls on them in turn, `Admission.place`; a claim records `served_port`, where the stream really arrived), `LIVE_STATS=1` adding them to each worker's stats; `LIVE_FAULT=committed:<phone>` with `LIVE_FAULT_ARM=<file>` (the live worker-failure check only: the worker dies after that caller's commit, once the file exists) |
| `salon/agent.yaml` | the manifest: `state:`, `identity:`, `entry`, `locale`, `model`, the `robin` persona; five declared operations (`lookup_customer`, `book_appointment`, `move_appointment`, `cancel_appointment`, `record_complaint`, each with `args`, `readback`, `reconcile`, `matches`, `succeeds`, `assign`, `say`; `subject`, `resource`, `version`, `rejects`, `idempotent` and `recap` on the four writes, `changes: false` and `restated: confirms` on the lookup: the same number said back is a yes); `locale:` with the salon's own goodbye; four `conversation` steps (`concierge`, `verify`, `booking`, `complaints`), each with its `fields`, `choices`, `operations`, `exits`; `concierge` and `complaints` prose; `verify` `speech: rendered`, with `opens_with`, `asks`, `refusals` and `reads_back`; `booking` `speech: rendered` since session 4e, with `refusals`, `reads_back`, and its choices `appointment` (after no field) and `slot` (after service, day and time), each worded by its function's `say` |
| `salon/app.py` | `FLOW` (`SPEC.compile()`, at import) and `open_call`: one session per call, the carrier's number prefetched (`prefetch_number`), the diary and clock passed in as services |
| `salon/functions.py` | the typed domain functions `agent.yaml` connects to: `Salon` (the call's services: the diary and its key; a write's diary token is its `Intent` id; `note_intent`, `current_version`, `owed`), each operation's function (`lookup_customer`, `book`, `move`, `cancel`, `record_complaint`), its readback and reconciliation (`find_booking`, `find_move`, `find_cancel`, `find_complaint`; a refused or fenced receipt names the booking the request was about), `find_times` and `held_bookings` (each returns `say`, the salon's words for its offer: nothing free, which of two dates, the caller's own booking), `phone_refusal` (verify's words for a number it cannot look up), `slot_refusal`, `appointment_refusal`, `service_refusal`, `time_refusal` (booking's) |
| `salon/models/` | `shapes` (`Phone`/`SubmittedPhone`, `Service`, `Verification`, ...), `receipts` (`CustomerLookup`, `Saved`, `Held`, `Slots`, what the diary may say), `state` (`SalonState`, the complaint-request word list `_ASKING_TO_COMPLAIN`, `prefetch_number`) |
| `salon/diary.py` | `Diary`: customers, bookings, complaints, write results and intents in one SQLite file every worker shares (rollback journal: this SQLite predates the WAL-reset fix), a trigger holding the one-chair capacity, a move as one `UPDATE`, `outcome_or_void` (a reconciled write with no result is fenced), revisions checked on move and cancel (a call has seen the revision its own move made); offers in process memory, per call; `resolve_day`, the salon's day grammar ("day after tomorrow" with or without "the"); and the oracle that says no |
| `salon/prompts/` | one Handlebars prompt per step, the shared voice and diary rules (`_voice.md`, `_diary.md`), and `knowledge/` with the salon's own documents |
| `evals/` | `doubles`, `load`, `faults`: the offline Stage C runner checks (scripted model, energy STT, paced TTS; fake Twilio phones on a real websocket; the 1-2-5 ramp, with `--idle`, `--live` and `--tracemalloc` for memory runs; worker kills, a stale worker, overload, drain; F6: the placed pair on two runner processes with the live fault armed after placement). `placement`: arms `LIVE_FAULT_ARM` only once two active calls were claimed by different workers on different ports (offline F6 and the live pair). `calls`: the live text checkpoint, four scripted calls (T1-T4) on the real engine, checked on the diary. Live, billable, except `calls --recheck`. `cases`: reviewed reply cases shared by the live and offline checks. `line` and `matrix`: the harness arm of the N1-N13 native comparison, offline, on the real pipeline. `paired`: session 4's classifier-vs-node-agent semantic check, the same file run once per arm in its own checkout |
| `tests/` | `test_graph` (legal moves, prerequisite interposition, every node's first request pinned in `golden/agents.json`), `test_business` (the acceptance matrix A-L, verify's words said by code), `test_moves` (V1-V11: moves and cancels on one shared diary file with a second caller, refusals bound to their request, picks kept across a day change, no yes by repetition, each tool problem's next step, the live check's contender, a day kept beside a refused id, a day said two ways is one day, the caller's own booking is not taken, V12: an offer changed in the same turn is not said, V13: the booking just made is moved and cancelled with no second lookup), `test_requests` (K1-K8: booking requests per semantic turn, the session 4e replies against the 4f ones, same words and diary), `test_contract` (G1-G7: session 4g's two browser traces replayed, a time and its slot in one `collect`, a move over a cut receipt, `--table` for requests and repetitions), `test_walkthrough` (one booking call through every boundary), `test_continuation` (a turn joined over several caller pieces), `test_overlap` (timed barge-in and delivery replay), `test_state`, `test_diary`, `test_budget`, `test_traces`, `test_core` (direct engine and Pipecat adapter agree), `test_calls` (the live text checkpoint's own machinery, scripted), `test_marks` (M1-M6: marks through the real adapter, TTS base, FastAPI websocket transport and Twilio serializer over a fake phone), `test_browser` (BR1-BR5: `bot.py` run as a script serves core's page at `/voice/`; `run_bot` over SmallWebRTC with the offline doubles hears the greeting by the page's report, refuses a page that reports nothing before its greeting, and books once through two confirmed read-backs, the caller speaking; a number read back while the page's output was stopped confirms nothing, and is withdrawn as `unconfirmed`, not cut), `test_recovery` (the C26pPHK6 family, one budget per joined turn that grows with its pieces, cut read-backs said again: B4.1-B4.6), `test_ceiling`, `test_store` (B1-B9: worker processes on one diary file, judged from the file), `test_runner` (A1-A8: admission, the memory diagnostic, the live fault fired once, placement on two processes), `test_audit_export` (the runtime's own spans judged by the real `load_report` command), `script.py` (the scripted provider every test drives the real flow with; on a rendered step `say` is the agent's `wait`, and `text` is prose it may not send). `defects.py` is outside the suite, as in core |

**`use-cases/salone-concierge-graph-new/`**, the same salon on the simpler authoring contract: the Coval candidate beside salon-graph.

| Path | Owns |
|---|---|
| `bot.py`, `server.py` | copies of salon-graph's; the server's offline-doubles mode (`VOICE_DOUBLES`) is refused, since this build has no `evals` |
| `salon/agent.yaml` | manifest version 2 since session 4 of the typed nodes (`app_version` `2026.09.30-v2`), about 110 lines, no regex: steps keyed by id, every one `speech: structured` (no `collect`: each update rides the step's one decision), each seeing only its listed facts and its own last eight turns (`context: {facts: none, history: {scope: node, turns: 8}}`); `current_datetime` (Europe/Madrid, 14 labelled days), five operations as one line each (`operations.py:Class`, the same names `load_report` reads), four steps and a `stylist` that `concierge` and `booking` consult, the refund policy as a deferred capability. Its prompts name the structured outputs (`reply`, `record`, an operation's `update`, `reply` with `decline`), a prompt change the live run checks |
| `salon/state.py` | `SalonState` only: `customer_phone: core.Phone`, `customer: Customer` (the lookup's receipt, whole), `booking_day: date` (never past, from the calendar), `booking_time: time` or a part of the day, `complaint`/`wanted_outcome: core.Quoted`, `wants_refund` (replaces salon-graph's money regex) |
| `salon/receipts.py`, `salon/operations.py`, `salon/offers.py`, `salon/capabilities.py`, `salon/app.py` | `core.Receipt` models; the `Salon` services and five operation classes; the two choice functions; the refund bundle; `FLOW` and `open_call` (`caller_id=`) |
| `salon/diary.py` | salon-graph's diary with its day and time grammar removed: `find_slots` takes a `date` |
| `tests/` | `test_state`, `test_business` (every move structured: verify, the caller's number said back as a yes, book with one availability read, a repeat that reads nothing, move, cancel, a complaint read back, the locale's refusal words, a past date, stylist detours and a handback, a side question and a correction, refunds, a whole call at 1, 3, 1, 1 requests, every step's prompt naming no tool a structured step lacks, the requests pinned in `golden/payloads.json`; `--table` prints the whole call's bytes at the model boundary and at the Gemini adapter's mapping, offline), `test_audit_export` (the real `load_report` on this build's spans) |

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
| `bot.py` | the same pipeline as salon-graph's, with Faro's greeting (`faro.greeting`: the caller ID read back), `close_call` on disconnect, its own speech languages, and the salon's refusal of a line that reports no playback, before the greeting |
| `faro/agent.yaml` | the manifest, version 2 (`app_version` `2026.09.30-v2`): nine steps keyed by id (`identify`, `signup`, `onboarding`, `assessment`, `plan`, `menu`, `lesson`, the code node `help`, `coach`), each seeing only its listed facts; identify, plan, menu and coach `speech: structured`, signup and onboarding prose, assessment and lesson rendered; the coach sees the whole conversation (the exercise's words are no state field); thirteen operations, `language: explanation` with an English and a Spanish locale. Every save the learner's words feed is `carries_out`; every receipt that moves the learner says where (`route: next`) |
| `faro/` | also a package, so faro-tools can import it: `__init__` (`FLOW` compiled on first use, `open_call`, `greeting`, `close_call`), `state` (`FaroState`, plain data: no learner fact before `confirmed`, a new number only with the +34 the caller said), `functions` (the typed functions, their receipts, the services object `Faro`, `ACTIVE`: one call per learner), `curriculum` (the versioned placement bank, lessons, level rule, every line code says), `models` (the store's records, the phone and language types), `store` (SQLite: WAL, `synchronous=FULL`, one worker thread; `fixture`, `backup`, `bench`), `prompts/` (`_voice.md` and one per step) |
| `tests/` | `test_faro` (L01-L16, the acceptance cases, and F1-F11: tools per step and no structured prompt naming a tool it lacks, help makes an answer assisted, a language switch (`record` on identify, whose read-back words follow `asked_language`), hostile model output, delivery, a lost reply reconciled once, two calls side by side, call-local consent, no retired module in a fresh process; F10: an update rides its read-back, a question is no yes; F11: what lesson, coach and plan each see, help before the coach, the coach's grade against the store's `assisted` receipt, the route back to the lesson; `--table` prints the teaching path's bytes), `test_bot` (B1-B3: the page served, a reporting page greeted, one that reports nothing refused first), on a real SQLite file |

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
docs/research/reusable-core/session-4d-correctness/package-proof.sh   # the wheel, installed outside the checkout
cd core && uv run python -m tests.client_check                  # core's page in headless Chrome, offline
cd core/client && npm ci && npm run build                       # rebuild client/sdk.js from the pinned official packages
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
   mark came back (Twilio's `mark`, sent after its last media chunk, or the
   browser page's playout report: its output played without a break from
   before the segment's start until its playout passed the segment's RTP
   bound) with no
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

No classifier or solver runs anywhere: the active step's own agent reads the
caller's reply, and code decides whether that reading may authorise a write
(`ProposalLedger`, `CallContext.approve`, `ConfirmedOperation`). The legacy
reader modules and the hand-built step were retired in session 5, with Faro,
their last user (`docs/research/reusable-core/core-cleanup.md`).
`core/tests/test_nodes.py` checks they are gone and never imported. Faro's
new build has had no live run: its grading and step choices are unmeasured.
A conversation operation follows `route:` on immediate success, but start-of-turn
settlement only repairs state and speech; it does not follow that route. Faro's
`record_attempt` is its only reconciled/idempotent operation, so a lost reply
at a phase boundary can leave the current node behind the store's checkpoint.
See `docs/research/reusable-core/core-cleanup.md` for that limit.

The support desk's write step, the salon's `verify` step (since session 4d)
and `booking` step (since session 4e) are `speech: rendered`, so nothing they
say is model prose. Concierge and complaints are still `prose`; moving them is a prompt change that needs
live checks. `tests.defects` keeps a prose claim visible as a known limit.

No word list guards model speech any more: the claim patterns
(`Locale.claims`, `guarded`, the `unbacked` correction) were retired in the
simpler-authoring session. What is guaranteed is that receipts, recaps,
goodbyes and fixed lines are rendered by code, that model text beside an
operation's own speech is dropped (`Node.speaks_for`), and that nothing is
done without a heard read-back and a yes. A prose step that claims a change
nothing made is an evaluation failure, measured on live calls. `times_named` in
`salon/diary.py` still only steers which times a diary read prefers, and the
complaint-request word list in `salon/models/state.py`
(`_ASKING_TO_COMPLAIN`) is English only. Structured output validates shape,
not semantic accuracy; run the explicit live evaluation before making
accuracy or latency claims.

Session 4f's live calls left two behavior limits (`docs/research/reusable-core/session-4f.md`): `decline`
lives on the step that owns the operation, so a no said after a hand-over records nothing (a goodbye then
says the change was left undone, which is true); and a goodbye is the model's reading, so "Yes, that's
perfect. Can you..." was once taken as leaving. N12's relative rule still fails; its 50 ms ceiling passes.

The browser's playback evidence (session 4f follow-up) proves the page's audio element was attached,
playing, unmuted and holding the bot's stream from before a segment began until the browser's playout
passed its end; not that a speaker or a person heard it (system volume, the output device). Two private
fields carry it, each failing closed: `WebRTCPlayout` reads the last RTP timestamp sent from aiortc's sender
(pinned 1.15.0), and `playout.js` reads the receiver from `SmallWebRTCTransport`'s `pc` (1.10.8 declares
no accessor: the SDK capability that is missing). If either goes, nothing is heard, or the page declares
nothing and the call is refused before its greeting. Pipecat's stock prebuilt UI (`pipecat-ai-prebuilt`
1.2.1) has no runtime hook, so it cannot report, and a call from it is refused before the greeting.
Faro's `bot.py` serves the page and refuses such a call the same way.

In one of two live text runs of the session-4 wording, the `verify` step's
first request answered "I'm sorry, I cannot do that right now" to an
ordinary request, with nothing unusual about it; the second run of the same
case passed (`docs/research/reusable-core/session-4/ledger.md`, live text
check `text-v2`). Seen once in two, not yet reproduced or explained.
