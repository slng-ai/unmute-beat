# CLAUDE.md

A Pydantic AI agency standing where Pipecat's LLM service would. Pipecat owns
transport, VAD, turn taking, STT and TTS; the agents own the conversation.

```
transport.input() -> SlngSTT -> user_aggregator -> PydanticAgentProcessor -> SlngTTS -> transport.output()
```

`README.md` is the long version: how to run it and every design choice with
its supporting link. Read it before changing how a turn works.

## Layout

| Path | Owns |
|---|---|
| `bot.py` | the Pipecat pipeline: STT, VAD and smart-turn, TTS, transport, tracing worker, greeting. The only runnable entry point |
| `core/state.py` | what every agency shares: `STRICT`, `VoiceState`, `CallContext` and its speech ledger, `Handoff`, `Question`, `Held` (what the caller could still take back, `hold`/`held`/`release`) |
| `core/graph.py` | `Node`, `Exit`, `LocalAction`, `Opening`, `route`, `moved_by`, `lent`, `model_clients` |
| `core/flow.py` | `Flow`: the steps table as a `pydantic_graph` graph with each step's legal moves; it walks every turn and refuses an undeclared move; `render()` draws it |
| `core/policies.py` | `TurnBudget`, the hooks (`one_action_per_reply`, `speak_by`, `request_clock`), `pick`, `schema_tool` |
| `core/history.py` | `paired` and `project`: what each provider request is actually sent |
| `core/turn.py` | one caller turn as an event stream: `Turn`, `run_turn`, `take_turn`, `Barge`, the fixed lines. Imports no Pipecat |
| `core/locale.py` | `Locale`: the four lines the turn engine says itself, in one language; `ENGLISH`. A call picks one by class (`CallContext.locale`) |
| `core/processor.py` | the Pipecat adapter: events in, `LLMTextFrame`s out, the turn span. The only `core` module that imports Pipecat |
| `salon/models/` | `shapes` (Phone, Date, Time, Id), `consent` (`Asked`, `ConsentInput`, `ReplyClassification`), `receipts` (what the diary may say), `state` (`SalonState`, `SalonCall`, `open_call`) |
| `salon/tools/` | one module per step (`identity`, `booking`, `complaints`, `desk`), the write boundary (`writes`), `Ask`, and `policy` (`ACTIONS`, `SPEAK_BY`). Each write is an `Operation`: `LOOKUP`, `WRITE_BOOKING`, `WRITE_COMPLAINT` |
| `salon/diary.py` | `Diary`: customers, slots, offers, bookings, complaints, in memory, and the oracle that says no |
| `salon/flow.yaml` | the salon as four `agent` steps: each one's prompt, module, exits and requirements |
| `salon/steps/` | one module per step, each giving `node(step)`: its tools, outputs and openings |
| `salon/nodes.py` | `BUILDER` (core's `AgentBuilder`, given the salon's information) and `build()`, `advance()`, `decided()`, `solve()`, the guards |
| `salon/instructions.py` | loads `flow.yaml` as `SPEC`; `instructions(name)` is a step's compiled prompt; `GREETING`; `COMPLAINT_GUIDANCE` |
| `salon/classification.py` | `SALON`, the salon's conversation: core's acts given the salon's information, which builds its narrow readings (`CONFIRMATION`, `APPOINTMENT_CHOICE`, `CHOSEN_OFFER`, `LAST_CHANGE`, `RETAINED_APPROVAL`, `CONVERSATION`, `DETOUR`) and decides on them; `reading_of`, the merged view the guards read; `SOLVER` (`SALON_INTERPRETER=solver`); `classify_reply`. Tools reach it through the call (`SalonCall.read_reply`), never by import |
| `salon/baseline.py` | the same salon as one prompt on Pipecat's own Gemini service, the tools on the same `Diary`; `VOICE_HARNESS=pipecat` puts it in `bot.py` in place of the harness |
| `core/classifiers.py` | `Classifier`: one direct model request over an `Exchange` (question, answer, facts), the author's context and classes; `ClassSet` and `output_model` build classes from data |
| `core/decisions.py` | typed meanings of a reply (`Accept`, `SwitchTask`, ...); a business subclasses for its own |
| `core/proposals.py` | `Proposal` (structural), `BaseProposal`, `ProposalLedger`: the one question a call waits on, and when it can be answered |
| `core/operations.py` | `Operation`: one business action, `@final run` = validate, settle, authorise, execute, validate receipt, record; `needs_approval` says whether a read-back yes is required; `operation_token` |
| `core/completion.py` | `Completion`, `FieldsPresent`, `ReceiptMatched`: a step is done only on evidence in the facts |
| `core/steps.py` | `CollectStep`, `ConfirmStep`, `ActionStep`, `ConversationStep`, `AgentStep` (a hand-built step) and `AgentBuilder` (builds its agent the same way for every use case), `End`, compiled by `StepFlow` into a nodes table; `FlowCall`, `ConfirmedOperation` |
| `core/flowspec.py` | flow folders: `FlowSpec` (YAML or JSON via `yaml.safe_load`, one spec class per step kind), each step naming its prompt file and `code:` module inside the folder; `FlowSpecError` listing every problem |
| `core/readings.py` | narrow readings: `Reading` (one classifier, when it applies, its label), `classify`/`choose`, `ChoiceReading` (`picked`: only an id among its own options counts), the generic class sets (`CONFIRM`, `TAKE_BACK`, `STILL_AGREED`, `CLOSING`, `REQUEST`), `read` (run the ones that apply, side by side), `Acts` (answer, take back, pick, goodbye: the fixed order) |
| `core/conversation.py` | a conversation's acts as classes a use case fills in: `ReadBack`, `FixedChoice`, `Offer`, `HeardValue`, `TakeBack`, `HeldApproval`, `Detour`, `Closing`; `Conversation` builds their readings, runs them, and decides in core's order |
| `core/desk.py` | `Desk`: what every step can do, for any use case (`hand_over_for`, `hang_up`, `end_conversation`, `clarify`, `switch_task`), built from a call type, a `Handoff`, the action names and an optional `Carry` |
| `core/interpreters.py` | `Interpreter`: at most once per turn, cached on the call, budgeted, never published stale; `ClassifierInterpreter` |
| `salon/agents.py` | the `AGENTS` table, compiled from `flow.yaml`; re-exports what tests import |
| `salon/prompts/` | one Handlebars prompt per step, the shared voice and diary rules, and `knowledge/` with the salon's own documents |
| `examples/` | `access` (a ten-step request) and `help` (one conversation step), built on `core` alone, each a flow folder (`flow.yaml`, `voice.md`, `prompts/`, `steps/`); `VOICE_AGENCY=access` runs one in `bot.py` |
| `utils/observability.py` | Langfuse over OTel; call `setup()` once, before any agent is built |
| `evals/ledger.py` | reads any build's Langfuse traces (harness, one-prompt baseline, graph build, LiveKit) into one ledger: requests, tokens counted once, calls in a row, latency, and each write with the question it answered (`write` spans from `core/operations.py`) |
| `utils/coval_sim/` | the Coval release check: starts `bot.py -t twilio` behind a cloudflared tunnel and lets Coval call it. Needs network and keys, never runs in CI |
| `tests/` | twenty-three assert-script modules, no pytest, no network |

**The split that matters:** `core/turn.py` runs a turn and knows nothing about
how it reaches a person; `core/processor.py` is the Pipecat adapter over it.
Anything about *running* a turn goes in `turn`, anything about frames goes in
`processor`. `tests/test_core.py` asserts the two agree and that no `core`
module but the processor imports Pipecat.

`core` imports nothing from `salon`. `salon.models` imports `core.state`;
`salon.tools` imports models, the diary and `core.policies`, and reads a reply
through the call; `salon.classification` imports the tools its acts run;
`salon.agents` imports everything; `bot.py` wires it to Pipecat.

State is replaced wholesale via `call.commit(**updates)`: one validated
candidate or nothing, never field-by-field assignment. Tools live with the step
they belong to and bind to `RunContext[SalonCall]`. The model-facing tool has a
`ctx` first parameter; the executor it shares with `advance()` takes `call`.

## Commands

Everything that builds an agent needs a model key at import: `--env-file .env`,
or the placeholder `python -m tests` sets.

```bash
uv sync                                                  # venv + deps
uv run python -m tests                                   # all twenty-three modules, offline, no .env
uv run --env-file .env python bot.py                     # voice, http://localhost:7860
uv run ruff format . && uv run ruff check . && uv run ty check .
uv run python -m utils.coval_sim run VkpkmRMV            # Coval release check, 18 real calls, billable
uv run --env-file .env python -m evals.ledger harness CA... -- graph trace:...   # cost of calls, from traces
```

`uv run python -m tests` turns `ALLOW_MODEL_REQUESTS` off *before* any module
is imported, so a test that forgot to override an agent raises instead of
billing. One module alone needs a key in the environment:
`GOOGLE_API_KEY=x uv run python -m tests.test_salon`.

Before calling anything done: `ruff check`, `ty check` and `python -m tests`
all clean. Paste the real output; don't round up.

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
   heard in full only if its audio ran out (`BotStoppedSpeakingFrame`) before
   the caller cut in. Without playback signals the engine errs towards a new
   turn.

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
