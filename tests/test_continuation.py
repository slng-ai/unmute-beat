"""Run: uv run python -m tests.test_continuation

One caller turn lasts until the agent's answer reaches the caller.

The failure this is written against, from the Coval release run of
2026-09-23: the runtime closes the caller's turn on every pause, so "Yes,
please." and "That sounds perfect." arrived as two turns. The first was cut
off by the second before anything was said, and a question is answerable only
on the turn after it went out, so the reply that finished the sentence
answered nothing and the read-back was asked again. The same splitting cut a
complaint description into pieces no quote could span, and a phone number
into pieces nobody joined.

Scripted models, no audio, no network. Asserts, no pytest, same as the rest.
"""

import asyncio
from contextlib import ExitStack, contextmanager
from datetime import datetime
from unittest.mock import patch

from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import BotStartedSpeakingFrame, StartFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup
from pipecat.utils.asyncio.task_manager import TaskManager
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from core.history import project
from core.processor import PydanticAgentProcessor
from salon import agents, diary
from salon.classification import CLASSIFIER
from salon.diary import Diary
from salon.models.consent import ConsentInput, ReplyClassification
from salon.models.state import open_call
from salon.tools import identity
from salon.tools.booking import ask_booking

PHONE = '+34611111111'
CLOCK = datetime(2026, 9, 21, 10, tzinfo=diary.TZ)
books = Diary()
"""This module's salon, emptied by each case."""


class Probe(PydanticAgentProcessor):
    """The real processor with its frames collected instead of pushed down a pipeline."""

    def __init__(self, call):
        super().__init__(call, agents.AGENTS)
        self.frames = []

    async def push_frame(self, frame, direction=None):
        self.frames.append(frame)

    def fixed(self) -> list[str]:
        return [f.text for f in self.frames if isinstance(f, TTSSpeakFrame)]


def booking_call():
    """An identified caller in the booking step, with a haircut read back to them and heard in full."""
    books.reset()
    books.set_clock(CLOCK)
    call = open_call(PHONE, diary=books)
    call.commit(agent='booking', customer_verified='existing', verified_phone=PHONE)
    return call


async def read_back_a_haircut(call, probe) -> str:
    """Turn one: the agent reads a Friday haircut back, and its audio plays."""
    call.turn_started('A haircut on Friday at nine, please.')
    slots = books.find_slots(PHONE, 'Friday', 'haircut', books.now(), offered_to=call.id)['slots']
    slot = next(s for s in slots if s['time'] == '09:00')
    context = RunContext(deps=call, model=None, usage=None)  # ty: ignore[invalid-argument-type]
    question = await ask_booking(context, 'book', slot['slot_id'], '')
    await call.speak_now(question.put(call), origin='ask')
    call.note_reached()
    call.note_drained()
    call.turn_ended(interrupted=False)
    return question.spoken


@contextmanager
def classifier(labels: dict[str, str], hold: str = ''):
    """The consent classifier, scripted by utterance. `hold` is an utterance whose reading never comes back.

    Returns the list of utterances it was asked about, so a test can see what it was given.
    """
    asked: list[str] = []
    held = asyncio.Event()

    async def classify(messages, info):
        data = ConsentInput.model_validate_json(messages[-1].parts[0].content)
        asked.append(data.utterance)
        if data.utterance == hold:
            held.set()
            await asyncio.Event().wait()
        confirmation = labels.get(data.utterance, 'unclear') if data.pending else 'not_applicable'
        result = ReplyClassification(confirmation=confirmation)  # ty: ignore[invalid-argument-type]
        return ModelResponse(parts=[TextPart(result.model_dump_json())])

    with patch.object(CLASSIFIER, 'model', FunctionModel(function=classify)):
        yield asked, held


def scripted_steps(reasks: list[int]):
    """Every step's model, scripted: booking asks the question again (what the model did live), the rest speak."""

    async def booking(messages, info):
        reasks.append(1)
        slot = books.find_slots(PHONE, 'Friday', 'haircut', books.now(), offered_to='x')['slots'][0]
        yield {
            0: DeltaToolCall(
                name='ask_booking',
                json_args=f'{{"action":"book","slot_id":"{slot["slot_id"]}","booking_id":""}}',
            )
        }

    async def other(messages, info):
        yield 'Anything else?'

    stack = ExitStack()
    for name, node in agents.AGENTS.items():
        model = FunctionModel(stream_function=booking if name == 'booking' else other)
        stack.enter_context(node.agent.override(model=model))
    return stack


async def a_reply_split_by_a_pause_is_one_answer():
    """The Coval failure itself: the second half of a yes still answers the question, and nothing is asked twice."""
    call = booking_call()
    probe = Probe(call)
    readback = await read_back_a_haircut(call, probe)
    joined = 'Yes, please. That sounds perfect.'
    reasks: list[int] = []
    with classifier({joined: 'accept'}, hold='Yes, please.') as (asked, held), scripted_steps(reasks):
        probe.interrupt()
        first = asyncio.create_task(probe._turn('Yes, please.'))
        await asyncio.wait_for(held.wait(), 3)
        probe.interrupt()
        await asyncio.wait_for(first, 3)
        await probe._turn('That sounds perfect.')
    assert call.turn == 2 and call.heard == joined, (call.turn, call.heard)
    assert asked[-1] == joined, asked
    assert call.state.writes == 1, 'the finished yes did not book'
    assert probe.fixed().count(readback) == 1, probe.fixed()
    assert not reasks, 'the booking model was asked to handle an answer the application already had'
    said = [p.content for m in call.messages for p in m.parts if isinstance(p, UserPromptPart)]
    assert said[-2:] == ['Yes, please.', 'That sounds perfect.'], said


async def a_reply_after_the_answer_played_is_a_new_turn():
    """The other side of the line: once the caller has heard the agent, what they say next is a new turn."""
    call = booking_call()
    probe = Probe(call)
    await read_back_a_haircut(call, probe)
    reasks: list[int] = []
    with classifier({}), scripted_steps(reasks):
        probe.interrupt()
        await probe._turn('Hmm, how long does it take?')
        assert call.turn == 2
        call.note_reached()
        probe.interrupt()
        await probe._turn('Okay.')
    assert call.turn == 3 and call.heard == 'Okay.', (call.turn, call.heard)


async def speech_nobody_heard_is_recorded_as_unheard():
    """A piece's answer that never played is kept for audit but no longer shown to the model as said."""
    call = booking_call()
    probe = Probe(call)
    await read_back_a_haircut(call, probe)
    call.turn_started('Yes, please.')
    await call.speak_now('Let me check that.', origin='entry')
    unheard = call.spoken[-1]
    call.note_barge()
    call.turn_started('That sounds perfect.')
    assert call.turn == 2 and unheard.text == '' and unheard.generated == 'Let me check that.'
    assert 'Let me check that.' not in str(project(call, call.messages))


async def an_attempted_write_is_never_authorised_twice():
    """If the first piece already acted on the answer, the continuation cannot spend the same answer again."""
    call = booking_call()
    probe = Probe(call)
    await read_back_a_haircut(call, probe)
    call.turn_started('Yes, please.')
    assert call.pending('booking') is not None
    call.answered()
    call.note_business_attempt()
    call.note_barge()
    call.turn_started('And thanks.')
    assert call.turn == 2 and call.pending('booking') is None, 'a spent answer came back'

    call = booking_call()
    probe = Probe(call)
    await read_back_a_haircut(call, probe)
    call.turn_started('Yes, please.')
    call.answered()
    call.note_barge()
    call.turn_started('That sounds perfect.')
    assert call.pending('booking') is not None, 'a half-sentence consumed the question for good'


async def a_read_back_cut_off_is_not_agreed_to():
    """The K3 failure: a read-back the caller spoke over is not a question they can say yes to.

    The caller's next words restated the number, the classifier read that as
    agreement, and the caller was verified on a read-back that played for
    200 ms. Whether the runtime's text report is ever matched to the line or
    not, audio that never ran to its end was not heard in full.
    """
    for started in (False, True):
        call = booking_call()
        probe = Probe(call)
        call.turn_started('A haircut on Friday at nine, please.')
        slots = books.find_slots(PHONE, 'Friday', 'haircut', books.now(), offered_to=call.id)['slots']
        slot = next(s for s in slots if s['time'] == '09:00')
        context = RunContext(deps=call, model=None, usage=None)  # ty: ignore[invalid-argument-type]
        question = await ask_booking(context, 'book', slot['slot_id'], '')
        await call.speak_now(question.put(call), origin='ask')
        if started:
            call.note_reached()
        probe.interrupt()
        call.turn_started('Yes, book it.')
        assert call.pending('booking') is None, f'agreed to a read-back cut off (audio started: {started})'
        readback = next(s for s in call.all_spoken if s.generated == question.spoken)
        assert readback.interrupted and (readback.text == '') is not started, readback


def verify_call():
    """A caller in verify who has just been asked for their number, and heard the request in full."""
    books.reset()
    books.set_clock(CLOCK)
    call = open_call('', diary=books)
    call.commit(agent='verify', pending='booking')
    call.turn_started('I would like a haircut on Friday.')
    call.note_said(
        'fixed', 'Which phone number should I use? Please say it with the country code.', origin='guidance'
    )
    call.note_reached()
    call.note_drained()
    call.turn_ended(interrupted=False)
    return call


def a_number_cut_off_mid_digits_is_waited_for():
    """Which words count as a caller stopping in the middle of a number."""
    call = verify_call()
    for heard, waits in (
        ("Yes, please. It's +. 3.", True),
        ('+34 611.', True),
        ('+34 611 111 111.', False),
        ('It is from Spain.', False),
        ('Yes, please.', False),
    ):
        call._pieces = [heard]
        call.heard = heard
        assert identity.number_unfinished(call) is waits, heard
    call.commit(agent='booking')
    call.heard = 'Friday at 9'
    assert not identity.number_unfinished(call), 'only verify waits for digits'


async def a_number_in_pieces_arrives_as_one_reply():
    """The K3 loop: "+34." and a pause is waited for, and the rest joins it, so the number is read back once."""
    for goes_on in (True, False):
        call = verify_call()
        probe = Probe(call)
        heard_by_model: list[str] = []

        async def verify(messages, info, call=call, heard_by_model=heard_by_model):
            heard_by_model.append(call.heard)
            digits = ''.join(c for c in call.heard if c.isdigit())
            if len(digits) == 11:
                yield {0: DeltaToolCall(name='ask_number', json_args=f'{{"phone":"+{digits}"}}')}
            else:
                yield {0: DeltaToolCall(name='verification_guidance', json_args='{"topic":"incomplete"}')}

        with (
            patch.object(identity, 'UNFINISHED_WAIT', 0.3),
            agents.AGENTS['verify'].agent.override(model=FunctionModel(stream_function=verify)),
        ):
            probe.interrupt()
            first = asyncio.create_task(probe._turn("It's +34."))
            await asyncio.sleep(0.1)
            assert not heard_by_model, 'answered a caller who had stopped mid-number'
            if goes_on:
                probe.interrupt()
                await asyncio.wait_for(first, 3)
                await probe._turn('611 111 111.')
            else:
                await asyncio.wait_for(first, 3)
        said = probe.fixed()
        if goes_on:
            assert heard_by_model == ["It's +34. 611 111 111."], heard_by_model
            assert said == ['Let me read that back to you: +34 611 111 111. Have I got that right?'], said
            assert call.turn == 2
        else:
            assert said == ['Please give me the rest of the digits.'], said


async def the_next_reply_is_told_what_was_not_heard():
    """The K5 failure: an answer spoken over is said to the next run as a fact, with what was missed."""
    call = booking_call()
    call.commit(agent='concierge')
    probe = Probe(call)
    seen: list[str] = []

    async def concierge(messages, info):
        seen.append(info.instructions or '')
        yield (
            'We do colour work, but balayage is not on our list.' if len(seen) == 1 else 'Four on Saturdays.'
        )

    with agents.AGENTS['concierge'].agent.override(model=FunctionModel(stream_function=concierge)):
        await probe._turn('Do you do balayage?')
        call.note_reached()
        probe.interrupt()
        segment = next(s for s in call.spoken if s.kind == 'model')
        call.note_heard('We do', segment_ids=(segment.id,), interrupted=True)
        await probe._turn('Oh, and when do you close on Saturday?')
    assert call.turn == 2
    assert (
        'They heard: "We do". They did not hear: "colour work, but balayage is not on our list."' in seen[-1]
    ), seen[-1]
    assert 'spoke over' not in seen[0]


async def a_missing_barge_never_merges_turns():
    """Without the caller cutting in, a silent turn is still over: a missing signal errs towards a new turn."""
    call = booking_call()
    call.turn_started('One.')
    call.turn_ended(interrupted=False)
    call.turn_started('Two.')
    assert call.turn == 2 and call.heard == 'Two.'


def heard_is_matched_by_words():
    """What the runtime says went out is matched word by word, and an interruption always cuts."""
    call = booking_call()
    call.turn_started('Do you do balayage?')
    call.note_model_text('We do colour work, but balayage is not on our menu. Hair colour is 75 euros.')
    segment = call.spoken[-1]
    assert call.note_heard('we do colour work but', segment_ids=(segment.id,), interrupted=True)
    assert segment.text == 'We do colour work, but' and segment.interrupted, segment.text

    call.turn_started('Next.')
    call.note_model_text('Hair colour is 75 euros.')
    segment = call.spoken[-1]
    assert not call.note_heard('Hair color is seventy', segment_ids=(segment.id,))
    assert segment.text == 'Hair colour is 75 euros.', 'a report that does not line up changed nothing'
    assert call.note_heard('Hair color is seventy', segment_ids=(segment.id,), interrupted=True)
    assert segment.text == 'Hair colour is 75' and segment.interrupted, segment.text


async def playback_reaches_the_call():
    """The transport's own "bot started speaking" is what tells the call its answer reached the caller."""
    call = booking_call()
    probe = Probe(call)
    await probe.setup(
        FrameProcessorSetup(
            clock=SystemClock(),
            task_manager=TaskManager(loop=asyncio.get_running_loop()),
            pipeline_worker=None,  # ty: ignore[invalid-argument-type]
        )
    )
    try:
        await probe.queue_frame(StartFrame())
        call.turn_started('Hello.')
        assert not call._reached
        await probe.queue_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        for _ in range(100):
            if call._reached:
                break
            await asyncio.sleep(0.01)
        assert call._reached and call._playback_known
    finally:
        await probe.cleanup()


async def main():
    await a_reply_split_by_a_pause_is_one_answer()
    await a_reply_after_the_answer_played_is_a_new_turn()
    await speech_nobody_heard_is_recorded_as_unheard()
    await an_attempted_write_is_never_authorised_twice()
    await a_read_back_cut_off_is_not_agreed_to()
    await a_missing_barge_never_merges_turns()
    a_number_cut_off_mid_digits_is_waited_for()
    await the_next_reply_is_told_what_was_not_heard()
    await a_number_in_pieces_arrives_as_one_reply()
    heard_is_matched_by_words()
    await playback_reaches_the_call()
    print('continuation ok')


if __name__ == '__main__':
    asyncio.run(main())
