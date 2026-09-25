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
import json
from contextlib import ExitStack, contextmanager, suppress
from datetime import date, datetime
from unittest.mock import patch

from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import BotStartedSpeakingFrame, StartFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup
from pipecat.utils.asyncio.task_manager import TaskManager
from pydantic import create_model
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from core.history import project
from core.processor import PydanticAgentProcessor
from core.readings import NamedChoice
from core.state import Checkpoint
from salon import agents, diary
from salon.classification import SALON
from salon.diary import Diary
from salon.models.state import open_call
from salon.tools.asking import Ask
from salon.tools.booking import booking_question, offer, offer_cancel, undo_ready
from tests.classifier_model import scripted as scripted_readings

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


def played_out(call) -> None:
    """The audio ran out after playing in full: the runtime's stop signal, a long while after its start.

    A test has no audio, so start and stop arrive in the same instant, which
    is what audio cut off by the caller looks like (`CallContext._stopped_early`).
    The start is moved back instead.
    """
    if call._burst != float('inf'):
        call._burst -= 60
    call.note_drained()


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
    question = booking_question(call, 'book', slot['slot_id'], '')
    await call.speak_now(question.put(call), origin='ask')
    call.note_reached()
    played_out(call)
    call.turn_ended(interrupted=False)
    return question.spoken


@contextmanager
def classifier(labels: dict[str, str], hold: str = ''):
    """The consent classifier, scripted by utterance. `hold` is an utterance whose reading never comes back.

    Returns the list of utterances it was asked about, so a test can see what it was given.
    """
    asked: list[str] = []
    held = asyncio.Event()

    async def classify(name, data):
        if name != 'confirmation':
            return None
        asked.append(data['answer'])
        if data['answer'] == hold:
            held.set()
            await asyncio.Event().wait()
        return labels.get(data['answer'], 'unclear')

    with scripted_readings(classify):
        yield asked, held


async def agreed_to_a_pick(call, probe, pick: str) -> None:
    """A pick of an offered time is read back with no model, and the caller's "Yes." books it.

    The script in force must read `pick` as that slot and "Yes." as accept.
    """
    writes = call.state.writes
    probe.interrupt()
    await probe._turn(pick)
    assert call.state.writes == writes, f'{pick!r} booked before it was read back'
    assert probe.fixed()[-1].startswith('Shall I book you in for a'), probe.fixed()
    call.note_reached()
    played_out(call)
    probe.interrupt()
    await probe._turn('Yes.')
    assert call.state.writes == writes + 1, 'the yes to the read-back did not book'
    call.note_reached()
    played_out(call)


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
        played_out(call)
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


async def a_holding_line_heard_in_full_keeps_the_turn_open():
    """A holding line answers nothing: a caller who goes on after hearing all of it is still in the same turn."""
    call = booking_call()
    probe = Probe(call)
    await read_back_a_haircut(call, probe)
    call.turn_started('Yes, please.')
    await call.say('Let me have a look.', origin='tool_filler')
    call.note_reached()
    played_out(call)
    call.note_barge()
    call.turn_started('That sounds perfect.')
    assert call.turn == 2 and call.heard == 'Yes, please. That sounds perfect.', (call.turn, call.heard)


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
        question = booking_question(call, 'book', slot['slot_id'], '')
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
    assert heard_by_model == ["It's +34."], 'the whole number is read back by code, not counted by the model'
    assert probe.fixed()[-1] == 'Let me read that back to you: +34 611 111 111. Have I got that right?'


async def a_number_with_a_digit_too_many_is_refused_with_its_reason():
    """Coval run 4bLgLR4t, cases 06 and 13: speech to text heard one digit too many after "+34".

    The agent said "Please say the full number again" with no reason, and the
    caller said the same number again. Now the count is said, by code.
    """
    from salon.tools.identity import number_problem

    assert number_problem('+34 672. 1. 273. 45. 4.') == (
        'I heard 10 digits after +34, and a Spanish number has nine. Could you say the whole number again, please?'
    )
    assert number_problem("It's +34 6. 71. 2.") == '', 'too few is a caller still going'
    assert number_problem("Of course. It's +34 6. 71. 2. 73. 4. 5. 4.") == '', 'a whole number has no problem'

    books.reset()
    books.set_clock(CLOCK)
    call = open_call('', diary=books)
    call.commit(agent='verify', pending='booking')
    probe = Probe(call)

    async def verify(messages, info):
        raise AssertionError('the model was asked to explain a count the application had')
        yield ''

    with agents.AGENTS['verify'].agent.override(model=FunctionModel(stream_function=verify)):
        await probe._turn('+34 672. 1. 273. 45. 4.')
    assert probe.fixed()[-1].startswith('I heard 10 digits after +34'), probe.fixed()
    assert call.requests == [], 'saying why costs no request'


async def a_number_in_pieces_is_read_back_without_the_model():
    """Coval run L5RWCS7b, K3: nine digits said in pauses, and the model asked for "the rest" three times.

    Speech-to-text writes the digits as numerals, so the application counts
    them. Nine after "+34" is read back with no model request; anything else
    is still the model's, which gets the count in the validation error.
    """
    from salon.tools.identity import number_heard

    assert number_heard("Of course. It's +34 6. 71. 2. 73. 4. 5. 4.") == '+34671273454'
    assert number_heard("That's the full number. +34. 671. 273. 45. 4.") == '+34671273454'
    assert number_heard('+34. 671. 273. 4. 5. 4. 0. And sorry, can we make it Saturday?') == '', 'ten digits'
    assert number_heard('+34 611 111 112, no, +34 611 111 111') == '+34611111111', 'the last number said'
    assert number_heard('six one one, one one one, one one one') == '', 'digits in words are the model'

    books.reset()
    books.set_clock(CLOCK)
    call = open_call('', diary=books)
    call.commit(agent='verify', pending='booking')
    probe = Probe(call)

    async def verify(messages, info):
        raise AssertionError('the model was asked to count digits the application had')
        yield ''

    with agents.AGENTS['verify'].agent.override(model=FunctionModel(stream_function=verify)):
        await probe._turn("Of course. It's +34 6. 71. 2. 73. 4. 5. 4.")
    assert probe.fixed()[-1] == 'Let me read that back to you: +34 671 273 454. Have I got that right?'
    assert call.requests == [], 'reading a whole number back costs no request'


async def a_move_after_a_booking_is_read_back_not_cancelled():
    """The user's call of 24 September (trace cd3c08fa), turns 6 to 9: book, then move it to Saturday, same time.

    "Move it to Saturday instead" was read as taking the booking back, and the
    caller was offered a cancel. Then "Same time." cost six requests in a row
    and a refused save. Now the take-back reading says `change`, the front
    desk hands the move over in one request, booking offers Saturday's times
    marked as a move with no model, the pick is read back as the move with no
    model, and the yes moves it.
    """
    call = booking_call()
    probe = Probe(call)
    call.turn_started('A haircut on Friday.')
    await offer(call, 'Friday', 'haircut')
    call.note_reached()
    played_out(call)
    call.turn_ended(interrupted=False)
    friday = next(s for s in call.state.offered if s.time.hour == 9 and s.time.minute == 0)
    saturday: list[str] = []

    def classify(name, data):
        answer = data['answer']
        if name == 'chosen_offer' and answer == "Nine o'clock works.":
            return friday.slot_id
        if name == 'chosen_offer' and answer == 'Same time.':
            nine = next(o for o in data['options'] if '9:00' in o['said'])
            saturday.append(nine['id'])
            return nine['id']
        if name == 'confirmation' and answer in ('Yes.', 'Yes, please.'):
            return 'accept'
        if name == 'last_change' and 'Saturday' in answer:
            return 'change'
        if name == 'conversation':
            return 'continue'
        return None

    asked: dict[str, int] = {}

    async def desk(messages, info):
        asked['concierge'] = asked.get('concierge', 0) + 1
        yield {
            0: DeltaToolCall(
                name='hand_over',
                json_args=json.dumps(
                    {
                        'to': 'booking',
                        'because': 'move the haircut to Saturday',
                        'booking': {'service': 'haircut', 'when': 'Saturday', 'change': 'move'},
                    }
                ),
            )
        }

    async def other(messages, info):
        asked['step'] = asked.get('step', 0) + 1
        yield 'Anything else?'

    with ExitStack() as stack:
        stack.enter_context(scripted_readings(classify))
        for name, node in agents.AGENTS.items():
            model = FunctionModel(stream_function=desk if name == 'concierge' else other)
            stack.enter_context(node.agent.override(model=model))
        await agreed_to_a_pick(call, probe, "Nine o'clock works.")
        (held,) = books.bookings_for(PHONE, CLOCK.date())
        asked.clear()

        probe.interrupt()
        await probe._turn('Sorry, can you actually move the appointment to Saturday instead?')
        offered = probe.fixed()[-1]
        assert 'cancel' not in offered.lower(), 'a caller who asked to move was offered a cancel'
        assert offered.startswith('For a haircut on Saturday'), offered
        assert asked == {'concierge': 1}, asked
        call.note_reached()
        played_out(call)

        probe.interrupt()
        await probe._turn('Same time.')
        assert probe.fixed()[-1].startswith('Shall I move your appointment to Saturday'), probe.fixed()[-1]
        assert asked == {'concierge': 1}, f'the pick of a move cost a model request: {asked}'
        call.note_reached()
        played_out(call)

        probe.interrupt()
        await probe._turn('Yes.')
    (moved,) = books.bookings_for(PHONE, CLOCK.date())
    assert moved['booking_id'] == held['booking_id'], 'the booking held was moved, not a second one added'
    assert date.fromisoformat(moved['date']).weekday() == 5 and moved['time'] == '09:00', (held, moved)
    assert saturday and asked == {'concierge': 1}, asked


AJ4T = "Yes, that's right. Oh, and sorry, can we make it Saturday instead of Friday?"
"""Coval run hdbogMqB, AJ4t: the caller's answer to their number read back."""


async def a_yes_with_a_new_request_is_verified_and_the_rest_answered():
    """Coval run hdbogMqB, AJ4t: a yes to the number, and a new day in the same breath.

    The yes is the check's: the lookup runs with no model. The new day is the
    booking step's: the caller goes back to it in the same turn, the Friday
    times its opening would have offered are skipped because the request
    changed, and its model is given the whole reply. Nobody asks for the
    number again.
    """
    books.reset()
    books.set_clock(CLOCK)
    call = open_call(PHONE, diary=books)
    probe = Probe(call)
    booking_asked: list[str] = []

    def classify(name, data):
        if name == 'confirmation':
            return 'accept' if data['answer'] == AJ4T else None
        if name == 'request_change':
            return 'changed'
        if name == 'conversation':
            return 'continue'
        return None

    async def desk(messages, info):
        yield {
            0: DeltaToolCall(
                name='hand_over',
                json_args=json.dumps(
                    {
                        'to': 'booking',
                        'because': 'wants a haircut on Friday',
                        'booking': {'service': 'haircut', 'when': 'Friday'},
                    }
                ),
            )
        }

    async def booking(messages, info):
        booking_asked.append(f'{messages!r} {info.instructions}')
        (tool,) = [t.name for t in info.output_tools if 'offer_times' in t.name]
        yield {0: DeltaToolCall(name=tool, json_args=json.dumps({'when': 'Saturday', 'service': 'haircut'}))}

    async def other(messages, info):
        yield 'Anything else?'

    steps = {'concierge': desk, 'booking': booking}
    with ExitStack() as stack:
        stack.enter_context(scripted_readings(classify))
        for name, node in agents.AGENTS.items():
            model = FunctionModel(stream_function=steps.get(name, other))
            stack.enter_context(node.agent.override(model=model))
        await probe._turn('A haircut on Friday, please.')
        assert call.state.agent == 'verify', call.state.agent
        assert probe.fixed() == ['Let me read that back to you: +34 611 111 111. Have I got that right?'], (
            probe.fixed()
        )
        call.note_reached()
        played_out(call)
        booking_asked.clear()

        probe.interrupt()
        await probe._turn(AJ4T)
    assert call.state.customer_verified and call.state.verified_phone == PHONE, 'the yes did not verify'
    assert call.state.agent == 'booking', call.state.agent
    assert len(booking_asked) == 1, 'the booking model did not answer the rest, once'
    assert 'Saturday instead of Friday' in booking_asked[0], 'the booking model was not given the new day'
    assert 'verify completed' in booking_asked[0], 'the booking model was not told the check completed'
    assert not any(line.startswith('For a haircut on Friday') for line in probe.fixed()), probe.fixed()
    thanks, offered = probe.fixed()[-2:]
    assert thanks in ('Thanks, I have you.', 'Thank you, I have you now.'), probe.fixed()
    assert offered.startswith('For a haircut on Saturday'), probe.fixed()
    assert 'I have you' in booking_asked[0], 'the booking model was not shown the line already said'
    asked = call.pending_question()
    assert asked is None or asked.about != 'phone', 'the number was asked for again'


async def a_booking_taken_back_is_offered_for_cancelling(asks_price: bool):
    """The K2 failure: a picked time books at once; "leave it" must not end the call with it still booked.

    Whether "leave it" comes on the very next turn or after "How much is that?"
    (Coval run Y47Ut7oC, K2, where the later one hung up on it).

    And a reply that agrees and says goodbye in one breath runs what it agreed
    to: "Yes, cancel it. Bye." used to hang up on the yes.
    """
    call = booking_call()
    probe = Probe(call)
    call.turn_started('A hair colour on Friday.')
    await offer(call, 'Friday', 'hair-color')
    call.note_reached()
    played_out(call)
    call.turn_ended(interrupted=False)
    slot = next(s for s in call.state.offered if s.time.hour == 9 and s.time.minute == 0)
    readings = {
        "Nine o'clock works.": {'chosen_offer': slot.slot_id},
        'Yes.': {'confirmation': 'accept'},
        "Oh, that's too much for me. Leave it, thanks. Goodbye.": {
            'last_change': 'withdraw',
            'conversation': 'end',
        },
        'Yes, cancel it. Bye.': {'confirmation': 'accept', 'last_change': 'withdraw', 'conversation': 'end'},
    }
    given: list[tuple[str, str]] = []

    def classify(name, data):
        given.append((name, data['answer']))
        return readings.get(data['answer'], {}).get(name)

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
        stack.enter_context(scripted_readings(classify))
        for name, node in agents.AGENTS.items():
            model = FunctionModel(stream_function=booking if name == 'booking' else other)
            stack.enter_context(node.agent.override(model=model))
        await agreed_to_a_pick(call, probe, "Nine o'clock works.")
        assert len(books.bookings_for(PHONE, CLOCK.date())) == 1
        if asks_price:
            probe.interrupt()
            await probe._turn('How much is that?')
            call.note_reached()
            played_out(call)
        probe.interrupt()
        await probe._turn("Oh, that's too much for me. Leave it, thanks. Goodbye.")
        assert ('last_change', call.heard) in given, (
            'the reply was not read against what had just been booked'
        )
        assert call.ended == '', 'hung up with the booking still in the diary'
        cancel = probe.fixed()[-1]
        assert cancel.startswith('Shall I cancel your hair colour'), cancel
        call.note_reached()
        played_out(call)
        probe.interrupt()
        await probe._turn('Yes, cancel it. Bye.')
    assert call.state.writes == 2, 'the agreed cancel was dropped for the goodbye'
    assert books.bookings_for(PHONE, CLOCK.date()) == [], books.bookings_for(PHONE, CLOCK.date())


async def a_goodbye_after_a_booking_reads_only_what_can_end_or_undo_it():
    """Coval run 4eDqRVLR: "That's all, thank you. Goodbye." after a booking ran four readings, one needed.

    On "anything else?" the two choice readings ran on every reply and never
    decided one. They are left to the booking tool, which reads them itself
    if the model goes on to move or add; what the goodbye needs is whether it
    takes the booking back and whether it ends the call.
    """
    call = booking_call()
    probe = Probe(call)
    call.turn_started('A hair colour on Friday.')
    await offer(call, 'Friday', 'hair-color')
    call.note_reached()
    played_out(call)
    call.turn_ended(interrupted=False)
    slot = next(s for s in call.state.offered if s.time.hour == 9 and s.time.minute == 0)
    readings = {
        "Nine o'clock works.": {'chosen_offer': slot.slot_id},
        'Yes.': {'confirmation': 'accept'},
        "That's all, thank you. Goodbye.": {'last_change': 'keep', 'conversation': 'end'},
    }
    given: list[tuple[str, str]] = []

    def classify(name, data):
        given.append((name, data['answer']))
        return readings.get(data['answer'], {}).get(name)

    async def booking(messages, info):
        yield {
            0: DeltaToolCall(
                name='save_booking',
                json_args=f'{{"action":"book","slot_id":"{slot.slot_id}","booking_id":""}}',
            )
        }

    async def other(messages, info):
        yield 'Goodbye.'

    with ExitStack() as stack:
        stack.enter_context(scripted_readings(classify))
        for name, node in agents.AGENTS.items():
            model = FunctionModel(stream_function=booking if name == 'booking' else other)
            stack.enter_context(node.agent.override(model=model))
        await agreed_to_a_pick(call, probe, "Nine o'clock works.")
        assert call.table() == (), 'the times offered for a booking stay on the table after it is made'
        probe.interrupt()
        await probe._turn("That's all, thank you. Goodbye.")
    read = sorted(name for name, answer in given if answer == "That's all, thank you. Goodbye.")
    assert read == ['conversation', 'last_change'], read
    assert call.ended, 'the goodbye did not end the call'
    assert len(books.bookings_for(PHONE, CLOCK.date())) == 1, 'the goodbye touched the booking'


async def a_pick_said_with_a_goodbye_is_asked_before_they_go():
    """Coval run RPH7TzmF, K5: "Fine, nine o'clock then. That's all. Goodbye!" got "So that is a hair colour...".

    The caller heard a confirmation and hung up with nothing booked. The
    read-back now asks from its first word, and says why before they go.
    """
    call = booking_call()
    probe = Probe(call)
    call.turn_started('A hair colour on Friday.')
    await offer(call, 'Friday', 'hair-color')
    call.note_reached()
    played_out(call)
    call.turn_ended(interrupted=False)
    nine = next(s for s in call.state.offered if s.time.hour == 9 and s.time.minute == 0).slot_id
    words = "Nine o'clock then. That's all I need. Goodbye!"

    def classify(name, data):
        return {'chosen_offer': nine, 'conversation': 'end'}.get(name)

    with scripted_readings(classify):
        probe.interrupt()
        await probe._turn(words)
    asked = probe.fixed()[-1]
    assert asked.startswith('Before you go: shall I book you in for a hair colour'), asked
    assert call.state.writes == 0 and call.ended == '', 'booked, or hung up, before the yes'


async def a_time_named_with_a_condition_is_not_read_back():
    """Coval run L5RWCS7b, K5: "09:00. But only if it costs less than 30 euros" was read back as a booking.

    The pick reading was right, no pick; the booking model read 9:00 back
    anyway and left the price unanswered. `ask_booking` now refuses a time
    the caller named without picking it plainly, and the model answers.
    """
    call = booking_call()
    probe = Probe(call)
    call.turn_started('A hair colour on Friday.')
    await offer(call, 'Friday', 'hair-color')
    call.note_reached()
    played_out(call)
    call.turn_ended(interrupted=False)
    nine = next(s for s in call.state.offered if s.time.hour == 9 and s.time.minute == 0).slot_id
    tries: list[str] = []

    def classify(name, data):
        return {'chosen_offer': ''}.get(name)

    async def booking(messages, info):
        tries.append('booking')
        if len(tries) == 1:
            yield {
                0: DeltaToolCall(
                    name='ask_booking', json_args=f'{{"action":"book","slot_id":"{nine}","booking_id":""}}'
                )
            }
        else:
            yield {0: DeltaToolCall(name='booking_guidance', json_args='{"topic":"time"}')}

    with (
        scripted_readings(classify),
        agents.AGENTS['booking'].agent.override(model=FunctionModel(stream_function=booking)),
    ):
        probe.interrupt()
        await probe._turn('09:00. But only if it costs less than 30 euros.')
    assert tries == ['booking', 'booking'], 'the refusal did not send the model back to answer'
    assert not any(line.startswith('Shall I book') for line in probe.fixed()), probe.fixed()
    assert call.pending_question() is None or call.pending_question().about != 'booking'


async def a_named_pick_is_refused_without_the_word_list():
    """With the named pick on, the reading says which time was asked about, so no English word list is needed.

    "The first one, if it is under 30 euros" names no time `times_named` knows,
    so the word-list guard reads it back. The named reading refuses it.
    """
    choosing = SALON.offer('chosen_offer')
    named_output = create_model('ChosenOfferChoice', __base__=NamedChoice)

    async def attempt(named: bool) -> tuple[list[str], list[str]]:
        call = booking_call()
        probe = Probe(call)
        call.turn_started('A hair colour on Friday.')
        await offer(call, 'Friday', 'hair-color')
        call.note_reached()
        played_out(call)
        call.turn_ended(interrupted=False)
        first = call.state.offered[0].slot_id
        tries: list[str] = []

        def classify(name, data):
            return {'choice': '', 'named': first} if name == 'chosen_offer' and named else None

        async def booking(messages, info):
            tries.append('booking')
            if len(tries) == 1:
                yield {
                    0: DeltaToolCall(
                        name='ask_booking',
                        json_args=f'{{"action":"book","slot_id":"{first}","booking_id":""}}',
                    )
                }
            else:
                yield {0: DeltaToolCall(name='booking_guidance', json_args='{"topic":"time"}')}

        with ExitStack() as stack:
            if named:
                stack.enter_context(patch.object(choosing, 'named', True))
                stack.enter_context(patch.object(choosing.reading.classifier, 'output_type', named_output))
            stack.enter_context(scripted_readings(classify))
            stack.enter_context(
                agents.AGENTS['booking'].agent.override(model=FunctionModel(stream_function=booking))
            )
            probe.interrupt()
            await probe._turn('The first one, if it is under 30 euros.')
        return tries, probe.fixed()

    tries, said = await attempt(named=False)
    assert tries == ['booking'] and any(line.startswith('Shall I book') for line in said), (
        'the word-list guard is expected to miss this wording; if it now catches it, this check shows no gain'
    )
    tries, said = await attempt(named=True)
    assert tries == ['booking', 'booking'] and not any(line.startswith('Shall I book') for line in said), said


async def only_a_slot_on_the_table_can_be_picked():
    """A pick is the classifier's reading checked against the table, never a word list and never a free slot id.

    "Nine, but only if it costs less than thirty" named a time and used to
    book on the time alone. And a reading naming a real slot that was never
    offered to this caller authorises nothing, whoever produced it. A pick
    that counts is read back, with no model; nothing is booked on the pick.
    """
    for reading, read_back in (
        ('offered', True),
        ('conditional', False),
        ('elsewhere', False),
    ):
        call = booking_call()
        probe = Probe(call)
        call.turn_started('A hair colour on Friday.')
        await offer(call, 'Friday', 'hair-color')
        call.note_reached()
        played_out(call)
        call.turn_ended(interrupted=False)
        nine = next(s for s in call.state.offered if s.time.hour == 9 and s.time.minute == 0).slot_id
        saturday = books.find_slots(PHONE, 'Saturday', 'hair-color', books.now(), offered_to='someone else')
        chosen = {'offered': nine, 'conditional': '', 'elsewhere': saturday['slots'][0]['slot_id']}[reading]

        def classify(name, data, chosen=chosen):
            return chosen if name == 'chosen_offer' else None

        async def booking(messages, info):
            yield {0: DeltaToolCall(name='booking_guidance', json_args='{"topic":"time"}')}

        with (
            scripted_readings(classify),
            agents.AGENTS['booking'].agent.override(model=FunctionModel(stream_function=booking)),
        ):
            probe.interrupt()
            await probe._turn("Nine o'clock, but only if it costs less than thirty euros.")
        assert call.state.writes == 0, (reading, 'a pick booked before it was read back')
        put = call.all_spoken[-1].generated if call.all_spoken else ''
        assert put.startswith('Shall I book you in for a') is read_back, (reading, put)
        if read_back:
            assert {r.kind for r in call.requests} == {'classifier'}, 'reading a pick back needs no model'


async def a_pick_with_no_question_pending_is_still_read_back():
    """Coval run DkLvLzC5, K5: the pick came two turns after the offer, with no question pending.

    "Nine, but only if it costs less than thirty" answered the offer; the
    price came back from the front desk; "Fine, nine o'clock then, that's
    all, goodbye" picked from the same table. The reply was read only when a
    question was pending, so the front desk's model heard it and hung up. The
    table is enough to read it: the pick is read back before the goodbye, and
    the yes to it books from the front desk, with no model either time.
    """
    call = booking_call()
    probe = Probe(call)
    call.turn_started('A hair colour on Friday.')
    await offer(call, 'Friday', 'hair-color')
    call.note_reached()
    played_out(call)
    call.turn_ended(interrupted=False)
    nine = next(s for s in call.state.offered if s.time.hour == 9 and s.time.minute == 0).slot_id
    readings = {
        "Nine o'clock, but only if it costs less than thirty euros.": {'confirmation': 'unclear'},
        "Oh, okay. Fine. Nine o'clock then. That's all I need. Thank you. Goodbye.": {
            'chosen_offer': nine,
            'conversation': 'end',
        },
        'Yes.': {'confirmation': 'accept'},
    }

    def classify(name, data):
        return readings[data['answer']].get(name)

    runs: list[str] = []

    async def desk(messages, info):
        runs.append('concierge')
        yield 'Hair colour is 75 euros.'

    call.commit(agent='concierge')
    with (
        scripted_readings(classify),
        agents.AGENTS['concierge'].agent.override(model=FunctionModel(stream_function=desk)),
    ):
        probe.interrupt()
        await probe._turn("Nine o'clock, but only if it costs less than thirty euros.")
        assert call.state.writes == 0 and runs == ['concierge'], 'a condition books nothing'
        call.note_reached()
        played_out(call)
        await agreed_to_a_pick(
            call, probe, "Oh, okay. Fine. Nine o'clock then. That's all I need. Thank you. Goodbye."
        )
    assert call.ended == '', 'hung up on a pick that was still to be read back'
    assert runs == ['concierge'], 'the front desk model answered a reply the application had already read'


async def an_add_on_to_a_pick_is_read_with_it():
    """Coval run ad3n4Vxn, K5: "09:00." booked at once, then "but only if it costs less than 30 euros".

    No endpointing can tell a complete answer from one the caller will add
    to. A pick is read back now, not booked, so the add-on continues the turn
    of the pick and nothing is booked on a condition. "Fine, nine o'clock
    then" is read back again and the yes books it; kept after that, the cancel
    offer is off the table, where the model had read the cancellation back
    anyway.
    """
    call = booking_call()
    probe = Probe(call)
    call.turn_started('A hair colour on Friday.')
    await offer(call, 'Friday', 'hair-color')
    call.note_reached()
    played_out(call)
    call.turn_ended(interrupted=False)
    nine = next(s for s in call.state.offered if s.time.hour == 9 and s.time.minute == 0).slot_id
    joined = '09:00. But only if it costs less than 30 euros.'
    readings = {
        '09:00.': {'chosen_offer': nine},
        joined: {'confirmation': 'conditional'},
        'Oh, okay. Fine. 09:00 then.': {'chosen_offer': nine},
        'Yes.': {'confirmation': 'accept'},
        'Great, I will keep it.': {'last_change': 'keep'},
    }
    seen: dict[str, set[str]] = {}

    def classify(name, data):
        seen.setdefault(data['answer'], set()).add(name)
        return readings[data['answer']].get(name)

    async def speak(messages, info):
        yield 'A hair colour is 75 euros.'

    async def booking(messages, info):
        yield {0: DeltaToolCall(name='booking_guidance', json_args='{"topic":"time"}')}

    with ExitStack() as stack:
        stack.enter_context(scripted_readings(classify))
        for name in ('booking', 'concierge', 'complaints'):
            model = FunctionModel(stream_function=booking if name == 'booking' else speak)
            stack.enter_context(agents.AGENTS[name].agent.override(model=model))
        probe.interrupt()
        await probe._turn('09:00.')
        assert call.state.writes == 0, 'a pick booked before it was read back'
        probe.interrupt()
        await probe._turn('But only if it costs less than 30 euros.')
        assert call.heard == joined and call.turn == 2, 'the add-on continues the turn of the pick'
        assert call.state.writes == 0, 'booked on a condition'
        call.note_reached()
        played_out(call)
        await agreed_to_a_pick(call, probe, 'Oh, okay. Fine. 09:00 then.')
        probe.interrupt()
        await probe._turn('Great, I will keep it.')
    assert 'last_change' in seen['Great, I will keep it.'], 'a booking still held can still be taken back'
    assert not undo_ready(call), 'the caller kept it, so the cancel offer is off the table'
    context = RunContext(deps=call, model=None, usage=None)  # ty: ignore[invalid-argument-type]
    try:
        await offer_cancel(context)
    except ModelRetry:
        pass
    else:
        raise AssertionError('a cancel was read back to a caller keeping the booking')
    assert call.state.writes == 1 and call.undo_booking, 'the booking stands'
    assert call.picked_offer() == '', 'a time the caller already holds is not on the table to pick again'
    asked = call.pending_question()
    assert asked is None or asked.about != 'move_or_add', (
        'the caller was asked to move or add the booking they kept'
    )


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
    assert ' '.join(call.detour_words()) == 'Actually, make it Saturday. Yes.', call.detour_words()
    call.note_request_kept()
    assert call.request_kept
    call.turn_started('And one more thing.')
    assert not call.request_kept, 'a reading of one turn is no evidence about the next'


async def a_yes_said_as_the_line_drops_still_counts():
    """Coval run 4eDqRVLR, K4: the caller approved and hung up while their yes was still being read.

    The pipeline was torn down at once and the approved complaint was never
    recorded. The running turn now gets a bounded time to finish; a turn that
    never finishes does not hold the line open.
    """
    call = booking_call()
    probe = Probe(call)
    await read_back_a_haircut(call, probe)
    gate = asyncio.Event()

    async def classify(name, data):
        if name != 'confirmation':
            return None
        await gate.wait()
        return 'accept'

    reasks: list[int] = []
    with scripted_readings(classify), scripted_steps(reasks):
        probe.interrupt()
        turn = asyncio.create_task(probe.serve('Yes, please.'))
        await asyncio.sleep(0.01)
        finishing = asyncio.create_task(probe.finish_turn(3))
        await asyncio.sleep(0.05)
        assert not finishing.done(), 'the line was torn down while the yes was still being read'
        gate.set()
        await asyncio.wait_for(finishing, 3)
        await turn
    assert call.state.writes == 1, 'the yes said as the line dropped did not book'

    call = booking_call()
    probe = Probe(call)
    await read_back_a_haircut(call, probe)
    with scripted_readings(lambda name, data: asyncio.Event().wait()), scripted_steps(reasks):
        probe.interrupt()
        turn = asyncio.create_task(probe.serve('Yes, please.'))
        await asyncio.sleep(0.01)
        started = asyncio.get_running_loop().time()
        await probe.finish_turn(0.1)
        assert asyncio.get_running_loop().time() - started < 1, 'a turn that never finishes held the line'
        turn.cancel()
        with suppress(asyncio.CancelledError):
            await turn


def a_read_back_said_again_in_full_can_be_answered():
    """Coval run 4eDqRVLR, K3: the caller talked over the number read-back, and it was said again, whole.

    Both copies had the same words on the same turn, and the ledger took the
    cut-off first one as the question's fate: the "yes" to the second found
    nothing pending, and the number was read back a third time. The copy the
    caller heard last is the one they are answering.
    """
    call = open_call('', diary=books)
    call.commit(agent='verify')
    call.turn_started("It's plus three four, six seven one, two seven three, four five four.")
    readback = 'Let me read that back to you: +34 671 273 454. Have I got that right?'
    Ask(about='phone', value='+34671273454', spoken=readback).put(call)
    call.note_said('fixed', readback, origin='ask')
    call.note_heard('Let me read that', segment_ids=(call.spoken[-1].id,), interrupted=True)
    Ask(about='phone', value='+34671273454', spoken=readback).put(call)
    call.note_said('fixed', readback, origin='ask')
    call.turn_started("Yes. That's right.")
    assert call.pending('phone') is not None, 'the read-back heard in full is waiting on this answer'


async def a_question_cut_off_as_the_audio_stops_is_not_heard():
    """Coval run Z56CoZax, K5: the audio's stop reached the call before the caller's cut-in did.

    The confirmation and its "Anything else?" played for 56 ms, so it was
    already `played` when the cut-in arrived, and "That's perfect." read as a
    goodbye to a question the caller never heard. Either order is one event:
    the audio stops because the caller cut in.
    """
    from salon.tools.writes import close_with

    async def line(text):
        """The call's line to the caller: without one, nothing said is recorded, and nothing here is tested."""

    for stop_first in (True, False):
        call = booking_call()
        call._say = line
        call.turn_started('Yes, please.')
        await close_with(call, "You're all set for Friday at 9:00 AM. Anything else I can help with?")
        call.note_reached()
        if stop_first:
            call.note_drained()
            call.note_barge()
        else:
            call.note_barge()
            call.note_drained()
        call.turn_ended(interrupted=True)
        call.turn_started("That's perfect.")
        assert call.pending_question() is None, (stop_first, 'answered a question the caller never heard')
        assert call.heard == "Yes, please. That's perfect.", (stop_first, call.heard)

    call = booking_call()
    call._say = line
    call.turn_started('Yes, please.')
    await close_with(call, 'Anything else?')
    assert call.all_spoken[-1].generated == 'Anything else?', 'nothing was recorded as said'
    call.note_reached()
    await asyncio.sleep(0.6)
    call.note_drained()
    call.note_barge()
    call.turn_ended(interrupted=False)
    call.turn_started('No, thanks.')
    asked = call.pending_question()
    assert asked is not None and asked.about == 'closing', 'a question that played out in full was heard'


async def a_cut_off_question_reported_in_full_is_still_cut_off():
    """Coval run 4bLgLR4t, case 05 (call CA6912853c): "That's perfect." hung up on an "Anything else?" cut off.

    The bot log: the line started at 44.830, the caller cut in at 45.034,
    the audio stopped at 45.084. Between the cut-in and the stop, Pipecat's
    assistant aggregator reported what was spoken, and that report held the
    whole line, because the TTS had been handed all of it. The report cleared
    the cut-in's mark, the stop then counted the line as played, and the next
    piece began a new turn that answered the unheard question.
    """
    from core import turn as engine
    from salon.tools.writes import close_with

    async def line(text):
        """The call's line to the caller: without one, nothing said is recorded."""

    words = "You're all set for Friday at 9:00 AM. Anything else I can help with?"
    orders = (('barge', 'report', 'stop'), ('stop', 'barge', 'report'), ('barge', 'stop', 'report'))
    for order in orders:
        call = booking_call()
        call._say = line
        call.turn_started('Yes, please.')
        await close_with(call, words)
        call.turn_ended(interrupted=False)
        call.note_reached()
        for signal in order:
            if signal == 'barge':
                call.note_barge()
            elif signal == 'report':
                engine.note_spoken(call, words, interrupted=True)
            else:
                call.note_drained()
        call.turn_started("That's perfect.")
        assert call.pending_question() is None, (order, 'answered a question the caller never heard')
        assert call.heard == "Yes, please. That's perfect.", (order, call.heard)


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
    await a_holding_line_heard_in_full_keeps_the_turn_open()
    await an_attempted_write_is_never_authorised_twice()
    await a_read_back_cut_off_is_not_agreed_to()
    await a_missing_barge_never_merges_turns()
    await the_next_reply_is_told_what_was_not_heard()
    await a_number_talked_over_arrives_whole()
    await a_number_in_pieces_is_read_back_without_the_model()
    await a_number_with_a_digit_too_many_is_refused_with_its_reason()
    await a_booking_taken_back_is_offered_for_cancelling(asks_price=False)
    await a_move_after_a_booking_is_read_back_not_cancelled()
    await a_yes_with_a_new_request_is_verified_and_the_rest_answered()
    await a_booking_taken_back_is_offered_for_cancelling(asks_price=True)
    await a_goodbye_after_a_booking_reads_only_what_can_end_or_undo_it()
    await a_pick_said_with_a_goodbye_is_asked_before_they_go()
    await a_time_named_with_a_condition_is_not_read_back()
    await a_named_pick_is_refused_without_the_word_list()
    await only_a_slot_on_the_table_can_be_picked()
    await a_pick_with_no_question_pending_is_still_read_back()
    await an_add_on_to_a_pick_is_read_with_it()
    a_detour_keeps_the_rest_of_its_first_turn()
    await a_yes_said_as_the_line_drops_still_counts()
    a_read_back_said_again_in_full_can_be_answered()
    await a_question_cut_off_as_the_audio_stops_is_not_heard()
    await a_cut_off_question_reported_in_full_is_still_cut_off()
    heard_is_matched_by_words()
    await playback_reaches_the_call()
    print('continuation ok')


if __name__ == '__main__':
    asyncio.run(main())
