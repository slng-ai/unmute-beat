# slng-voice-studio

A local studio for slng-voice-core: every agent in the repository, its step graph, and (as the later phases
land) its runs live, forks from an earlier turn, and config editing. Design and plan:
[docs/studio](../docs/studio/README.md).

## Run it

Needs Docker with compose v2 and Node 22. From the repository root:

```bash
uv sync --all-packages                          # once
npm --prefix studio/web ci                      # once
npm --prefix studio/web run fonts               # optional: Saans, from a slng-ai/frontend checkout next to this repo
npm --prefix studio/web run build               # the page the engine serves
uv run --directory studio slng studio up        # http://localhost:7800, ctrl+c stops everything
```

| Command | Does |
|---|---|
| `slng studio up` | Starts the engine, which serves the page at once and brings up Postgres and Redis behind it (the loading screen follows along). Ctrl+c stops the containers; the data stays. |
| `slng studio up --dev` | Also runs Vite with hot reload on :7802, sending `/api` to the engine. |
| `slng studio up --no-services` | Leaves Docker alone. Agents and graphs still work; nothing is stored. |
| `slng studio down [--volumes]` | Removes the containers, and with `--volumes` every stored run. |
| `slng studio status` | The containers and the engine. |
| `slng studio agents` | Every `agent.yaml` and whether the loader accepts it, offline. Exit 1 if any does not. |

Settings come from the environment: `STUDIO_PORT` (7800), `STUDIO_PG_PORT` (5432), `STUDIO_REDIS_PORT`
(6379), `STUDIO_AGENTS` (`use-cases/*/*/agent.yaml`), `STUDIO_ROOT` (the repository). Every port binds to
127.0.0.1 only.

## Layout

| Path | Owns |
|---|---|
| `compose.yaml` | Postgres 16 and Redis 7, health-checked, local ports only, the `slng-studio_pg` volume |
| `src/studio/cli.py` | the `slng` command |
| `src/studio/settings.py` | ports, paths and URLs from the environment |
| `src/studio/boot.py` | the start-up sequence `/api/boot` reports and the loading screen draws |
| `src/studio/discovery.py` | every `agent.yaml`: `core.load` for its steps, `studio.check` for the loader's verdict and the engine's own moves, groups for the map |
| `src/studio/check.py` | `FlowSpec.problems()` and `Flow.moves` (compiled on `TestModel`, no provider called) in a process of its own, with the use case importable |
| `src/studio/events.py` | the run format: twelve events, one discriminated union |
| `src/studio/server.py` | the FastAPI app: `/api/health`, `/api/boot`, `/api/agents[/id]`, `/api/runs` (501 until phase 2), `/api/events/schema`, the built page |
| `src/studio/schema.sql` | the store: configs, runs, events, snapshots |
| `tests/` | asserts, no pytest: discovery on the `desk` fixture and every use case, events, boot on a scripted runner, the API, the CLI |
| `web/` | Vite, React, TypeScript; React Flow and ELK for the map, loaded only when it opens. `src/styles/tokens.css` is the design's tokens board; `src/pages/` one file per page; `src/components/` the graph's four views (focus, map, path over turns, moves table) and the mic check |

## Check it

```bash
uv run --directory studio python -m tests
uv run ruff format studio && uv run ruff check studio
uv run --directory studio ty check .
npm --prefix studio/web run build
```
