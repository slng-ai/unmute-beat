"""A caller, a voice and a speaker for a real LiveKit `AgentSession`, offline: what `test_livekit` drives.

Everything LiveKit runs is its own: the session, its turn taking, its
interruption handling, its transcript synchroniser and its reply pipeline.
Only the edges are made up, as LiveKit's own tests make them up (adapted
from livekit/agents `tests/fake_vad.py`, `fake_stt.py` and `fake_io.py`,
Apache-2.0):

* `Caller` is the person on the line, driven by the test as it goes: it
  speaks for so long, with these words, and both the VAD and the STT hear it,
  the VAD at once and the STT a little later, interim words first;
* `Voice` is a TTS: `SECONDS_PER_CHAR` of silent audio per character, none for
  a line that starts with `[silent]`;
* `Speaker` is the room's audio output, with LiveKit's own semantics: a
  virtual playout clock, `playback_finished` once a flushed segment's audio
  has played, interrupted with its position on a clear, held while paused.

Every wait is real time, kept short.
"""

import asyncio
import contextlib
import time
from dataclasses import dataclass

from livekit import rtc
from livekit.agents import NOT_GIVEN, LanguageCode, NotGivenOr, tts, utils
from livekit.agents.stt import STT, RecognizeStream, SpeechData, SpeechEvent, SpeechEventType, STTCapabilities
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions
from livekit.agents.utils.audio import AudioBuffer
from livekit.agents.vad import VAD, VADCapabilities, VADEvent, VADEventType, VADStream
from livekit.agents.voice.io import AudioInput, AudioOutput, AudioOutputCapabilities, TextOutput

SAMPLE_RATE = 24000
SECONDS_PER_CHAR = 0.012
"""How long the voice takes per character: a 60-character read-back plays for about 0.7 s."""
STT_DELAY = 0.1
"""How long after the caller stops their final words arrive."""
VAD_TICK = 0.05


@dataclass
class _Utterance:
    words: str
    seconds: float
    final: bool = True


class Caller:
    """The person on the line. `await caller.says('Yes.')` speaks, and returns once they have stopped.

    Each LiveKit VAD and STT stream registers here when the session opens it,
    so a handoff's new streams hear the same caller.
    """

    def __init__(self, min_silence: float = 0.3) -> None:
        self.min_silence = min_silence
        self._vads: list[_VADStream] = []
        self._stts: list[_STTStream] = []
        self.spoke: list[tuple[str, float, float]] = []
        """Each utterance: its words, and when it began and ended on `time.perf_counter`."""

    async def says(
        self, words: str, seconds: float | None = None, *, final: bool = True, late: float = 0.0
    ) -> None:
        """Speak `words` for `seconds` (by default as long as the voice would take), then stop.

        The VAD hears the start at once; the STT sends the first two words as
        an interim halfway through, and the whole as final `STT_DELAY` after
        the end, unless `final=False` (a sound with no words, if `words` is '').
        `late` is an STT that sends no interim and its final that long after the
        VAD heard the caller stop, as a slow or final-only STT does.
        """
        seconds = seconds if seconds is not None else max(0.3, len(words) * SECONDS_PER_CHAR)
        started = time.perf_counter()
        for vad in self._vads:
            vad.speech_started()
        interim_at = seconds / 2
        elapsed = 0.0
        sent_interim = False
        while elapsed < seconds:
            await asyncio.sleep(VAD_TICK)
            elapsed = time.perf_counter() - started
            for vad in self._vads:
                vad.speaking(elapsed)
            if words and not late and not sent_interim and elapsed >= interim_at:
                sent_interim = True
                for stt in self._stts:
                    stt.transcript(' '.join(words.split()[:2]), final=False)
        ended = time.perf_counter()
        self.spoke.append((words, started, ended))
        for vad in self._vads:
            vad.silence_started(seconds)

        if late:
            await asyncio.sleep(self.min_silence)
            for vad in self._vads:
                vad.speech_ended(seconds, self.min_silence)
            await asyncio.sleep(late)
            if words and final:
                for stt in self._stts:
                    stt.transcript(words, final=True)
            return
        await asyncio.sleep(STT_DELAY)
        if words and final:
            for stt in self._stts:
                stt.transcript(words, final=True)
        await asyncio.sleep(max(0.0, self.min_silence - STT_DELAY))
        for vad in self._vads:
            vad.speech_ended(seconds, self.min_silence)


class FakeVAD(VAD):
    def __init__(self, caller: Caller) -> None:
        super().__init__(capabilities=VADCapabilities(update_interval=VAD_TICK))
        self._caller = caller

    @property
    def model(self) -> str:
        return 'fake-vad'

    @property
    def provider(self) -> str:
        return 'test'

    def stream(self) -> VADStream:
        stream = _VADStream(self)
        self._caller._vads.append(stream)
        return stream


class _VADStream(VADStream):
    def __init__(self, vad: FakeVAD) -> None:
        super().__init__(vad)
        self._origin = time.perf_counter()

    async def _main_task(self) -> None:
        async for _ in self._input_ch:
            pass

    def _send(self, type_: VADEventType, speech: float, silence: float, *, speaking: bool) -> None:
        with contextlib.suppress(Exception):
            self._event_ch.send_nowait(
                VADEvent(
                    type=type_,
                    samples_index=0,
                    timestamp=time.perf_counter() - self._origin,
                    speech_duration=speech,
                    silence_duration=silence,
                    speaking=speaking,
                    probability=1.0 if speaking else 0.0,
                    raw_accumulated_speech=speech if speaking else 0.0,
                    raw_accumulated_silence=silence,
                )
            )

    def speech_started(self) -> None:
        self._send(VADEventType.START_OF_SPEECH, VAD_TICK, 0.0, speaking=True)

    def speaking(self, elapsed: float) -> None:
        self._send(VADEventType.INFERENCE_DONE, elapsed, 0.0, speaking=True)

    def silence_started(self, speech: float) -> None:
        self._send(VADEventType.INFERENCE_DONE, speech, VAD_TICK, speaking=False)

    def speech_ended(self, speech: float, silence: float) -> None:
        self._send(VADEventType.END_OF_SPEECH, speech, silence, speaking=False)


class FakeSTT(STT):
    def __init__(self, caller: Caller) -> None:
        super().__init__(capabilities=STTCapabilities(streaming=True, interim_results=True))
        self._caller = caller

    @property
    def model(self) -> str:
        return 'fake-stt'

    @property
    def provider(self) -> str:
        return 'test'

    async def _recognize_impl(
        self, buffer: AudioBuffer, *, language: NotGivenOr[str] = NOT_GIVEN, conn_options: APIConnectOptions
    ) -> SpeechEvent:
        return SpeechEvent(
            type=SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[SpeechData(text='', language=LanguageCode('en'))],
        )

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> RecognizeStream:
        stream = _STTStream(stt=self, conn_options=conn_options)
        self._caller._stts.append(stream)
        return stream


class _STTStream(RecognizeStream):
    async def _run(self) -> None:
        async for _ in self._input_ch:
            pass

    def transcript(self, text: str, *, final: bool) -> None:
        kind = SpeechEventType.FINAL_TRANSCRIPT if final else SpeechEventType.INTERIM_TRANSCRIPT
        with contextlib.suppress(Exception):
            self._event_ch.send_nowait(
                SpeechEvent(type=kind, alternatives=[SpeechData(text=text, language=LanguageCode('en'))])
            )


class Voice(tts.TTS):
    """Silent audio, `SECONDS_PER_CHAR` a character, after `ttfb`; none for a line that starts with `[silent]`."""

    def __init__(self, ttfb: float = 0.05) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False), sample_rate=SAMPLE_RATE, num_channels=1
        )
        self.ttfb = ttfb
        self.said: list[str] = []

    @property
    def model(self) -> str:
        return 'fake-voice'

    @property
    def provider(self) -> str:
        return 'test'

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        return _Chunks(tts=self, input_text=text, conn_options=conn_options)


class _Chunks(tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        voice = self._tts
        assert isinstance(voice, Voice)
        voice.said.append(self._input_text)
        output_emitter.initialize(
            request_id=utils.shortuuid('voice_'),
            sample_rate=SAMPLE_RATE,
            num_channels=1,
            mime_type='audio/pcm',
        )
        await asyncio.sleep(voice.ttfb)
        if not self._input_text.strip().startswith('[silent]'):
            samples = int(SAMPLE_RATE * SECONDS_PER_CHAR * len(self._input_text.strip()))
            chunk = SAMPLE_RATE // 100
            for start in range(0, samples, chunk):
                output_emitter.push(b'\x00\x00' * min(chunk, samples - start))
                await asyncio.sleep(0)
        output_emitter.flush()


class Microphone(AudioInput):
    """The caller's audio track: one burst of silence, enough for LiveKit to start its VAD and STT streams."""

    def __init__(self) -> None:
        super().__init__(label='test.Microphone')
        self._frames = utils.aio.Chan[rtc.AudioFrame]()

    async def __anext__(self) -> rtc.AudioFrame:
        return await self._frames.__anext__()

    def push(self, seconds: float = 0.1) -> None:
        samples = int(16000 * seconds)
        self._frames.send_nowait(
            rtc.AudioFrame(
                data=b'\x00\x00' * samples, sample_rate=16000, num_channels=1, samples_per_channel=samples
            )
        )


class Speaker(AudioOutput):
    """The room's audio output: plays each flushed segment on a virtual clock and reports it, as the room output does.

    `can_pause` gives LiveKit its false-interruption handling (pause, then resume
    or interrupt); a pause holds the clock. `reports` keeps each segment's
    report, in order. `gate`, when set, holds every frame until it opens, as the
    room output holds them until the caller subscribes (`waiting` says a frame is
    held there).
    """

    def __init__(self, *, can_pause: bool = False) -> None:
        super().__init__(
            label='test.Speaker',
            next_in_chain=None,
            sample_rate=SAMPLE_RATE,
            capabilities=AudioOutputCapabilities(pause=can_pause),
        )
        self._pushed = 0.0
        self._flushed = False
        self._handle: asyncio.TimerHandle | None = None
        self._started: float | None = None
        self._paused: float | None = None
        self._held = 0.0
        self.reports: list[tuple[float, bool]] = []
        self.pauses = 0
        self.gate: asyncio.Event | None = None
        self.waiting = asyncio.Event()

    def _played(self) -> float:
        if self._started is None:
            return 0.0
        end = self._paused if self._paused is not None else time.time()
        return end - self._started - self._held

    def _reset(self) -> None:
        self._pushed, self._flushed, self._started, self._paused, self._held = 0.0, False, None, None, 0.0
        if self._handle:
            self._handle.cancel()
            self._handle = None

    def _schedule(self) -> None:
        if self._handle:
            self._handle.cancel()
            self._handle = None
        if self._paused is not None:
            return
        pushed = self._pushed

        def finished() -> None:
            self._reset()
            self.reports.append((pushed, False))
            self.on_playback_finished(playback_position=pushed, interrupted=False)

        self._handle = asyncio.get_event_loop().call_later(max(pushed - self._played(), 0.0), finished)

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        if self.gate is not None:
            self.waiting.set()
            await self.gate.wait()
        await super().capture_frame(frame)
        self._pushed += frame.duration
        if self._paused is None and self._started is None:
            self._started = time.time()
            self.on_playback_started(created_at=self._started)

    def pause(self) -> None:
        super().pause()
        self.pauses += 1
        if self._paused is None:
            self._paused = time.time()
        if self._handle:
            self._handle.cancel()
            self._handle = None

    def resume(self) -> None:
        super().resume()
        if self._paused is not None:
            self._held += time.time() - self._paused
            self._paused = None
        if self._started is None and self._pushed > 0:
            self._started = time.time()
            self.on_playback_started(created_at=self._started)
        if self._flushed:
            self._schedule()

    def flush(self) -> None:
        super().flush()
        if not self._pushed:
            return
        self._flushed = True
        self._schedule()

    def clear_buffer(self) -> None:
        if not self._pushed:
            return
        position = min(self._played(), self._pushed)
        self._reset()
        self.reports.append((position, True))
        self.on_playback_finished(playback_position=position, interrupted=True)


class Screen(TextOutput):
    """What a reader sees: each segment of text, as LiveKit flushed it."""

    def __init__(self) -> None:
        super().__init__(label='test.Screen', next_in_chain=None)
        self._text = ''
        self.segments: list[str] = []

    async def capture_text(self, text: str) -> None:
        self._text += text

    def flush(self) -> None:
        self.segments.append(self._text)
        self._text = ''
