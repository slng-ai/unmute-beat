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
import sys
import time
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
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
        SpeechCreatedEvent,
        UserInputTranscribedEvent,
        UserStateChangedEvent,
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

_tracer = trace.get_tracer('slng-voice-harness')

ONSET_LEAD = 0.25
"""Seconds before LiveKit says the caller is speaking that their speech is taken to have begun.

Its VAD confirms speech only after `min_speech_duration` (Silero's 0.05 s)
and an inference window, and the state change is stamped when it is emitted.
An onset taken early can only stop a reply from answering a read-back that
was still playing, never let one through, so this errs early.
"""

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

    def put(self, text: str) -> None:
        self.stream.put_nowait(text)

    def close_segment(self) -> None:
        if self.segment:
            self.stream.put_nowait(FlushSentinel())
            self.segment = ''


@dataclass
class _Playing:
    """One LiveKit segment on its way out: the call's segment it carries and what the output saw of it."""

    segment: str
    mark: str
    frames: int = 0
    gapped: bool = False


class SegmentPlayout(lkio.AudioOutput):
    """The top of the session's audio chain: each segment's playback, told to the call.

    LiveKit forwards a reply's segments one after another, each as frames and
    one `flush`, and its room output answers each flushed segment with one
    `playback_finished` (interrupted when a clear came first). A segment is
    bound to the call's segment the reply registered for it, in order
    (`FlowAgent._bind`): the k-th segment a speech forwards is the k-th it
    registered. A segment that sent no frames is silent, and one whose synthesis
    failed may be partial: neither is ever heard.

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
        """Segments flushed and waiting for their `playback_finished`, oldest first."""
        self.counts = {'segments': 0, 'played': 0, 'cut': 0, 'silent': 0, 'gapped': 0}

    @property
    def _next(self) -> lkio.AudioOutput:
        assert self.next_in_chain is not None
        return self.next_in_chain

    def _begin(self) -> _Playing | None:
        segment = self._agent._bind()
        if not segment:
            return None
        self.counts['segments'] += 1
        return _Playing(segment, self._call.mark_for(segment))

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        if self._current is None:
            self._current = self._begin() or _Playing('', '')
        self._current.frames += 1
        await super().capture_frame(frame)
        await self._next.capture_frame(frame)

    def flush(self) -> None:
        super().flush()
        playing, self._current = self._current or self._begin() or _Playing('', ''), None
        if playing.mark:
            self._call.note_mark_sent(playing.mark, time.perf_counter(), media=playing.frames)
        if playing.frames:
            self._flushed.append(playing)
        elif playing.segment:
            self.counts['silent'] += 1
        self._next.flush()

    def _mark_input_truncated(self) -> None:
        """The segment's speech synthesis failed (LiveKit retried it first): it may be partial or silent."""
        if self._current is None:
            self._current = self._begin() or _Playing('', '')
        if self._current.mark:
            self._call.note_speech_failed(time.perf_counter())
        super()._mark_input_truncated()

    def clear_buffer(self) -> None:
        """LiveKit stops the audio: the caller cut in, or the agent was interrupted. The engine's barge-in."""
        if (self._current is not None or self._flushed) and not self._agent._closing():
            self._agent.interrupt()
        self._next.clear_buffer()

    def pause(self) -> None:
        """A possible interruption: the room output drops what it queued, so the segment playing has a gap."""
        for playing in (self._current, *self._flushed):
            if playing is not None and playing.mark and not playing.gapped:
                playing.gapped = True
                self.counts['gapped'] += 1
                self._call.note_gap(playing.mark, time.perf_counter())
        super().pause()

    def on_playback_started(self, *, created_at: float) -> None:
        self._call.note_reached()
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
        if not interrupted and not self._flushed and self._current is None:
            self._call.note_drained()
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
        call._say = self.say
        call._end = self.hang_up
        call._playback_known = True

    def attach(self, session: AgentSession) -> None:
        """Wire this agent into its started session: the audio (or text) tap, and the events a turn needs."""
        self._session = session
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
        segment = next((s for s in reversed(self.call.spoken) if s.text == text), None)
        reply = self._reply
        if reply is not None and (reply.handle is None or not reply.handle.done()):
            reply.close_segment()
            if segment is not None:
                self._register(reply.handle, segment.id)
            reply.put(text)
            reply.stream.put_nowait(FlushSentinel())
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
        self._served.update(item.id for item in users)
        prompt = ' '.join(text for item in users if (text := (item.text_content or '').strip())) or None
        if self._turn is not None and not self._turn.done():
            self.interrupt()
            self._turn.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._turn
        reply = self._reply = _Reply(handle)
        self._cancel_quiet()
        self._turn = asyncio.create_task(self.serve(prompt, reply))
        return self._relay(reply, self._turn)

    async def _relay(self, reply: _Reply, task: asyncio.Task[None]) -> AsyncIterator[str | FlushSentinel]:
        try:
            while (item := await reply.stream.get()) is not None:
                yield item
        except BaseException:
            if not task.done() and not self._closing():
                self.interrupt()
                task.cancel()
            raise
        finally:
            if self._reply is reply:
                self._reply = None

    async def serve(self, prompt: str | None, reply: _Reply | None = None) -> None:
        """One caller turn, marked as running until it is over, so a dropped line can let it finish."""
        self._idle.clear()
        try:
            await self._run(prompt, reply or _Reply(None))
        finally:
            self._idle.set()
            if reply is not None:
                reply.stream.put_nowait(None)

    async def _run(self, prompt: str | None, reply: _Reply) -> None:
        """One caller turn, translated from the engine's events into LiveKit segments.

        The same bookkeeping as `core.processor.PydanticAgentProcessor._turn`:
        words the last turn never got into the history are carried to the front
        of this one unless this one continues it, one span per turn named after
        the step that answered, a turn that unwinds without a `TurnFinished` is
        given one, and a cancellation that was not a barge-in is the transport.
        """
        if self._carried:
            if not self.call.continues_turn():
                prompt = f'{self._carried} {prompt}'.strip() if prompt else self._carried
            self._carried = ''
        with _tracer.start_as_current_span(self.call.state.agent) as span:
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

    def interrupt(self) -> None:
        """The caller spoke over us: the engine's barge-in. Told to the call even with no turn running."""
        self._interrupted = True
        self.call.note_barge()
        self._barge.fire()

    async def finish_turn(self, timeout: float) -> None:
        """Wait, for up to `timeout` seconds, for a turn already running to finish (the line has dropped)."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._idle.wait(), timeout)

    async def remind(self) -> None:
        """The caller has been silent since our last words played: `core.turn.remind`, unless a turn runs."""
        if self._idle.is_set():
            await core.remind(self.call)

    async def close(self) -> None:
        """Stop what this agent runs on its own: a turn still going, the silence timer, a hang-up."""
        self._cancel_quiet()
        tasks = [t for t in (self._turn, *self._tasks) if t is not None and not t.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # --- session events -------------------------------------------------------------------------

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
        """
        if event.new_state != 'speaking':
            return
        self._cancel_quiet()
        self.call.note_onset(_perf(event.created_at) - ONSET_LEAD)
        session = self._session
        if session is not None and self._idle.is_set() and session.current_speech is None:
            self.interrupt()

    def _on_transcribed(self, event: UserInputTranscribedEvent) -> None:
        """Words while a turn works and nothing of it plays: drop that reply, as Pipecat does on a new user turn.

        LiveKit 1.8 holds a reply while the caller speaks and plays it once they
        stop, answering words they had not finished. Only words count, never a
        sound: a cough must not cancel a turn that no new turn will replace.
        """
        session = self._session
        if session is None or not event.transcript.strip() or self._idle.is_set():
            return
        if session.agent_state == 'speaking':
            return
        reply = self._reply
        handle = reply.handle if reply is not None else None
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
        if session is None or session.user_state == 'speaking' or session.agent_state != 'listening':
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
