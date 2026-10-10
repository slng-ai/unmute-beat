# SLNG Studio: the plan for runs, going back, and per-agent metrics

This is the plan for what comes after the first studio PR. It records the decisions taken on
2026-10-10, says how each piece will work, and gives the order to build them in. Nothing here is
built yet, apart from what [the studio README](../../studio/README.md) lists.

The screens are on the [design canvas](https://claude.ai/artifact/RcRy8iV6yEFohokqe2GUzp): boards
00 (agent: build and try), 06 (metrics), 07 (start a call), 08 (go back and carry on), 09 (how a call
runs) and 04 (config, with action steps and closing lines).

## 1. Decisions taken

| # | Question | Decision |
|---|---|---|
| 1 | Where does a studio call run? | In a **runner service**: a FastAPI app that loads every agent and carries calls. One or many copies, scaled up and down. Pipecat now, LiveKit later. |
| 2 | A listener hook in core? | **Yes**: async, off by default, no new dependency. Built first, because the live view and going back both need it. |
| 3 | Who writes Redis and Postgres? | **Only the studio.** Runners send their events to the studio. |
| 4 | Which Python clients? | **`redis`** (the official client, with asyncio) and **`asyncpg`**, in the studio package only. |
| 5 | The Redis plan | **As proposed**: every event and a snapshot per turn in Redis for 7 days, copied to Postgres behind it. |
| 6 | Going back in core | **Yes**, planned in section 5 below with the Pydantic pieces it uses. |
| 7 | Business data when going back | **Left as it is**, and said on screen. Going back can also change the state before carrying on. |
| 8 | Channels | **Browser calls only**, over WebRTC (Pipecat's SmallWebRTC), plus text runs. No phone calls. |
| 9 | The Saans font | **Left out** of the repository. |
| 10 | Closing lines | The editor shows and edits each operation's closing line (section 7). |

## 2. How the studio is laid out

Three places at the top, so nothing is hidden behind a single agent:

- **Agents.** Pick one agent. Its page has its config on the left and a run on the right ("build and
  try"): change a prompt, save, start a call, see each turn. A change made during a run offers
  "save and carry on from the last turn" on the new config. Tabs on the same page open its graph,
  its runs, its metrics and the full config. The old Live and Config screens are the full-size
  versions of these two halves.
- **Runs.** Every run of every agent, newest first, filterable by agent, channel and outcome.
- **Metrics.** An overview filterable by agent, and one run's turns and spans. The agent filter is
  only on the overview: a run belongs to one agent, so its view names the agent instead.

## 3. The runner service

**What it is.** A FastAPI app, run as one or more processes. Each process loads every agent in the
repository once, warms its model clients once, and carries up to `MAX_CALLS` calls at a time on one
asyncio loop. The studio never carries audio.

**Endpoints.**

| Endpoint | Does |
|---|---|
| `GET /agents` | Each agent it loaded: id, version, whether it needs a heard yes |
| `POST /calls {agent, channel, config?, resume?, caller_id?, delivery?}` | Admits a call or answers `429 busy`. Returns `{call_id, offer_url, ice_servers}`; `offer_url` names this exact process |
| `POST /calls/{id}/offer` and `PATCH /calls/{id}/offer` | The browser's WebRTC offer and its ICE candidates, handed to Pipecat's `SmallWebRTCRequestHandler` |
| `POST /calls/{id}/turns` | A text run's next caller turn |
| `DELETE /calls/{id}` | Hang up: the same path as a disconnect |
| `GET /ready` | 503 while warming, stopping, or full. What a load balancer or autoscaler reads |
| `GET /stats` | Active calls, capacity, loop lag, stopping or not |

**One run function, not five bots.** Today each use case's `bot.py` repeats the same pipeline and
differs in about five places (the greeting, the services object, an on-open and on-close hook, the
speech languages). The plan moves the shared body into core, as `core.processor.run_call(entry,
transport, ...)`, and gives each use case a small `entry.py` naming its flow, how to open a call, its
greeting and its hooks. Each `bot.py` shrinks to a few lines and keeps working on its own. The runner
imports the five `entry` modules by package name.

**Scaling.** Copies of the runner sit behind a load balancer. One that is full or stopping says so on
`/ready` and gets no new calls; on SIGTERM it finishes the calls it has (the salon's `server.py`
already does this, and its warm-up, drain and readiness code is what the runner reuses). Pipecat keeps
a WebRTC connection in the memory of the process that answered the offer, so the browser sends its
offer and ICE to the `offer_url` it was given, never to the load balancer at random.

**WebRTC beyond this machine.** Today no STUN or TURN server is set, so calls work on localhost and a
local network. A runner on another network needs `PIPECAT_ICE_SERVERS` and, behind a NAT, a TURN
server. This is the main reason LiveKit (which routes media itself) suits a large fleet better.

**LiveKit later.** `run_call` takes a transport that is already built. A Pipecat WebRTC adapter
builds one now; a LiveKit adapter (Pipecat's own LiveKit transport, or a LiveKit Agents worker on the
model of [unmute-core](https://github.com/slng-ai/unmute-core)'s adapters) can be added without
touching the rest. Every adapter must supply playback evidence, or core will confirm nothing on it.

## 4. The listener hook in core

Core gets one optional listener per call, set by whoever opens it:

```python
class CallListener(Protocol):
    async def event(self, event: CallEvent) -> None: ...
```

- It receives the events `studio/src/studio/events.py` defines today: step entered, model request
  and its deltas, tool result, state committed, intent changed, bot speech and its delivery, turn
  finished. Core imports no studio code, so those models move into core (`core.events`) and the
  studio imports them from there.
- Core calls it without waiting on it: events go on a bounded queue a background task drains, so a
  slow listener can never slow a turn. A full queue drops the event and counts it.
- With no listener set, nothing happens and nothing is imported.
- The runner's listener sends the events to the studio over one WebSocket per runner. The studio
  writes them to a Redis stream and fans them out to the live view.

## 5. Going back and carrying on, with Pydantic

Verified against the installed versions, pydantic-ai 2.46.0 and pydantic-graph 2.46.0.

**What has to be saved.** Where the caller is lives on the call, not in the graph: each turn is one
short `pydantic_graph` run with no graph state (`core/src/core/flow.py`). pydantic-graph 2.46 has no
persistence module (`BaseStatePersistence` and its snapshots belonged to the old graph API), and none
is needed. What a turn leaves behind is all on the `FlowCall`:

| Part | Where it is | How it is saved |
|---|---|---|
| Step, `pending`, `consulting`, the application's facts | `call.state`, a Pydantic model | `model_dump_json()` |
| Message history, with core's step and turn tags | `call.messages` | `ModelMessagesTypeAdapter` (round trip checked, tags kept) |
| What the bot said, and whether each line was heard | the speech ledger, plain dataclasses | a `TypeAdapter(list[...])`, without the in-process clock values |
| Every action and its status | `Intent` models in a private dict | `model_dump()` each |
| Validated receipts | `Evidence` dataclasses | their fields, all JSON-safe |
| Turn number, when each step was reached, the caller's words by turn | private fields | plain values |

The speech ledger matters: core rebuilds the bot's side of the history from it. A test restore with
the messages alone sent the model a history with every bot line blank; with the ledger restored too,
the history was identical to the original call.

**The snapshot.** One Pydantic model, saved by the studio at the end of every turn:

```python
class TurnSnapshot(BaseModel):
    model_config = ConfigDict(ser_json_bytes='base64', val_json_bytes='base64')

    schema_version: int
    run_id: str
    turn: int
    app_version: str
    manifest_hash: str          # a resume onto a changed flow is refused, or confirmed by the tester
    call_now: datetime          # the call's clock, so "Thursday" means the same day
    agent: str
    pending: str | None
    consulting: str | None
    facts: dict[str, Any]       # validated against the flow's state type on restore
    messages: list[ModelMessage]
    speech: list[SpokenRecord]
    evidence: list[EvidenceRecord]
    intents: list[Intent]
    arrived: dict[str, int]
    inputs: dict[int, str]
```

`FlowCall.snapshot() -> TurnSnapshot` builds it; the listener hook delivers it with "turn finished".
Core writes nothing itself.

**Carrying on.** `StepFlow.open(..., resume=snapshot, facts=edited)` opens a new call from it, in
this order:

1. The call's clock is the snapshot's.
2. History, the speech ledger (all of it in the past), the turn number and the receipts are put back.
3. Only then the state, edited or not, is validated whole, with the same context core always uses
   (today, now, the caller's words, and the snapshot's facts as `before`). An edit the application's
   types refuse is shown on the page, and nothing opens. Doing this after the history is back matters:
   a `Quoted` field checks the caller's words, which come from the history.
4. The step, `pending` and `consulting` are set if each names a step of the flow.
5. Actions go through the `intents=` path core already has: one that was sent and not settled comes
   back unknown and is checked with the business before anything new is sent. One only prepared is
   dropped, so its step asks again.
6. Offers and a waiting read-back are never restored. The step offers again for the state as it is
   now, and the read-back is asked again, fresh: a yes never carries across, which a test resume
   confirmed (the old yes booked nothing; the booking was read back again).

**What going back cannot undo.** A booking made after turn N stays in the diary. The page lists what
was done after N, so the tester knows the replay may meet it.

**Two limits to settle while building it.**

- A read-back core gave up on (`withdrawn`) is dropped on restore today, which gives it a fresh
  allowance. Keeping it withdrawn needs a small change to `Intent` restore.
- When a turn starts by settling an unknown action, core repairs the state and the speech but does not
  follow that operation's `route:` (the known limit in CLAUDE.md). A resumed call meets this the same
  way a fresh process does.

## 6. Metrics per agent

- **Tag every span with its agent and run.** `core.observability.setup(session=, trace_name=)` gains
  `agent=`, stamped on every span as `slng.agent.id`, with the run as `slng.run.id`. `service.name`
  stays one value for the whole process, because a runner carries every agent.
- **Filter in Jaeger, not in the page.** `studio.metrics.collect(agent=...)` adds
  `tags={"slng.agent.id": agent}` to Jaeger's query; `/api/metrics?agent=` exposes it.
- **The numbers at the top.** Voice to voice, LLM first token, TTS first audio, each at **p90**,
  and errors.
- **Latency by model**, at the bottom of both views: one row per STT, LLM and TTS model (and turn
  detection), keyed by kind, model and provider, with requests, first byte p90, total p90, errors and
  the share over the limit. Each span names its model (`gen_ai.request.model`), so a model swapped
  between runs shows as two rows, with the runs and dates each covers, and the overview says when the
  period mixes models. One run's view is always one set of models.
- **Known gap.** Pydantic AI's model requests record a duration but no first token, and core's agents
  make no Pipecat `llm` span (only the two baselines do). Until core's listener reports the first
  delta (step 2), a core agent's LLM row shows its total and leaves first token empty, rather than
  passing the total off as a first token.

## 7. Closing lines

Every operation can end with a sentence code says after it works, filled from its receipt: `say:`,
with `{field}` placeholders over the receipt's fields (`You're all set for {spoken}.`). The model
never says a result. `recap:` is the line a goodbye says later. A refusal says nothing and goes back to
the step's agent; an unknown outcome says the locale's unsettled line. An action step has no prompt
and no model: it runs one operation, says its closing line, and moves on by `then`, `on_error`,
`on_unknown`, or `returns`.

The editor shows, for each operation: the closing line, the goodbye line, the receipt's fields as
buttons to insert, and the line filled from the last real receipt. Core does not check placeholders
when it loads a manifest, so the studio does: a field the receipt does not have (spoken as a gap), an
unclosed brace, a `recap` on an operation that changes nothing.

unmute-core's tasks have the same idea with a different shape (`{{result.x}}` and `{{variable}}`, up
to five lines picked in turn, and confirm questions as templates; its PR 17 added the confirms, the
opening line, and replies to any tool result). Lining the two up is a separate decision, not part of
this plan.

## 8. Build order

| Step | Delivers | Done when |
|---|---|---|
| 1 | Metrics per agent: `setup(agent=)`, the `slng.agent.id` and `slng.run.id` tags, `collect(agent=)`, p90 tiles, the per-model table on both views | The Metrics page shows only the chosen agent's turns, and a model swap shows as two rows |
| 2 | The listener hook in core, and `FlowCall.snapshot()` | A scripted salon call delivers every event in order and a snapshot per turn; no listener means no change in core's tests |
| 3 | `core.processor.run_call` and one `entry.py` per use case; each `bot.py` a few lines | Every use case's tests pass, and `bot.py` still runs on its own |
| 4 | The runner service: the endpoints above, admission, warm-up, drain, `/ready` | Two runner processes take calls from the studio, and one stopping finishes its calls and takes no new ones |
| 5 | The studio stores events and snapshots (`redis`, `asyncpg`), the live view and the Runs page read them, the Agents page starts runs | A text run and a browser call from the Agents page show live, and appear on Runs filtered by agent |
| 6 | Going back: `StepFlow.open(resume=, facts=)`, the go-back screen, an edited state checked before it opens | Going back to turn N of a salon call carries on from N's state, an edit the app refuses opens nothing, and nothing sent before N is sent again |
| 7 | The editor: closing lines, action steps, checks, save to `agent.yaml`, versions | A broken closing line is caught before save, and every run names the version it ran on |

Each step is its own PR, with `ruff`, `ty` and `python -m tests` clean in every member it touches.
