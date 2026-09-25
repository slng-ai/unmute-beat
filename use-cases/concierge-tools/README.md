# Concierge tools

Tools that compare the salon builds, `../salon-graph` and `../salon-prompt`.
Neither tool imports a build. They start a build's `bot.py`, or read its
traces.

| Path | What it does |
|---|---|
| `coval_sim/` | starts a use case's `bot.py -t twilio` behind a cloudflared tunnel, and lets Coval call it. Needs network and keys, never runs in CI |
| `ledger.py` | reads any build's Langfuse traces into one ledger: requests, tokens counted once, calls in a row, latency, and each write with the question it answered |

## How to run them

```bash
uv run python -m coval_sim run VkpkmRMV                     # Coval release check, 18 real calls, billable
uv run ledger.py harness CA... -- graph trace:...          # cost of calls, from traces
```

A Coval agent named `voice-harness-<agency>-pipecat` calls the folder
`use-cases/<agency>`. `salon` is the one exception: it calls `salon-graph`,
because that agent existed before the split. Create the baseline's agent with
`uv run python -m coval_sim add salon-prompt`.

Both read the `.env` at the repository root.

## How to check them

```bash
uv run python -m tests                 # offline: scripted Coval replies and recorded traces
```
