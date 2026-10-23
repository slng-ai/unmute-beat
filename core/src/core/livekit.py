"""The LiveKit side of a caller turn: LiveKit's user turns in, LiveKit speech out.

The same engine as on Pipecat (`core.turn`), behind LiveKit Agents' own seam:
an `Agent` whose `llm_node` runs the flow's turn instead of a model call.
LiveKit owns the room, VAD, end of turn, interruptions, STT and TTS; the flow
owns the conversation. `core.processor` is the same adapter for Pipecat; what
a turn is, and every promise about it, is `core.turn`'s on both.

    room audio -> STT -> end of turn -> FlowAgent.llm_node (core.run_turn) -> TTS -> SegmentPlayout -> room

What each piece of LiveKit is used for, and why:

* **The turn.** LiveKit calls `llm_node` once per committed user turn, with
  its chat context; the words not served yet (by message id) are the turn.
  The turn itself runs in a task of its own and the node relays its speech,
  so LiveKit cancelling the node is a decision this side makes: a barge-in
  cancels the turn (as Pipecat's interruption does), a closing session lets a
  turn already writing finish (`finish_turn`), as a dropped Pipecat line does.
* **Speech.** Every segment the call records (`CallContext.spoken`) is one
  LiveKit speech segment: the node yields `FlushSentinel` between them, so
  each is synthesised, played and reported on its own, and the next one's TTS
  starts while this one plays. A fixed line said mid-turn (a holding line, a
  read-back by code) joins the same reply as its own segment; one said with no
  turn running (the greeting, a reminder) is its own `session.say`.
* **Playback evidence.** `SegmentPlayout` sits at the top of the session's
  audio chain and sees each segment's frames, its flush, a clear and a pause,
  and the room output's own `playback_finished` for it. A segment that played
  out with no clear and no pause is returned as `local`: LiveKit's room output
  reports when its audio left this process at real time (its `AudioSource`
  queue drained), not when anyone heard it, so it counts as heard only on a
  call opened for simulated playout (`DELIVERY=simulated`), as Coval's
  mark-less Twilio route does. A segment cut by a clear is cut to the words
  the transcript synchroniser says played. A pause (LiveKit's false
  interruption) drops what the output had queued, so a paused segment can
  never count as heard (`CallContext.note_gap`).
* **Interruptions.** LiveKit decides when speech over the agent interrupts it
  (VAD or its adaptive detector, which lets a backchannel through). Whatever it
  decides reaches here as a clear of the output, or as the reply's speech
  handle interrupted, and becomes the engine's barge-in (`interrupt`).
  Words from the caller while a turn is still working and nothing of it has
  played (agent `thinking`) drop that reply, as Pipecat does on any new user
  turn: LiveKit 1.8 would otherwise hold it and play it after them.
* **Onsets and silence.** Each start of caller speech is an onset on the
  segments' clock (`note_onset`), so a yes that began before a read-back
  finished playing is not an answer to it. A caller silent for
  `IDLE_SECONDS` after the last words played is reminded by code
  (`core.turn.remind`), also while nothing else runs.

Private LiveKit internals this reads, each failing closed: the speech handle
of the running reply (`_SpeechHandleContextVar`, how a segment is bound to the
reply that plays it) and whether the session is closing (`_is_closing`). If
either goes, segments are not bound and nothing is heard, or a closing session
is taken as a barge-in. Pinned to livekit-agents 1.8.

Imports LiveKit (the `livekit` extra); no other `core` module but
`core.livekit_launch` does.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from opentelemetry import context as otel_context
from opentelemetry import trace

try:
    from livekit import rtc
    from livekit.agents import llm
    from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, FlushSentinel
    from livekit.agents.voice import Agent, AgentSession, ModelSettings
    from livekit.agents.voice import io as lkio
    from livekit.agents.voice.agent_activity import _SpeechHandleContextVar
    from livekit.agents.voice.events import (
        AgentStateChangedEvent,
        ConversationItemAddedEvent,
        SpeechCreatedEvent,
        UserInputTranscribedEvent,
        UserStateChangedEvent,
        UserTranscriptionTimeoutEvent,
    )
    from livekit.agents.voice.speech_handle import SpeechHandle
except ImportError as missing:
    raise ImportError(
        f'core.livekit needs the livekit extra: install slng-voice-core[livekit] ({missing})'
    ) from missing

from core import turn as core
from core.graph import Node
from core.policies import TurnBudget
from core.state import CallContext, Question
from core.voice import outcome

_tracer = trace.get_tracer('slng-voice-core')

ONSET_LEAD = 0.25
"""Seconds before LiveKit says the caller is speaking that their speech is taken to have begun.

Its VAD confirms speech only after `min_speech_duration` (Silero's 0.05 s)
and an inference window, and the state change is stamped when it is emitted.
An onset taken early can only stop a reply from answering a read-back that
was still playing, never let one through, so this errs early.
"""

GATE_SECONDS = 10.0
"""Longest a change waits for its reply to be allowed to play (`FlowAgent._authorized`) before it goes anyway."""

FAILED_TURNS = 3
"""Turns in a row the engine may report failed (a dead model provider) before the line is closed."""

IDLE_SECONDS = 20.0
"""Silence after the last words played before the caller is asked if they are still there (`core.launch.CallTiming`)."""


def _perf(wall: float) -> float:
    """A LiveKit timestamp (`time.time()`) on the segments' clock (`time.perf_counter()`)."""
    return time.perf_counter() - (time.time() - wall)


def _words(text: str) -> int:
    return len(text.split())


class FlowLLM(llm.LLM):
    """The session's LLM slot, filled so LiveKit runs its reply pipeline: the flow answers in `FlowAgent.llm_node`.

    LiveKit generates a reply only when the session has an LLM, and calls the
    agent's `llm_node` to do it; the node never calls `chat`. A request that
    reached here would bypass the flow, so it raises.
    """

    @property
    def model(self) -> str:
        return 'flow'

    @property
    def provider(self) -> str:
        return 'slng-voice-core'

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: Any = NOT_GIVEN,
        tool_choice: Any = NOT_GIVEN,
        extra_kwargs: Any = NOT_GIVEN,
    ) -> llm.LLMStream:
        raise RuntimeError("the flow answers in FlowAgent.llm_node; LiveKit's LLM is never called")


@dataclass
class _Reply:
    """One LiveKit reply the flow is speaking into: its speech handle and the stream the node relays."""

    handle: SpeechHandle | None
    stream: asyncio.Queue[str | FlushSentinel | None] = field(default_factory=asyncio.Queue)
    segment: str = ''
    """The call's segment being streamed now: text for another one starts a new LiveKit segment."""
    boundary: bool = False
    """A segment ended after some text: the next one opens with a space, as LiveKit joins them with nothing."""
    played: bool = False
    """The room started playing something of this reply (`SegmentPlayout.on_playback_started`)."""

    def put(self, text: str) -> None:
        if self.boundary:
            text, self.boundary = f' {text.lstrip()}', False
        self.stream.put_nowait(text)

    def flush(self) -> None:
        self.stream.put_nowait(FlushSentinel())
        self.boundary = True

    def close_segment(self) -> None:
        if self.segment:
            self.flush()
            self.segment = ''


@dataclass
class _Playing:
    """One LiveKit segment on its way out: the call's segment it carries and what the outputs saw of it."""

    segment: str
    mark: str
    before: int
    """How many segments the output below had counted when this one began: more at its flush means it reached it."""
    frames: int = 0
    counted: bool = False
    """This output counted it as a playback segment, so LiveKit waits for one report of it."""
    started: bool = False
    gapped: bool = False


class SegmentPlayout(lkio.AudioOutput):
    """The top of the session's audio chain: each segment's playback, told to the call.

    LiveKit forwards a reply's segments one after another, each as frames and
    one `flush`, and its room output answers each segment that reached it with
    one `playback_finished` (interrupted when a clear came first). A segment is
    bound to the call's segment the reply registered for it, in order
    (`FlowAgent._bind`): the k-th segment a speech forwards is the k-th it
    registered. A segment that sent no frames is silent, and one whose synthesis
    failed may be partial: neither is ever heard.

    Each segment is started on its own (`CallContext.note_started`) when the
    room begins playing it: the end of one segment never counts the next as
    played, since LiveKit plays the next only once this one is out. A segment
    the room never counted (its first frame held below, then cut) is answered
    here at once, so LiveKit waits for no report that will never come and the
    next segment's report stays its own.

    What the room output reports is its own playout clock: the audio was handed
    to WebRTC at real time. That is recorded as `local`, which counts as heard
    only on a call opened for simulated playout (see the module docstring).
    """

    def __init__(self, agent: FlowAgent, next_in_chain: lkio.AudioOutput) -> None:
        super().__init__(
            label='core.SegmentPlayout',
            capabilities=lkio.AudioOutputCapabilities(pause=True),
            next_in_chain=next_in_chain,
            sample_rate=next_in_chain.sample_rate,
        )
        self._agent = agent
        self._call = agent.call
        self._current: _Playing | None = None
        """The segment whose frames are arriving now."""
        self._flushed: deque[_Playing] = deque()
        """Segments that reached the room output and wait for their `playback_finished`, oldest first."""
        self.counts = {'segments': 0, 'played': 0, 'cut': 0, 'silent': 0, 'gapped': 0}

    @property
    def _next(self) -> lkio.AudioOutput:
        assert self.next_in_chain is not None
        return self.next_in_chain

    def _begin(self) -> _Playing:
        segment = self._agent._bind()
        if segment:
            self.counts['segments'] += 1
        return _Playing(
            segment,
            self._call.mark_for(segment) if segment else '',
            self._next.captured_playout_segments,
        )

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        """Count the frame here, then hand it on; it is the segment's only once the output below took it.

        The output below may hold a frame (the room until the caller subscribes,
        the transcript synchroniser between segments), and LiveKit may cancel the
        segment while it does: that frame never reached the room.
        """
        if self._current is None:
            self._current = self._begin()
        playing = self._current
        await super().capture_frame(frame)
        playing.counted = True
        await self._next.capture_frame(frame)
        playing.frames += 1

    def flush(self) -> None:
        super().flush()
        playing, self._current = self._current or self._begin(), None
        reached = self._next.captured_playout_segments > playing.before
        if playing.mark:
            self._call.note_mark_sent(
                playing.mark, time.perf_counter(), media=playing.frames if reached else 0
            )
        if reached:
            self._flushed.append(playing)
        else:
            if playing.segment:
                self.counts['silent'] += 1
            if playing.counted:
                lkio.AudioOutput.on_playback_finished(self, playback_position=0.0, interrupted=True)
        self._next.flush()

    def _mark_input_truncated(self) -> None:
        """The segment's speech synthesis failed (LiveKit retried it first): it may be partial or silent."""
        if self._current is None:
            self._current = self._begin()
        if self._current.mark:
            self._call.note_speech_failed(time.perf_counter())
        super()._mark_input_truncated()

    def clear_buffer(self) -> None:
        """LiveKit stops the audio: the caller cut in, or the agent was interrupted. The engine's barge-in."""
        if (self._current is not None or self._flushed) and not self._agent._closing():
            self._agent.interrupt()
        self._next.clear_buffer()

    def pause(self) -> None:
        """A possible interruption: the room output drops what it queued, so a segment in flight has a gap.

        ponytail: a segment whose last frames were all in the room's 200 ms
        queue plays on through the pause and loses nothing, but is marked too:
        it fails safe (the read-back is asked again). Reading the room's own
        discarded duration is the upgrade.
        """
        for playing in (*self._flushed, self._current):
            if playing is not None and playing.mark and not playing.gapped:
                playing.gapped = True
                self.counts['gapped'] += 1
                self._call.note_gap(playing.mark, time.perf_counter())
        super().pause()

    def on_playback_started(self, *, created_at: float) -> None:
        """The room began playing a segment: the oldest one in flight that had not started."""
        now = time.perf_counter()
        for playing in (*self._flushed, self._current):
            if playing is not None and not playing.started:
                playing.started = True
                if playing.segment:
                    self._call.note_started(playing.segment, now)
                self._agent._note_playing()
                break
        super().on_playback_started(created_at=created_at)

    def on_playback_finished(
        self, *, playback_position: float, interrupted: bool, synchronized_transcript: str | None = None
    ) -> None:
        playing = self._flushed.popleft() if self._flushed else None
        if playing is None and interrupted and self._current is not None:
            playing, self._current = self._current, None
        now = time.perf_counter()
        if playing is not None and playing.segment:
            if interrupted:
                self.counts['cut'] += 1
                core.note_spoken(
                    self._call, synchronized_transcript or '', True, segment_ids=(playing.segment,)
                )
            elif self._call.note_mark_returned(playing.mark, now, local=True):
                self.counts['played'] += 1
        super().on_playback_finished(
            playback_position=playback_position,
            interrupted=interrupted,
            synchronized_transcript=synchronized_transcript,
        )


class SegmentText(lkio.TextOutput):
    """The top of a text-only session's chain: each segment the reader received is acknowledged.

    For a session with no audio output (LiveKit's text mode, a chat client):
    the text runtime's own signal, as `core.text_turn`'s reader gives
    (`CallContext.acknowledge`). A segment whose forwarding was cut short is
    acknowledged for the words that went out.
    """

    def __init__(self, agent: FlowAgent, next_in_chain: lkio.TextOutput | None = None) -> None:
        super().__init__(label='core.SegmentText', next_in_chain=next_in_chain)
        self._agent = agent
        self._segment: str | None = None
        self._text = ''

    async def capture_text(self, text: str) -> None:
        if self._segment is None:
            self._segment = self._agent._bind()
        self._text += text
        if self.next_in_chain is not None:
            await self.next_in_chain.capture_text(text)

    def flush(self) -> None:
        segment, text = self._segment or self._agent._bind(), self._text
        self._segment, self._text = None, ''
        if segment:
            spoken = next((s for s in self._agent.call.all_spoken if s.id == segment), None)
            whole = spoken is not None and _words(text) >= _words(spoken.text)
            self._agent.call.acknowledge(segment, heard=None if whole else text)
        if self.next_in_chain is not None:
            self.next_in_chain.flush()


def _seconds(value: object) -> float | None:
    """A latency in seconds as LiveKit reported it, or None when it is missing, negative or not a number."""
    if isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value < float('inf'):
        return float(value)
    return None


class CallTrace:
    """The call's spans on LiveKit, in the shape unmute-core's adapters write on both frameworks.

    unmute-core's span contract (its `docs/TELEMETRY.md`): `conversation` is the call, the root of a
    trace of its own; a `turn` is one caller turn and everything the agent does until the next; `stt`
    is one caller message and `tts` one piece of speech. Here a turn is one caller turn as core counts
    it: a caller who goes on before any answer played continues it (`CallContext.continues_turn`), and
    the agent speaking with no turn open (the greeting) opens one with no caller words. The model
    requests are Pydantic AI's own `chat` spans, under the span of the step that answered, under the
    turn.

    Every number is one LiveKit measured and put on its messages, never estimated here: a turn's
    `turn.e2e_seconds` (the caller stopped, the agent's first audio played), `turn.stt_final_seconds`,
    `turn.eou_seconds` (how long end of turn waited), `turn.llm_ttft_seconds` (the node's first words,
    every model request before them included) and `turn.tts_ttfb_seconds`; an `stt` span's
    `metrics.ttfb` (the caller stopped, the final transcript) and a `tts` span's (its first text, its
    first audio). A missing one is left out, never sent as 0. The
    ledger read these as they read Pipecat's own `turn`, `stt` and `tts` spans.
    """

    def __init__(self, call: CallContext) -> None:
        self.call = call
        self.root: trace.Span | None = None
        self.turn: trace.Span | None = None
        self._number = 0
        self._seen: set[str] = set()
        self._callers: list[llm.ChatMessage] = []
        self._replies: list[llm.ChatMessage] = []
        self._lines: list[str] = []
        self._transcript: list[dict[str, str]] = []

    def start(self) -> None:
        """Open the call's root span, `conversation`, in a new trace. A second call does nothing."""
        if self.root is None:
            self.root = _tracer.start_span(
                'conversation',
                context=otel_context.Context(),
                attributes={'gen_ai.operation.name': 'invoke_workflow', 'runtime': 'livekit'},
            )

    def current(self) -> contextlib.AbstractContextManager[Any]:
        """The call's span made current, so what LiveKit's session starts inside it (its own spans) is in the call's trace."""
        if self.root is None:
            return contextlib.nullcontext()
        return trace.use_span(self.root, end_on_exit=False)

    def context(self) -> otel_context.Context:
        """Where a span about the turn now running goes: under the open turn, or the call."""
        parent = self.turn or self.root
        return trace.set_span_in_context(parent) if parent is not None else otel_context.Context()

    def caller(self, messages: list[llm.ChatMessage], *, continues: bool) -> None:
        """A caller turn starts (or goes on, `continues`) with these messages: each one is an `stt` span.

        A message already recorded is not recorded again, and a typed one (no speech timings) has none.
        """
        if self.root is None:
            return
        if self.turn is None or not continues:
            self._open()
        for message in messages:
            if message.id in self._seen:
                continue
            self._seen.add(message.id)
            self._callers.append(message)
            self._stt(message)

    def agent_line(self, message: llm.ChatMessage) -> None:
        """A line LiveKit added to the agent's history, with its timings: it belongs to the open turn."""
        if self.root is None or not message.text_content:
            return
        if self.turn is None:
            self._open()
        self._replies.append(message)
        self._lines.append(message.text_content)

    def start_tts(self, tts: Any) -> trace.Span | None:
        """Open a `tts` span for one piece of speech by `tts` (the session's plugin), under the open turn."""
        if self.root is None:
            return None
        if self.turn is None:
            self._open()
        voice = getattr(getattr(tts, '_opts', None), 'voice', None)
        attributes = {
            'langfuse.observation.type': 'generation',
            'gen_ai.operation.name': 'tts',
            'gen_ai.output.type': 'speech',
            'gen_ai.provider.name': str(getattr(tts, 'provider', 'unknown') or 'unknown').lower(),
            'gen_ai.request.model': str(getattr(tts, 'model', 'unknown') or 'unknown'),
        }
        if voice:
            attributes['tts.voice'] = str(voice)
        return _tracer.start_span('tts', context=self.context(), attributes=attributes)

    @staticmethod
    def end_tts(span: trace.Span, text: str, ttfb: float | None, *, interrupted: bool) -> None:
        """Close a `tts` span with what it was given to say and how long its first audio took."""
        attributes: dict[str, Any] = {
            'input.value': text,
            'metrics.character_count': len(text),
            'tts.interrupted': interrupted,
        }
        if (first := _seconds(ttfb)) is not None:
            attributes['metrics.ttfb'] = first
        span.set_attributes(attributes)
        span.end()

    def _open(self) -> None:
        self._end_turn()
        self._number += 1
        self.turn = _tracer.start_span(
            'turn',
            context=trace.set_span_in_context(self.root) if self.root is not None else None,
            attributes={'turn.number': self._number, 'gen_ai.operation.name': 'invoke_agent'},
        )

    def _stt(self, message: llm.ChatMessage) -> None:
        metrics = message.metrics
        started, heard = metrics.get('started_speaking_at'), metrics.get('stt_metadata')
        if started is None or heard is None:
            return
        attributes: dict[str, Any] = {
            'langfuse.observation.type': 'generation',
            'gen_ai.operation.name': 'stt',
            'gen_ai.provider.name': str(heard.get('model_provider') or 'unknown').lower(),
            'gen_ai.request.model': str(heard.get('model_name') or 'unknown'),
        }
        span = _tracer.start_span(
            'stt', context=self.context(), attributes=attributes, start_time=int(started * 1e9)
        )
        ended: dict[str, Any] = {'output.value': message.text_content or ''}
        if (delay := _seconds(metrics.get('transcription_delay'))) is not None:
            ended['metrics.ttfb'] = delay
        span.set_attributes(ended)
        span.end()

    def _end_turn(self) -> None:
        turn = self.turn
        if turn is None:
            return

        def first(messages: list[llm.ChatMessage], name: str) -> float | None:
            return next(
                (found for m in messages if (found := _seconds(m.metrics.get(name))) is not None), None
            )

        heard = ' '.join(m.text_content or '' for m in self._callers).strip()
        if heard:
            self._transcript.append({'role': 'user', 'content': heard})
        self._transcript += [{'role': 'assistant', 'content': line} for line in self._lines]
        attributes: dict[str, Any] = {
            'turn.interrupted': any(m.interrupted for m in self._replies),
            'slng.turn': self.call.turn,
        }
        if heard:
            attributes['input.value'] = heard
        if self._lines:
            attributes['output.value'] = ' '.join(self._lines)
        latencies = {
            'turn.e2e_seconds': first(self._replies, 'e2e_latency'),
            'turn.stt_final_seconds': first(self._callers, 'transcription_delay'),
            'turn.eou_seconds': first(self._callers, 'end_of_turn_delay'),
            'turn.llm_ttft_seconds': first(self._replies, 'llm_node_ttft'),
            'turn.tts_ttfb_seconds': first(self._replies, 'tts_node_ttfb'),
        }
        attributes |= {key: value for key, value in latencies.items() if value is not None}
        turn.set_attributes(attributes)
        turn.end()
        self.turn = None
        self._callers, self._replies, self._lines = [], [], []

    def close(self, ending: str) -> None:
        """End the open turn and the call, with its transcript and how it ended. A second call does nothing."""
        if self.root is None:
            return
        self._end_turn()
        root, self.root = self.root, None
        root.set_attributes(
            {
                'input.value': json.dumps(self._transcript, ensure_ascii=False),
                'output.value': json.dumps({'end_reason': ending}),
                'unmute.call.end_reason': ending,
            }
        )
        root.end()


class FlowAgent(Agent):
    """A compiled flow as a LiveKit `Agent`: one caller turn per `llm_node`, the speech it said bound to playback.

    Build one per call, on the call (`StepFlow.open`) and the flow's nodes,
    start the session with it, then `attach` it to that session before the
    first word: the audio tap and the session's events are wired there.
    """

    def __init__(
        self,
        call: CallContext,
        agents: dict[str, Node[Any]],
        *,
        budget: TurnBudget = TurnBudget(),
        idle_seconds: float = IDLE_SECONDS,
        reminders: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            instructions='',
            llm=FlowLLM(),
            turn_handling={'preemptive_generation': {'enabled': False}},
            use_tts_aligned_transcript=False,
            **kwargs,
        )
        self.call = call
        self._agents, self._budget = agents, budget
        self._session: AgentSession | None = None
        self._served: set[str] = set()
        """User messages already served, by id: LiveKit hands each node its whole chat context."""
        self._carried = ''
        self._interrupted = False
        self._barge = core.Barge()
        self._turn: asyncio.Task[None] | None = None
        self._reply: _Reply | None = None
        self._unbound: dict[str, deque[str]] = {}
        """Per LiveKit speech, the call's segments registered and not yet bound to a forwarded segment."""
        self._idle = asyncio.Event()
        self._idle.set()
        self._idle_seconds, self._reminders = idle_seconds, reminders
        self._quiet: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self.playout: SegmentPlayout | None = None
        self.finished: list[core.TurnFinished] = []
        """How each turn ended, in order: for tests and the launcher's log."""
        self._failures = 0
        """Turns in a row the engine reported failed: past `FAILED_TURNS` the line is closed."""
        self._held: list[float] = []
        """When each stretch of the caller's speech began, held until one proves to be a reply (`_commit_onset`)."""
        self._wordless: list[float] = []
        """Starts LiveKit found no words for in time (`_on_no_words`): used only if words come later after all."""
        self.trace = CallTrace(call)
        """The call's spans: `conversation`, a `turn` per caller turn, `stt` and `tts` (`CallTrace`)."""
        call._say = self.say
        call._end = self.hang_up
        call._dispatch_gate = self._authorized
        call._playback_known = True

    def attach(self, session: AgentSession) -> None:
        """Wire this agent into its started session: the audio (or text) tap, and the events a turn needs."""
        self._session = session
        self.trace.start()
        if session.output.audio is not None:
            self.playout = SegmentPlayout(self, session.output.audio)
            session.output.audio = self.playout
        else:
            session.output.transcription = SegmentText(self, session.output.transcription)
            self.call.note_transport('livekit-text')
        session.on('speech_created', self._on_speech_created)
        session.on('user_state_changed', self._on_user_state)
        session.on('agent_state_changed', self._on_agent_state)
        session.on('user_input_transcribed', self._on_transcribed)
        session.on('user_transcription_timeout', self._on_no_words)
        session.on('conversation_item_added', self._on_item)
        session.on('close', lambda _event: self._cancel_quiet())

    @property
    def live(self) -> AgentSession:
        if self._session is None:
            raise RuntimeError('FlowAgent.attach(session) was not called')
        return self._session

    def _closing(self) -> bool:
        session = self._session
        if session is None:
            return False
        closing = getattr(session, '_is_closing', None)
        return bool(closing()) if callable(closing) else bool(getattr(session, '_closing', False))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- speech ---------------------------------------------------------------------------------

    def _register(self, handle: SpeechHandle | None, segment: str) -> None:
        if handle is not None and segment:
            self._unbound.setdefault(handle.id, deque()).append(segment)

    def _bind(self) -> str:
        """The call's segment LiveKit is forwarding now: the next one its current speech registered, or ''."""
        current = self._session.current_speech if self._session is not None else None
        queue = self._unbound.get(current.id) if current is not None else None
        return queue.popleft() if queue else ''

    async def say(self, text: str) -> None:
        """Put one fixed line on the wire, after whatever is queued ahead of it.

        Inside a turn it joins that turn's reply as a segment of its own, so it
        plays in order with the model's words; with no turn running it is its
        own LiveKit speech. The call has already recorded it (`CallContext.say`). Once
        the session is closing, a turn left to finish its write says nothing more.
        """
        if not text.strip():
            return
        segment = next((s for s in reversed(self.call.spoken) if s.text == text), None)
        reply = self._reply
        if reply is not None and (reply.handle is None or not reply.handle.done()):
            reply.close_segment()
            if segment is not None:
                self._register(reply.handle, segment.id)
            reply.put(text)
            reply.flush()
            return
        if self._closing():
            logger.info('the session is closing: {!r} goes nowhere', text)
            return
        handle = self.live.say(text, add_to_chat_ctx=True)
        if segment is not None:
            self._register(handle, segment.id)

    async def greet(self, line: str | Question) -> None:
        """Open the call with a fixed line, recorded as said (`core.turn.greet`)."""
        await self.say(core.greet(self.call, line))

    async def hang_up(self) -> None:
        """Close the line once what is queued has played: the session drains its speech, then closes."""
        self._spawn(self._hang_up())

    async def _hang_up(self) -> None:
        with contextlib.suppress(Exception):
            if self._turn is not None and self._turn is not asyncio.current_task():
                await asyncio.wait({self._turn}, timeout=30)
            self.live.shutdown(drain=True)

    # --- turns ----------------------------------------------------------------------------------

    async def llm_node(
        self, chat_ctx: llm.ChatContext, tools: list[llm.Tool], model_settings: ModelSettings
    ) -> AsyncIterable[str | FlushSentinel]:
        """One caller turn: the words LiveKit committed and this agent has not served, run by the flow.

        The turn runs as its own task and this node relays what it says. Cancelled
        by LiveKit (the reply interrupted), the turn is barged in on and cancelled,
        as Pipecat cancels a turn the caller talks over; cancelled because the
        session is closing, it is left to finish (`finish_turn`).

        The previous turn, if it is still unwinding, is waited for without
        swallowing a cancel of this node; the words count as served only once
        their turn exists, so a node cancelled before then leaves them for the next.

        Never speculative: this agent turns LiveKit's preemptive generation off,
        and a reply not yet scheduled (a preemptive one) waits here until it is,
        so a turn's writes run only for words LiveKit committed.
        """
        handle = _SpeechHandleContextVar.get(None)
        while handle is not None and not handle.scheduled and not handle.done():
            await asyncio.sleep(0.02)
        users = [
            item
            for item in chat_ctx.items
            if item.type == 'message' and item.role == 'user' and item.id not in self._served
        ]
        prompt = ' '.join(text for item in users if (text := (item.text_content or '').strip())) or None
        if prompt:
            self._commit_onset()
        if self._turn is not None and not self._turn.done():
            self.interrupt()
            self._turn.cancel()
            await asyncio.wait({self._turn})
        reply = self._reply = _Reply(handle)
        self._cancel_quiet()
        self._turn = asyncio.create_task(self.serve(prompt, reply, users))
        self._served.update(item.id for item in users)
        return self._relay(reply, self._turn)

    async def _relay(self, reply: _Reply, task: asyncio.Task[None]) -> AsyncIterator[str | FlushSentinel]:
        """Pass on what the turn says, until it ends; cancelled by LiveKit, decide what the cancel was.

        A barge-in only when the reply was interrupted (or nothing says it was
        not). LiveKit also cancels a node whose speech synthesis failed: then the
        caller said nothing, so the turn is left to finish what it is doing, its
        speech going nowhere, and the segments waiting for audio are failed.
        """
        try:
            while (item := await reply.stream.get()) is not None:
                yield item
        except BaseException:
            if not task.done() and not self._closing():
                if reply.handle is None or reply.handle.interrupted:
                    self.interrupt()
                    task.cancel()
                else:
                    logger.warning('LiveKit stopped this reply without an interruption: its speech failed')
                    self.call.note_speech_failed(time.perf_counter())
            raise
        finally:
            if self._reply is reply:
                self._reply = None

    async def serve(
        self, prompt: str | None, reply: _Reply | None = None, heard: list[llm.ChatMessage] | None = None
    ) -> None:
        """One caller turn, marked as running until it is over, so a dropped line can let it finish.

        `heard` is LiveKit's messages the turn is made of, with the timings it measured on them (`CallTrace`).
        """
        self._idle.clear()
        try:
            await self._run(prompt, reply or _Reply(None), heard or [])
        finally:
            self._idle.set()
            if reply is not None:
                reply.stream.put_nowait(None)

    async def _run(self, prompt: str | None, reply: _Reply, heard: list[llm.ChatMessage]) -> None:
        """One caller turn, translated from the engine's events into LiveKit segments.

        The same bookkeeping as `core.processor.PydanticAgentProcessor._turn`:
        words the last turn never got into the history are carried to the front
        of this one unless this one continues it, one span per turn named after
        the step that answered, a turn that unwinds without a `TurnFinished` is
        given one, and a cancellation that was not a barge-in is the transport.
        """
        continues = self.call.continues_turn()
        if self._carried:
            if not continues:
                prompt = f'{self._carried} {prompt}'.strip() if prompt else self._carried
            self._carried = ''
        if heard or self.trace.turn is None:
            self.trace.caller(heard, continues=continues)
        with _tracer.start_as_current_span(self.call.state.agent, context=self.trace.context()) as span:
            if prompt:
                span.set_attribute('langfuse.observation.input', prompt)
            span.set_attribute('runtime', 'livekit')
            started = time.perf_counter()
            spoke = False
            self._barge = core.Barge()
            self._interrupted = False
            finished: core.TurnFinished | None = None
            try:
                async for event in core.run_turn(self.call, self._agents, prompt, self._budget, self._barge):
                    match event:
                        case core.TextDelta(text=text) if text:
                            if not spoke:
                                spoke = True
                                span.set_attribute('turn_first_word', round(time.perf_counter() - started, 3))
                            segment = self.call.spoken[-1] if self.call.spoken else None
                            if (
                                segment is not None
                                and segment.kind == 'model'
                                and segment.id != reply.segment
                            ):
                                reply.close_segment()
                                self._register(reply.handle, segment.id)
                                reply.segment = segment.id
                            reply.put(text)
                        case core.RunEnded():
                            reply.close_segment()
                        case core.FixedSpeech(text=text):
                            reply.close_segment()
                            await self.say(text)
                        case core.TurnFinished() as done:
                            finished = done
                            self._failures = self._failures + 1 if done.status == 'failed' else 0
                            spoken = self.call.spoken
                            if said := ' '.join(s.text for s in spoken if s.text):
                                span.set_attribute('langfuse.observation.output', said)
                            span.set_attribute('speech_segments', [s.id for s in spoken])
                            span.set_attribute('turn_pieces', len(self.call._pieces))
                            if self.call._continued_from:
                                span.set_attribute('continued_from', self.call._continued_from)
                            if self.call.unheard_receipt(repeats=sys.maxsize):
                                span.set_attribute('receipt_unheard', True)
                            span.update_name(done.agent)
                            span.set_attribute('langfuse.observation.type', 'agent')
                            span.set_attribute('agent', done.agent)
                            span.set_attribute('status', done.status)
                            span.set_attribute('model_requests', done.requests)
                            if done.handoffs:
                                span.set_attribute('handoffs', ', '.join(done.handoffs))
                            if done.detail:
                                span.set_attribute('detail', done.detail)
            except asyncio.CancelledError:
                if not self._interrupted:
                    self.call.end_requested('transport')
                raise
            finally:
                reply.close_segment()
                if finished is None:
                    ended = self.call.ended
                    finished = core.TurnFinished(
                        status='interrupted' if self._interrupted else 'failed',
                        agent=self.call.state.agent,
                        handoffs=(),
                        requests=len(self.call.requests),
                        detail=f'the turn never finished ({ended or "no reason recorded"})',
                    )
                    span.set_attribute('status', finished.status)
                    span.set_attribute('detail', finished.detail)
                self.finished.append(finished)
                if self.call.ended:
                    span.set_attribute('ended', self.call.ended)
                self.call.turn_ended(interrupted=self._interrupted)
                if prompt and not self.call.input_retained:
                    self._carried = self.call.fresh
                if self._failures >= FAILED_TURNS and not self.call.ended:
                    logger.error(f'{self._failures} turns in a row failed: closing the line')
                    self.call.end_requested('error')
                    await self.hang_up()

    async def _authorized(self) -> None:
        """A change may be sent once its reply may play: first in LiveKit's queue, and the caller not speaking.

        LiveKit's own rule for running a tool. Until then the caller may still go
        on: words drop the reply (`_on_transcribed`) and cancel the turn here,
        before anything is sent. A reply that never gets there (a speech LiveKit
        did not schedule) is waited for at most `GATE_SECONDS`, then the change
        goes, as it did before the gate.
        """
        reply, session = self._reply, self._session
        if reply is None or reply.handle is None or session is None:
            return
        deadline = time.perf_counter() + GATE_SECONDS
        while time.perf_counter() < deadline:
            if session.current_speech is reply.handle and session.user_state != 'speaking':
                return
            await asyncio.sleep(0.02)
        logger.warning(f'a change waited {GATE_SECONDS:.0f}s for its reply to be allowed to play: sending it')

    def _commit_onset(self) -> None:
        """The caller's speech is a reply (it has words, LiveKit interrupted for it, or it made a turn): note its onset.

        The earliest start held since the last reply: words can come well after
        the caller stopped (a final-only or slow STT), and a reply that paused
        mid-way began at its first stretch. A sound LiveKit found no words in is
        set aside before then (`_on_no_words`), so it never counts while a later
        stretch is held. Words with only set-aside starts behind them are late
        words for the latest of those: that start counts, so a slow STT never
        leaves a reply with no start at all (found live: with none, no read-back
        could ever be agreed to).

        ponytail: a yes said over the end of a read-back whose words came later
        than `TRANSCRIPTION_TIMEOUT`, then a cough after the read-back ended,
        would start at the cough. Word timings from the STT are the upgrade.
        """
        if self._held:
            self.call.note_onset(min(self._held))
        elif self._wordless:
            self.call.note_onset(max(self._wordless))
        self._held.clear()
        self._wordless.clear()

    def _on_no_words(self, event: UserTranscriptionTimeoutEvent) -> None:
        """LiveKit heard speech that gave no words (a cough, a breath): its starts are no reply."""
        since = _perf(event.vad_speech_started_at) - ONSET_LEAD - 0.05
        self._wordless += [t for t in self._held if t >= since]
        self._held = [t for t in self._held if t < since]

    def interrupt(self, *, reply: bool = True) -> None:
        """The caller spoke over us: the engine's barge-in. Told to the call even with no turn running.

        `reply=False` is the bookkeeping barge-in of a caller who starts speaking
        with nothing of ours playing (`_on_user_state`): it proves nothing yet
        about their speech being a reply, so its onset stays held.
        """
        if reply:
            self._commit_onset()
        self._interrupted = True
        self.call.note_barge()
        self._barge.fire()

    async def finish_turn(self, timeout: float) -> None:
        """Wait, for up to `timeout` seconds, for a turn already running to finish (the line has dropped)."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._idle.wait(), timeout)

    async def remind(self) -> None:
        """The caller has been silent since our last words played: `core.turn.remind`, unless a turn runs."""
        if self._idle.is_set() and self._reply is None:
            await core.remind(self.call)

    async def close(self) -> None:
        """Stop what this agent runs on its own (a turn still going, the silence timer, a hang-up), and end its trace."""
        self._cancel_quiet()
        tasks = [t for t in (self._turn, *self._tasks) if t is not None and not t.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.trace.close(outcome(self.call))

    async def tts_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncIterator[rtc.AudioFrame]:
        """LiveKit's own speech synthesis, each piece of speech a `tts` span: from its first text to its first audio."""
        span: trace.Span | None = None
        first_text = 0.0
        ttfb: float | None = None
        spoken: list[str] = []
        done = False

        async def given() -> AsyncIterator[str]:
            nonlocal span, first_text
            async for chunk in text:
                if span is None:
                    first_text = time.perf_counter()
                    span = self.trace.start_tts(self.session.tts)
                spoken.append(chunk)
                yield chunk

        try:
            async for frame in Agent.default.tts_node(self, given(), model_settings):
                if ttfb is None and span is not None:
                    ttfb = time.perf_counter() - first_text
                yield frame
            done = True
        finally:
            if span is not None:
                self.trace.end_tts(span, ''.join(spoken), ttfb, interrupted=not done)

    async def on_enter(self) -> None:
        """Count every caller message already in this agent's history as served: none of it is a new turn.

        A `FlowAgent` built with an earlier agent's history (`update_agent`)
        would otherwise take every past caller message as one fresh turn.
        """
        self._served.update(
            item.id for item in self.chat_ctx.items if item.type == 'message' and item.role == 'user'
        )

    async def on_user_turn_exceeded(self, ev: Any) -> None:
        """A caller talking past LiveKit's turn limit is left to finish: no reply they cannot interrupt.

        LiveKit's default cuts in with an uninterruptible reply, which here would
        be a whole flow turn, operations and all, that the caller cannot stop.
        """
        logger.info('the caller passed the turn limit; letting them finish')

    def _note_playing(self) -> None:
        """The room started playing a segment: if it is the open reply's, something of it has played."""
        reply, session = self._reply, self._session
        if reply is not None and session is not None and session.current_speech is reply.handle:
            reply.played = True

    # --- session events -------------------------------------------------------------------------

    def _on_item(self, event: ConversationItemAddedEvent) -> None:
        """A line LiveKit added to the agent's history, with its timings: the open turn's (`CallTrace`)."""
        item = event.item
        if isinstance(item, llm.ChatMessage) and item.role == 'assistant':
            self.trace.agent_line(item)

    def _on_speech_created(self, event: SpeechCreatedEvent) -> None:
        handle = event.speech_handle
        handle.add_done_callback(self._on_speech_done)

    def _on_speech_done(self, handle: SpeechHandle) -> None:
        """A speech is over: forget what it never forwarded; an interrupted one is a barge-in."""
        self._unbound.pop(handle.id, None)
        if handle.interrupted and not self._closing():
            self.interrupt()

    def _on_user_state(self, event: UserStateChangedEvent) -> None:
        """The caller started speaking: an onset, and with nothing of ours running or waiting, a barge-in.

        As Pipecat's interruption at every user turn start, so the next words are
        known to come after the caller spoke again (`continues_turn`). A reply
        still running or waiting to play is LiveKit's to interrupt or keep, and
        reaches here as a clear or an interrupted speech if it does.

        The onset is held until the speech proves to be a reply: words for it
        (`_on_transcribed`), LiveKit interrupting for it (`interrupt`), or a turn
        made of it (`llm_node`). A sound LiveKit found no words in (a cough, an
        "mm" it let pass: `user_transcription_timeout`) is dropped, so a
        read-back that played on under it can still be answered after it.

        The caller stopping with nothing of ours running or waiting (a cough, an
        "um" that made no turn) starts the silence timer again: otherwise only the
        agent's next words would, and a silent caller would never be reminded.
        """
        session = self._session
        if event.new_state == 'listening':
            if (
                session is not None
                and session.agent_state == 'listening'
                and self._idle.is_set()
                and self._reply is None
                and session.current_speech is None
            ):
                self._arm_quiet()
            return
        if event.new_state != 'speaking':
            return
        self._cancel_quiet()
        self._held.append(_perf(event.created_at) - ONSET_LEAD)
        if session is not None and self._idle.is_set() and session.current_speech is None:
            self.interrupt(reply=False)

    def _on_transcribed(self, event: UserInputTranscribedEvent) -> None:
        """Words while a turn works and nothing of it plays: drop that reply, as Pipecat does on a new user turn.

        LiveKit 1.8 holds a reply while the caller speaks and plays it once they
        stop, answering words they had not finished. Only words count, never a
        sound: a cough must not cancel a turn that no new turn will replace. Nor
        once anything of the reply has played: a reply LiveKit paused for a
        possible interruption is LiveKit's to resume or cut.
        """
        session = self._session
        if event.transcript.strip():
            self._commit_onset()
        if session is None or not event.transcript.strip() or self._idle.is_set():
            return
        reply = self._reply
        if reply is None or reply.played or session.agent_state == 'speaking':
            return
        handle = reply.handle
        if handle is not None and not handle.done() and handle.allow_interruptions:
            logger.info('the caller spoke while the turn was working: dropping its reply')
            handle.interrupt()

    def _on_agent_state(self, event: AgentStateChangedEvent) -> None:
        if event.new_state == 'listening':
            self._arm_quiet()
        else:
            self._cancel_quiet()

    def _arm_quiet(self) -> None:
        self._cancel_quiet()
        if not self._reminders or self._idle_seconds <= 0 or self._closing():
            return
        self._quiet = asyncio.get_running_loop().call_later(self._idle_seconds, self._quiet_reached)

    def _cancel_quiet(self) -> None:
        if self._quiet is not None:
            self._quiet.cancel()
            self._quiet = None

    def _quiet_reached(self) -> None:
        self._quiet = None
        session = self._session
        if (
            session is None
            or session.user_state == 'speaking'
            or session.agent_state != 'listening'
            or session.current_speech is not None
        ):
            return
        self._spawn(self.remind())


def reports_playback(call: CallContext) -> bool:
    """Whether a read-back can ever be heard on this LiveKit call: what the launcher asks before its greeting.

    LiveKit reports its own playout clock, never the far end's, so only a call
    opened for simulated playout, or a text session whose reader acknowledges
    what it got, can confirm anything.
    """
    return call.simulated_playout or call.transport == 'livekit-text'


def playout(call: CallContext, *, simulate: bool) -> None:
    """Name this call's transport, and let LiveKit's own playout count as heard when the launcher asked for it."""
    if simulate:
        call.simulate_playout()
        logger.warning("simulated playout: LiveKit's own playout counts as heard on this call")
    call.note_transport('livekit')


Wiring = Callable[[FlowAgent, AgentSession], None]
