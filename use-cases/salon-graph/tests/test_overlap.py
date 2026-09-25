"""Run: uv run python -m tests.test_overlap

Timed event replay: the real processor, driven by caller speech, endpoints and
playback placed on one fake clock, with every event written to a timeline.
The timeline says which caller and agent intervals overlapped and which
read-back was heard in full, so a case proves the overlap happened rather than
naming it.

Two groups. The pauses place the caller's second piece below, near and above
the thresholds `bot.py` configures (Silero's `stop_secs=0.2`, the soonest
smart-turn can end a turn; smart-turn's `stop_secs=1.6`, its silence
fallback), with STT and playback delayed separately. The endpoint itself is a
synthetic event: smart-turn's judgement is not reimplemented here, so each
case says whether it judged the first piece complete. The writes place a
barge before a booking is dispatched, while its outcome is unknown, and after
its receipt, with the turn still running where the barge lands.

The diary is synchronous, so a barge can land only at the await points around
`save_booking`, never inside it. "During" is the await after an attempt whose
answer was lost.

Scripted models, no audio, no network. Asserts, no pytest, same as the rest.
"""

import asyncio
from dataclasses import dataclass, field

from pydantic_ai.models.function import AgentInfo, FunctionModel

from salon import agents
from tests.classifier_model import classifier_model
from tests.test_continuation import (
    PHONE,
    Probe,
    booking_call,
    books,
    classifier,
    played_out,
    read_back_a_haircut,
    scripted_steps,
)

VAD_STOP = 0.2
"""`VADParams(stop_secs=0.2)` in `bot.py`: the soonest a turn can end, if smart-turn judges it complete."""
TURN_STOP = 1.6
"""`SmartTurnParams(stop_secs=1.6)` in `bot.py`: the silence after which a turn ends whatever smart-turn thinks."""
VAD_START = 0.2
"""Silero's default `start_secs`: how long the caller speaks before the runtime notices and interrupts."""


@dataclass
class Timeline:
    """What happened, in the order it happened, on one clock: `(seconds, kind, detail)`."""

    events: list[tuple[float, str, str]] = field(default_factory=list)

    def add(self, at: float, kind: str, detail: str = '') -> None:
        self.events.append((round(at, 3), kind, detail))

    def spans(self, opens: str, closes: tuple[str, ...]) -> list[tuple[float, float, str]]:
        """Each interval from an `opens` event to the next of `closes`."""
        found, begun = [], None
        for at, kind, detail in sorted(self.events):
            if kind == opens:
                begun = (at, detail)
            elif kind in closes and begun is not None:
                found.append((begun[0], at, begun[1]))
                begun = None
        return found

    def overlaps(self) -> list[tuple[tuple[float, float, str], tuple[float, float, str]]]:
        """Each caller interval and agent audio interval that were on the line at the same time."""
        caller = self.spans('caller starts', ('caller stops',))
        agent = self.spans('audio starts', ('audio ends', 'audio cut'))
        return [(c, a) for c in caller for a in agent if c[0] < a[1] and a[0] < c[1]]


def heard(call) -> list[str]:
    """The questions whose read-back played to its end: what a yes could answer."""
    return [
        s.generated
        for s in call.all_spoken
        if s.origin == 'ask' and s.playback == 'played' and not s.interrupted
    ]


async def answering(messages, info: AgentInfo):
    yield 'We do colour work, yes.'


@dataclass(frozen=True)
class Pause:
    """The caller says two pieces with a pause between; each field is one thing the runtime could do."""

    pause: float
    complete: bool
    """Whether smart-turn judges the first piece a finished turn: it then ends at VAD_STOP, else at TURN_STOP."""
    stt_delay: float = 0.0
    playback_delay: float = 0.8
    """From the transcript reaching the engine to the answer's audio starting."""
    answer_seconds: float = 1.5


FIRST, SECOND = 'Do you do colour?', 'And balayage?'


async def replay_pause(case: Pause) -> tuple[Timeline, object, object]:
    """Drive the real processor through one pause, in time order. Returns the timeline, the call and the answer."""
    call = booking_call()
    call.commit(agent='concierge')
    probe = Probe(call)
    line = Timeline()
    first_end, second_start = 1.0, 1.0 + case.pause
    second_end = second_start + 0.8
    line.add(0.0, 'caller starts', FIRST)
    line.add(first_end, 'caller stops', FIRST)
    line.add(second_start, 'caller starts', SECOND)
    line.add(second_end, 'caller stops', SECOND)
    stop = VAD_STOP if case.complete else TURN_STOP
    plan: list[tuple[float, str]] = [
        (second_start + VAD_START, 'noticed'),
        (second_end + VAD_STOP + case.stt_delay, 'second'),
    ]
    if case.pause > stop:
        endpoint = first_end + stop
        line.add(endpoint, 'endpoint', FIRST)
        begins = endpoint + case.stt_delay
        plan += [
            (begins, 'first'),
            (begins + case.playback_delay, 'audio'),
            (begins + case.playback_delay + case.answer_seconds, 'ended'),
        ]
    else:
        plan[1] = (plan[1][0], 'whole')
    answer, playing, flushed = None, False, False
    with (
        classifier_model(),
        agents.AGENTS['concierge'].agent.override(model=FunctionModel(stream_function=answering)),
    ):
        for at, event in sorted(plan):
            if event == 'first':
                line.add(at, 'transcript', FIRST)
                probe.interrupt()
                await probe._turn(FIRST)
                answer = next(s for s in call.spoken if s.kind == 'model')
                line.add(at, 'answer generated', answer.id)
            elif event == 'audio' and not flushed:
                playing = True
                call.note_reached()
                line.add(at, 'audio starts', answer.id if answer else '')
            elif event == 'ended' and playing:
                playing = False
                played_out(call)
                line.add(at, 'audio ends', answer.id if answer else '')
            elif event == 'noticed':
                flushed = True
                probe.interrupt()
                line.add(at, 'barge', 'interruption')
                if playing:
                    playing = False
                    call.note_drained()
                    line.add(at, 'audio cut', answer.id if answer else '')
            elif event == 'whole':
                line.add(at, 'endpoint', f'{FIRST} {SECOND}')
                await probe._turn(f'{FIRST} {SECOND}')
            elif event == 'second':
                line.add(at, 'endpoint', SECOND)
                await probe._turn(SECOND)
    return line, call, answer


async def pauses_around_the_thresholds():
    """Below a threshold the runtime never ends the turn; above it, what matters is whether the answer played out."""
    both = f'{FIRST} {SECOND}'

    line, call, answer = await replay_pause(Pause(0.1, complete=True))
    assert answer is None and call.turn == 1 and call.heard == both, 'a pause under VAD_STOP is no endpoint'
    assert not line.overlaps()

    line, call, answer = await replay_pause(Pause(1.5, complete=False))
    assert answer is None and call.turn == 1 and call.heard == both, (
        'under the fallback, an incomplete piece waits'
    )

    line, call, answer = await replay_pause(Pause(0.5, complete=True))
    assert answer is not None and call.turn == 1 and call.heard == both, (
        'premature endpoint: one logical turn'
    )
    assert answer.interrupted and answer.text == '' and answer.playback == 'queued', 'generated, never played'
    assert not line.overlaps(), 'the answer never reached the line'

    line, call, answer = await replay_pause(Pause(1.2, complete=True))
    assert answer is not None and call.turn == 1 and call.heard == both, (
        'cut off before it finished: same turn'
    )
    assert answer.interrupted, 'audio cut by the caller is not delivered'
    ((caller, audio),) = line.overlaps()
    assert caller[2] == SECOND and audio[2] == answer.id, line.overlaps()

    line, call, answer = await replay_pause(Pause(1.7, complete=False, playback_delay=0.15))
    assert answer is not None and call.turn == 1, 'the agent spoke over a caller who had already resumed'
    assert answer.interrupted, 'its words are not counted as heard'
    ((caller, audio),) = line.overlaps()
    assert audio[0] > caller[0], ('the audio started after the caller did', line.events)

    line, call, answer = await replay_pause(Pause(5.0, complete=False))
    assert answer is not None and call.turn == 2 and call.heard == SECOND, (
        'after an answer played out: a new turn'
    )
    assert answer.playback == 'played' and not answer.interrupted
    assert not line.overlaps()

    line, call, answer = await replay_pause(Pause(0.5, complete=True, stt_delay=0.4))
    assert answer is not None and call.turn == 1 and call.heard == both, 'a late transcript changes nothing'


async def barge_before_the_write():
    """The caller goes on while their yes is still being read: nothing is dispatched, the reply is joined."""
    call = booking_call()
    probe = Probe(call)
    readback = await read_back_a_haircut(call, probe)
    assert heard(call) == [readback]
    saves = watch_saves()
    reasks: list[int] = []
    joined = 'Yes. Actually, make it ten.'
    with classifier({joined: 'revise'}, hold='Yes.') as (asked, held), scripted_steps(reasks):
        probe.interrupt()
        first = asyncio.create_task(probe._turn('Yes.'))
        await asyncio.wait_for(held.wait(), 3)
        assert not first.done(), 'the barge lands while the turn is running'
        probe.interrupt()
        await asyncio.wait_for(first, 3)
        await probe._turn('Actually, make it ten.')
    assert asked == ['Yes.', joined], asked
    assert saves == [], 'no write was dispatched'
    assert call.turn == 2 and call.heard == joined and call.state.writes == 0, (call.turn, call.heard)
    del books.save_booking


def watch_saves() -> list[str]:
    """Every `save_booking` the module's diary is asked for, by slot."""
    real, seen = books.save_booking, []

    def save_booking(
        phone, action, slot_id, booking_id, additional, at_time, operation_token='', offered_to=''
    ):
        seen.append(slot_id)
        return real(phone, action, slot_id, booking_id, additional, at_time, operation_token, offered_to)

    books.save_booking = save_booking  # ty: ignore[invalid-assignment]
    return seen


async def barge_while_the_outcome_is_unknown():
    """The write lands, its answer is lost, and the caller barges over what they are told.

    The outcome is recorded as unknown and said in core's fixed words
    (`Locale.unsettled`), with no step model; nothing is claimed, the barge
    does not repeat the write, and the caller's next words settle it from the
    diary before anything else, by code (`nodes.advance`). Session 3 moved both
    to code: live text T4.v2 left the wording, and so the settling, to a model.
    """
    call = booking_call()
    probe = Probe(call)
    await read_back_a_haircut(call, probe)
    real_save, real_outcome = books.save_booking, books.outcome_of
    saves, asked = [], []

    def save_booking(
        phone, action, slot_id, booking_id, additional, at_time, operation_token='', offered_to=''
    ):
        saves.append(slot_id)
        real_save(phone, action, slot_id, booking_id, additional, at_time, operation_token, offered_to)
        raise TimeoutError('the diary did not answer')

    def outcome_of(token):
        asked.append(token)
        return None if len(asked) == 1 else real_outcome(token)

    books.save_booking, books.outcome_of = save_booking, outcome_of  # ty: ignore[invalid-assignment]
    asked_model: list[str] = []

    async def booking(messages, info):
        asked_model.append(call.heard)
        yield 'Let me see.'

    try:
        with (
            classifier({'Yes.': 'accept'}),
            agents.AGENTS['booking'].agent.override(model=FunctionModel(stream_function=booking)),
        ):
            probe.interrupt()
            await asyncio.wait_for(probe._turn('Yes.'), 3)
            assert probe.fixed()[-1] == call.locale.unsettled, probe.fixed()
            assert not asked_model, 'an unknown outcome is said by code, not worded by a model'
            probe.interrupt()
            assert len(saves) == 1, 'the barge did not repeat the write'
            assert call.state.unresolved is not None and call.state.appointment is None, (
                'unknown, and nothing claimed'
            )
            assert len(books.bookings_for(PHONE, books.now().date())) == 1, 'it did land'
            await asyncio.wait_for(probe._turn('Okay, can you check?'), 3)
        assert 'Friday the 25th at 9:00 AM' in probe.fixed()[-1], probe.fixed()
        assert len(saves) == 1 and len(asked) == 2, 'settled by asking, not by writing again'
        assert call.state.appointment is not None and call.state.unresolved is None
        assert not asked_model, 'settling the next turn costs no model request'
    finally:
        del books.save_booking, books.outcome_of


async def barge_after_the_receipt():
    """The booking lands and its confirmation is cut off by the caller. The booking stays, and is not made twice."""
    call = booking_call()
    probe = Probe(call)
    line = Timeline()
    readback = await read_back_a_haircut(call, probe)
    line.add(0.0, 'audio starts', 'readback')
    line.add(4.0, 'audio ends', 'readback')
    saves = watch_saves()
    reasks: list[int] = []
    try:
        with classifier({'Yes.': 'accept'}), scripted_steps(reasks):
            probe.interrupt()
            line.add(5.0, 'caller starts', 'Yes.')
            line.add(5.4, 'caller stops', 'Yes.')
            await probe._turn('Yes.')
            confirmation = call.spoken[-1]
            assert call.state.writes == 1 and len(saves) == 1, 'booked on the yes'
            call.note_reached()
            line.add(6.0, 'audio starts', confirmation.id)
            line.add(6.5, 'caller starts', 'Sorry, what time?')
            probe.interrupt()
            line.add(6.7, 'audio cut', confirmation.id)
            call.note_drained()
            line.add(7.0, 'caller stops', 'Sorry, what time?')
            await probe._turn('Sorry, what time?')
    finally:
        del books.save_booking
    assert heard(call) == [readback], 'the read-back was heard in full before the yes'
    assert confirmation.interrupted, 'the confirmation was cut off'
    assert call.turn == 2 and call.heard == 'Yes. Sorry, what time?', (call.turn, call.heard)
    assert len(saves) == 1 and call.state.writes == 1, 'the continuation did not book again'
    assert len(books.bookings_for(PHONE, books.now().date())) == 1 and call.state.appointment is not None
    ((caller, audio),) = line.overlaps()
    assert caller[2] == 'Sorry, what time?' and audio[2] == confirmation.id


async def main():
    await pauses_around_the_thresholds()
    await barge_before_the_write()
    await barge_while_the_outcome_is_unknown()
    await barge_after_the_receipt()
    print('overlap ok')


if __name__ == '__main__':
    asyncio.run(main())
