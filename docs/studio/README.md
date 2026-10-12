# SLNG Studio: design and plan

A local studio for slng-voice-core. One command starts it. It finds every agent in the repository, draws
its step graph, and lets you try an agent in a real run by text or by voice from the browser, follow every
run live, go back to an earlier turn and carry on from there with the state changed, change an agent's
prompts, closing lines and action routes from the page, and read its timings per agent and per run.

- **Design:** [SLNG Voice Studio canvas](https://claude.ai/artifact/RcRy8iV6yEFohokqe2GUzp): the screens on
  its first page, and five ways to draw a graph compared on its "Graph options" page. It is private until
  someone shares it from the canvas's Share menu.
- **Code:** [`studio/`](../../studio/README.md).
- **Plan:** [the decisions behind the second phase, how each piece works, and what was built](plan.md).

This page says what each screen shows, where in core each thing on it comes from, and the order to build
the rest in. Read the [architecture guide](../architecture/README.md) first if a word here is new: step,
exit, operation, intent, read-back.

## 1. What a person sees

The look: dark ground (brand ink `#15120D`), one yellow (brand `#FBE566`), square corners, 1 px rules.
Saans for words people read, Geist Mono for anything the engine produced. Each signal colour means one
thing: yellow is the engine and the next move, blue is the caller, orange is the bot, green is a receipt or
a healthy service, red is a failure or a write whose result is unknown. The tokens board in the design and
`studio/web/src/styles/tokens.css` hold the same values.

Three places at the top: **Agents**, **Runs**, **Metrics**. The boards are on the design canvas; the
pages are what was built from them.

| Board | Page | Shows |
|---|---|---|
| 01 Start up | `#/boot` | `slng studio up` coming up: Docker, Postgres, Redis, Jaeger, the store, every agent read, a runner, the web app. A segmented bar, a line that changes every few seconds (each one a real thing start-up does), and a failed step that says what to do. |
| 00 Agent: build and try | `#/agents/<agent>/build/<step>` | One agent. The steps on the left (the live run's step marked), the step's config in the middle (its prompt, the closing line of each operation it carries out, an action step's routes), and **Try it** on the right: a text run or a browser call on the config as saved, the conversation with what was heard, what the model is doing request by request, the state, the business actions. Save and Check in the bar; a run on an older config offers to carry on from its last turn on the new one. Tabs: **Operations** (every closing and goodbye line, with the receipt's fields and the line filled from its last real receipt), **Graph** (focus, map, moves), **Its runs**, **Its metrics**, **Full config**. |
| 02 Live run | `#/run/<run>` | One run, whole, live or read back: the conversation, every model request as it streamed and what it decided (and what core sent back), where the caller is, the state, the business actions, each finished turn with "go back here", and the path over turns. |
| 03 Runs | `#/runs` | Every run of every agent, newest first: agent, channel, start, turns, outcome, config version, the run it carried on from. Filters: agent (all first), channel, outcome. |
| 07 Start a call | Try it, on the agent's page | Text or voice from the browser; from the start, or from a saved turn of one of its last runs. The runner it lands on is the studio's choice. |
| 08 Go back and carry on | `#/back/<run>/<turn>` | A saved turn: what was said up to it, the state to carry on with (edited and checked by the agent's own types), what comes along, what starts fresh, and what the business still holds from after it. Carry on by text or by voice. |
| 04 Agent config | the Agents page | As board 00's middle column and its Operations and Full config tabs: closing lines with their receipt fields, action routes, prompts, the whole manifest, checked on a copy before saving. |
| 05 Tokens and parts | none | Colours, type, controls, step nodes in each state, edge styles. |
| 06 Trace and metrics | `#/metrics` | Where to find what is slow. The overview, filterable by agent, or one run, which names its agent: voice to voice, LLM first token and TTS first audio at p90, errors, every turn's requests with anything over its limit flagged, and latency by model (a model swapped between runs is two rows). Section 5. |
| 09 How a call runs | section 6 | Browser, studio, runners, Redis then Postgres, Jaeger. |
| 10 Agent facts (and 10b at phone width) | `#/agents/<agent>/facts/<fact>` | What the agent keeps track of on a call: its state, which the studio calls its **facts** everywhere. Each fact a card, grouped by who fills it in (the caller, a tool, the call's opening): its name in plain words, its kind in words, what it means to the model, where it is used. A fact's detail, adding one, and the empty, error and "edit in code" states. Section 2, "Editing the facts". |

The inspector starts with the conversation and the state open, and expands to the model and the step. Each
section opens and closes on its own.

| Inspector section | Shows |
|---|---|
| Timing | The selected turn as a waterfall, from the moment you stopped speaking: endpointing, STT final, LLM first token and done, TTS first audio; its voice-to-voice time against the 1.0 s target; and what was over its limit, with the span to open. The same time sits under each turn in the path, coloured. |
| Conversation | Caller and bot lines in order, each bot line tagged with where its words came from (model, or code: greeting, read-back, offer, receipt, goodbye) and whether it was heard. Engine moves in between: a record, a hand-over, a receipt. |
| State | The application state after the turn, field by field, with what changed marked. Then the intents (prepared, dispatched, unknown, succeeded, rejected, withdrawn) and receipts. |
| Model | The active step's request: number in the turn's budget, tokens, the decision streaming in, and the outputs this step may choose. |
| Step | What the manifest says about the step picked in the graph, and where it can send the caller, with each exit's purpose. |

## 2. Where everything comes from

Two rules keep the studio honest:

1. **It only listens.** Nothing the studio does changes what a call does. The live view is drawn from events
   the engine publishes; the store keeps the same events; a replay reads them back.
2. **It shows what core decided, in core's words.** An agent is "compiled" only if the loader accepts it,
   and a broken one shows the loader's `FlowSpecError`. A step is "can go" only if the manifest declares that
   move. A line is "heard" only on core's evidence (`Spoken.delivered`).

### Agents and graphs (built)

`studio.agents.catalog` reads each `agent.yaml` with core's own loader and no model key:

- `core.load` reads the shape: the steps and what each declares.
- `studio.agents.check`, in a process of its own with the use case's folder as its import root, runs
  `FlowSpec.problems()` against the application's code. When there are none it compiles the flow on Pydantic
  AI's `TestModel` (no provider is called) and returns `Flow.moves`: every move the engine allows. Results are
  cached until a file in the folder changes.

So the lines on the page are the engine's own, not a reading of the YAML that could miss one: a gate's
return to the step that was waiting, a consulted step's hand-back, an operation's `route:`. Each move is
labelled by why the engine allows it (exit, consult, hand-back, prerequisite, return, route, then, error,
unknown, take-back), and every conversation step gets a goodbye to one end node the studio adds.

Steps that all hand over to one another (exits both ways, three or more, the entry aside) form a **group**.
The salon's booking, moving, cancelling and complaints are one: 12 moves between them that the map draws as
one label, "any to any".

### Events (built: `core.events`)

`core/src/core/events.py` is the one format; the studio names it (`studio.events`). Every event carries
its run, a number (`seq`, from 0), its caller turn and the time since the listener was attached. A call's
listener (`CallContext.listen`) gets them off the turn's path, through a bounded queue: a slow listener
never slows a turn, and with no listener nothing is built.

| Event | What it says | Where in core |
|---|---|---|
| `run.started` | agent, config version, channel, entry step, first state, the run it carries on from | the opener (`Events.started`): `core.launch.run_call`, the runner's text calls |
| `caller.speech` | the words a turn (or a piece that joins it) gets | `CallContext.turn_started` |
| `step.entered` | step, where from, how where core knows (exit, consult, handback, resume) | `CallContext.commit`; the studio labels the rest from the engine's own moves |
| `model.request` | step, request number, budget | the turn engine's `before_model_request` hook |
| `model.delta` | speech, or a tool call's arguments, as they stream | the run's own stream (`core.turn`) |
| `model.decision` | each tool or output the model called, and each one core sent back, with why | the run's messages (`core.turn`) |
| `model.ended` | model, tokens in and out, milliseconds, time to first part | the `after_model_request` hook |
| `state.committed` | the whole state, what changed | `CallContext.commit` |
| `intent.changed` | intent, operation, status, arguments | `intent_for`, `set_intent`, `restore` |
| `receipt` | a validated receipt, as filed | `CallContext.note_evidence` |
| `bot.speech` | segment, queued, delivered (with its evidence), cut; source and reason | the speech ledger: `note_said`, the closing of a streamed segment, marks, playout reports, acknowledgements |
| `turn.finished` | answered, interrupted, exhausted, failed | `core.turn.Turn.events` |
| `turn.snapshot` | what going back to the end of the turn needs (`core.snapshot.TurnSnapshot`) | after each turn, and again once its speech is known heard |
| `run.ended` | goodbye, hang-up, ceiling, failed, stopped; events dropped | the opener (`Events.ended`) |

### Going back a turn

A call cannot be rewound: a booking made in turn 3 stays made when you look at turn 2. So going back opens
a new run from that turn's saved state, and the old run stays as it was. Section 4 has the whole proposal:
what each turn saves, Redis for speed and Postgres to keep it, and how a voice call goes back without the
mic stopping.

### Editing the config

The file on disk is the source of truth; every form on the config page edits it. Saving:

1. writes the change to a scratch copy of the agent's folder,
2. runs `studio.agents.check` on it, and refuses the save unless it comes back empty (the same check a launcher's
   compile makes),
3. writes the files, and stores the new version (a hash of `agent.yaml` and every file it names) in
   `agent_configs`.

Every run records the version it ran on, so a run can always be shown with the config it used.

### Editing the facts (built)

The facts are the fields of the state model `state:` names, in Python, so the studio edits code here, and
proves every edit the way it proves a YAML one:

1. **Read.** `studio.agents.facts`, inside the check's own process, reads each field's real annotation:
   `core.Phone` is a phone number, `core.Quoted` the caller's own words, `Annotated[date, core.NotPast]` a
   date not in the past, a nested model a group of details, a union "a time, or one of: morning,
   afternoon, evening". A validator the application wrote is shown read-only, with what its error says
   and, from the locale's `refusals`, what the caller hears. Where a fact is used comes from the manifest
   and the compiled operations (`binds`, `paths`, `assign`), the prompts that name `state.facts.<it>` and
   any other module that names it.
2. **Edit.** `studio.agents.state_source` finds the class with `ast` and replaces the text span of one
   field's statement, adds one at the end of the class (and a name to an import when a new kind needs
   it), or removes one with its note. What can change: a description (a prompt change: the model reads
   it, and the page says a live check is wise), the choices of an inline `Literal`, limits on plain text
   and numbers, whether a plain fact may be empty (with a starting value when it may not), the steps
   that fill it in (`fields:` and that step's `context.facts`, line by line), a new fact from ten kinds,
   and removing one that nothing uses. Everything else says "edit in code" and why: choices defined in
   another module, a kind made of two kinds, a rule written in code.
3. **Prove**, on a copy, before anything is written: the new module differs from the old in that fact and
   its imports only (compared as `ast`); `FlowSpec.problems()` on the copy is empty; the state's JSON
   schema is the old one with exactly that change, the fact's own schema rebuilt by Pydantic from the
   edit itself, never from the edited code; and the steps that fill it in are the ones asked for.
4. **Version and carry.** The config version includes the state module, so a saved fact is a new version.
   A runner imports code once: it says which code it carries for each agent when it links, refuses a call
   once the code on disk changed, and the studio places runs only on runners with the code on disk. After
   a save the studio starts a new runner of its own, waits until it is ready, then lets the old one finish
   its calls (at most its drain limit) and stop: a run started before the change keeps the facts it began
   with. A runner started by hand (`slng studio runner`) is not restarted; runs of that agent are refused
   with words until it is.
5. **Going back** to a turn saved with facts the agent no longer has is checked against the facts as they
   are now: the page says which facts are gone and offers to leave them out, and the agent's types refuse
   anything that no longer fits, by its name.

A rename is not offered: past runs saved under the old name could no longer go back, and code that reads a
fact by name (a choice function, an operation's alias) cannot all be found and proved. Adding the new fact,
moving its uses and removing the old one does it, each step proved.

One rule from this repository applies: **prompts are moved, not edited, without a live check**
(`CLAUDE.md`). The studio makes editing a prompt easy, so it must also make the rule visible: a version
whose prompts changed is marked "unmeasured" until a live check passes on it.

## 3. Drawing the graph

The salon has 7 steps and 40 moves; nearly every step reaches every other. Drawn whole, that is a tangle:
studies of graph readability find box-and-line drawings get hard to read on dense graphs even with few steps,
and that tables beat them there for everything but following a path. So the studio draws the same graph four
ways, each where it fits (the design's "Graph options" page compares five; Mermaid's whole-graph drawing,
option A, was the one left out).

| View | Where | What it does | Built with |
|---|---|---|---|
| **Focus** (B) | Live, default | The step in focus in the middle; where the caller came from on the left (in a run, the real path; without one, every step that can lead here); every move from here on the right, grouped: stays here (the step's operations), hand over, goes back, needs first, goes on by itself, consult, end. A move to a step the run has already been in is marked "was here T*n*". Stays readable whatever the agent's size. | React and CSS |
| **Map** (C) | Live, one click | The whole agent. Groups drawn once, a line into or out of a group when every member makes that move, goodbyes left out (the end node says every step can end). Lines routed with right angles; the moves out of the step in focus lit and labelled. | React Flow (`@xyflow/react` 12.11.6) and ELK (`elkjs` 0.11.1), loaded only when the map opens |
| **Path over turns** (E) | Live, under the graph | One row per step the call has been in, one column per turn. Going back up the graph is the line climbing. Each turn is a button: look at it, or go back to it and keep talking. | React and SVG |
| **Moves table** (D) | Config, Moves tab | Every move, rows from and columns to, at any size. | React |

ELK is the large one: the map's chunk is about 1.6 MB (0.5 MB gzipped), so it loads when the map first
opens, and the page itself stays about 220 KB.

## 4. Going back a turn while you talk

Agreed on 2026-10-10. How it is built on Pydantic, what a snapshot holds and the order a call is
restored in are in [the plan](plan.md), section 5.

The person testing an agent talks to it through their microphone. When it goes wrong at turn 4, they want to
go back to turn 2 and try again, by voice, in about the time it takes to click. This section proposes how;
it is the decision phase 4 makes.

**What is saved.** At the end of every turn (`turn.finished`) the engine saves a snapshot: the step the caller
is in, the whole application state, the call's history as Pydantic AI serialises it, the intents with their
status, and the question the call is waiting on, if any.

**Two layers.**

| Layer | Holds | Why |
|---|---|---|
| **Redis** (hot) | `studio:run:<id>:events`, a stream (`XADD`) of the run's events; `studio:run:<id>:turn:<n>`, each turn's snapshot as JSON; `studio:run:<id>:head`, the latest turn. Kept 7 days after a run ends. | The live view reads the stream as it grows; going back reads one key, a few milliseconds, so it feels instant on a call. |
| **Postgres** (durable) | `events` and `snapshots` (schema.sql), filled by a persister that reads the stream as a consumer group. | Nothing is lost when Redis is flushed or a run is older than a week: a replay or a fork reads Postgres when Redis no longer has it. |

The engine writes to Redis only, after the turn's last word has gone out, so saving never slows a turn;
Postgres is written behind it.

**Going back, by voice.**

1. The tester picks T2 in the path and presses "Go back to T2 and keep talking".
2. The engine reads `turn:2` from Redis (or Postgres), opens a new call from it: a new run with `forked_from`
   and `forked_turn`, on the same agent version or an edited one.
3. The browser connection stays up: the new call takes over the same audio session, so the mic never stops.
4. If T2 ended waiting on a question (a read-back), it is asked again, fresh, so the tester hears where they
   are; otherwise the bot waits for them to speak.
5. The old run stays as it was, and the path strip shows the branch.

**What going back cannot undo.** A booking made at T3 stays made in the business: going back changes the
conversation, not the world. Intents settled before T2 come along as settled; any still unknown come back
unknown and are reconciled before anything else is sent, as core already does for a fresh process
(`StepFlow.open(intents=...)`). The page says so where the tester decides.

**What core does for it** (built): `FlowCall.snapshot()` at the end of every turn, and
`StepFlow.open(resume=snapshot, facts=edited)` to carry on: the history and the speech ledger first, then
the state validated whole. The plan's section 5 has the details; the go-back page is the studio's.

## 5. Timing and traces: finding what is slow

A person testing an agent needs to see, turn by turn, why a reply took long: the endpointing, the
transcription, the model, or the voice. The numbers come from OpenTelemetry spans the call already makes,
collected locally, so nothing is estimated and nothing needs a cloud account.

**What is collected, and from where.**

| Number | Span | Attribute |
|---|---|---|
| Voice to voice (you stopped, the bot's first sound) | Pipecat `turn` | `turn.user_bot_latency_seconds` |
| Turn number, interrupted | Pipecat `turn` | `turn.number`, `turn.was_interrupted` |
| STT first byte, TTS first audio | Pipecat `stt`, `tts` | `metrics.ttfb` (seconds), `gen_ai.request.model`, `gen_ai.provider.name` |
| LLM first token | Pydantic AI model request, or Pipecat `llm` (the baselines) | `gen_ai.client.operation.time_to_first_chunk`, or `metrics.ttfb` |
| LLM request duration, tokens | Pydantic AI model request | `gen_ai.operation.name` = `chat`, the span's duration |
| Agent, run | every span of a run | `slng.agent.id`, `slng.run.id` (`core.observability.setup`) |
| Errors | any of them | `otel.status_code` = `ERROR` |

The span names and attributes were checked against `pipecat.utils.tracing` and a live call. Pydantic AI
writes each streamed request's time to first chunk on its span, so a core agent's LLM first token comes from
the trace; a request that did not stream is judged on its duration (the "LLM request" limit). An STT or TTS
request with no first byte is not judged: its length is the audio's.

**How it gets there.** `slng studio up` starts Jaeger beside Postgres and Redis (127.0.0.1 only: OTLP in
on 4318, its UI on 16686, traces kept in memory) and starts its runner with
`OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318`, so every run from the Agents page sends its spans
there: `core.observability.setup` adds an OTLP exporter for it, with or without Langfuse, batching spans
off the turn's path. A text run has a `conversation` span and a `turn` span per turn of the runner's own,
so its requests group the same way. `slng studio bot salon` does the same for a use case's own `bot.py`.
`studio.metrics` reads Jaeger 2.21's query API v3 (the older JSON API is gone), asks for one agent or one
run by attribute, puts each request in the turn it belongs to, and flags it against its limit.

**What counts as slow** (defaults for every agent; per agent with the config page later):

| | Slow | Over the limit |
|---|---|---|
| Voice to voice | > 1.0 s | > 1.5 s |
| STT first byte | > 250 ms | > 400 ms |
| LLM first token | > 600 ms | > 700 ms |
| LLM request, with no first-token time | > 1.0 s | > 1.5 s |
| TTS first audio | > 300 ms | > 400 ms |
| LLM requests in one turn | > 1 | > 2 |

**Built:** Jaeger in compose and in the start-up sequence, core's OTLP export with the agent and run on
every span, `studio.metrics` with its tests, `/api/metrics` (`?agent=`), `/api/metrics/run/<run>`, and the
Metrics page: the overview with its agent filter, one run's view, p90 tiles, latency by model. Not yet:
the endpointing time as its own number (Pipecat records it inside the turn, not as an attribute), and
per-agent limits.

## 6. How it runs

```
slng studio up
  └─ engine (uvicorn, :7800)           serves /api, core's voice page (/voice-sdk) and the built web app
       ├─ boot: docker compose up --wait postgres redis jaeger, the store, every agent.yaml, a runner
       ├─ catalog: core.load + studio.agents.check per agent (own process)
       └─ runners (voice_runner, :7810 and up): every use case's core.Entry, loaded once
            ├─ text turns and WebRTC calls (the browser's audio goes straight here)
            ├─ each call's events → one websocket per runner → the studio → Redis (7 days) → the page
            │                                                                 └→ Postgres (kept)
            └─ spans → Jaeger (OTLP), read back for Metrics
```

The runners import the use cases (the workspace has one environment), so the engine itself never does:
it reads agents through `studio.agents.check`, in a process of its own. A runner that fails takes its own calls
with it and nothing else. More runners carry more calls at once (`slng studio runner`); the studio places
each new run on the least busy one that is ready.

## 7. Build order

The first PR delivered the design, `slng studio up`, discovery, the event format, the graph views and
the Metrics page. The second phase, in order, with the decisions taken on 2026-10-10, is in
[the plan](plan.md): metrics per agent, the listener hook in core, one run function for every use
case, the runner service, stored runs and the Agents page, going back a turn, and the editor. All of
it is built; the plan's section 9 says where the build differs and what was checked live.

## 8. Decisions

All of the decisions this section used to list were taken on 2026-10-10 and are recorded in
[the plan](plan.md), section 1: a runner service, a listener hook in core, the studio as the only
writer to Redis (`redis`) and Postgres (`asyncpg`), the Redis plan as proposed, going back in core,
business data left as it is, browser calls only, and the Saans font left out.
