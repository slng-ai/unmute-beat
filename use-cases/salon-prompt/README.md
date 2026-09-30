# Salon, as one prompt

The same salon as `../salon`, built the usual Pipecat way. One system
prompt holds every rule. Pipecat's own Gemini service runs it, with the diary's
tools. It is the baseline the graph build is measured against.

What stays the same as the salon on core: STT, VAD, smart-turn, TTS, the transport,
the diary (`salon/diary.py`) and the model. What changes is who owns the
conversation. This folder does not use core or Pydantic AI.

## How to run it

```bash
uv run bot.py
```

Then open http://localhost:7860. The `.env` at the repository root is found by
walking up. `DIARY_PER_CALL=1` gives every call a fresh diary.

## What is here

| Path | What it holds |
|---|---|
| `bot.py` | the Pipecat pipeline around the baseline |
| `salon/baseline.py` | the one prompt, the tools, and the Gemini service |
| `salon/diary.py`, `salon/models/shapes.py` | copies of the salon's diary and phone shape, so both builds book into the same kind of diary |
| `salon/prompts/` | the shared voice and diary rules, the salon's documents, and `baseline.md` |
| `utils/observability.py` | Langfuse over OTel, without the Pydantic AI hook |

The diary and the prompts are copies. Change them in both folders, or the two
builds stop being comparable.

## How to check it

```bash
uv run python -m tests
```
