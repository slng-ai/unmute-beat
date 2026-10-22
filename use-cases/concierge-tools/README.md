# Concierge tools

Tools that compare the salon builds: `../salon` on core, and the baselines
`../salon-prompt` and `../salon-pipecat-flows`.
Neither tool imports a build. They start a build's `bot.py`, or read its
traces.

| Path | What it does |
|---|---|
| `coval_sim/` | starts a use case's `bot.py -t twilio` behind a cloudflared tunnel, or `bot.py livekit start` with a token endpoint behind one, and lets Coval call it. Needs network and keys, never runs in CI |
| `ledger.py` | reads any build's Langfuse traces into one ledger: requests, tokens counted once, calls in a row, latency, and each write with the question it answered |

## How to run them

```bash
# one job: one agent (by ID), one test set, one persona. `plan` launches nothing.
uv run python -m coval_sim plan --agency salon --agent JxGgSPA55DcFcDcvbKAUQM \
    --test-set kQ8b6nMZ --persona MUFJdYAHdBHU6UbRRu4ykM --max-simulations 1 --label session-2
uv run python -m coval_sim run  ...the same arguments...    # launches it, billable
uv run ledger.py core CA... -- graph trace:...          # cost of calls, from traces
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

A Coval agent named `voice-core-<agency>-pipecat` calls the folder
`use-cases/<agency>`, so `voice-core-salon-pipecat` calls `use-cases/salon`.
Create the baseline's agent with
`uv run python -m coval_sim add salon-prompt`.

### On LiveKit

A Coval agent named `voice-core-<agency>-livekit` calls the same use case
running as a LiveKit worker. Only a use case on core can (salon,
faro-language-learning, nosh, wasla, kestrel); salon-prompt,
salon-pipecat-flows and graph are Pipecat only.

```bash
uv run python -m coval_sim add salon --livekit     # creates voice-core-salon-livekit (type livekit)
uv run python -m coval_sim plan --agency salon --agent <its ID> --test-set <ID> --persona <ID> \
    --max-simulations 1 --label session-1 --profile <speech-profile.env>
uv run python -m coval_sim run  ...the same arguments...    # billable
```

It needs `LIVEKIT_URL` (wss://), `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET`
in the environment or `.env`, and `.env` must not set `DELIVERY`,
`LIVEKIT_AGENT_NAME` or `LIVEKIT_IDLE_PROCESSES`: the job sets them. `run`
starts `bot.py livekit start` under a worker name of its own
(`DELIVERY=simulated`, since LiveKit cannot report the caller's playback),
waits for LiveKit's `registered worker` line, then serves `POST /token` behind
a cloudflared tunnel and points the Coval agent at it. Each token dispatches
that worker into Coval's room. Every token request is recorded with the
simulation ID Coval sends (`<agency>-tokens.jsonl` in the job's log folder),
and after the run each simulation is printed with its room, which is the
call's Langfuse session: pass it on as `load_report.py --call SIMULATION=ROOM`.
A simulation whose request carried no ID is printed as unknown, never guessed.
`serve` and `run --host` are Pipecat only. Each LiveKit job's worker takes
a free health port (`LIVEKIT_HTTP_PORT=0`), so jobs can run side by side.

Both read the `.env` at the repository root.

## How to check them

```bash
uv run python -m tests                 # offline: scripted Coval replies and recorded traces
```
