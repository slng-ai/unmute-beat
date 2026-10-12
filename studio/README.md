# slng-voice-studio

A local studio for slng-voice-core. Pick an agent from the repository, change its config, and try it in a
real run, by text or by voice from the browser. Every run is kept: follow it live, read it back, go back to
any turn, change the state and carry on. Timings per agent and per run come from the run's own traces.
Design and plan: [docs/studio](../docs/studio/README.md).

## Run it

Needs Docker with compose v2, Node 22, and the model and speech keys in the root `.env` (`GOOGLE_API_KEY`,
`SLNG_API_KEY`), as every `bot.py` does. From the repository root:

```bash
uv sync --all-packages                          # once
npm --prefix studio/web ci                      # once
npm --prefix studio/web run fonts               # optional: Saans, from a slng-ai/frontend checkout next to this repo
npm --prefix studio/web run build               # the page the engine serves
uv run --directory studio slng studio up        # http://localhost:7800, ctrl+c stops everything
```

| Command | Does |
|---|---|
| `slng studio up` | Starts the engine, which serves the page at once and brings up Postgres, Redis and Jaeger behind it, opens the store, reads every agent and starts one runner (the loading screen follows along). Ctrl+c stops the runner after its calls, then the containers; the data stays. |
| `slng studio up --dev` | Also runs Vite with hot reload on :7802, sending `/api` to the engine. |
| `slng studio up --no-services` | Leaves Docker alone. Runs are kept in memory until the studio stops, and no timings are collected. |
| `slng studio up --no-runner` | Starts no runner; start them yourself with `slng studio runner`. |
| `slng studio runner [--port N] [--max-calls N]` | One more runner, linked to the studio that is up: more calls at once. |
| `slng studio down [--volumes]` | Removes the containers, and with `--volumes` every stored run. |
| `slng studio status` | The containers and the engine. |
| `slng studio agents` | Every `agent.yaml` and whether the loader accepts it, offline. Exit 1 if any does not. |
| `slng studio bot <use case>` | Runs that use case's `bot.py` with its traces sent to the studio: for a transport the page does not own (`-t twilio`). |

## What it does

- **Agents.** One agent at a time. **Build and try**: the steps on the left, the step's config in the
  middle (its prompt, the closing line of each operation it carries out, an action step's routes), a run
  on the right. Start a text run or a browser call, talk, watch each request stream and what the model
  decided, the state, the business actions and their receipts. Save, and the change is checked on a copy
  first; the next run uses it. A run on an older config offers to carry on from its last turn on the new
  one. **Operations**: every operation's closing line (said after it runs, filled from its receipt) and
  goodbye line, with the fields to insert and the line filled from a real receipt. A closing line can be
  one line or a line per result (**by result**): one when the customer is found, another when they are
  new, another when the service says no. Each line offers only the fields its own result has.
  **Graph**, **Its runs**, **Its metrics**, **Full config** (the whole manifest, checked by the loader).
- **Runs.** Every run of every agent, newest first, filterable by agent, channel and outcome. A run opens
  its full view: live while it goes on, read back from the store once it is over.
- **Going back.** Any turn of a run: what was said up to it, the state to carry on with (edit it; the
  agent's own types check it, and a refused edit opens nothing), what comes along, what starts fresh, and
  what the business still holds from after that turn. Carry on by text or by voice.
- **Metrics.** The overview of recent runs, filterable by agent; one run's view, which names its agent.
  Voice to voice, LLM first token and TTS first audio at p90, errors, every turn's requests, and latency by
  model: one row per STT, LLM and TTS model, so a model swapped between runs is two rows.

Calls run on **runners** (`runner/`, `voice_runner`): each loads every use case's `core.Entry` once and
carries text and WebRTC calls. The browser's audio goes straight to the runner that holds the call; the
studio only places calls and passes the offer on. Browser calls work on this machine and its network:
another network needs a STUN or TURN server (`PIPECAT_ICE_SERVERS`).

Settings come from the environment: `STUDIO_PORT` (7800), `STUDIO_RUNNER_PORT` (7810), `STUDIO_PG_PORT`
(5432), `STUDIO_REDIS_PORT` (6379), `STUDIO_OTLP_PORT` (4318), `STUDIO_JAEGER_PORT` (16686), `STUDIO_AGENTS`
(`use-cases/*/*/agent.yaml`), `STUDIO_ROOT` (the repository). Every port binds to 127.0.0.1 only. The
runners' logs and the use cases' business data (the salon's diary, Faro's learners) live in `data/studio/`.

## Layout

| Path | Owns |
|---|---|
| `compose.yaml` | Postgres 16 and Redis 7 (health-checked) and Jaeger 2.21 (OTLP in, traces in memory, query API v3), local ports only, the `slng-studio_pg` volume |
| `src/studio/cli.py` | `Cli`: the `slng` command |
| `src/studio/settings.py` | ports, paths and URLs from the environment |
| `src/studio/boot.py` | the start-up sequence `/api/boot` reports and the loading screen draws: Docker, the services, the store, the agents, the runner |
| `src/studio/agents/` | every `agent.yaml` (`catalog`: the loader's verdict and the engine's own moves, groups for the map); the check that runs in a process of its own (`check`: `FlowSpec.problems()`, `Flow.moves`, each operation's closing lines and its receipt the way the state is described: JSON schema, fields by path for a success and a refusal, closed values); the config's files and version (`configs`); the editor (`editor`, `manifest_text`, `closing_review`, `models`) |
| `src/studio/runs/` | the hub (`hub`: runners link in, each run placed on the least busy ready runner, every event stored and streamed to the pages watching); the store (`store`: Redis for 7 days, Postgres behind, or memory); a runner the studio starts (`process`) |
| `src/studio/metrics/` | timings from Jaeger's API v3 (`jaeger`), each turn's STT, LLM and TTS requests flagged against `Limits` and the page's report (`timings`) |
| `src/studio/api/` | `Studio` and its routes: agents, runs, metrics, core's voice page at `/voice-sdk/`, the built page |
| `src/studio/events.py` | the run format, which is core's (`core.events`) |
| `src/studio/schema.sql` | the store: configs, runs, events, snapshots; applied on every start |
| `tests/` | asserts, no pytest: the catalog, events, metrics on API v3 answers, boot, the API, the CLI, the editor on copies of the salon and Faro, the store (Redis and Postgres too when they are up) |
| `web/` | Vite, React, TypeScript; React Flow and ELK for the map, loaded only when it opens. `src/pages/` one file per place (Agents, Runs, a run, going back, Metrics); `src/live/` the live socket (`useRun`), the try panel, browser calls on core's own client (`voice.ts`); `src/model/` a state and a receipt drawn and edited from their models' JSON schema, nested models included; `src/closing/` the closing-line editor |

The runner service and the studio together are tested in `runner/tests/test_runner.py`: two runners, runs
placed and stored, going back with an edit, a runner stopping, a saved config.

## Check it

```bash
uv run --directory studio python -m tests
uv run --directory runner python -m tests
uv run ruff format studio runner && uv run ruff check studio runner
uv run --directory studio ty check . && uv run --directory runner ty check .
npm --prefix studio/web run build
```
