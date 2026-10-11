"""The flow on LiveKit Agents: a real `AgentSession` with `core.livekit.FlowAgent`, offline, every caller behaviour.

LiveKit's own session, turn taking, interruption handling, transcript
synchroniser and reply pipeline, around the support desk's flow (no salon)
and the scripted model; only the caller, the voice and the speaker are made
up (`tests.livekit_fakes`). Checked:

* L1: one call through: the greeting, a turn with hand-overs, a read-back
  bound to its own LiveKit segment and playback report, a yes after it played
  resolves the ticket once, and a second yes sends nothing;
* L2: without simulated playout LiveKit's own playout is `local`, never heard:
  a yes resolves nothing, and a confirming flow refuses the line
  (`reports_playback`);
* L3: the caller talks over a read-back: LiveKit interrupts it, the segment is
  cut to the words the synchroniser says played, reads `cut`, and a yes then
  resolves nothing; the read-back is put again in the next turn;
* L4: the caller goes on while the turn is still working (nothing played): its
  reply is dropped unsaid, and both pieces are one turn the next request reads;
* L5: a sound with no words while the turn works cancels nothing: the reply
  plays once the caller is quiet (on an output that can pause, as LiveKit's
  room output can: LiveKit itself drops a reply on any sound over one that
  cannot);
* L6: a false interruption (a sound over a read-back, on an output that can
  pause): LiveKit pauses and resumes it, and the segment, which lost what the
  output had queued, is never heard;
* L7: a silent caller is reminded by code, the question put again, and after
  the reminders the line closes;
* L8: a goodbye closes the session once its words played;
* L9: a text-only session (LiveKit's own text mode) acknowledges what the reader
  got: a read-back and a yes resolve the ticket once;
* L10: a turn already writing when the room closes finishes its write;
* L11: a segment with no audio is never heard: LiveKit retries its synthesis,
  then reports it failed;
* L12: LiveKit never runs a turn speculatively: preemptive generation is off
  for this agent whatever the session says;
* L13: the same scripted turns say the same words on LiveKit as over text
  (`core.text_turn`): one engine, two frameworks.

Every wait is bounded.
"""

import asyncio
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from livekit.agents import AgentSession
from livekit.agents.voice.transcription.synchronizer import TranscriptSynchronizer

import core
from core.livekit import FlowAgent, playout, reports_playback
from tests.livekit_fakes import Caller, FakeSTT, FakeVAD, Microphone, Screen, Speaker, Voice
from tests.script import Call, Reply
from tests.test_tickets import desk_in, opened
from tests.ticket_flow.worker import TO_READ_BACK

READBACK = '¿Marco el ticket T-101 como resuelto: la impresora ya imprime?'
FIRST_WORDS = 'Hola, la impresora del ticket T-101 ya funciona. Cuenta ACC-1234.'


class Room:
    """One LiveKit call on the desk: the session, the agent, the caller, and what each said and heard."""

    def __init__(
        self,
        folder: Path,
        *,
        simulate: bool = True,
        can_pause: bool = False,
        idle_seconds: float = 30.0,
        audio: bool = True,
        session_options: dict[str, Any] | None = None,
    ) -> None:
        self.desk = desk_in(folder)
        self.script, self.flow, self.call = opened(self.desk)
        self.caller = Caller()
        self.voice = Voice()
        options: dict[str, Any] = {
            'turn_handling': {
                'turn_detection': None,
                'endpointing': {'min_delay': 0.2, 'max_delay': 2.0},
                'interruption': {'min_duration': 0.2, 'false_interruption_timeout': 0.6},
            },
            'aec_warmup_duration': None,
            'user_away_timeout': None,
        }
        options.update(session_options or {})
        self.session = AgentSession(
            vad=FakeVAD(self.caller), stt=FakeSTT(self.caller), tts=self.voice, **options
        )
        self.microphone, self.speaker, self.screen = Microphone(), Speaker(can_pause=can_pause), Screen()
        self.sync: TranscriptSynchronizer | None = None
        if audio:
            self.sync = TranscriptSynchronizer(
                next_in_chain_audio=self.speaker, next_in_chain_text=self.screen
            )
            self.session.input.audio = self.microphone
            self.session.output.audio = self.sync.audio_output
            self.session.output.transcription = self.sync.text_output
        else:
            self.session.output.transcription = self.screen
        self.agent = FlowAgent(self.call, self.flow.nodes, idle_seconds=idle_seconds)
        playout(self.call, simulate=simulate)
        self.closed = asyncio.Event()
        self.session.on('close', lambda _event: self.closed.set())

    async def __aenter__(self) -> Room:
        await self.session.start(self.agent)
        if not self.sync:
            self.session.output.set_audio_enabled(False)
        self.agent.attach(self.session)
        self.microphone.push()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.agent.close()
        await self.session.aclose()
        if self.sync is not None:
            await self.sync.aclose()

    async def until(self, ready: Callable[[], bool], timeout: float = 8.0, what: str = '') -> None:
        deadline = time.perf_counter() + timeout
        while not ready():
            if time.perf_counter() > deadline:
                raise AssertionError(f'timed out waiting for {what or ready}: {self.said()}')
            await asyncio.sleep(0.02)

    async def settled(self, timeout: float = 8.0) -> None:
        """The agent is listening again with no turn running and nothing left to play."""
        await self.until(
            lambda: (
                self.agent._idle.is_set()
                and self.session.agent_state == 'listening'
                and self.session.current_speech is None
            ),
            timeout,
            'the agent to settle',
        )

    async def turn(self, words: str, seconds: float, *moves: Any, final: bool = True) -> None:
        """The caller says `words`; returns once that turn has finished and the agent is listening again."""
        self.script.moves = list(moves)
        before = len(self.agent.finished)
        await self.caller.says(words, seconds, final=final)
        await self.until(lambda: len(self.agent.finished) > before, what=f'a turn for {words!r}')
        await self.settled()

    def said(self) -> list[tuple[str, str, str]]:
        return [(s.text, s.delivered, self.call.hearing(s)) for s in self.call.all_spoken]

    def segment(self, text: str) -> core.Spoken:
        found = [s for s in self.call.all_spoken if s.generated == text or s.text == text]
        assert found, f'{text!r} was never said: {self.said()}'
        return found[-1]

    async def to_read_back(self) -> core.Spoken:
        self.script.moves = list(TO_READ_BACK)
        await self.caller.says(FIRST_WORDS, 0.6)
        await self.until(
            lambda: any(s.generated == READBACK for s in self.call.all_spoken), what='the read-back'
        )
        await self.settled()
        return self.segment(READBACK)


async def one_call_through(folder: Path):
    """L1."""
    async with Room(folder) as room:
        await room.agent.greet('Hola, soporte técnico.')
        await room.settled()
        readback = await room.to_read_back()
        assert readback.delivered == 'delivered' and readback.heard_by == 'local', room.said()
        await asyncio.sleep(0.3)
        await room.turn('Sí.', 0.3, Call('resolve_ticket'))
        assert room.desk.sent() == 1 and room.desk.ticket('T-101') == (2, 'resolved'), room.desk.ticket(
            'T-101'
        )
        playout_counts = room.agent.playout.counts if room.agent.playout else {}
        spoken = [s for s in room.call.all_spoken if s.text]
        assert playout_counts['segments'] == len(spoken) == playout_counts['played'], (
            playout_counts,
            room.said(),
        )
        assert all(s.delivered == 'delivered' for s in spoken), room.said()
        await room.turn('Sí, sí.', 0.3, Call('resolve_ticket'))
        assert room.desk.sent() == 1, 'a second yes sent it again'
        assert [f.status for f in room.agent.finished] == ['answered'] * 3, room.agent.finished


async def livekit_playout_alone_is_never_heard(folder: Path):
    """L2."""
    async with Room(folder, simulate=False) as room:
        assert not reports_playback(room.call) and room.flow.confirms
        readback = await room.to_read_back()
        assert readback.heard_by == 'local' and readback.delivered == 'unknown', room.said()
        await room.turn('Sí.', 0.3, Call('resolve_ticket'))
        assert room.desk.sent() == 0, 'an estimate approved a write'


async def talking_over_a_read_back_cuts_it(folder: Path):
    """L3."""
    async with Room(folder) as room:
        room.voice.ttfb = 0.05
        room.script.moves = list(TO_READ_BACK)
        await room.caller.says(FIRST_WORDS, 0.6)
        await room.until(lambda: room.session.agent_state == 'speaking', what='the reply to play')
        await room.until(
            lambda: any(s.generated == READBACK and s.mark for s in room.call.all_spoken),
            what='the read-back to start playing',
        )
        await asyncio.sleep(0.15)
        await room.turn('Sí, sí, márcalo.', 0.5, Call('resolve_ticket'))
        cut, *again = [s for s in room.call.all_spoken if s.generated == READBACK]
        assert room.call.hearing(cut) == 'cut' and cut.delivered == 'interrupted', room.said()
        assert cut.text != READBACK and READBACK.startswith(cut.text), cut.text
        assert room.desk.sent() == 0, 'a yes over a cut read-back resolved the ticket'
        assert [s.text for s in again] == [READBACK], 'the read-back was not put again'
        assert any(cut for _, cut in room.speaker.reports), room.speaker.reports


async def going_on_while_the_turn_works_joins_the_pieces(folder: Path):
    """L4: the first request is cancelled while it waits, so it never says (or takes) its move."""
    async with Room(folder) as room:
        room.script.delay = 0.8
        room.script.moves = [Reply('Claro, te ayudo con la impresora.')]
        await room.caller.says('Hola.', 0.3)
        await room.until(lambda: len(room.script.requests) == 1, what='the first request')
        room.script.delay = 0.0
        await room.caller.says('Mi impresora no imprime.', 0.5)
        await room.until(lambda: len(room.agent.finished) == 2, what='the joined turn')
        await room.settled()
        assert [f.status for f in room.agent.finished] == ['interrupted', 'answered'], room.agent.finished
        texts = [s.text for s in room.call.all_spoken if s.text]
        assert texts == ['Claro, te ayudo con la impresora.'], texts
        assert room.call.turn == 1 and room.call._pieces == ['Hola.', 'Mi impresora no imprime.'], (
            room.call._pieces
        )
        assert len(room.script.requests) == 2, len(room.script.requests)
        last = room.script.requests[-1].told
        assert 'Hola.' in last and 'Mi impresora no imprime.' in last, last


async def a_sound_while_the_turn_works_cancels_nothing(folder: Path):
    """L5: on an output that can pause, as a room's can; one that cannot lets LiveKit drop the reply on any sound."""
    async with Room(folder, can_pause=True) as room:
        room.script.delay = 0.6
        room.script.moves = [Reply('De nueve a cinco, de lunes a viernes.')]
        await room.caller.says('¿Qué horario tenéis?', 0.4)
        await room.until(lambda: len(room.script.requests) == 1, what='the request')
        await room.caller.says('', 0.4, final=False)
        await room.until(lambda: len(room.agent.finished) == 1, what='the turn')
        await room.settled()
        texts = [s.text for s in room.call.all_spoken if s.text]
        assert texts == ['De nueve a cinco, de lunes a viernes.'], texts
        assert len(room.script.requests) == 1 and room.agent.finished[-1].status == 'answered', (
            room.agent.finished
        )


async def a_false_interruption_leaves_a_gap(folder: Path):
    """L6."""
    async with Room(folder, can_pause=True) as room:
        room.script.moves = list(TO_READ_BACK)
        await room.caller.says(FIRST_WORDS, 0.6)
        await room.until(
            lambda: (
                any(s.generated == READBACK for s in room.call.all_spoken)
                and room.session.agent_state == 'speaking'
            ),
            what='the read-back to play',
        )
        await asyncio.sleep(0.1)
        await room.caller.says('', 0.35, final=False)
        await room.settled(timeout=10)
        readback = room.segment(READBACK)
        assert room.speaker.pauses >= 1, 'LiveKit never paused'
        assert readback.text == READBACK and readback.lost == 'cleared', room.said()
        assert room.call.hearing(readback) == 'cut', room.said()
        await room.turn('Sí.', 0.3, Call('resolve_ticket'))
        assert room.desk.sent() == 0, 'a read-back with a gap was agreed to'


async def a_silent_caller_is_reminded_then_let_go(folder: Path):
    """L7."""
    async with Room(folder, idle_seconds=0.6) as room:
        await room.agent.greet('Hola, soporte técnico.')
        await room.until(room.closed.is_set, timeout=12, what='the line to close')
        texts = [s.text for s in room.call.all_spoken if s.text]
        locale = room.call.locale
        assert texts.count(locale.still_there) == 2 and texts[-1] == locale.idle_goodbye, texts
        assert room.call.ended == 'abandoned', room.call.ended


async def a_goodbye_closes_the_line(folder: Path):
    """L8."""
    async with Room(folder) as room:
        room.script.moves = [Call('goodbye')]
        await room.caller.says('Nada más, adiós.', 0.4)
        await room.until(room.closed.is_set, what='the line to close')
        goodbye = room.call.all_spoken[-1]
        assert goodbye.text and goodbye.delivered == 'delivered', room.said()
        assert room.call.ended == 'caller', room.call.ended


async def a_text_session_acknowledges_what_the_reader_got(folder: Path):
    """L9."""
    async with Room(folder, audio=False) as room:
        assert reports_playback(room.call) and room.call.transport == 'livekit-text'
        room.script.moves = list(TO_READ_BACK)
        result = await room.session.run(user_input=FIRST_WORDS)
        readback = room.segment(READBACK)
        assert readback.heard_by == 'ack' and readback.delivered == 'delivered', room.said()
        assert any(READBACK in (e.item.text_content or '') for e in result.events if e.type == 'message'), (
            result.events
        )
        room.script.moves = [Call('resolve_ticket')]
        await room.session.run(user_input='Sí.')
        await room.settled()
        assert room.desk.sent() == 1 and room.desk.ticket('T-101') == (2, 'resolved'), room.desk.sent()


async def a_turn_writing_when_the_room_closes_finishes(folder: Path):
    """L10."""
    async with Room(folder) as room:
        await room.to_read_back()
        await asyncio.sleep(0.3)
        room.script.delay = 0.5
        room.script.moves = [Call('resolve_ticket')]
        await room.caller.says('Sí.', 0.3)
        await room.until(
            lambda: len(room.script.requests) >= 4 and not room.agent._idle.is_set(), what='the turn'
        )
        room.session.shutdown(drain=False)
        await room.until(room.closed.is_set, what='the session to close')
        await room.agent.finish_turn(5.0)
        assert room.desk.sent() == 1 and room.desk.ticket('T-101') == (2, 'resolved'), room.desk.sent()


async def a_segment_with_no_audio_is_never_heard(folder: Path):
    """L11."""
    async with Room(folder) as room:
        await room.agent.greet('[silent] Hola.')
        greeting = room.call.all_spoken[0]
        await room.until(lambda: greeting.lost != '', timeout=15, what='LiveKit to give up on the line')
        assert greeting.lost in ('silent', 'failed') and room.call.hearing(greeting) == 'failed', room.said()
        assert greeting.delivered != 'delivered', room.said()


async def no_turn_runs_speculatively(folder: Path):
    """L12."""
    async with Room(
        folder,
        session_options={
            'turn_handling': {'turn_detection': None, 'preemptive_generation': {'enabled': True}}
        },
    ) as room:
        activity = room.session._activity
        assert activity is not None and activity.preemptive_generation_opts['enabled'] is False


async def the_same_words_on_both_frameworks(folder: Path):
    """L13."""
    text_script, text_flow, text_call = opened(desk_in(folder / 'text'))
    text_script.moves = list(TO_READ_BACK)
    reply = await core.text_turn(text_flow, text_call, FIRST_WORDS)
    over_text = [s.text for s in reply.segments]
    async with Room(folder / 'livekit') as room:
        await room.to_read_back()
        on_livekit = [s.text for s in room.call.all_spoken if s.text]
    assert on_livekit == over_text, (on_livekit, over_text)
    assert room.voice.said == over_text, room.voice.said


async def main() -> None:
    scenarios = (
        one_call_through,
        livekit_playout_alone_is_never_heard,
        talking_over_a_read_back_cuts_it,
        going_on_while_the_turn_works_joins_the_pieces,
        a_sound_while_the_turn_works_cancels_nothing,
        a_false_interruption_leaves_a_gap,
        a_silent_caller_is_reminded_then_let_go,
        a_goodbye_closes_the_line,
        a_text_session_acknowledges_what_the_reader_got,
        a_turn_writing_when_the_room_closes_finishes,
        a_segment_with_no_audio_is_never_heard,
        no_turn_runs_speculatively,
        the_same_words_on_both_frameworks,
    )
    chosen = [s for s in scenarios if not sys.argv[1:] or s.__name__ in sys.argv[1:]]
    with tempfile.TemporaryDirectory() as root:
        for index, scenario in enumerate(chosen):
            print(f'{scenario.__doc__} {scenario.__name__}', flush=True)
            await asyncio.wait_for(scenario(Path(root) / str(index)), 40)
    print('ok')


if __name__ == '__main__':
    asyncio.run(main())
