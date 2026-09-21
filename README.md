# slng-voice-harness

A Pydantic AI agency standing where Pipecat's LLM service would.

Pipecat owns the transport, VAD, turn taking, STT and TTS (through SLNG). The
agents own the conversation: prompts, tools, typed state, and which step is
running. The seam between them is one frame processor.

```
transport.input() -> SlngSTT -> user_aggregator -> PydanticAgentProcessor -> SlngTTS -> transport.output() -> assistant_aggregator
```

---

# Running it

## 1. What you need

- **uv** — `brew install uv`, or see https://docs.astral.sh/uv/. It installs the
  right Python itself; nothing else is needed. This project pins 3.14.
- **A Vertex AI Express Mode key** for `GOOGLE_API_KEY`. The model string is
  `google-cloud:gemini-3.5-flash-lite`, so a plain Gemini Developer API key is
  rejected.
- **An SLNG key** for `SLNG_API_KEY`, from https://slng.ai. One key covers both
  speech to text and text to speech.
- Langfuse keys are optional. Without them the bot runs and prints one warning
  that traces are going nowhere.

## 2. Install

```bash
git clone <this repo> && cd slng_voice_harness
uv sync                       # creates .venv and installs everything
cp .env.example .env          # then fill in the two required keys
```

`uv sync` pulls Pipecat, `pipecat-slng`, Pydantic AI and the Silero VAD model.
The first run takes a minute or two; after that it is cached.

## 3. Talk to it, with no audio

Do this first. It is the fastest way to see the agency work, it needs no
microphone and no browser, and it is the loop to use when changing prompts.

```bash
uv run --env-file .env python talk.py
```

Type a line, press enter, read the transcript. An empty line or Ctrl-D ends it.
You can also script the whole call up front:

```bash
uv run --env-file .env python talk.py \
  "Hi, can I book a haircut for Monday?" \
  "Yes, that's right." \
  "Ten thirty works." \
  "That's all, thanks. Bye!"
```

```
  [fixed] Hi, you've reached Sage and Stone. Robin speaking, what can I do for you?

CALLER: Hi, can I book a haircut for Monday?
  [fixed] Let me confirm who I'm speaking to.
  [model] Is that +34600111222?
  -> agent='verify' customer_phone='+34600111222'  (1.6s)

CALLER: Yes, that's right.
  [fixed] Let me have a look at the diary.
  [model] For that haircut on Monday, I have 9:00 AM, 9:30, or 10:00. Which one?
  -> agent='booking' customer_verified='existing'  (2.2s)
```

`[fixed]` is a line this code speaks. `[model]` is the agent's own streamed
speech. The arrow line is the typed state after the turn, and the handoffs are
logged as they happen.

## 4. Talk to it with your voice

```bash
uv run --env-file .env python bot.py
```

Then open **http://localhost:7860** and click connect. The runner serves a
browser client, so there is nothing else to start.

```
🚀 Bot ready!
   → Open: http://localhost:7860
   → Enabled transports: webrtc, telephony, websocket
```

Useful flags, all from Pipecat's runner:

```bash
uv run --env-file .env python bot.py --port 8080
uv run --env-file .env python bot.py -t webrtc     # one transport only
uv run --env-file .env python bot.py --help
```

## 5. Run the tests

No pytest and no network: they script the model, so they are fast and free.

```bash
uv run python -m tests
```

All ten modules, with live model requests disabled rather than merely unused,
so a missed override raises instead of billing. No `.env` needed. It prints
`all 10 modules ok` and exits 0, or names what failed and exits 1.

One at a time, which is what you want while working on one thing:

```bash
uv run --env-file .env python -m tests.test_graph   # the graph: routing, the waiting step, the schema
uv run --env-file .env python -m tests.test_salon   # the salon agency
uv run --env-file .env python -m tests.test_voice   # the processor itself
uv run --env-file .env python -m tests.test_traces  # one call is one trace
uv run --env-file .env python -m tests.test_state   # validated state, and the phone policy
uv run --env-file .env python -m tests.test_diary   # the service oracle: can it say no
uv run --env-file .env python -m tests.test_flow    # guards and conflicts, at execution
uv run --env-file .env python -m tests.test_budget  # one allowance per caller turn
uv run --env-file .env python -m tests.test_bench   # the measurement, measured
uv run --env-file .env python -m tests.test_core    # the core and the adapter agree
```

Each prints `ok` or raises on the first failure.

Run singly they are safe by convention: every agent they touch is overridden
with a scripted model. Run through `python -m tests` they are safe by
enforcement. Prefer the latter before committing.

## 6. Switch agencies

`salon` is the default. `voice` is a two-agent demo -- a concierge and a phone
number intake, no external tool -- useful for checking the plumbing in isolation.

```bash
HARNESS=voice uv run --env-file .env python talk.py "my number is +34600111222"
HARNESS=voice uv run --env-file .env python bot.py
```

## 7. Traces

With the Langfuse keys set, one call is one trace. Pipecat opens a span for the
whole conversation and one per turn; the agents' runs go inside the turn they
belong to, so a handoff reads as two runs in one exchange rather than two
unrelated traces.

```
conversation                      conversation.id, session.id
├── turn 1                        turn.was_interrupted, turn.user_bot_latency_seconds
│   ├── stt
│   ├── agents  agent=concierge hops=1
│   │   └── concierge run
│   └── tts
└── turn 2
    ├── stt
    ├── agents  agent=verify hops=2 handoffs=verify
    │   ├── concierge run         -- hands over
    │   └── verify run
    └── tts
```

VAD has no span of its own. What it decides shows up on the turn:
`turn.was_interrupted` when the caller barged in, and
`turn.user_bot_latency_seconds` for the gap between them finishing and the bot
starting.

The `stt` and `tts` observations carry the transcript and the spoken text as
their output and input, the SLNG route as the model, and everything else Pipecat
measured — `metrics.ttfb`, the voice, the language, the character count — as
metadata.

Four things make this work, and all four are needed:

| Where | What |
|---|---|
| `bot.py` | `enable_tracing=True` on the `PipelineWorker`, or Pipecat opens no spans at all |
| `utils/observability.py` | a Langfuse client that exports non-GenAI spans; the default filter drops the conversation span everything hangs from |
| `utils/observability.py` | a span processor translating Pipecat's attributes into the `langfuse.observation.*` keys Langfuse renders. Pipecat writes `transcript` / `text` / `metrics.*`; Langfuse reads neither, so untranslated every speech span arrives empty |
| `voice/processor.py` | each caller turn parented on Pipecat's open turn span |

`talk.py` has no pipeline and so no turn: it still emits one trace per agent run.

## Environment

| Variable | Required | What it is |
|---|---|---|
| `GOOGLE_API_KEY` | yes | Vertex AI Express Mode key |
| `SLNG_API_KEY` | yes | SLNG key, for both STT and TTS |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` | no | traces, one per call; one warning if unset |
| `HARNESS` | no | `salon` (default) or `voice` |
| `CALLER_ID` | no | the number a phone call would arrive with |
| `SLNG_BASE_URL` | no | `eu-north.api.slng.ai` |
| `SLNG_STT_MODEL` / `SLNG_TTS_MODEL` / `SLNG_TTS_VOICE` | no | the SLNG routes and voice |

`.env` is git-ignored; `.env.example` is the template.

## When it does not work

| What you see | What it is |
|---|---|
| `KeyError: 'SLNG_API_KEY'` | no `.env`, or you ran `python bot.py` without `--env-file .env` |
| `404` or `PermissionDenied` from the model | `GOOGLE_API_KEY` is a Gemini Developer key, not a Vertex Express Mode one |
| `warning: LANGFUSE_PUBLIC_KEY unset` | expected; traces are off and everything else works |
| `TemplateSchemaError: Field 'x' not found in schema` | a prompt uses `{{x}}` and `SalonState` has no field `x`. Deliberate: it fails at import, never mid-call |
| `Address already in use` | something else is on 7860; pass `--port` |
| The browser connects but nobody speaks | check the terminal: a model or SLNG error is logged there |

---

# How it is built

```
voice/harness.py     VoiceState, CallContext, Handoff, Node, TurnBudget  -- shared
voice/core.py        one caller turn, as an event stream. No runtime imports.
voice/processor.py   the Pipecat node: translates those events into frames
voice/{state,agents}.py   the two-agent demo
salon/state.py       shapes, SalonState, SalonCall, caller-ID prefetch
salon/diary.py       customers, slots, bookings, complaints (in memory)
salon/agents.py      the four agents, their tools, the AGENTS table
prompts/salon/       one prompt per agent, plus the voice rules and documents they share
knowledge/           the salon's own documents, substituted into the prompts at import
bot.py               pipeline wiring
talk.py              the no-audio transcript loop
tests/               assert scripts, no pytest, no network
bench/               latency and behaviour against the real model, and the trace reader
```

## How a turn works

The user aggregator finishes the caller's turn and pushes an `LLMContextFrame`.
The processor hands the transcript to `voice.core.run_turn`, which runs the
current agent and yields an ordered stream of events; the processor turns
`RunStarted` into `LLMFullResponseStartFrame`, each `TextDelta` into an
`LLMTextFrame`, and `RunEnded` into `LLMFullResponseEndFrame`. TTS, the
transcript and the interruption machinery are built on those three frames and
nothing else, which is why a Pydantic AI agent can sit here at all.

Pipecat's own `LLMContext` still fills up beside the agents' history. No model
reads it; it is the aggregators' record of what was actually *spoken*, and
`note_spoken` uses it to cut the agents' history down to what the caller heard
when they talk over the bot.

## Steps are agents

Each agent has its own prompt file, its own tools, and can have its own model.
A run ends one of three ways:

- **plain text** — streams to TTS, the turn is over, the agent stays current;
- **a borrowed tool** — the step called a tool another step owns, so the caller
  is now in that step. The request that did their work is the request that moved
  them, and nothing was spent announcing it;
- **`hand_over`** — names what the caller *wants*, and the graph works out where
  that actually puts them. The edge that costs a request, kept for moves no tool
  can stand for;
- **a finisher** — `confirm_customer`, `save_booking`, `hang_up`: one model
  request does the work, records it, and the graph works out who speaks next.
  `save_booking` also says the confirmation itself, in the diary's own words, so
  no second request exists to get it wrong. A failure raises `ModelRetry`
  instead, so the step stays open with the real reason in front of the model.

`hand_over` is an action rather than a schema for a measured reason: given a
`Handoff` model to fill in, the model wrote *"let me put you through to our
complaints team"* and then did nothing.

Adding a step is a prompt in `prompts/salon/`, an entry in `AGENTS`, and a name
in `AgentName`. No node config, no snapshot and no restore.

## The graph is three fields and one function

`AGENTS` declares, per step, where a caller may go and what has to be true first:

```python
'booking': build('booking', tools=[read_diary], finishers=[save_booking],
                 exits=(Exit('concierge'), Exit('complaints')),
                 requires=('customer_verified',), satisfied_by='verify'),
```

and which of its edges cost nothing:

```python
'concierge': build('concierge', ...,
                   exits=(Exit('booking'),                              # a hand_over
                          Exit('booking', verified, via=(read_diary,)), # free
                          ...)),
```

`voice.harness.route` is the whole graph, and it never runs a model:

- **A prerequisite is interposed.** The front desk asks for `booking`; nobody is
  identified; the graph pushes `booking`, enters `verify`, and the finisher there
  pops it back. No request decides that, and no prompt states the ordering.
- **Each step's `hand_over` carries only its own exits**, as a narrowed
  `Literal` rebuilt per turn from the live state. An illegal transition is not
  refused at runtime — it is unrepresentable in the schema the model is sent.
- **A borrowed tool is the move.** A step lends its neighbour's entry tool while
  the guard holds — the front desk holds `read_diary` once somebody is
  identified — and calling it puts the caller in the step that owns it. Shut,
  the tool is not in the schema at all, so the model cannot call it and be
  refused, which would cost the round trip the lending is there to save.
- **A side question moves nothing.** A turn that only speaks takes no edge, so
  `state.agent` does not move and there is nothing to suspend or resume.

Six decisions that used to be the model's are now the application's; two
semantic ones stay — what the caller wants, and which slot they picked.

**Why the lending exists.** A live call on 2026-09-20 spent three of its
twenty-four model requests on the front desk saying where the caller was going
and nothing else: 0.85s of silence each, and the worst turn in that call — "can
you move the appointment to Saturday" — cost four requests and 3.37s. Every
request in that trace took 0.85s whatever it did, so a turn's latency is just
how many of them it takes. An edge that costs a request is an edge the caller
waits through.

## Measuring it

`bench/` answers the questions the design makes, on the real model, with no audio:

```bash
uv run --env-file .env python -m bench.scenarios 3    # requests and seconds per turn
uv run --env-file .env python -m bench.preamble 3     # does the model cover its own gaps?
uv run python -m bench.turns --env .env --sessions    # per-turn numbers off Langfuse
```

`BENCH_PINNED=1` pins the model to `gemini-3.1-flash-lite` with
`thinking_level: MINIMAL`, which is what the `salon-concierge` package runs, so
a comparison between the two measures the architecture rather than the model.

The reader normalises both arms onto one caller turn: this harness opens an
`agents` span per accepted turn and the native one has only Pipecat's `turn`
span, so it picks one or the other and never counts both. The greeting is the
turn Pipecat records no `user_bot_latency_seconds` for. What a caller waited
is that attribute; a turn span's own duration runs until the bot stops
speaking and is three to fifteen seconds on the same call. `bench/review-20260921.md`
is the ledger of what that reader used to get wrong, and what is still open
before any latency claim is worth making.

If an agent's words should be heard, it is an agent in `AGENTS`. If they should
not — a classifier, an extractor, a summariser — it is delegation:
`await worker.run(..., deps=ctx.deps, usage=ctx.usage)` from inside a tool.

## Two rules the salon agency is built on

**The prompt is already branched.** `prompts/salon/*.md` are Handlebars
templates compiled against `SalonState`, so every `{{#if}}` resolves before the
model reads it. An unverified caller's concierge prompt does not contain the
"skip verification" branch at all.

**Anything code can work out, code works out.** Which day "next Friday" is,
whether a slot has already passed, what a booking sounds like said out loud,
what to say when a booking lands. None of it is asked of a model, so none of it
can be got wrong.
