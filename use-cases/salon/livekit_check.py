"""The salon on LiveKit Agents, live: real Gemini, and in `voice` mode real SLNG speech both ways. Billable.

Not a test in the suite: a run against the real model and speech services, read by a person. It builds
the session exactly as `core.livekit_launch.run_call` does (`session_for`, `FlowAgent`), on the salon's
own entry, and drives it with a scripted caller instead of a LiveKit room:

    uv run livekit_check.py text     # LiveKit's own text mode (`AgentSession.run`), real model
    uv run livekit_check.py voice    # real STT, TTS and Silero VAD; the caller is synthesised speech

`voice` plays a caller who also talks over the agent once (a barge-in), and prints, for every segment
the agent said, whether it was delivered and how it was heard, every final transcript, and the wait
from the end of each caller line to the agent's first audio, and the intents the call settled. Playback is simulated (`DELIVERY=simulated`): no room, no far end to report it.

Speech runs on SLNG's eu-north gateway unless `SLNG_BASE_URL` / `SLNG_TTS_BASE_URL` say otherwise.
"""

# ruff: noqa: E402
import asyncio
import os
import ssl
import sys
import time
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv(override=True)
os.environ.setdefault('SLNG_TTS_BASE_URL', 'eu-north.api.slng.ai')
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')

import aiohttp
from livekit import rtc
from livekit.agents import AgentSession
from livekit.agents.voice.io import AudioInput, AudioOutput, AudioOutputCapabilities, TextOutput
from livekit.agents.voice.transcription.synchronizer import TranscriptSynchronizer
from livekit.plugins import silero, slng

import core
from core.livekit import FlowAgent, playout
from core.livekit_launch import session_for, speech_services
from core.voice import vad_stop_seconds
from salon.entry import ENTRY

PHONE = '+34611222333'
CALLER_VOICE = ('deepgram/aura:2', 'aura-2-thalia-en')
"""The caller speaks in another voice than the agent, through the same gateway."""


@dataclass
class Line:
    words: str
    barge_after: float | None = None
    """Seconds into the agent's reply to the line before to start talking over it; None waits for it to end."""


SCRIPT = (
    Line("Hi, I'd like to book a haircut for tomorrow morning, please."),
    Line("Yes, that's my number."),
    Line('Ten o clock would be perfect.', barge_after=1.2),
    Line('Yes, please book it.'),
    Line("No, that's everything. Thank you, bye!"),
)


def proxied() -> aiohttp.ClientSession:
    """An HTTP session that goes out through the environment's proxy, as a sandbox needs."""
    context = ssl.create_default_context(cafile=os.getenv('SSL_CERT_FILE') or None)
    return aiohttp.ClientSession(trust_env=True, connector=aiohttp.TCPConnector(ssl=context))


class CallerLine(AudioInput):
    """The caller's microphone: silence, or a queued utterance, in 10 ms frames at real time."""

    RATE = 24000

    def __init__(self) -> None:
        super().__init__(label='check.CallerLine')
        self._pending = bytearray()
        self._next = time.perf_counter()
        self.done = asyncio.Event()
        self.done.set()
        self.ended_at = 0.0

    def play(self, pcm: bytes) -> None:
        self._pending.extend(pcm)
        self.done.clear()

    async def __anext__(self) -> rtc.AudioFrame:
        self._next += 0.01
        await asyncio.sleep(max(0.0, self._next - time.perf_counter()))
        size = self.RATE // 100 * 2
        chunk = bytes(self._pending[:size])
        del self._pending[:size]
        if not self._pending and not self.done.is_set():
            self.ended_at = time.perf_counter()
            self.done.set()
        chunk = chunk.ljust(size, b'\x00')
        return rtc.AudioFrame(
            data=chunk, sample_rate=self.RATE, num_channels=1, samples_per_channel=size // 2
        )


class Ear(AudioOutput):
    """The caller's speaker: plays each segment on a real-time clock and reports it, as a room's output does."""

    def __init__(self) -> None:
        super().__init__(
            label='check.Ear',
            next_in_chain=None,
            sample_rate=24000,
            capabilities=AudioOutputCapabilities(pause=True),
        )
        self._pushed = 0.0
        self._started: float | None = None
        self._paused: float | None = None
        self._held = 0.0
        self._timer: asyncio.TimerHandle | None = None
        self.first_audio: list[float] = []

    def _played(self) -> float:
        if self._started is None:
            return 0.0
        return (self._paused or time.time()) - self._started - self._held

    def _finish(self, position: float, interrupted: bool) -> None:
        self._pushed, self._started, self._paused, self._held = 0.0, None, None, 0.0
        if self._timer:
            self._timer.cancel()
            self._timer = None
        self.on_playback_finished(playback_position=position, interrupted=interrupted)

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        self._pushed += frame.duration
        if self._started is None and self._paused is None:
            self._started = time.time()
            self.first_audio.append(time.perf_counter())
            self.on_playback_started(created_at=self._started)

    def flush(self) -> None:
        super().flush()
        if self._pushed and self._paused is None:
            pushed = self._pushed
            self._timer = asyncio.get_event_loop().call_later(
                max(0.0, pushed - self._played()), self._finish, pushed, False
            )

    def clear_buffer(self) -> None:
        if self._pushed:
            self._finish(min(self._played(), self._pushed), True)

    def pause(self) -> None:
        super().pause()
        self._paused = self._paused or time.time()
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def resume(self) -> None:
        super().resume()
        if self._paused is not None:
            self._held += time.time() - self._paused
            self._paused = None
        if self._pushed:
            self.flush()


class Screen(TextOutput):
    def __init__(self) -> None:
        super().__init__(label='check.Screen', next_in_chain=None)

    async def capture_text(self, text: str) -> None:
        pass

    def flush(self) -> None:
        pass


@dataclass
class Record:
    heard: list[str] = field(default_factory=list)
    waits: list[float] = field(default_factory=list)


async def synthesise(http: aiohttp.ClientSession, words: str) -> bytes:
    model, voice = CALLER_VOICE
    tts = slng.TTS(model=model, voice=voice, slng_base_url=os.environ['SLNG_TTS_BASE_URL'], http_session=http)
    pcm = bytearray()
    async for chunk in tts.synthesize(words):
        frame = chunk.frame
        if frame.sample_rate != CallerLine.RATE:
            raise RuntimeError(f'caller audio at {frame.sample_rate} Hz')
        pcm.extend(frame.data.tobytes())
    return bytes(pcm)


async def text() -> None:
    flow = ENTRY.flow()
    call = ENTRY.open(flow, core.Opening(caller_id=PHONE, key='livekit-check-text'))
    session = AgentSession()
    agent = FlowAgent(call, flow.nodes, reminders=False)
    async with session:
        await session.start(agent)
        session.output.set_audio_enabled(False)
        agent.attach(session)
        print(f'AGENT  {ENTRY.greeting(flow, call)}')
        await agent.greet(ENTRY.greeting(flow, call))
        for line in SCRIPT:
            if line.barge_after is not None:
                line = Line(line.words)
            started = time.perf_counter()
            result = await session.run(user_input=line.words)
            said = ' '.join((e.item.text_content or '').strip() for e in result.events if e.type == 'message')
            print(
                f'CALLER {line.words}\nAGENT  {said}   [{time.perf_counter() - started:.2f}s, {agent.finished[-1].status}]'
            )
            if session._closing or call.ended:
                break
    report(call)


async def voice() -> None:
    async with proxied() as http:
        flow = ENTRY.flow()
        call = ENTRY.open(flow, core.Opening(caller_id=PHONE, key='livekit-check-voice'))
        stt, tts = speech_services(ENTRY, http_session=http)
        session = session_for(
            ENTRY, stt=stt, tts=tts, vad=silero.VAD.load(min_silence_duration=vad_stop_seconds())
        )
        line, ear = CallerLine(), Ear()
        sync = TranscriptSynchronizer(next_in_chain_audio=ear, next_in_chain_text=Screen())
        session.input.audio = line
        session.output.audio = sync.audio_output
        session.output.transcription = sync.text_output
        agent = FlowAgent(call, flow.nodes, reminders=False)
        playout(call, simulate=True)
        record = Record()
        session.on('user_input_transcribed', lambda e: e.is_final and record.heard.append(e.transcript))
        clock = time.perf_counter()
        if os.getenv('CHECK_TRACE'):
            session.on(
                'user_input_transcribed',
                lambda e: print(
                    f'{time.perf_counter() - clock:7.2f} stt {"final" if e.is_final else "interim"} {e.transcript!r}',
                    flush=True,
                ),
            )
            session.on(
                'agent_state_changed',
                lambda e: print(f'{time.perf_counter() - clock:7.2f} agent {e.new_state}', flush=True),
            )
            session.on(
                'user_state_changed',
                lambda e: print(f'{time.perf_counter() - clock:7.2f} user {e.new_state}', flush=True),
            )
        print('synthesising the caller...', flush=True)
        audio = {item.words: await synthesise(http, item.words) for item in SCRIPT}
        closed = asyncio.Event()
        session.on('close', lambda _e: closed.set())
        await session.start(agent)
        agent.attach(session)
        await agent.greet(ENTRY.greeting(flow, call))

        async def settled() -> None:
            while not (
                agent._idle.is_set() and session.agent_state == 'listening' and session.current_speech is None
            ):
                await asyncio.sleep(0.05)

        async def answered(turns: int) -> None:
            """The agent has finished `turns` turns, spoken, and is listening again."""
            while len(agent.finished) < turns or not ear.first_audio:
                await asyncio.sleep(0.05)
            await settled()

        for index, item in enumerate(SCRIPT):
            if closed.is_set():
                break
            if item.barge_after is None:
                await asyncio.wait_for(answered(index), 60)
                await asyncio.sleep(0.8)
            else:
                await asyncio.sleep(item.barge_after)
                print(f'-- talking over the agent {item.barge_after}s into its speech')
            heard_before = len(ear.first_audio)
            line.play(audio[item.words])
            await line.done.wait()
            ended = line.ended_at
            print(f'CALLER {item.words}', flush=True)
            for _ in range(600):
                if len(ear.first_audio) > heard_before and ear.first_audio[-1] > ended:
                    record.waits.append(ear.first_audio[-1] - ended)
                    break
                await asyncio.sleep(0.05)
        await asyncio.wait_for(settled(), 60)
        await asyncio.sleep(2)
        print('\nfinal transcripts:', record.heard)
        print(
            'waits from the end of each caller line to the first audio:', [round(w, 2) for w in record.waits]
        )
        print('playout:', agent.playout.counts if agent.playout else None)
        await agent.close()
        await session.aclose()
        await sync.aclose()
    report(call)


def report(call) -> None:
    print('\nwhat the agent said, and how it was heard:')
    for s in call.all_spoken:
        if s.text or s.generated:
            print(
                f'  [{s.delivered:>11} {call.hearing(s):>11}] {s.text!r}'
                + (f' (generated {s.generated!r})' if s.generated and s.generated != s.text else '')
            )
    print('turns:', call.turn, 'ended:', call.ended or '-', 'step:', call.state.agent)
    print(
        'intents:',
        [(i.operation, i.status) for i in call.intents.values()]
        if hasattr(call.intents, 'values')
        else call.intents,
    )


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'text'
    asyncio.run({'text': text, 'voice': voice}[mode]())
