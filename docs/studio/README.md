# SLNG Studio: design and plan

A local studio for slng-voice-core. One command starts it. It finds every agent in the repository, draws
its step graph, and (once the later phases land) lets you follow a call live, go back to an earlier turn and
try again from there, and change an agent's prompts and wiring from the page.

- **Design:** [SLNG Voice Studio canvas](https://claude.ai/artifact/RcRy8iV6yEFohokqe2GUzp): the screens on
  its first page, and five ways to draw a graph compared on its "Graph options" page. It is private until
  someone shares it from the canvas's Share menu.
- **Code:** [`studio/`](../../studio/README.md).

This page says what each screen shows, where in core each thing on it comes from, and the order to build
the rest in. Read the [architecture guide](../architecture/README.md) first if a word here is new: step,
exit, operation, intent, read-back.

## 1. What a person sees

The look: dark ground (brand ink `#15120D`), one yellow (brand `#FBE566`), square corners, 1 px rules.
Saans for words people read, Geist Mono for anything the engine produced. Each signal colour means one
thing: yellow is the engine and the next move, blue is the caller, orange is the bot, green is a receipt or
a healthy service, red is a failure or a write whose result is unknown. The tokens board in the design and
`studio/web/src/styles/tokens.css` hold the same values.

| Board | Page | Shows |
|---|---|---|
| 01 Start up | `#/boot` | `slng studio up` coming up: Docker, Postgres, Redis, the engine, every agent read, the web app. A segmented bar, a line that changes every few seconds (each one a real thing start-up does), and a failed step that says what to do. |
| 02 Live run | `#/live/<agent>/<step>` | Built for someone testing an agent with their microphone. The mic first: its level, its device, start and end the call. Then the graph, in one of two views (section 3): **Focus**, the step the call is in, where the caller came from and every move from here, back moves included; or **Map**, the whole agent. Under it, the **path over turns**: one row per step, one column per turn, so going back up the graph shows as the line climbing. Picking a turn shows the state, chat and graph as they were after it, with "Go back to T*n* and keep talking". The inspector on the right. |
| 03 Agents and runs | `#/runs/<agent>` | Every agent the engine found, compiled or not (with the loader's own error), and that agent's runs: channel, config version, turns, writes, the path through the steps, how it ended. |
| 04 Agent config | `#/config/<agent>/<step>` | One step at a time: prompt, fields, facts, operations, history window, exits and their purposes. A **Moves** tab: every move as a table, rows from and columns to. Live checks (the loader's), the diff against the saved version, and what to do after saving: new runs only, or fork a run with the new config. |
| 05 Tokens and parts | none | Colours, type, controls, step nodes in each state, edge styles. |

The inspector starts with the conversation and the state open, and expands to the model and the step. Each
section opens and closes on its own.

| Inspector section | Shows |
|---|---|
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

`studio.discovery` reads each `agent.yaml` with core's own loader and no model key:

- `core.load` reads the shape: the steps and what each declares.
- `studio.check`, in a process of its own with the use case's folder as its import root, runs
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

### Events (format built in this PR; publishing is phase 2)

`studio/src/studio/events.py` is the one format. Every event carries its run, a number (`seq`, from 0, no
gaps), its caller turn and the time since the run started.

| Event | What it says | Where in core | Today |
|---|---|---|---|
| `run.started` | agent, config version, channel, entry step, first state | `StepFlow.open` | the engine knows it |
| `caller.speech` | started, partial, final, stopped; whether it joins the turn | Pipecat VAD and STT frames; `CallContext.continues_turn` | frames exist; needs the processor to forward them |
| `step.entered` | step, where from, how (exit, consult, prerequisite, return, route) | the graph walk in `core.flow` | needs an observer |
| `model.request` | step, request number, budget | `core.turn.RunStarted`, `TurnBudget` | event exists |
| `model.delta` | the decision's words as they stream | `core.turn.TextDelta` | event exists |
| `model.decision` | output (`reply`, `record`, an operation, `hand_over`, `goodbye`, `done`, a read), its arguments, a refusal | `core.structured` | needs an observer |
| `model.ended` | tokens in and out, milliseconds | `core.turn.RunEnded`, Pydantic AI usage | event exists, usage needs adding |
| `state.committed` | the whole state, what changed, what was reset | `FlowCall.update` / `collect` | needs an observer |
| `intent.changed` | intent, operation, status, arguments, receipt | `core.state.Intent`, `Operation.run` | needs an observer |
| `bot.speech` | segment, queued, playing, delivered, cut, cleared; source and reason; the evidence | `CallContext` speech ledger, `SegmentMark`, playout reports | ledger exists; needs an observer |
| `turn.finished` | answered, interrupted, exhausted, failed | `core.turn.TurnFinished` | event exists |
| `run.ended` | goodbye, hang-up, ceiling, failed, stopped | `record_ending` | the launcher knows it |

"Needs an observer" means one small hook in core: a `CallContext` attribute holding a callable that core
calls at each of those points, `None` by default, never awaited in a way that can slow a turn, and never
able to change what happens. That is the only change to core the plan needs, and it is phase 2's first step.

### Going back a turn

A call cannot be rewound: a booking made in turn 3 stays made when you look at turn 2. So going back opens
a new run from that turn's saved state, and the old run stays as it was. Section 4 has the whole proposal:
what each turn saves, Redis for speed and Postgres to keep it, and how a voice call goes back without the
mic stopping.

### Editing the config

The file on disk is the source of truth; every form on the config page edits it. Saving:

1. writes the change to a scratch copy of the agent's folder,
2. runs `studio.check` on it, and refuses the save unless it comes back empty (the same check a launcher's
   compile makes),
3. writes the files, and stores the new version (a hash of `agent.yaml` and every file it names) in
   `agent_configs`.

Every run records the version it ran on, so a run can always be shown with the config it used.

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

## 4. Going back a turn while you talk (proposal)

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

**What core needs for it.** Opening a call at a given step with a given history (beside `StepFlow.open`,
which already takes a state and unresolved intents), and the observer hook from phase 2 to know when a turn
ended. Python clients for Redis and Postgres are new dependencies to agree on (section 7).

## 5. How it runs

```
slng studio up
  └─ engine (uvicorn, :7800)           serves /api and the built web app
       ├─ boot: docker compose up --wait postgres redis, then every agent.yaml
       ├─ discovery: core.load + studio.check per agent (own process)
       └─ runs (phase 2+): one worker per run, in the use case's own environment
            └─ the use case's flow, with core's observer publishing events → Redis → page
                                                                         └→ Postgres (kept)
```

A run's worker runs in the use case's own environment (`uv run --directory use-cases/<agent>`), as its
`bot.py` does, so the studio never needs an application's dependencies, and a crashing call takes nothing
else down.

## 6. Build order

| Phase | Delivers | Done when |
|---|---|---|
| 1 (this PR) | The design. `slng studio up`, compose, schema, start-up sequence, discovery with the loader's checks and the engine's own moves, the event format, the API, the web app with real agents in the four graph views and a working mic check | `python -m tests` in `studio/` passes, the page shows every agent in every view |
| 2 | The observer hook in core. Text runs from the page: the worker, events to Redis and Postgres, the live view and the runs table filled from them | A text run on the salon shows every event in the table above, and its replay matches the live view |
| 3 | Voice runs in the browser (core's own page and playout reports), caller and bot lanes, heard marks | A browser call shows who spoke when and which lines were heard, from core's evidence only |
| 4 | Turn snapshots in Redis and Postgres, going back a turn by voice (section 4) | Going back to turn N of a salon call keeps the mic open, continues from turn N's state, and a write made before N is not sent again |
| 5 | Config editing: forms, checks before save, versions, the "unmeasured" mark on changed prompts | A broken edit cannot be saved, and every run links to the version it used |

## 7. Decisions waiting on you

- **Postgres and Redis clients.** Phases 2 and 4 need a Python client for each (for example `asyncpg` and
  `redis`). Neither is in the lock today, and the repository asks before adding a dependency.
- **The Redis layer** in section 4 is a proposal: key names, the 7-day window and write-behind to Postgres.
- **The Saans font.** It is licensed and not committed; `npm --prefix studio/web run fonts` copies it from a
  `slng-ai/frontend` checkout. Bundling it in this repository is your call.
- **Twilio runs.** The design lists them as a channel. Whether the studio should place or watch phone calls,
  or only browser and text runs, decides how much of `server.py` it reuses.
