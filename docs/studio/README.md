# SLNG Studio: design and plan

A local studio for slng-voice-core. One command starts it. It finds every agent in the repository, draws
its step graph, and (once the later phases land) lets you follow a call live, go back to an earlier turn and
try again from there, and change an agent's prompts and wiring from the page.

- **Design:** [SLNG Voice Studio canvas](https://claude.ai/artifact/RcRy8iV6yEFohokqe2GUzp): five boards,
  four of them clickable. It is private until someone shares it from the canvas's Share menu.
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
| 02 Live run | `#/live/<agent>/<step>` | The graph on the left, the inspector on the right. Who is active (caller, engine, bot) in the top bar. The active step in yellow, the steps it can move to marked "can go". Under the graph: who spoke when in this turn, and every turn as a button; picking an old turn shows the state, chat and graph as they were after it, with "Fork from here". |
| 03 Agents and runs | `#/runs/<agent>` | Every agent the engine found, compiled or not (with the loader's own error), and that agent's runs: channel, config version, turns, writes, the path through the steps, how it ended. |
| 04 Agent config | `#/config/<agent>/<step>` | One step at a time: prompt, fields, facts, operations, history window, exits and their purposes. Live checks (the loader's), the diff against the saved version, and what to do after saving: new runs only, or fork a run with the new config. |
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

### Agents and graphs (built in this PR)

`studio.discovery` reads each `agent.yaml` twice, both with no model and no key:

- `core.load` reads the shape. The graph comes from it: exits, consults, prerequisites (`satisfied_by`),
  an action's `then` and failure paths, an end step's `take_back`, and one end node the studio adds for
  every conversation step's `goodbye`.
- `FlowSpec.problems()` checks every step, exit, field, operation and prompt against the application's
  code. It imports the application's modules, so it runs in a process of its own (`studio.check`), with the
  use case's folder as its import root. Results are cached until a file in the folder changes.

Not drawn yet: an operation's `route:` (a receipt naming the next step). It is a `Literal` on the receipt
class, which only resolving the operation reads. Faro moves its learner mostly by `route:`, so its graph
looks thinner than it is. The fix is to resolve operations in `studio.check` and return their routes.

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

A call cannot be rewound: a booking made in turn 3 stays made when you look at turn 2. So the studio forks.

- After every turn the engine saves a snapshot: the step, the whole state, and the history
  (Postgres `snapshots`).
- **Viewing** an old turn is read-only: the page shows that snapshot and the events up to it. The call keeps
  running.
- **Forking** from turn N opens a new text run from turn N's snapshot, on the current config or an edited
  one. It is a new run with `forked_from` and `forked_turn`, never a change to the old one.
- Intents settled before turn N come along with their status. Intents still unknown come back unknown and
  are reconciled before anything else is sent, as core already does for a fresh process
  (`StepFlow.open(intents=...)`). The page says so in plain words: "Writes already made stay made."

Core already opens a call from a state and unresolved intents. What it lacks is opening one at a given step
with a given history; that is a small addition beside `StepFlow.open`, made in phase 4.

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

## 3. How it runs

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

## 4. Build order

| Phase | Delivers | Done when |
|---|---|---|
| 1 (this PR) | The design. `slng studio up`, compose, schema, start-up sequence, discovery with real loader checks, the event format, the API, the web shell with real agents and graphs | `python -m tests` in `studio/` passes, the page shows every agent and its graph |
| 2 | The observer hook in core. Text runs from the page: the worker, events to Redis and Postgres, the live view and the runs table filled from them | A text run on the salon shows every event in the table above, and its replay matches the live view |
| 3 | Voice runs in the browser (core's own page and playout reports), caller and bot lanes, heard marks | A browser call shows who spoke when and which lines were heard, from core's evidence only |
| 4 | Turn snapshots and fork from a turn | A fork from turn N of a salon run continues with turn N's state, and a write made before N is not sent again |
| 5 | Config editing: forms, checks before save, versions, the "unmeasured" mark on changed prompts | A broken edit cannot be saved, and every run links to the version it used |

## 5. Decisions waiting on you

- **Postgres and Redis clients.** Phase 2 needs a Python client for each (for example `asyncpg` and
  `redis`). Neither is in the lock today, and the repository asks before adding a dependency.
- **The Saans font.** It is licensed and not committed; `npm --prefix studio/web run fonts` copies it from a
  `slng-ai/frontend` checkout. Bundling it in this repository is your call.
- **Twilio runs.** The design lists them as a channel. Whether the studio should place or watch phone calls,
  or only browser and text runs, decides how much of `server.py` it reuses.
