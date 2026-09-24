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
from core.state import Checkpoint
from salon import agents, diary
from salon.classification import CLASSIFIER, DETOUR
from salon.diary import Diary
from salon.models.consent import ConsentInput, ReplyClassification
from salon.models.state import open_call
from salon.tools.booking import ask_booking, offer

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
        asked.append(data.answer)
        if data.answer == hold:
            held.set()
            await asyncio.Event().wait()
        confirmation = labels.get(data.answer, 'unclear') if data.pending else 'not_applicable'
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
        call.note_drained()
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


async def the_next_reply_is_told_what_was_not_heard():
    """The K5 failure: the caller talks over the first answer with their second question.

    They were still talking, so it is one turn with both questions in it, and
    the run that answers it is told how far the first answer got.
    """
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
    assert call.turn == 1, call.turn
    assert call.heard == 'Do you do balayage? Oh, and when do you close on Saturday?', call.heard
    kept = [s.text for s in call.all_spoken if s.generated.startswith('We do colour')]
    assert kept == ['We do'], 'what the caller heard of the cut-off answer was lost'
    assert (
        'They heard: "We do". They did not hear: "colour work, but balayage is not on our list."' in seen[-1]
    ), seen[-1]
    assert 'spoke over' not in seen[0]


async def a_number_talked_over_arrives_whole():
    """The K3 loop: the caller goes on with the digits while the agent is asking for the rest.

    When the caller stops is the runtime's endpointing. The harness's part is
    that they were still talking, so their pieces accumulate and the last
    piece completes the number, whatever the agent had begun to say.
    """
    books.reset()
    books.set_clock(CLOCK)
    call = open_call('', diary=books)
    call.commit(agent='verify', pending='booking')
    probe = Probe(call)
    heard_by_model: list[str] = []

    async def verify(messages, info):
        heard_by_model.append(call.heard)
        digits = ''.join(c for c in call.heard if c.isdigit())
        if len(digits) == 11:
            yield {0: DeltaToolCall(name='ask_number', json_args=f'{{"phone":"+{digits}"}}')}
        else:
            yield {0: DeltaToolCall(name='verification_guidance', json_args='{"topic":"incomplete"}')}

    with agents.AGENTS['verify'].agent.override(model=FunctionModel(stream_function=verify)):
        await probe._turn("It's +34.")
        call.note_reached()
        probe.interrupt()
        await probe._turn('611 111 111.')
    assert call.turn == 1 and call.heard == "It's +34. 611 111 111.", (call.turn, call.heard)
    assert heard_by_model == ["It's +34.", "It's +34. 611 111 111."], heard_by_model
    assert probe.fixed()[-1] == 'Let me read that back to you: +34 611 111 111. Have I got that right?'


async def a_booking_taken_back_is_offered_for_cancelling():
    """The K2 failure: a picked time books at once; "leave it" must not end the call with it still booked.

    And a reply that agrees and says goodbye in one breath runs what it agreed
    to: "Yes, cancel it. Bye." used to hang up on the yes.
    """
    call = booking_call()
    probe = Probe(call)
    call.turn_started('A hair colour on Friday.')
    await offer(call, 'Friday', 'hair-color')
    call.note_reached()
    call.note_drained()
    call.turn_ended(interrupted=False)
    slot = next(s for s in call.state.offered if s.time.hour == 9 and s.time.minute == 0)
    readings = {
        "Nine o'clock works.": {'chosen_offer': slot.slot_id},
        "Oh, that's too much for me. Leave it, thanks. Goodbye.": {
            'last_change': 'withdraw',
            'conversation': 'end',
        },
        'Yes, cancel it. Bye.': {'confirmation': 'accept', 'conversation': 'end'},
    }
    given: list[ConsentInput] = []

    async def classify(messages, info):
        data = ConsentInput.model_validate_json(messages[-1].parts[0].content)
        given.append(data)
        fields = dict(readings.get(data.answer, {}))
        if not data.just_changed:
            fields.pop('last_change', None)
        if data.pending is None or data.pending.about != 'booking':
            fields.pop('confirmation', None)
        if data.pending is None or data.pending.about != 'offer':
            fields.pop('chosen_offer', None)
        return ModelResponse(parts=[TextPart(ReplyClassification.model_validate(fields).model_dump_json())])

    async def booking(messages, info):
        yield {
            0: DeltaToolCall(
                name='save_booking',
                json_args=f'{{"action":"book","slot_id":"{slot.slot_id}","booking_id":""}}',
            )
        }

    async def other(messages, info):
        yield 'Anything else?'

    with ExitStack() as stack:
        stack.enter_context(patch.object(CLASSIFIER, 'model', FunctionModel(function=classify)))
        for name, node in agents.AGENTS.items():
            model = FunctionModel(stream_function=booking if name == 'booking' else other)
            stack.enter_context(node.agent.override(model=model))
        await probe._turn("Nine o'clock works.")
        assert call.state.writes == 1 and len(books.bookings_for(PHONE, CLOCK.date())) == 1
        call.note_reached()
        call.note_drained()
        probe.interrupt()
        await probe._turn("Oh, that's too much for me. Leave it, thanks. Goodbye.")
        assert given[-1].just_changed, 'the classifier was not told what had just been booked'
        assert call.ended == '', 'hung up with the booking still in the diary'
        cancel = probe.fixed()[-1]
        assert cancel.startswith('So that is your hair colour') and cancel.endswith(
            'cancelled. Shall I go ahead?'
        )
        call.note_reached()
        call.note_drained()
        probe.interrupt()
        await probe._turn('Yes, cancel it. Bye.')
    assert call.state.writes == 2, 'the agreed cancel was dropped for the goodbye'
    assert books.bookings_for(PHONE, CLOCK.date()) == [], books.bookings_for(PHONE, CLOCK.date())


async def only_a_slot_on_the_table_can_be_picked():
    """A pick is the classifier's reading checked against the table, never a word list and never a free slot id.

    "Nine, but only if it costs less than thirty" named a time and used to
    book on the time alone. And a reading naming a real slot that was never
    offered to this caller authorises nothing, whoever produced it.
    """
    for reading, books_it in (
        ('offered', True),
        ('conditional', False),
        ('elsewhere', False),
    ):
        call = booking_call()
        probe = Probe(call)
        call.turn_started('A hair colour on Friday.')
        await offer(call, 'Friday', 'hair-color')
        call.note_reached()
        call.note_drained()
        call.turn_ended(interrupted=False)
        nine = next(s for s in call.state.offered if s.time.hour == 9 and s.time.minute == 0).slot_id
        saturday = books.find_slots(PHONE, 'Saturday', 'hair-color', books.now(), offered_to='someone else')
        chosen = {'offered': nine, 'conditional': '', 'elsewhere': saturday['slots'][0]['slot_id']}[reading]

        async def classify(messages, info, chosen=chosen):
            return ModelResponse(parts=[TextPart(ReplyClassification(chosen_offer=chosen).model_dump_json())])

        async def booking(messages, info):
            yield {0: DeltaToolCall(name='booking_guidance', json_args='{"topic":"time"}')}

        with (
            patch.object(CLASSIFIER, 'model', FunctionModel(function=classify)),
            agents.AGENTS['booking'].agent.override(model=FunctionModel(stream_function=booking)),
        ):
            probe.interrupt()
            await probe._turn("Nine o'clock, but only if it costs less than thirty euros.")
        assert (call.state.writes == 1) is books_it, (reading, call.state.writes)
        if books_it:
            assert [r.kind for r in call.requests] == ['classifier'], 'a plain pick needs no model'


async def a_pick_with_no_question_pending_still_books():
    """Coval run DkLvLzC5, K5: the pick came two turns after the offer, with no question pending.

    "Nine, but only if it costs less than thirty" answered the offer; the
    price came back from the front desk; "Fine, nine o'clock then, that's
    all, goodbye" picked from the same table. The reply was read only when a
    question was pending, so the front desk's model heard it and hung up. The
    table is enough to read it, and the pick books before the goodbye.
    """
    call = booking_call()
    probe = Probe(call)
    call.turn_started('A hair colour on Friday.')
    await offer(call, 'Friday', 'hair-color')
    call.note_reached()
    call.note_drained()
    call.turn_ended(interrupted=False)
    nine = next(s for s in call.state.offered if s.time.hour == 9 and s.time.minute == 0).slot_id
    readings = {
        "Nine o'clock, but only if it costs less than thirty euros.": {'confirmation': 'unclear'},
        "Oh, okay. Fine. Nine o'clock then. That's all I need. Thank you. Goodbye.": {
            'chosen_offer': nine,
            'conversation': 'end',
        },
    }

    async def classify(messages, info):
        data = ConsentInput.model_validate_json(messages[-1].parts[0].content)
        return ModelResponse(
            parts=[TextPart(ReplyClassification.model_validate(readings[data.answer]).model_dump_json())]
        )

    runs: list[str] = []

    async def desk(messages, info):
        runs.append('concierge')
        yield 'Hair colour is 75 euros.'

    call.commit(agent='concierge')
    with (
        patch.object(CLASSIFIER, 'model', FunctionModel(function=classify)),
        agents.AGENTS['concierge'].agent.override(model=FunctionModel(stream_function=desk)),
    ):
        probe.interrupt()
        await probe._turn("Nine o'clock, but only if it costs less than thirty euros.")
        assert call.state.writes == 0 and runs == ['concierge'], 'a condition books nothing'
        call.note_reached()
        call.note_drained()
        probe.interrupt()
        await probe._turn("Oh, okay. Fine. Nine o'clock then. That's all I need. Thank you. Goodbye.")
    assert call.state.writes == 1, 'the pick from the table was not booked'
    assert runs == ['concierge'], 'the front desk model answered a reply the application had already read'


async def a_missing_barge_never_merges_turns():
    """Without the caller cutting in, a silent turn is still over: a missing signal errs towards a new turn."""
    call = booking_call()
    call.turn_started('One.')
    call.turn_ended(interrupted=False)
    call.turn_started('Two.')
    assert call.turn == 2 and call.heard == 'Two.'


def a_detour_keeps_the_rest_of_its_first_turn():
    """The caller went on in the turn that started the identity check: that piece was said to the check too.

    The checkpoint holds the request as it stood when the detour began, and
    the continuation keeps the turn's number, so counting only later turns
    lost "make it Saturday" from the words the waiting step is given.
    """
    call = open_call('', diary=books)
    call.turn_started('A haircut on Friday.')
    call._checkpoint = Checkpoint('booking', [], frozenset(), call.heard, call.turn)
    call._playback_known, call._interrupted = True, True
    call.turn_started('Actually, make it Saturday.')
    assert call.turn == 1, 'a piece before any answer played continues the turn'
    assert call.detour_request == 'A haircut on Friday.'
    assert call.detour_words() == ['Actually, make it Saturday.'], call.detour_words()
    call.turn_started('Yes.')
    assert call.detour_words() == ['Actually, make it Saturday.', 'Yes.'], call.detour_words()
    assert DETOUR._input(call).answer == 'Actually, make it Saturday. Yes.', DETOUR._input(call)
    call.note_request_kept()
    assert call.request_kept
    call.turn_started('And one more thing.')
    assert not call.request_kept, 'a reading of one turn is no evidence about the next'


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
    await the_next_reply_is_told_what_was_not_heard()
    await a_number_talked_over_arrives_whole()
    await a_booking_taken_back_is_offered_for_cancelling()
    await only_a_slot_on_the_table_can_be_picked()
    await a_pick_with_no_question_pending_still_books()
    a_detour_keeps_the_rest_of_its_first_turn()
    heard_is_matched_by_words()
    await playback_reaches_the_call()
    print('continuation ok')


if __name__ == '__main__':
    asyncio.run(main())
