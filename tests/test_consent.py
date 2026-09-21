"""Run: uv run python -m tests.test_consent

The five conversation failures of 2026-09-21, replayed offline against a
scripted model. Four of them are the live calls N1-N4 in
`TRACE_FOLLOWUP_2026-09-21.md`; the fifth is the false cancellation of the
earlier call at 12:23.

They are one module because they are one finding. Every consent fact this
agency acted on used to arrive as a model-authored tool argument --
`confirmed=True`, `additional=True`, the number handed to a lookup, the
goodbye string, the day handed to the diary -- and the application never saw
what the caller had actually said. Each test below is the shortest script that
makes the model assert one of those things falsely.

Asserts, no pytest, no network.
"""

import asyncio
import json
from contextlib import ExitStack
from datetime import date, datetime

from pipecat.frames.frames import EndWorkerFrame, LLMTextFrame, TTSSpeakFrame
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from salon import agents, diary
from salon.agents import AGENTS
from salon.state import agrees, ends, move_or_add, open_call, times_named
from voice.processor import PydanticAgentProcessor

# --- the scripted model ------------------------------------------------------
#
# The same shape as `tests.test_salon`'s, written out again rather than
# imported: that module runs its own suite at import, so importing it here
# would run the salon tests twice and this module's failures would be reported
# under its name.


def says(text, chunk=8):
    async def stream(messages, info: AgentInfo):
        for start in range(0, len(text), chunk):
            yield text[start : start + chunk]

    return stream


def outputs(name, **args):
    async def stream(messages, info: AgentInfo):
        matches = [t.name for t in info.output_tools if name.casefold() in t.name.casefold()]
        assert matches, f'{name} is not an output tool here: {[t.name for t in info.output_tools]}'
        yield {0: DeltaToolCall(name=matches[0], json_args=json.dumps(args))}

    return stream


def then(*steps):
    """A different scripted response for each request this agent receives."""
    calls = {'n': 0}

    async def stream(messages, info: AgentInfo):
        step = steps[min(calls['n'], len(steps) - 1)]
        calls['n'] += 1
        async for chunk in step(messages, info):
            yield chunk

    return stream


def scripted(**by_agent):
    stack = ExitStack()
    for name, fn in by_agent.items():
        stack.enter_context(AGENTS[name].agent.override(model=FunctionModel(stream_function=fn)))
    return stack


class Probe(PydanticAgentProcessor):
    def __init__(self, call, **kwargs):
        super().__init__(call, AGENTS, **kwargs)
        self.frames = []

    async def push_frame(self, frame, direction=None):
        self.frames.append(frame)

    def spoken(self):
        return ''.join(f.text for f in self.frames if isinstance(f, LLMTextFrame))

    def fixed(self):
        return [f.text for f in self.frames if isinstance(f, TTSSpeakFrame)]


def _heard(call) -> list[str]:
    """Attach a collector to the call's line and hand back what reaches it."""
    said: list[str] = []

    async def collect(text: str) -> None:
        said.append(text)

    call._say = collect
    return said


def _context(call):
    return RunContext(deps=call, model=None, usage=None)  # type: ignore[arg-type]


async def _refuses(fn, *args) -> str:
    """Run a tool that must hand the model back a retry, and return what it said."""
    try:
        await fn(*args)
    except ModelRetry as retry:
        return str(retry)
    raise AssertionError(f'{getattr(fn, "__name__", fn)} accepted what it must refuse')


PHONE = '+34680840464'
# Monday, the clock every one of those calls ran under. "Friday" is the 25th
# and "next Friday" is 2 October, which is the whole of N2.
CLOCK = datetime(2026, 9, 21, 10, 0, tzinfo=diary.TZ)
FRIDAY = date(2026, 9, 25)
NEXT_FRIDAY = date(2026, 10, 2)
SATURDAY = date(2026, 9, 26)

assert diary.resolve_day('friday', CLOCK.date()) == FRIDAY
assert diary.resolve_day('next friday', CLOCK.date()) == NEXT_FRIDAY


def _salon(verified: bool = True):
    """A fresh salon on the pinned clock, and a call already past verification."""
    diary.reset()
    diary.set_clock(CLOCK)
    call = open_call(PHONE)
    if verified:
        call.commit(customer_verified='existing', verified_phone=PHONE, agent='booking')
    return call


def _slot(call, when: str, service: str = 'haircut', at: str = ''):
    """Offer this call the day's slots and hand back one of them."""
    found = diary.find_slots(PHONE, when, service, diary.now(), offered_to=call.id)
    slots = found['slots']
    assert slots, found
    if at:
        match = [s for s in slots if s['time'] == at]
        assert match, [s['time'] for s in slots]
        return match[0]
    return slots[0]


# --- the words themselves ----------------------------------------------------


def the_matchers_refuse_rather_than_guess():
    """Every one of them fails towards asking. That is the property, not the word lists."""
    for yes in ('yes', "yeah, that's right", 'Sounds good', 'perfect', 'go ahead'):
        assert agrees(yes), yes
    for other in (
        '',
        'no',
        'Yes, but can you make it 3pm instead',
        'Actually, Friday',
        "I'd rather have the afternoon",
    ):
        assert not agrees(other), other

    # N4's yes is a perfectly good yes. That is the point: the wording is not
    # what was wrong with it. It answered "are you calling from Spain?", and
    # nothing compared it against the question that had actually been asked,
    # so it became agreement to a phone number instead. What stops it is the
    # pending question, not the word list -- see `n4_a_yes_about_spain_...`.
    assert agrees('Yes, from Spain')

    for bye in ("no, that's all thanks", "I'm good, thanks", 'goodbye', "that's everything, thank you"):
        assert ends(bye), bye
    for still in (
        # N1, word for word.
        'Can you help me moving it to Saturday, the same time?',
        "that's all -- oh, can you also book Friday?",
        'no',
        '',
    ):
        assert not ends(still), still

    assert move_or_add('move it to Saturday') == 'move'
    assert move_or_add("I'd like another one as well") == 'add'
    # Both, or neither, is a question and not a decision.
    assert move_or_add('change it and add another') == ''
    assert move_or_add('three o clock') == ''

    # Both readings of an unqualified hour, so the diary's own offers decide.
    assert times_named("three o'clock") == {'03:00', '15:00'}
    assert times_named('3 pm') == {'15:00'}
    assert times_named('half past two') == {'02:30', '14:30'}
    # Not a time. Intersected with real offers, a wrong reading books something.
    assert times_named('Friday the 25th') == set()
    assert times_named('for 2 people') == set()


# --- N1: a question answered by hanging up -----------------------------------


async def n1_a_move_question_cannot_hang_up():
    """Live call 12:57:53, `hang_up` observation `2c818c9ca1ddb7ce`.

    The caller asked: *"Can you help me moving it to Saturday, the same
    time?"* The agent read Saturday's availability, then called `hang_up` and
    said goodbye. No move was written; the Friday 09:00 booking is still in
    that call's outcome record. The turn was scored `answered`.

    Nothing in the tool's arguments was wrong. The goodbye was a string the
    model wrote, and a string the model wrote was the whole of the evidence
    that the call was over.
    """
    call = _salon()
    call.commit(agent='concierge')  # where `hang_up` lives
    probe = Probe(call)
    # The refusal leaves the step open with the reason in front of the model,
    # so its next request can do what the caller asked. That is the second
    # half of the fix: refusing to hang up is worth nothing if the caller is
    # left with an apology instead.
    answer = 'Of course, let me look at Saturday for you.'
    with scripted(concierge=then(outputs('hang_up', goodbye='Take care, bye!'), says(answer))):
        await probe._turn('Can you help me moving it to Saturday, the same time?')
    assert not [f for f in probe.frames if isinstance(f, EndWorkerFrame)], 'the line was closed'
    assert 'Take care, bye!' not in probe.fixed(), probe.fixed()
    assert call.ended == '', call.ended
    assert probe.spoken() == answer, probe.spoken()

    # The caller's own goodbye still works, and still works in one turn.
    probe = Probe(call)
    with scripted(concierge=outputs('hang_up', goodbye='Take care, bye!')):
        await probe._turn("no, that's all thanks")
    assert 'Take care, bye!' in probe.fixed(), probe.fixed()
    assert [f for f in probe.frames if isinstance(f, EndWorkerFrame)]
    assert call.ended == 'caller'


async def n1_the_move_it_asked_for_is_the_move_it_makes():
    """And the move that question was asking for goes through, in one turn.

    "The same time" names a time exactly without naming any digits. Refusing
    to hang up is worth nothing if the alternative is refusing to help.
    """
    call = _salon()
    _heard(call)
    call.turn_started('book me a haircut on Friday at nine')
    friday = _slot(call, 'friday', 'haircut', '09:00')
    await agents.save_booking(_context(call), 'book', friday['slot_id'], '')
    held = diary.bookings_for(PHONE, CLOCK.date())
    assert len(held) == 1 and held[0]['date'] == FRIDAY.isoformat(), held

    call.turn_started('Can you help me moving it to Saturday, the same time?')
    saturday = _slot(call, 'saturday', 'haircut', '09:00')
    moved = await agents.save_booking(_context(call), 'move', saturday['slot_id'], held[0]['booking_id'])
    assert 'moved' in moved, moved
    after = diary.bookings_for(PHONE, CLOCK.date())
    assert len(after) == 1, after
    assert after[0]['date'] == SATURDAY.isoformat() and after[0]['time'] == '09:00', after[0]


# --- N2: the day the caller did not say --------------------------------------


async def n2_friday_is_not_next_friday():
    """Live call 12:58:57, lookup `340b3c24843835dd`, write `5a9ecb2aef3c48fd`.

    The caller asked for a haircut **on Friday**. The model called
    `read_diary(when='next Friday')`. Under the Monday clock those are a week
    apart: Friday is 25 September and next Friday is 2 October. The diary
    answered the question it was asked, correctly, and the booking landed on
    2 October. The caller later moved it to Saturday, so the *final* state
    looks fine -- which is why this asserts the day of every accepted write
    and not the state at the end.
    """
    call = _salon()
    _heard(call)
    call.turn_started('I would like a haircut on Friday please')
    refusal = await _refuses(agents.read_diary, _context(call), 'next Friday', 'haircut')
    assert 'friday' in refusal.lower() and 'next friday' in refusal.lower(), refusal
    assert diary.bookings_for(PHONE, CLOCK.date()) == [], 'nothing may be written'

    # Their own words go through, and resolve to the day they named.
    found = await agents.read_diary(_context(call), 'Friday', 'haircut')
    assert found.date == FRIDAY.isoformat(), found.date

    # And the modifier is not forbidden -- it is only forbidden when the
    # caller did not say it. A caller who says "next Friday" gets next Friday.
    later = _salon()
    _heard(later)
    later.turn_started('can I come in next Friday?')
    found = await agents.read_diary(_context(later), 'next Friday', 'haircut')
    assert found.date == NEXT_FRIDAY.isoformat(), found.date


async def n2_the_day_survives_the_verification_detour():
    """The caller says the day once, before anybody knows who they are.

    In the live call the day was collected by the front desk and the lookup
    happened two agents later. Whatever the caller said has to still be there.
    """
    call = _salon(verified=False)
    _heard(call)
    call.turn_started('Hi, I would like a haircut on Friday')
    assert call.day_words == ('friday',), call.day_words
    # A verification turn in between, naming no day at all.
    call.turn_started('yes, that is my number')
    assert call.day_words == ('friday',), call.day_words
    call.commit(customer_verified='existing', verified_phone=PHONE, agent='booking')
    assert 'friday' in (await _refuses(agents.read_diary, _context(call), 'next Friday', 'haircut')).lower()


# --- N3 and N4: a flag that granted itself permission ------------------------


async def n3_a_flipped_flag_is_not_consent():
    """Live calls 13:00:19 and 13:02:11, one second between the two attempts.

    The diary returns the appointments the caller already holds; the booking
    tool is sent `additional=false` and refuses; on the very next model
    request, with no caller turn in between, the same call arrives with
    `additional=true` and succeeds. Both callers ended up holding two
    appointments having asked for one.

    The flag is gone from the signature. What decides it is the caller's
    answer to the question the refusal asks.
    """
    call = _salon()
    _heard(call)
    call.turn_started('a haircut on Friday at nine please')
    first = _slot(call, 'friday', 'haircut', '09:00')
    await agents.save_booking(_context(call), 'book', first['slot_id'], '')
    assert call.state.writes == 1

    # A new turn: they pick another time. The diary already holds one for them.
    call.turn_started('and three in the afternoon')
    second = _slot(call, 'friday', 'haircut', '15:00')
    refusal = await _refuses(agents.save_booking, _context(call), 'book', second['slot_id'], '')
    assert 'move that one or add another' in refusal, refusal

    # The retry, in the same turn, with nothing said in between. This is the
    # finding: there is no argument left to flip, and no second way in.
    again = await _refuses(agents.save_booking, _context(call), 'book', second['slot_id'], '')
    assert 'Nothing was saved' in again, again
    assert len(diary.bookings_for(PHONE, CLOCK.date())) == 1, 'still the one they asked for'

    # A different slot is not a different question, either.
    third = _slot(call, 'friday', 'haircut', '11:00')
    elsewhere = await _refuses(agents.save_booking, _context(call), 'book', third['slot_id'], '')
    assert 'move that one or add another' in elsewhere, elsewhere
    assert len(diary.bookings_for(PHONE, CLOCK.date())) == 1

    # Nor is an unrelated next utterance an answer to it. The caller has
    # spoken, so the old "has the caller had a chance to answer" rule would
    # let this through -- and what they said was about something else.
    call.turn_started('what do you charge for a blow dry?')
    unrelated = await _refuses(agents.save_booking, _context(call), 'book', second['slot_id'], '')
    assert 'Nothing was saved' in unrelated, unrelated
    assert len(diary.bookings_for(PHONE, CLOCK.date())) == 1


async def n3_an_answered_question_books_what_they_answered():
    """The other half. A caller who says "another one" gets another one."""
    call = _salon()
    _heard(call)
    call.turn_started('a haircut on Friday at nine please')
    first = _slot(call, 'friday', 'haircut', '09:00')
    await agents.save_booking(_context(call), 'book', first['slot_id'], '')

    call.turn_started('three in the afternoon as well')
    second = _slot(call, 'friday', 'haircut', '15:00')
    await _refuses(agents.save_booking, _context(call), 'book', second['slot_id'], '')

    call.turn_started('another one, on top of the nine o clock')
    saved = await agents.save_booking(_context(call), 'book', second['slot_id'], '')
    assert 'booked' in saved, saved
    assert len(diary.bookings_for(PHONE, CLOCK.date())) == 2

    # And a caller who says "move it" moves it, rather than adding a third.
    call.turn_started('actually, move the afternoon one to four o clock')
    held = diary.bookings_for(PHONE, CLOCK.date())
    afternoon = next(b for b in held if b['time'] == '15:00')
    later = _slot(call, 'friday', 'haircut', '16:00')
    await agents.save_booking(_context(call), 'move', later['slot_id'], afternoon['booking_id'])
    after = diary.bookings_for(PHONE, CLOCK.date())
    assert len(after) == 2, after
    assert sorted(b['time'] for b in after) == ['09:00', '16:00'], after


async def n4_a_yes_about_spain_verifies_nobody():
    """Live call 13:02:11, country question `ca274984fbfe565e`, lookup `07bd23e2d1a6ce43`.

    The caller gave a full +34 number. The agent asked *"Are you calling from
    Spain?"* -- which it did not need to, the country code was already there
    -- the caller said yes, and `confirm_customer` ran on a number that had
    never been read back in that call.

    The number was structurally fine. What the yes proves is that the caller
    is in Spain, and what `phone_confirmed` is supposed to mean is that they
    heard their own digits and agreed to them.
    """
    call = _salon(verified=False)
    _heard(call)
    call.turn_started('yes, from Spain')
    refusal = await _refuses(agents.confirm_customer, _context(call), PHONE)
    assert 'read back' in refusal, refusal
    assert call.state.customer_verified == '' and not call.state.phone_confirmed

    # The readback, then the yes. Two turns, and the second one is theirs.
    call.turn_started('my number is six eight zero, eight four zero, four six four')
    question = await agents.ask_number(_context(call), PHONE)
    assert '+34 680 840 464' in question.spoken, question.spoken
    question.put(call)
    call.turn_started("yes, that's right")
    handoff = await agents.confirm_customer(_context(call), PHONE)
    assert call.state.customer_verified and call.state.phone_confirmed
    assert call.state.verified_phone == PHONE and handoff.to == 'concierge'


async def n4_a_correction_is_not_an_agreement():
    """Different digits back are the caller correcting the readback, not taking it."""
    call = _salon(verified=False)
    _heard(call)
    call.turn_started('my number is six eight zero...')
    (await agents.ask_number(_context(call), PHONE)).put(call)
    call.turn_started('no, it ends four six five')
    refusal = await _refuses(agents.confirm_customer, _context(call), PHONE)
    assert 'did not agree' in refusal, refusal
    assert call.state.customer_verified == ''

    # And an agreement that arrives a turn late is not an answer either: the
    # caller has said something else since, and that is what is on the table.
    call = _salon(verified=False)
    _heard(call)
    call.turn_started('my number is six eight zero...')
    (await agents.ask_number(_context(call), PHONE)).put(call)
    call.turn_started('sorry, what were the opening hours again?')
    call.turn_started('yes')
    assert call.pending('phone', value=PHONE) is None
    assert 'read back' in await _refuses(agents.confirm_customer, _context(call), PHONE)


# --- the earlier call: a sentence about a write that never happened ----------


async def earlier_b_a_cancellation_nobody_made_is_not_spoken():
    """Live call 12:20:54, 12:23:22: *"I've cancelled your appointment, and I have
    you down for a redo on Saturday at 2:30 PM."*

    The last write in that call was a move, ninety seconds earlier. No
    `save_booking` ran in that turn or after it. The next turn promised the
    redo would be free.

    The existing guard corrects this *after* the words have gone to TTS, which
    is the best that can be done once they have. The check here is that they
    do not go: on a step that can write, a first-person claim about a write is
    held back until the clause is complete and then not sent at all.
    """
    call = _salon()
    _heard(call)
    call.turn_started('a haircut on Friday at nine please')
    friday = _slot(call, 'friday', 'haircut', '09:00')
    await agents.save_booking(_context(call), 'book', friday['slot_id'], '')

    # `save_booking` sent them back to the front desk, and the live call's
    # invention came from the step that can write. Put them back in it.
    call.commit(agent='booking')
    probe = Probe(call)
    invention = "I've cancelled your appointment, and I have you down for a redo on Saturday at 2:30 PM."
    with scripted(booking=says(invention)):
        await probe._turn('can you sort that out for me?')
    said = probe.spoken()
    assert 'cancelled' not in said.lower(), said
    assert 'redo' not in said.lower(), said
    # And the caller is told plainly, rather than being left with silence.
    assert any('has been changed' in line or 'correct myself' in line for line in probe.fixed()), (
        probe.fixed()
    )
    assert len(diary.bookings_for(PHONE, CLOCK.date())) == 1


async def recounting_what_is_really_booked_still_streams():
    """The false positive that matters. An honest answer is an ordinary answer."""
    call = _salon()
    _heard(call)
    call.turn_started('a haircut on Friday at nine please')
    friday = _slot(call, 'friday', 'haircut', '09:00')
    await agents.save_booking(_context(call), 'book', friday['slot_id'], '')

    call.commit(agent='booking')
    probe = Probe(call)
    honest = "You're booked in for a haircut on Friday at 9 AM. Anything else?"
    with scripted(booking=says(honest)):
        await probe._turn('what have I got booked?')
    assert probe.spoken() == honest, probe.spoken()


async def main():
    the_matchers_refuse_rather_than_guess()
    await n1_a_move_question_cannot_hang_up()
    await n1_the_move_it_asked_for_is_the_move_it_makes()
    await n2_friday_is_not_next_friday()
    await n2_the_day_survives_the_verification_detour()
    await n3_a_flipped_flag_is_not_consent()
    await n3_an_answered_question_books_what_they_answered()
    await n4_a_yes_about_spain_verifies_nobody()
    await n4_a_correction_is_not_an_agreement()
    await earlier_b_a_cancellation_nobody_made_is_not_spoken()
    await recounting_what_is_really_booked_still_streams()
    print('ok')


if __name__ == '__main__':
    # Guarded, unlike its siblings: `tests/__main__.py` runs each module with
    # `run_name='__main__'`, so this still runs there, and a debugging session
    # can import one of these replays without running all of them.
    asyncio.run(main())
