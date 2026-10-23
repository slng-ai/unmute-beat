# 06 — Running on LiveKit

Core is the brain of a call. Pipecat and LiveKit Agents are two ways to give it ears and a mouth. This chapter explains how the brain plugs into LiveKit, what LiveKit tells it, and what is still missing.

Read [chapter 01](01-stack-and-boundaries.md) first: everything there stays true. Only the media framework changes.

## 1. One entrypoint, two frameworks

A use case still says what it is once, in its `entry.py` (`core.Entry`). Its `bot.py` runs that entry on either framework:

```bash
cd use-cases/salon
uv run bot.py                     # Pipecat, as before: open http://localhost:7860
uv run bot.py livekit dev         # LiveKit: a worker that takes one call per room
uv run bot.py livekit console     # LiveKit: one call in this terminal
uv run bot.py livekit start       # LiveKit: production worker
```

`VOICE_FRAMEWORK=livekit` does the same as the word `livekit`. `LIVEKIT_URL`, `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` say which LiveKit server to use. Set `LIVEKIT_AGENT_NAME` to take only rooms dispatched to that name.

| Piece | Pipecat | LiveKit |
|---|---|---|
| Runs one call | `core.launch.run_call` | `core.livekit_launch.run_call` |
| Adapter | `core.processor.PydanticAgentProcessor` (a frame processor) | `core.livekit.FlowAgent` (a LiveKit `Agent`) |
| Playback evidence | `TwilioMarks`, `WebRTCPlayout`, `LocalPlayout` | `core.livekit.SegmentPlayout` |
| Shared by both | `core.voice`: waits, call ceiling, how a call ended, `serve` | |
| The brain | `core.turn`, unchanged | `core.turn`, unchanged |

The settings are the same environment names on both: the SLNG speech models and voice, `VAD_STOP_SECS`, `MAX_CALL_SECONDS` and `DELIVERY`. One speech profile runs both.

## 2. Where the brain sits in LiveKit

LiveKit Agents has a slot for the language model: `Agent.llm_node`. LiveKit calls it once the caller has finished a turn. `FlowAgent` fills that slot with the whole flow: the step's agent, its tools, operations, hand-overs and every fixed line.

```mermaid
flowchart LR
    Room["Room audio"] --> Hear["LiveKit: VAD, STT, end of turn"]
    Hear -->|"llm_node(chat context)"| Brain["FlowAgent: core.run_turn"]
    Brain <--> Backend["Application functions"]
    Brain -->|"text, one segment at a time"| Speak["LiveKit: TTS"]
    Speak --> Tap["SegmentPlayout"]
    Tap --> Out["Room output"]
    Out -.->|"playback_finished per segment"| Tap
    Tap -.->|"played, cut, gap, failed"| Brain
```

Three rules make this safe:

1. **The turn runs in its own task.** The node only passes on what the turn says. When LiveKit cancels the node because the caller cut in, the turn is cancelled too, as on Pipecat. When LiveKit cancels it because the room is closing, the turn may finish its write (`CallTiming.FINISH_SECONDS`).
2. **Never on guessed words, never over the caller.** LiveKit can start a reply early, on words the caller has not finished (preemptive generation). A turn can write to the business, so `FlowAgent` turns that off for itself, and a reply that is not yet scheduled waits. A change also waits until its reply may play and the caller is quiet, as LiveKit's own tools do (`CallContext.wait_to_dispatch`). A caller who says "yes", pauses and goes on is heard before anything is sent (L20).
3. **LiveKit's own LLM is never called.** The session gets a stand-in (`FlowLLM`) only because LiveKit runs its reply pipeline when an LLM is set. It raises if anything calls it.

## 3. What the caller said: end of turn, joining, and dropping a stale reply

| What the caller does | What LiveKit does | What the brain does |
|---|---|---|
| Finishes speaking | Commits the turn (VAD, STT and its turn detector) | Takes every user message it has not served yet, by id, as one turn |
| Pauses, then goes on before any answer played | Commits a second turn | Joins both pieces into one turn (`continues_turn`), as on Pipecat |
| Goes on talking while the brain is still working | Would hold the old reply and play it after them | Drops the old reply as soon as there are words (L4) |
| Makes a sound with no words while the brain works | Pauses, then carries on | Cancels nothing: the reply plays once they are quiet (L5) |
| Says "mm-hm" while the agent speaks | Its adaptive detector can let the agent go on | Nothing is cut, so nothing is lost |
| Talks over the agent | Interrupts (or pauses, then interrupts) | The segment is cut to the words that played (L3, L14, L23) |
| Makes a short sound under a read-back that LiveKit lets play on | Nothing | Not taken as the reply: the "yes" said after it still counts (L21) |
| Says "yes", pauses, goes on before LiveKit interrupts | Holds the reply while they speak | The change waits; their words drop it; nothing is sent (L20) |
| Talks past LiveKit's turn limit (`user_turn_limit`) | Would cut in with a reply that cannot be interrupted | Lets them finish |
| Stays silent | Nothing | Asks if they are still there, puts the question again, then says goodbye (L7), also after a cough that made no turn (L18) |

Only words drop a reply that is still being prepared, never a sound. On Pipecat any new user turn interrupts; on LiveKit a cough no longer cancels a turn that nothing will replace.

A sound counts as the start of the caller's reply only once it has words, LiveKit interrupts for it, or it makes a turn. A sound LiveKit found no words in (`user_transcription_timeout`, after `TRANSCRIPTION_TIMEOUT`, 2 s) is set aside, so a cough under a read-back does not spoil the "yes" said after it (L21). Words can come long after the caller stopped (a slow or final-only STT): the earliest start held still counts, or the latest set-aside one when nothing else is held, so a reply always has a start (L24, found on a live run).

One LiveKit detail matters: on an audio output that cannot pause, LiveKit itself drops a reply on any sound longer than `min_duration`. A real room's output can pause, so this only shows up in custom outputs.

## 4. What the caller heard: one segment, one report

The brain records every line it says as a segment (`CallContext.spoken`): the model's words, a holding line, a read-back, a receipt. On LiveKit each segment becomes its own speech segment. The node sends a `FlushSentinel` between them, so each one is synthesised, played and reported on its own, and the next one's speech is made while this one plays.

`SegmentPlayout` sits at the top of LiveKit's audio chain. It sees each segment's audio, its end, a clear and a pause, and LiveKit's `playback_finished` for it. LiveKit plays a reply's segments one after another, so each segment is started on its own when the room begins playing it (`CallContext.note_started`): the end of one never counts the next as played (L14). A segment the room never took (its first frame held, then cut) is answered at once, so nothing waits for a report that will never come (L15). LiveKit's own history gets a space between segments (L17).

| LiveKit says | The segment is |
|---|---|
| Finished, not interrupted | Played (`local` evidence) |
| Finished, interrupted | Cut to the words the transcript synchroniser says played |
| Paused (a possible interruption), then resumed | Never heard in full: the room output drops what it had queued when it pauses (`CallContext.note_gap`) |
| No audio, or synthesis failed after LiveKit's retries | Failed: never heard. LiveKit then cancels the reply; that is not the caller talking, so the turn finishes (L16) |

A read-back can be agreed to only when its segment was heard in full and the caller's reply began after it ended, exactly as on Pipecat.

### The gap: LiveKit reports its own clock

LiveKit's room output reports when its audio left this process at real time. It does not report that a browser or a phone played it. Twilio marks and core's browser page do report that. So on LiveKit the evidence is `local`, the same class as Coval's mark-less phone line, and it counts as heard only on a call opened for simulated playout:

```bash
DELIVERY=simulated uv run bot.py livekit dev
```

Without it, a flow that confirms writes (the salon) says it cannot confirm changes on this line and hangs up, before the caller gives any details (`core.livekit.reports_playback`). A flow with no read-backs runs either way.

To close the gap, the far end has to report playout, as core's browser page does on Pipecat. LiveKit already has the shape for it: its avatar protocol has the remote side send `lk.playback_finished` by RPC. A LiveKit web client could report each segment the same way. That is the next step, not done yet.

## 5. Checks

| Check | What it proves | How to run |
|---|---|---|
| `core/tests/test_livekit.py` (L1-L25) | Every behaviour in sections 3 and 4, on LiveKit's real `AgentSession`, offline, with a made-up caller, voice and speaker; L25 the spans of section 6 | `cd core && uv run python -m tests.test_livekit` |
| `use-cases/salon/tests/test_store.py` (B10) | A LiveKit job process opens the diary its worker named and has its flow compiled before its call (section 7) | `cd use-cases/salon && uv run python -m tests.test_store` |
| `core/tests/test_package.py` | Only `core.livekit` and `core.livekit_launch` import LiveKit; `core.voice` imports no framework | `cd core && uv run python -m tests.test_package` |
| `use-cases/salon/livekit_check.py text` | The salon through LiveKit's own text mode, real Gemini | billable |
| `use-cases/salon/livekit_check.py voice` | The salon with real SLNG speech both ways and Silero, a synthetic caller who also talks over the agent | billable |

L13 runs the same scripted turn over text and over LiveKit and checks the words are the same: one brain, two frameworks.

The live checks were run on 2026-10-11 from a cloud sandbox:

- Text: verify, offer, read-back, yes, booked once, goodbye; 0.6 to 1.3 s per turn.
- Voice, after the review fixes (same day): number confirmed, the offer cut at "Tomorrow" when the caller talked over it, read-back heard, "Yes, please book it" booked once, goodbye. A first try of the onset change failed live (the speech-to-text words came after the caller stopped, and every start had already been dropped); L24 now covers that.
- Voice, first run: the caller talked over the list of times; LiveKit paused, then cut it; the offer was recorded as cut. "10:00 would be perfect" was read back, "Yes, please book it" booked it once, and the goodbye closed the call. From the final transcript to the agent's first audio took 1.3 to 1.7 s per turn. The sandbox's speech-to-text results came about 3 s after the caller stopped, so the full waits there (3.7 to 7.3 s) say nothing about a real deployment.

A real LiveKit room could not be tested from the sandbox: its network passes HTTPS only, and WebRTC media needs UDP or TURN. The worker did register with LiveKit Cloud.

## 6. Measuring a call

A LiveKit call writes the same spans unmute-core's adapters write on both frameworks (its `docs/TELEMETRY.md`), so a trace reads the same whichever runtime made it (`core.livekit.CallTrace`):

```mermaid
flowchart TD
    C[conversation: the call] --> T[turn: one caller turn]
    C --> S[agent_session: LiveKit's own spans]
    T --> A[stt: what the caller said]
    T --> D[the step that answered]
    D --> L[chat: each model request]
    D --> W[write: each business action]
    T --> B[tts: each piece of speech]
```

- **One trace per call.** `conversation` starts before LiveKit's session, so LiveKit's own spans (`agent_session`, `user_turn`, `agent_turn`, `tts_node` and the rest) sit inside the call's trace too.
- **A turn is core's turn.** A caller who goes on before any answer played continues the same `turn` (section 3). The greeting opens a turn with no caller words.
- **Every number is LiveKit's own**, from the timings it puts on each message, never estimated here: `turn.e2e_seconds` (the caller stopped, the agent's first audio played), `turn.stt_final_seconds`, `turn.eou_seconds` (how long end of turn waited), `turn.llm_ttft_seconds` (the flow's first words: every model request before them) and `turn.tts_ttfb_seconds`; an `stt` span's `metrics.ttfb` and a `tts` span's (its first text to its first audio). A missing one is left out, never sent as 0.

Readers: the ledger and `load_report` (`use-cases/concierge-tools`) read `turn.e2e_seconds` and `turn.interrupted` wherever they read Pipecat's `turn.user_bot_latency_seconds` and `turn.was_interrupted`.

## 7. Trying it: workers and Coval

**A worker.** `uv run bot.py livekit start` keeps call processes ready (`LIVEKIT_IDLE_PROCESSES`, LiveKit's default: one per CPU in `start`, none in `dev`). Each one loads Silero and imports the use case with its flow compiled before any call is given to it (`livekit_launch.prewarm`), so a call does not wait for `agent.yaml` to be read. The model clients still open their connections per call, while the greeting plays: a client belongs to the event loop of the call using it. LiveKit runs each call in a process of its own, so anything a use case keeps in memory is per call. The salon's diary is a file: with no `DIARY_PATH`, the worker names a fresh one in its environment and every call process opens that same file.

**Coval.** `coval_sim` drives a LiveKit agent too: Coval asks a small token endpoint for a room pass, joins the room, and LiveKit sends the worker in. See `use-cases/concierge-tools/README.md`.

## 8. Plugging into unmute-core

[unmute-core](https://github.com/slng-ai/unmute-core) is the shared runtime for Unmute's agents on LiveKit and Pipecat. It owns its own adapters (`unmute_core.livekit.UnmuteAgent`, `unmute_core.pipecat`) and one plug point for the model call (`ModelCall`). Core can plug in at the same place on each side:

| unmute-core | Core |
|---|---|
| `UnmuteAgent.llm_node` sends one model request | `FlowAgent.llm_node` runs a whole caller turn |
| `yield_to_caller` drops a reply the caller spoke over | `FlowAgent` drops it on words, never on a sound |
| `idle_nudges` | `core.turn.remind`, by code |
| `interruption(backchannels)` | `livekit_launch.turn_handling` (interruption on, adaptive where LiveKit has it) |
| Telemetry spans (`conversation`, `turn`, `stt`, `tts`) | the same spans on LiveKit (section 6); on Pipecat, Pipecat's own `turn` and the step spans |

The proposal: unmute-core gets a second plug point beside `ModelCall`, a "brain" that owns a whole turn (`async def turn(words) -> segments`) plus the playback reports this chapter describes. `FlowAgent` already has that shape on LiveKit and `PydanticAgentProcessor` on Pipecat, so core would implement it twice with no change to `core.turn`. The pieces that must cross the seam are small: the words of a turn, a barge-in, a speech onset, and one playback report per segment.

## 9. Limits

- Playback evidence is LiveKit's own clock (section 4). A client report is the fix.
- `FlowAgent` reads two private LiveKit internals, pinned to livekit-agents 1.8: the reply's speech handle (to bind a segment to the reply that plays it) and whether the session is closing. If either goes, nothing is heard, or a closing session is taken as a barge-in. Both fail closed.
- livekit-agents 1.8 declares `openai<3`, Pydantic AI needs openai 3. The workspace overrides it (`pyproject.toml`); every LiveKit module core uses imports and runs under openai 3.
- A real LiveKit room is untested from the cloud sandbox (no UDP): Coval's LiveKit jobs are tested offline up to the room, not through it.
- A model provider that keeps failing closes the line after three failed turns in a row (`FAILED_TURNS`, L19). Retries and a fallback model belong in Pydantic AI (`FallbackModel`).
- A "yes" that overlaps the last moments of a read-back is not an answer to it, and the read-back is asked again. That is core's rule on both frameworks, and safe; whether a small overlap may count is a product decision.
- Speaking over the goodbye still ends the call; the words after it are not served.
- Token usage and LiveKit's `LLMMetrics` are not reported to LiveKit's own metrics (core's spans carry them, section 6). LiveKit tags its `llm_node` span as one model call with no model or usage.
- Not used: preemptive generation (to gain back up to about 0.3 s with the tuned waits, a speculative first request on a forked call is the path), LiveKit-run tools and their fillers, and `update_agent` (one `FlowAgent` per session).

[← Back to the guide](README.md)
