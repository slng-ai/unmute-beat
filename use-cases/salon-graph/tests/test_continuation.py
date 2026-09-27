"""Run: uv run python -m tests.test_continuation

One caller turn lasts until the agent's answer reaches the caller.

The failure this is written against, from the Coval release run of
2026-09-23: the runtime closes the caller's turn on every pause, so "Yes,
please." and "That sounds perfect." arrived as two turns, and the reply that
finished the sentence answered nothing. Core joins the pieces
(`CallContext.continues_turn`); these checks drive that rule through the
node-agent salon and the real Pipecat processor, with scripted node agents.
Session 4 moved the reading of each reply from classifiers into the node's
own agent: what these checks hold is that the timing rules did not move.

Scripted models, no audio, no network. Asserts, no pytest, same as the rest.
"""

import asyncio
import json
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime

from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import BotStartedSpeakingFrame, StartFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup
from pipecat.utils.asyncio.task_manager import TaskManager
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from core.history import project
from core.policies import TurnBudget
from core.processor import PydanticAgentProcessor
from core.state import RECEIPT_REPEATS
from core.steps import Prompted, _args
from core.turn import Turn
from salon import diary
from salon.app import FLOW
from salon.diary import Diary
from tests.script import open_call

PHONE = '+34600111222'
CLOCK = datetime(2026, 9, 21, 10, tzinfo=diary.TZ)
books = Diary()
"""This module's salon, emptied by each case."""
BOOK = FLOW.by_id['booking'].operations[0]
"""The booking step's `book_appointment`, as core compiled it from the manifest."""


class Probe(PydanticAgentProcessor):
    """The real processor with its frames collected instead of pushed down a pipeline.

    The caller starting to speak is what the voice detector would report
    first: a turn (`_turn`) or a barge-in (`interrupt`) notes an onset, as
    `VADUserStartedSpeakingFrame` does in a pipeline.
    """

    def __init__(self, call):
        super().__init__(call, FLOW.nodes)
        self.frames = []

    async def push_frame(self, frame, direction=None):
        self.frames.append(frame)

    async def _turn(self, prompt):
        self._call.note_onset(time.perf_counter())
        await super()._turn(prompt)

    def interrupt(self):
        self._call.note_onset(time.perf_counter())
        super().interrupt()

    def fixed(self) -> list[str]:
        return [f.text for f in self.frames if isinstance(f, TTSSpeakFrame)]


def played_out(call) -> None:
    """The audio ran out after playing in full: every pending mark came back, then the runtime's stop signal.

    The marks are what deliver (`Spoken.delivered`); the stop signal decides
    turn-taking only. A test has no audio, so start and stop arrive in the
    same instant, which is what audio cut off by the caller looks like
    (`CallContext._stopped_early`). The start is moved back instead.
    """
    for name in list(call._pending_marks):
        call.note_mark_returned(name, time.perf_counter())
    if call._burst != float('inf'):
        call._burst -= 60
    call.note_drained()


def said(call, words: str) -> None:
    """The caller spoke: the voice detector's onset first, as a pipeline reports it, then their words."""
    call.note_onset(time.perf_counter())
    call.turn_started(words)


def booking_call():
    """An identified caller in the booking step."""
    books.reset()
    books.set_clock(CLOCK)
    call = open_call(PHONE, diary=books, verified_phone=PHONE, customer_status='existing')
    call.commit(agent='booking')
    return call


def offer_friday_at_nine(call) -> None:
    """The diary offers Friday's times to this call, and the caller's pick of 9:00 is recorded."""
    slots = books.find_slots(PHONE, 'Friday', 'haircut', books.now(), offered_to=call.services.call)['slots']
    call._offered = {'slot': {s['slot_id']: s['spoken'] for s in slots}}
    call.collect(service='haircut', booking_day='Friday', booking_time='09:00')
    call._offered = {'slot': {s['slot_id']: s['spoken'] for s in slots}}
    call.collect(slot=next(s['slot_id'] for s in slots if s['time'] == '09:00'))


async def read_back_a_haircut(call, probe, *, played: bool = True) -> str:
    """Turn one: the booking step reads a Friday haircut back, in the application's words, and its audio plays."""
    said(call, 'A haircut on Friday at nine, please.')
    offer_friday_at_nine(call)
    proposal = BOOK.proposal_for(call, _args(BOOK, call))
    question = Prompted(about=proposal.about, spoken=proposal.spoken, value=proposal.value)
    await call.speak_now(question.put(call), origin='ask')
    if played:
        call.note_reached()
        played_out(call)
        call.turn_ended(interrupted=False)
    return question.spoken


@contextmanager
def steps(**by_step):
    """Every step's model, scripted by the caller's words so far: `{step: fn(heard) -> move}`. Default: speak.

    On a rendered step `say` is its `wait` choice, as in `tests.script`.
    """
    stack = ExitStack()
    for name, node in FLOW.nodes.items():
        choose = by_step.get(name)
        rendered = getattr(FLOW.by_id.get(name), 'rendered', False)

        async def stream(messages, info, choose=choose, rendered=rendered):
            move = await choose(call_of[0]) if choose else ('say', 'Anything else?')
            if move[0] == 'say' and rendered:
                yield {0: DeltaToolCall(name='wait', json_args='{}')}
            elif move[0] == 'say':
                yield move[1]
            else:
                yield {0: DeltaToolCall(name=move[1], json_args=json.dumps(move[2] if len(move) > 2 else {}))}

        stack.enter_context(node.agent.override(model=FunctionModel(stream_function=stream)))
    with stack:
        yield


call_of: list = [None]
"""The call the scripted steps are reading, set by each case."""


async def a_reply_split_by_a_pause_is_one_answer():
    """The Coval failure itself: the second half of a yes still answers the read-back, and nothing is asked twice.

    The booking agent is slow on the first piece ("Yes, please."), the caller
    goes on, and the joined reply is what the agent answers: one booking, the
    read-back said once.
    """
    call = booking_call()
    call_of[0] = call
    probe = Probe(call)
    readback = await read_back_a_haircut(call, probe)
    joined = 'Yes, please. That sounds perfect.'
    slow = asyncio.Event()

    async def booking(call):
        if call.heard != joined:
            slow.set()
            await asyncio.Event().wait()
        return ('call', 'book_appointment')

    with steps(booking=booking):
        probe.interrupt()
        first = asyncio.create_task(probe._turn('Yes, please.'))
        await asyncio.wait_for(slow.wait(), 3)
        probe.interrupt()
        await asyncio.wait_for(first, 3)
        await probe._turn('That sounds perfect.')
    assert call.turn == 2 and call.heard == joined, (call.turn, call.heard)
    assert len(books.bookings_for(PHONE, CLOCK.date())) == 1, 'the finished yes did not book'
    assert probe.fixed().count(readback) == 1, probe.fixed()
    said = [p.content for m in call.messages for p in m.parts if isinstance(p, UserPromptPart)]
    assert said[-2:] == ['Yes, please.', 'That sounds perfect.'] or said[-1] == joined, said


async def a_reply_after_the_answer_played_is_a_new_turn():
    """The other side of the line: once the caller has heard the agent, what they say next is a new turn."""
    call = booking_call()
    call_of[0] = call
    probe = Probe(call)
    await read_back_a_haircut(call, probe)
    with steps():
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
    await read_back_a_haircut(call, Probe(call))
    said(call, 'Yes, please.')
    await call.speak_now('Let me check that.', origin='entry')
    unheard = call.spoken[-1]
    call.note_barge()
    said(call, 'That sounds perfect.')
    assert call.turn == 2 and unheard.text == '' and unheard.generated == 'Let me check that.'
    assert 'Let me check that.' not in str(project(call, call.messages))


async def a_holding_line_heard_in_full_keeps_the_turn_open():
    """A holding line answers nothing: a caller who goes on after hearing all of it is still in the same turn."""
    call = booking_call()
    await read_back_a_haircut(call, Probe(call))
    said(call, 'Yes, please.')
    await call.say('Let me have a look.', origin='tool_filler')
    call.note_reached()
    played_out(call)
    call.note_barge()
    said(call, 'That sounds perfect.')
    assert call.turn == 2 and call.heard == 'Yes, please. That sounds perfect.', (call.turn, call.heard)


async def one_holding_line_per_caller_turn():
    """A turn continued over several pieces says one holding line, not one per piece.

    Each piece gets a new `Turn`, which opens a new gap (`_covered`), and an
    empty per-piece ledger, so a check on that ledger alone said "Let me have
    a look" again on every piece: 4 caller turns in Coval run StET3kNN heard
    it twice. The next caller turn gets its own.
    """
    call = booking_call()
    Probe(call)

    async def piece(words: str) -> None:
        """What `Turn.events` does before a piece's work: a new gap, the words, then work that waits."""
        call._covered = False
        said(call, words)
        await Turn(call, FLOW.nodes, TurnBudget())._hold()

    def fillers() -> list[str]:
        return [s.id for s in call.all_spoken if s.origin == 'tool_filler' and s.text]

    await piece('I would like')
    call.note_reached()
    played_out(call)
    call.note_barge()
    await piece('a haircut on Friday.')
    assert call.turn == 1, 'the second piece continues the turn'
    assert len(fillers()) == 1, [(s.origin, s.text) for s in call.all_spoken]

    await call.speak_now('Which time would you like?', origin='ask')
    call.note_reached()
    played_out(call)
    call.turn_ended(interrupted=False)
    await piece('Nine.')
    assert call.turn == 2 and len(fillers()) == 2, 'a new turn, a new line'


async def a_holding_line_is_never_sent_to_the_model():
    """The model is never shown a holding line as its own words; a read-back the caller heard it is.

    Shown "One moment." as its own, the model opened 32 replies with it in
    the session-4 baseline, and the caller heard it twice.
    """
    call = booking_call()
    question = await read_back_a_haircut(call, Probe(call))
    call._covered = False
    said(call, 'Yes, please.')
    await call.say('Let me have a look.', origin='tool_filler')
    sent = str(project(call, call.messages))
    assert 'Let me have a look.' not in sent and question in sent, sent


async def an_attempted_write_is_never_authorised_twice():
    """If the first piece already acted on the answer, the continuation cannot spend the same answer again."""
    call = booking_call()
    await read_back_a_haircut(call, Probe(call))
    said(call, 'Yes, please.')
    assert call.pending('book_appointment') is not None
    call.answered()
    call.note_business_attempt()
    call.note_barge()
    said(call, 'And thanks.')
    assert call.turn == 2 and call.pending('book_appointment') is None, 'a spent answer came back'

    call = booking_call()
    await read_back_a_haircut(call, Probe(call))
    said(call, 'Yes, please.')
    call.answered()
    call.note_barge()
    said(call, 'That sounds perfect.')
    assert call.pending('book_appointment') is not None, 'a half-sentence consumed the question for good'


async def a_read_back_cut_off_is_not_agreed_to():
    """The K3 failure: a read-back the caller spoke over is not a question they can say yes to.

    Whether or not its audio had started, audio that never ran to its end
    was not heard in full, and the node agent's yes to it binds to nothing.
    """
    for started in (False, True):
        call = booking_call()
        call_of[0] = call
        probe = Probe(call)
        readback = await read_back_a_haircut(call, probe, played=False)
        if started:
            call.note_reached()
        probe.interrupt()

        async def booking(call):
            return ('call', 'book_appointment')

        with steps(booking=booking):
            await probe._turn('Yes, book it.')
        assert books.bookings_for(PHONE, CLOCK.date()) == [], (
            f'agreed to a cut-off read-back (started: {started})'
        )
        first = next(s for s in call.all_spoken if s.generated == readback)
        assert first.interrupted and (first.text == '') is not started, first


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

    with FLOW.nodes['concierge'].agent.override(model=FunctionModel(stream_function=concierge)):
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


async def an_unheard_receipt_is_said_again_and_not_booked_again():
    """Session 3, A3, through the processor: the receipt's audio never started, the caller speaks into the silence.

    Its words are said again by code, with no model and no write; the next
    reply after a heard one is the node agent's as usual.
    """
    call = booking_call()
    call_of[0] = call
    probe = Probe(call)
    await read_back_a_haircut(call, probe)
    requests: list[str] = []

    async def booking(call):
        requests.append(call.heard)
        return ('call', 'book_appointment') if call.heard == 'Yes.' else ('say', 'Of course.')

    with steps(booking=booking):
        probe.interrupt()
        await probe._turn('Yes.')
        receipt = next(s for s in call.spoken if s.generated.startswith("You're all set"))
        call._playback_known = True
        call.turn_ended(interrupted=False)
        assert receipt.playback == 'queued', receipt
        probe.interrupt()
        await probe._turn('Hello? Is it booked?')
        assert probe.fixed()[-1] == receipt.generated and requests == ['Yes.'], (probe.fixed(), requests)
        assert len(books.bookings_for(PHONE, CLOCK.date())) == 1
        call.note_reached()
        played_out(call)
        probe.interrupt()
        await probe._turn('Great, thanks.')
    assert requests == ['Yes.', 'Great, thanks.'], requests
    assert len(books.bookings_for(PHONE, CLOCK.date())) == 1, 'the booking was made again'


async def an_unheard_receipt_is_said_again_once_then_the_caller_is_heard():
    """Session 4d, live call 6: the move's receipt was said again every time the caller talked over it (six times).

    It is said again once, by code. When the caller talks over that copy
    too, their words go to the node's agent, which records their correction.
    The receipt is still unheard (not marked delivered), and nothing is
    booked twice. (A receipt a later change superseded is never said again:
    core `test_tickets` R9.)
    """
    call = booking_call()
    call_of[0] = call
    probe = Probe(call)
    await read_back_a_haircut(call, probe)
    requests: list[str] = []

    async def booking(call):
        requests.append(call.heard)
        if call.heard == 'Yes.':
            return ('call', 'book_appointment')
        if 'ten' in call.heard and call.state.facts.booking_time != '10:00':
            return ('call', 'collect', {'values': {'booking_time': '10:00'}})
        return ('say', 'Of course.')

    with steps(booking=booking):
        probe.interrupt()
        await probe._turn('Yes.')
        receipt = next(s for s in call.spoken if s.generated.startswith("You're all set"))
        call._playback_known = True
        call.turn_ended(interrupted=False)
        probe.interrupt()
        await probe._turn('Hello? Is it booked?')
        assert probe.fixed()[-1] == receipt.generated and requests == ['Yes.'], (probe.fixed(), requests)
        call.turn_ended(interrupted=False)
        before = len(probe.fixed())
        probe.interrupt()
        await probe._turn('Sorry, can we make it at ten?')
        assert len(requests) > 1 and requests[-1].endswith('Sorry, can we make it at ten?'), (
            'the caller was not heard',
            requests,
        )
        assert receipt.generated not in probe.fixed()[before:], 'the receipt was said a third time unasked'
        assert call.state.facts.booking_time == '10:00', 'the correction said over the receipt was lost'
        assert call.unheard_receipt(RECEIPT_REPEATS + 1) == receipt.generated, (
            'running out of repeats marked the receipt heard'
        )
    assert len(books.bookings_for(PHONE, CLOCK.date())) == 1, 'the booking was made again'


async def a_yes_with_a_new_request_is_verified_and_the_rest_kept():
    """Coval run hdbogMqB, AJ4t (P5, phone read-back): "Yes, that number is right; can we make it Saturday instead?"

    Two meanings, at most one action: the verify agent records the new day
    and accepts the unchanged number in the same reply. The number's yes
    stands, because nothing it rests on changed; the day is kept for
    booking, which the call returns to by code.
    """
    books.reset()
    books.set_clock(CLOCK)
    call = open_call(PHONE, diary=books, service='haircut', booking_day='Friday')
    call.commit(agent='verify', pending='booking')
    call_of[0] = call
    probe = Probe(call)
    said(call, 'A haircut on Friday.')
    lookup = FLOW.by_id['verify'].operations[0]
    proposal = lookup.proposal_for(call, _args(lookup, call))
    await call.speak_now(Prompted(proposal.about, proposal.spoken, proposal.value).put(call), origin='ask')
    call.note_reached()
    played_out(call)
    call.turn_ended(interrupted=False)
    replies = iter(
        [
            ('calls', [('collect', {'values': {'booking_day': 'Saturday'}}), ('lookup_customer', {})]),
        ]
    )

    async def verify_stream(messages, info):
        _kind, calls = next(replies)
        yield {i: DeltaToolCall(name=n, json_args=json.dumps(a)) for i, (n, a) in enumerate(calls)}

    async def booking_stream(messages, info):
        yield 'On Saturday, which time suits you?'

    with (
        FLOW.nodes['verify'].agent.override(model=FunctionModel(stream_function=verify_stream)),
        FLOW.nodes['booking'].agent.override(model=FunctionModel(stream_function=booking_stream)),
    ):
        probe.interrupt()
        await probe._turn('Yes, that number is right; can we make it Saturday instead?')
    facts = call.state.facts
    assert facts.verified_phone == PHONE and facts.customer_phone == PHONE, (
        'the yes did not verify the unchanged number'
    )
    assert facts.booking_day == '2026-09-26', ('the new day was lost', facts)
    assert call.state.agent == 'booking', call.state.agent


async def a_read_back_queued_behind_a_holding_line_is_not_heard_when_it_ends():
    """Session 4, Coval run GYZdoiie (simulation fymMyWTi): the number was looked up on a read-back nobody heard.

    The verify agent's `collect` armed the holding line; the read-back was
    queued while "Let me have a look." played; that line's audio ended half a
    second later and the read-back was counted as heard with it. The caller,
    who heard almost none of it, cut in and restated the digits, and the
    agent's yes to that looked the number up. A segment that joins playing
    audio is heard only once its own words had time to play.
    """
    books.reset()
    books.set_clock(CLOCK)
    call = open_call('+34671273454', diary=books)
    call.commit(agent='verify', pending='booking')
    call_of[0] = call
    probe = Probe(call)

    async def line(text):
        """The call's line to the caller: without one, a covering line is not recorded."""

    call._say = line
    said(call, 'Two seven three, four five four.')
    await call.say('Let me have a look.', origin='tool_filler')
    call.note_reached()
    lookup = FLOW.by_id['verify'].operations[0]
    proposal = lookup.proposal_for(call, _args(lookup, call))
    await call.speak_now(Prompted(proposal.about, proposal.spoken, proposal.value).put(call), origin='ask')
    call.note_drained()
    readback = next(s for s in call.spoken if s.generated == proposal.spoken)
    assert readback.playback == 'queued', ('counted as heard with the line it was queued behind', readback)
    call.note_barge()
    call.turn_ended(interrupted=True)

    async def verify(call):
        return ('call', 'lookup_customer')

    with steps(verify=verify):
        probe.interrupt()
        await probe._turn("That's two seven three, four five four.")
    assert call.state.facts.verified_phone == '', 'looked up on a read-back the caller never heard'
    assert any(s.generated == proposal.spoken for s in call.spoken), (
        'the number is read back again',
        call.spoken,
    )


def a_report_never_unhears_a_line_that_played():
    """Trace fbdd81b6: a question that played out was taken as unheard, and the answer to it joined the turn before.

    Two ways, both closed: the runtime's report of an assistant turn it still
    held open cut the line after its audio had run out, and a line whose
    start signal went missing stayed queued however long ago it was sent.
    """
    question = 'Would you like to move the appointment you already have, or add another one?'
    for missing_signal in (False, True):
        call = booking_call()
        call._playback_known = True
        said(call, 'Uh, can we do 10:30?')
        call.note_said('fixed', question, origin='guidance')
        if missing_signal:
            call.spoken[-1].at -= 60
        else:
            call.note_reached()
            played_out(call)
            call.note_heard('', segment_ids=(call.spoken[-1].id,), interrupted=True)
        call.note_barge()
        said(call, 'Uh, add another one.')
        assert call.turn == 2, (
            f'a question heard in full was taken as unheard (missing signal: {missing_signal})'
        )

    call = booking_call()
    call._playback_known = True
    said(call, 'Uh, can we do 10:30?')
    call.note_said('fixed', question, origin='guidance')
    call.note_barge()
    said(call, 'Uh, add another one.')
    assert call.turn == 1, 'a line cut off before it could play still continues the turn'


async def a_missing_barge_never_merges_turns():
    """Without the caller cutting in, a silent turn is still over: a missing signal errs towards a new turn."""
    call = booking_call()
    said(call, 'One.')
    call.turn_ended(interrupted=False)
    said(call, 'Two.')
    assert call.turn == 2 and call.heard == 'Two.'


def a_read_back_said_again_in_full_can_be_answered():
    """Coval run 4eDqRVLR, K3: the caller talked over the number read-back, and it was said again, whole.

    The copy the caller heard last is the one they are answering.
    """
    call = open_call('', diary=books)
    call.commit(agent='verify')
    said(call, "It's plus three four, six seven one, two seven three, four five four.")
    readback = 'Is your number +34 671 27 34 54?'
    Prompted(about='lookup_customer', spoken=readback, value='+34671273454').put(call)
    call.note_said('fixed', readback, origin='ask')
    call.note_heard('Let me read that', segment_ids=(call.spoken[-1].id,), interrupted=True)
    Prompted(about='lookup_customer', spoken=readback, value='+34671273454').put(call)
    call.note_said('fixed', readback, origin='ask')
    call.acknowledge(call.spoken[-1].id)
    said(call, "Yes. That's right.")
    assert call.pending('lookup_customer') is not None, (
        'the read-back heard in full is waiting on this answer'
    )


async def a_question_cut_off_as_the_audio_stops_is_not_heard():
    """Coval run Z56CoZax, K5: the audio's stop reached the call before the caller's cut-in did.

    Either order is one event: the audio stops because the caller cut in, so
    the receipt's question was not heard and the caller's words join the turn.
    """

    async def line(text):
        """The call's line to the caller: without one, nothing said is recorded, and nothing here is tested."""

    for stop_first in (True, False):
        call = booking_call()
        call._say = line
        said(call, 'Yes, please.')
        Prompted(about='closing', spoken="You're all set. Anything else I can help with?").put(call)
        await call.speak_now("You're all set. Anything else I can help with?")
        call.note_reached()
        if stop_first:
            call.note_drained()
            call.note_barge()
        else:
            call.note_barge()
            call.note_drained()
        call.turn_ended(interrupted=True)
        said(call, "That's perfect.")
        assert call.pending_question() is None, (stop_first, 'answered a question the caller never heard')
        assert call.heard == "Yes, please. That's perfect.", (stop_first, call.heard)


def heard_is_matched_by_words():
    """What the runtime says went out is matched word by word, and an interruption always cuts."""
    call = booking_call()
    said(call, 'Do you do balayage?')
    call.note_model_text('We do colour work, but balayage is not on our menu. Hair colour is 75 euros.')
    segment = call.spoken[-1]
    assert call.note_heard('we do colour work but', segment_ids=(segment.id,), interrupted=True)
    assert segment.text == 'We do colour work, but' and segment.interrupted, segment.text

    said(call, 'Next.')
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
        said(call, 'Hello.')
        assert not call._reached
        await probe.queue_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        for _ in range(100):
            if call._reached:
                break
            await asyncio.sleep(0.01)
        assert call._reached and call._playback_known
    finally:
        await probe.cleanup()


async def main() -> None:
    await a_reply_split_by_a_pause_is_one_answer()
    await a_reply_after_the_answer_played_is_a_new_turn()
    await speech_nobody_heard_is_recorded_as_unheard()
    await a_holding_line_heard_in_full_keeps_the_turn_open()
    await one_holding_line_per_caller_turn()
    await a_holding_line_is_never_sent_to_the_model()
    await an_attempted_write_is_never_authorised_twice()
    await a_read_back_cut_off_is_not_agreed_to()
    await the_next_reply_is_told_what_was_not_heard()
    await an_unheard_receipt_is_said_again_and_not_booked_again()
    await an_unheard_receipt_is_said_again_once_then_the_caller_is_heard()
    await a_yes_with_a_new_request_is_verified_and_the_rest_kept()
    await a_read_back_queued_behind_a_holding_line_is_not_heard_when_it_ends()
    a_report_never_unhears_a_line_that_played()
    await a_missing_barge_never_merges_turns()
    a_read_back_said_again_in_full_can_be_answered()
    await a_question_cut_off_as_the_audio_stops_is_not_heard()
    heard_is_matched_by_words()
    await playback_reaches_the_call()
    print(
        'continuation ok: joined pieces, unheard speech, spent answers, cut read-backs, unheard receipt, mixed yes'
    )


if __name__ == '__main__':
    asyncio.run(main())
