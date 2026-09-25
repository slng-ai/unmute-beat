# Salon, as a graph

Sage and Stone, a hair salon's phone line, built on core. Four steps (front
desk, verification, booking, complaints) are declared in `salon/flow.yaml`.
Each step is an agent. The application, not the model, decides what is
written to the diary. The root [README](../../README.md) explains how core
works, with this salon as its running example.

## How to run it


You need **uv** (`brew install uv`, or https://docs.astral.sh/uv/ — it installs
the right Python itself, this project pins 3.14) and a `.env` at the repository root
holding the two keys in the root README. Nothing else.

```bash
CALLER_ID=+34600111222 \
SALON_FIXTURE=fresh \
SALON_CLOCK=2026-09-21T10:00:00+02:00 \
uv run bot.py -t webrtc --host localhost --port 7861
```

Then open **http://localhost:7861** and click connect. The runner serves a
browser client, so there is nothing else to start.

```
🚀 Bot ready!
   → Open: http://localhost:7861
   → Enabled transports: webrtc
```

| | |
|---|---|
| `CALLER_ID` | the number a phone call would arrive with; read back and confirmed before it is trusted |
| `SALON_FIXTURE` | `fresh` empties the diary first, so every call starts from the same salon. `continue`, the default, is what a real salon does: the last call's booking is still there |
| `SALON_CLOCK` | pins the salon's idea of now, so "tomorrow" resolves to the same day on every run. No offset means the salon's own timezone |
| `-t webrtc` | one transport instead of all of them |
| `--host` / `--port` | Pipecat's runner flags. `uv run bot.py --help` for the rest |
| `SALON_INTERPRETER` | `solver` reads replies with one state-solver request instead of the narrow readings. See section 6 of the root README |
| `SALON_NAMED_PICK` | `0` turns off the pick reading's `named` field (on by default for the salon). See section 6 of the root README |

All of these are optional: `uv run bot.py` on its own is a live-clock
call, from an anonymous number, into the salon as the last call left it. The
remaining dials are in the root `.env.example`. Turn taking is Pipecat's (Silero VAD
and smart-turn v3), with the graph build's settings: a 0.2 s silence window
and a 1.6 s ceiling on a turn smart-turn thinks is not finished. At the 3 s
default, the ceiling was the whole tail of the caller's wait.

A booking, end to end: *"Hi, can I book a haircut for Friday?"* The front desk
hands the caller to booking; booking needs an identified caller, so the graph
interposes verification, which reads the caller ID back. *"Yes, that's right."*
One classifier request interprets the approval; application guards authorize
the lookup, the graph resumes booking, and the diary's free times are offered. *"Three o'clock."* The
application reads the time back: *"Shall I book you in for a haircut, Friday
at 3:00 PM?"* *"Yes."* Only that yes writes, and the confirmation is
spoken in the diary's own words. Neither turn needs a model request beyond the
one classifier. *"That's all, thanks, bye."* The line closes.



## How to check it

```bash
uv run python -m tests                 # offline, no .env, no network
uv run ty check .
```

`evals/` holds the classifier evaluations. They make live model requests, so
they cost money. `evals/compare.py` runs the no-model floor offline and the
rest with `--live`.

The one-prompt baseline of the same salon is in `../salon-prompt`. Coval calls
and the cost ledger that compare the two are in `../concierge-tools`.
