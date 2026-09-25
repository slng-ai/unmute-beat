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
# one job: one agent (by ID), one test set, one persona. `plan` launches nothing.
uv run python -m coval_sim plan --agency salon --agent JxGgSPA55DcFcDcvbKAUQM \
    --test-set kQ8b6nMZ --persona MUFJdYAHdBHU6UbRRu4ykM --max-simulations 1 --label session-2
uv run python -m coval_sim run  ...the same arguments...    # launches it, billable
uv run ledger.py harness CA... -- graph trace:...          # cost of calls, from traces
uv run python audio_timing.py call.wav                     # who spoke when, from a Coval recording
```

`plan` prints the exact agent, cases, persona, metrics and the number of calls
(cases × iterations; no mutations). It refuses an agent that calls another use
case, a test set not attached to it, an empty selection, an ambiguous persona,
more calls than `--max-simulations`, and a job already in the run log under the
same label. Every launch is written to the run log (`--log`, default
`runs.jsonl`) before and after it goes out. A launch whose answer was lost is
looked up by name, and if Coval has no such run it is marked uncertain and never
launched again until its lines are removed by hand. A local timeout exits 3:
still running, not failed, not cancelled. Each simulated call is capped at 180 s
by `bot.py` (`MAX_CALL_SECONDS`), because Coval has no call-length limit.

A Coval agent named `voice-harness-<agency>-pipecat` calls the folder
`use-cases/<agency>`. `salon` is the one exception: it calls `salon-graph`,
because that agent existed before the split. Create the baseline's agent with
`uv run python -m coval_sim add salon-prompt`.

Both read the `.env` at the repository root.

## How to check them

```bash
uv run python -m tests                 # offline: scripted Coval replies and recorded traces
```
