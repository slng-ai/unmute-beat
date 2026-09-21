# CLAUDE.md

A Pydantic AI agency standing where Pipecat's LLM service would. Pipecat owns
transport, VAD, turn taking, STT and TTS; the agents own the conversation.

```
transport.input() -> SlngSTT -> user_aggregator -> PydanticAgentProcessor -> SlngTTS -> transport.output()
```

`README.md` is the long version — architecture, the graph, the trace shape, the
measurements behind the design. Read it before changing how a turn works.

## Layout

| Path | Owns |
|---|---|
| `voice/harness.py` | what every agency shares: `VoiceState`, `CallContext`, `TurnBudget`, `Node`, `Exit`, `route`, `moved_by`, the hooks |
| `voice/core.py` | one caller turn, runtime-neutral. Yields an event stream (`RunStarted`, `TextDelta`, `FixedSpeech`, `RunEnded`, `TurnFinished`) and imports no Pipecat |
| `voice/processor.py` | the Pipecat adapter: translates that event stream into `LLMTextFrame`s, supplies the open turn span |
| `voice/{state,agents}.py` | the two-agent demo agency (`CallState`, `Call`) |
| `salon/state.py` | `SalonState`, `SalonCall`, the validated shapes, caller-ID prefetch |
| `salon/diary.py` | customers, slots, offers, bookings, complaints — in memory, and the oracle that says no |
| `salon/agents.py` | the four agents, their tools, the validated service boundary, the `AGENTS` table |
| `utils/observability.py` | Langfuse/OTel wiring; call `setup()` once, before any agent is built |
| `prompts/<agency>/` | one Handlebars prompt per agent, plus the shared voice rules and the salon's own documents, substituted in at import |
| `bench/` | latency and behaviour measurement against the real model |
| `bench/baseline/` | the pre-Phase-01 snapshot, digests and evidence ledger — don't edit |
| `tests/` | assert scripts — no pytest, no network |

**The split that matters:** `voice/core.py` runs a turn and knows nothing about
how it reaches a person; `voice/processor.py` is the Pipecat adapter over it.
Anything about *running* a turn goes in `core`, anything about frames goes in
`processor`. `tests/test_core.py` asserts the two agree.

Each agency supplies its own `VoiceState` subclass (validated, model-visible
facts) and `CallContext` subclass (state + history + the line to the caller).
`salon/` uses `SalonState`/`SalonCall`, `voice/` uses `CallState`/`Call`.
State is replaced wholesale via `call.commit(**updates)` — one validated
candidate or nothing, never field-by-field assignment.

`voice/` is two things at once: the shared framework (`harness`, `core`,
`processor`) **and** the demo agency (`agents`, `state`). That is why `salon/`
imports from a package named after the other agency. Known, not accidental.

Tools live with the agency whose state they bind to (`RunContext[SalonCall]`).
There is no shared top-level `tools/` on purpose: it would couple the agencies.

## Commands

Everything needs `--env-file .env` — the model key is read at import.

```bash
uv sync                                                  # venv + deps
uv run python -m tests                                   # all ten modules, offline, no .env
uv run --env-file .env python talk.py                    # no audio, the fast loop
uv run --env-file .env python bot.py                     # voice, http://localhost:7860
uv run ruff format . && uv run ruff check . && uv run ty check .
```

`uv run python -m tests` is the answer to "how do I test": it turns
`ALLOW_MODEL_REQUESTS` off *before* any scenario module is imported, so a
module that forgot to override an agent raises instead of billing. Running one
module alone (`uv run python -m tests.test_salon`) is what you want while
working on it, but it is only safe by convention.

`HARNESS=voice` swaps the salon for the demo agency. `talk.py` takes the whole
call as arguments, which is the quickest way to exercise a prompt change.

Before calling anything done: `ruff check`, `ty check` and `python -m tests`
all clean. Paste the real output; don't round up.

## Conventions

- **Docstrings carry the explanation**, not `#` comments. The exception is a
  short `# ponytail:` marker naming a deliberate shortcut and its upgrade path.
- Tool docstrings **are prompt text** — the model reads them. Editing one
  changes behaviour; treat it as a prompt change, not a style fix. This is why
  `D205`, `D210`, `D415` and `D417` are off in `pyproject.toml`.
- Single quotes, 110 columns, `ruff format` decides. `E501` is off.
- No pytest. Tests are assert scripts run as modules, and stay that way.
- No new dependency without asking. Stdlib first.

## Four things that bite

1. **Prompts are compiled against state.** `prompts/<agency>/*.md` are
   Handlebars templates typed against the agency's `CallContext`, so a
   `{{state.x}}` naming no field raises `TemplateSchemaError` **at import**,
   never mid-call. Deliberate. Add the field or fix the prompt.
2. **A new step is three edits in lockstep**: a prompt file in
   `prompts/<agency>/`, an entry in `AGENTS`, and a name in `AgentName`. Miss
   one and it fails at import.
3. **Anything a service returns is validated at the boundary.** `salon/agents.py`
   parses every `diary` response into a model (`Slots`, `Saved`, `Held`,
   `ComplaintRecord`) before a field is read. A return annotation is not
   validation — Python doesn't check it.
4. **One action per model reply.** `one_action_per_reply` picks the single tool
   call in a response allowed to move the caller or write to the business;
   `claim_or_refuse` is how an action checks it holds that claim.

## Known debt

`Node` is not generic, so `agent` is a bare `Agent` (ty resolves it to
`Agent[object, str]`) and its guard/output callables are fixed to `VoiceState`,
while every salon agent is `Agent[SalonCall, ...]` with `SalonState` callables.
`invalid-argument-type` is switched off in `pyproject.toml` to keep the baseline
green — which means ty catches no new errors of that kind anywhere. Making
`Node` generic over the state type fixes all ~15 sites and lets the line go.
Marked at the definition in `voice/harness.py`.
