"""Run: uv run --env-file .env python -m tests.test_salon

Asserts, no pytest. The diary and the knowledge base are exercised directly; the
agents are driven through the processor with a scripted model, so nothing here
touches the network.

"next Friday" is the week after, when this Friday has not happened yet.

A weekday named on that weekday means the next one, not this morning.

The diary hands back what the caller holds, so the refusal is actionable.

The refund tiers belong to the agent that handles refunds, and nowhere else: the front desk quoting
a tier is the thing the separate step exists to avoid.

A real number that merely starts like a withheld spelling is kept.

The front desk never holds a number, whatever state it is in.

Only the verification step renders one.

`tests/__main__.py` runs each module with `run_name='__main__'`, so this still runs there -- and a
debugging session can import one module without running every check in it.
"""

import asyncio
import json
from contextlib import ExitStack
from datetime import date, datetime, timedelta

from pipecat.frames.frames import (
    EndWorkerFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pydantic import BaseModel
from pydantic_ai import ModelRetry, RunContext, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from core.policies import TurnBudget
from core.processor import EXHAUSTED, PydanticAgentProcessor
from salon import agents, diary
from salon.agents import AGENTS
from salon.diary import Diary
from salon.instructions import instructions
from salon.models.consent import Asked
from salon.models.state import Appointment, SalonCall, SalonState, open_call, prefetch_number
from salon.tools import complaints
from salon.tools.booking import booking_question, read_diary
from salon.tools.complaints import _record_complaint
from salon.tools.writes import MANAGER_CALLS, REPLY_SOON
from tests.classifier_model import classifier_model

books = Diary()
"""This module's salon, emptied by each case that writes to it."""

PHONE = '+34600111222'
OTHER = '+34934567890'
MONDAY = date(2026, 9, 21)
"""a Saturday morning"""
AT = datetime(2026, 9, 19, 10, 0, tzinfo=diary.TZ)


TODAY = AT.date()
assert diary.resolve_day('today', TODAY) == TODAY
assert diary.resolve_day('tomorrow', TODAY) == TODAY + timedelta(days=1)
assert diary.resolve_day('friday', TODAY) == date(2026, 9, 25)
assert diary.resolve_day('next friday', TODAY) == date(2026, 9, 25), 'on a Saturday, the Friday of next week'
assert diary.resolve_day('saturday', TODAY) == date(2026, 9, 26)
assert diary.resolve_day('2026-10-01', TODAY) == date(2026, 10, 1)
assert diary.resolve_day('whenever suits', TODAY) is None
assert diary.resolve_day('', TODAY) is None

assert diary.spoken(TODAY, __import__('datetime').time(15, 0), TODAY) == 'today at 3:00 PM'
assert diary.spoken(MONDAY, __import__('datetime').time(9, 0), TODAY) == 'Monday the 21st at 9:00 AM'


books.reset()
assert books.find_slots(PHONE, '', 'haircut', AT)['status'] == 'need_day'
assert books.find_slots(PHONE, 'sunday', 'haircut', AT)['status'] == 'closed'
assert books.find_slots(PHONE, 'yesterday', 'haircut', AT)['status'] in ('need_day', 'past')

monday = books.find_slots(PHONE, 'monday', 'haircut', AT)
assert monday['status'] == 'ok' and monday['day'] == 'Monday the 21st' and monday['price_euros'] == 28
assert monday['slots'][0]['spoken'] == 'Monday the 21st at 9:00 AM'

today_free = books.find_slots(PHONE, 'today', 'haircut', AT)
"""A time already gone past on the salon clock is not offered, however empty the diary looks: at 10:00
the 9:00 slot today is not a slot.
"""
assert all(slot['time'] > '10:00' for slot in today_free['slots']), today_free['slots'][:3]


monday = books.find_slots(PHONE, 'monday', 'haircut', AT)
"""Read again: asking about today withdrew the Monday offer, because a search is the question now on
the table and the last one is over.
"""
booked = books.save_booking(PHONE, 'book', monday['slots'][0]['slot_id'], '', False, AT)
assert booked['status'] == 'booked' and booked['spoken'] == 'Monday the 21st at 9:00 AM'
assert books.bookings_for(PHONE, TODAY)[0]['booking_id'] == booked['booking_id']
assert books.find_slots(PHONE, 'monday', 'haircut', AT)['bookings']

for why, call in {
    'a second booking without asking for one': lambda: books.save_booking(
        PHONE, 'book', monday['slots'][1]['slot_id'], '', False, AT
    ),
    'a slot the diary never returned': lambda: books.save_booking(
        PHONE, 'book', 'made-up-slot', '', True, AT
    ),
    'a booking reference nobody holds': lambda: books.save_booking(PHONE, 'cancel', '', 'BK9999', False, AT),
}.items():
    try:
        call()
    except diary.RefusedError as refused:
        assert str(refused), why
    else:
        raise AssertionError(f'the diary must refuse {why}')

moved = books.save_booking(PHONE, 'move', monday['slots'][3]['slot_id'], booked['booking_id'], False, AT)
assert moved['status'] == 'moved' and moved['booking_id'] == booked['booking_id']
assert books.save_booking(PHONE, 'cancel', '', booked['booking_id'], False, AT)['status'] == 'cancelled'
assert books.bookings_for(PHONE, TODAY) == []


_concierge, _complaints = str(instructions('concierge')), str(instructions('complaints'))
"""They are prompt text now rather than something a tool retrieves, so what can go wrong is different:
a substitution that does not fire leaves the marker sitting in the prompt and the agent answering
from nothing at all. Both prompts are checked for a fact only their own document carries.
"""
assert 'metered street parking' in _concierge and 'nine in the morning' in _concierge
assert 'complaint_guidance' in _complaints and 'fourteen days' not in _complaints
assert '<!--' not in _concierge and '<!--' not in _complaints
assert 'twenty percent' not in _concierge


assert prefetch_number('+34600111222') == '+34600111222'
for withheld in ('', 'anonymous', '+266696687', '34600111222'):
    assert prefetch_number(withheld) == '', withheld
assert prefetch_number('+8656696123') == '+8656696123'


unverified = instructions('concierge').render(SalonCall(state=SalonState(customer_phone=PHONE)))
verified = instructions('concierge').render(
    SalonCall(state=SalonState(customer_phone=PHONE, customer_verified='existing', verified_phone=PHONE))
)
assert 'Nobody has been identified' in unverified
assert 'Nobody has been identified' not in verified
assert 'has been identified, for the rest of the call' in verified
assert PHONE not in unverified and PHONE not in verified
assert PHONE in instructions('verify').render(SalonCall(state=SalonState(customer_phone=PHONE)))

appointment = Appointment(
    booking_id='BK0001',
    service='haircut',
    date='2026-09-21',
    time='09:00',
    spoken='Monday at 9:00 AM',
    action='book',
)
with_booking = instructions('concierge').render(
    SalonCall(
        state=SalonState(
            customer_phone=PHONE,
            customer_verified='existing',
            verified_phone=PHONE,
            appointment=appointment,
        )
    )
)
assert 'Monday at 9:00 AM' in with_booking
assert 'Their booking' not in verified, 'no appointment, no section about one'


def says(text, chunk=8):
    async def stream(messages, info: AgentInfo):
        for start in range(0, len(text), chunk):
            yield text[start : start + chunk]

    return stream


def _output_tool(info: AgentInfo, name: str) -> str:
    matches = [tool.name for tool in info.output_tools if name.casefold() in tool.name.casefold()]
    assert matches, f'{name} is not an output tool of this agent: {[t.name for t in info.output_tools]}'
    return matches[0]


def outputs(name, **args):
    """Call one of the agent's output functions, which ends its run."""

    async def stream(messages, info: AgentInfo):
        yield {0: DeltaToolCall(name=_output_tool(info, name), json_args=json.dumps(args))}

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


def tool_call(name, **args):
    async def stream(messages, info: AgentInfo):
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args))}

    return stream


def outputs_or_says(name, text, **args):
    """Call an output function, or speak when the run has none.

    The run the processor forces once the hop budget is spent narrows the output
    to `str`, so speech is the only thing a real model could produce there.
    """

    async def stream(messages, info: AgentInfo):
        if not info.output_tools:
            async for chunk in says(text)(messages, info):
                yield chunk
            return
        yield {0: DeltaToolCall(name=_output_tool(info, name), json_args=json.dumps(args))}

    return stream


def open_days(count=2):
    """The next few days the salon is actually open, soonest first."""
    today = books.now().date()
    days, ahead = [], 1
    while len(days) < count:
        day = today + timedelta(days=ahead)
        if diary.HOURS[day.weekday()] is not None:
            days.append(day)
        ahead += 1
    return days


class Probe(PydanticAgentProcessor):
    """`call._end` is deliberately left alone: hanging up is the processor's own `hang_up`, and what it
    pushes is the thing worth asserting.
    """

    def __init__(self, call, **kwargs):
        super().__init__(call, AGENTS, **kwargs)
        self.frames = []

    async def push_frame(self, frame, direction=None):
        self.frames.append(frame)

    def spoken(self):
        return ''.join(f.text for f in self.frames if isinstance(f, LLMTextFrame))

    def fixed(self):
        return [f.text for f in self.frames if isinstance(f, TTSSpeakFrame)]

    def runs(self):
        return sum(isinstance(f, LLMFullResponseStartFrame) for f in self.frames)


def agreed(call, about='booking', said="yes, that's right", **fields):
    """Put the caller's agreement to one question on the call, as a readback turn would.

    Most of what follows is about the service boundary -- what a diary
    response is allowed to say, what a lost answer means -- and not about
    consent, which `tests.test_consent` covers on its own. This says in one
    line what those tests would otherwise each have to script: the question
    went out last turn, and this turn the caller agreed to it.
    """
    call.ask(Asked(about=about, **fields))
    call.turn_started(said)


def scripted(**by_agent):
    stack = ExitStack()
    for name, fn in by_agent.items():
        stack.enter_context(AGENTS[name].agent.override(model=FunctionModel(stream_function=fn)))
    return stack


async def verify_then_book():
    """The whole round trip in one turn, and the front desk never named `verify`.

    Last turn read their number back; this is the turn their yes arrives on.

    It asks for `booking`, which is all the model knows: what the caller wants. That verification
    comes first is the graph's business.

    The readback already happened; this is the turn after their yes. No `then:` -- the step that was
    interposed pushed where to come back to.

    verify handed straight to booking, never back through the front desk.

    Routing stays silent. The lookup says one short line of its own, and the actual availability
    speaks after it.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    probe = Probe(call, budget=TurnBudget(step_runs=4))

    day = diary.spoken_day(open_days()[-1], books.now().date())
    call.ask(Asked(about='phone', value=PHONE))
    with scripted(
        concierge=then(
            outputs('hand_over', to='booking', because='wants a haircut'),
            says("You're all set. Anything else?"),
        ),
        verify=outputs('confirm_customer', phone=PHONE),
        booking=outputs('offer_times', when=day, service='haircut'),
    ):
        await probe._turn("yes, that's right")

    assert call.state.customer_verified == 'existing', call.state.customer_verified
    assert call.state.customer_phone == PHONE
    assert call.state.agent == 'booking', call.state.agent
    assert call.state.pending is None, 'the stack was pushed on the way in and popped on the way out'
    assert probe.spoken() == ''
    assert all(not node.enter for node in AGENTS.values())
    assert len(probe.fixed()) == 2, probe.fixed()
    assert probe.fixed()[0] in ('Thanks, I have you.', 'Thank you, I have you now.'), probe.fixed()
    assert probe.fixed()[1].endswith('Which time works for you?'), probe.fixed()


async def the_diary_is_the_edge():
    """A verified caller changing a booking never pays for a routing request.

    This is the turn the whole change is for. It used to cost four requests --
    the front desk saying "booking" and nothing else, then the booking step
    reading the diary, then speaking -- measured at 3.37s on a live call, the
    worst turn in it. The front desk holds the diary once somebody is
    identified, so reading it *is* the move: two requests, and the caller is in
    `booking` from the next turn on with no request having said so.

    The day the script looks up is the day the caller names: `read_diary` now refuses a day the
    caller did not say, which is N2's finding.

    The move happened, and nothing was spent announcing it.

    One run, and two model requests inside it: the tool call and the answer. Before, this was two
    runs and four requests -- the front desk spending one to say "booking" and the booking step
    spending three more. The one fixed line is the holding line, said as the second request goes
    out.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    probe = Probe(call)

    day = diary.spoken_day(open_days()[-1], books.now().date())
    with scripted(
        concierge=then(
            tool_call('read_diary', when=day, service='haircut'), says('I can do that. Which suits you?')
        ),
    ):
        await probe._turn(f'can you move my appointment to {day}?')

    assert probe.spoken() == 'I can do that. Which suits you?'
    assert call.state.agent == 'booking', call.state.agent
    assert probe.runs() == 1, probe.runs()
    assert len(call.requests) == 2, call.requests
    assert len(probe.fixed()) == 1 and probe.fixed()[0] in call.locale.holding, probe.fixed()


async def the_diary_is_not_lent_before_verification():
    """An unidentified caller's front desk cannot read the diary, so it hands over.

    The guard is what keeps the free edge honest: lent unconditionally, the
    model would call the diary, be refused for want of an identified caller, and
    pay a retry -- the round trip the lending exists to save.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    probe = Probe(call)

    with scripted(
        concierge=outputs('hand_over', to='booking', because='wants to book'),
        verify=outputs('ask_number', phone=PHONE),
    ):
        await probe._turn('can I book a haircut?')

    assert call.state.agent == 'verify', call.state.agent
    assert call.state.pending == 'booking', call.state.pending


async def interpose_costs_one_run():
    """The invariant that protects goal 5: a deterministic hop costs no request.

    The front desk asks for `booking`, the graph interposes `verify`, and the
    caller hears verify's question. Two runs -- the same two that asking for
    `verify` directly would have cost. If a third ever appears here, some graph
    move has started running a model to decide itself.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    probe = Probe(call, budget=TurnBudget(step_runs=4))

    with scripted(
        concierge=outputs('hand_over', to='booking', because='wants a haircut'),
        verify=outputs('ask_number', phone=PHONE),
    ):
        await probe._turn('can I book a haircut on Monday')

    assert probe.runs() == 2, probe.runs()
    assert call.state.agent == 'verify', call.state.agent
    assert call.state.pending == 'booking', call.state.pending
    assert len(probe.fixed()) == 1, probe.fixed()
    assert probe.fixed()[0].startswith('Let me read that back to you:')
    assert call._asked and call._asked.value == PHONE


async def resume_after_finisher():
    """The pop, on its own: verify finishes and the caller lands where they were going.

    Two runs: the one that verified and the one that spoke. Nothing in between asked the model where
    the caller had been heading.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(agent='verify', pending='booking')
    call.ask(Asked(about='phone', value=PHONE))
    probe = Probe(call, budget=TurnBudget(step_runs=3))

    with scripted(
        verify=outputs('confirm_customer', phone=PHONE),
        booking=outputs('booking_guidance', topic='day'),
    ):
        await probe._turn("yes, that's right")

    assert call.state.agent == 'booking', call.state.agent
    assert call.state.pending is None
    assert 'Which day would you like?' in probe.fixed()
    assert probe.runs() == 2, probe.runs()


async def side_question_does_not_move():
    """A turn that only speaks takes no edge, so there is nothing to suspend or resume."""
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(agent='concierge', customer_verified='existing', verified_phone=PHONE)
    probe = Probe(call)

    with scripted(concierge=says('There is metered street parking outside.')):
        await probe._turn('quick one -- where do I park?')

    assert call.state.agent == 'concierge', call.state.agent
    assert call.state.pending is None, 'no edge was taken, so nothing was stacked'
    assert probe.runs() == 1


async def one_kind_b_per_turn():
    """Model-chosen edges are budgeted, never chained until the caller gives up.

    Bounded rather than hung, and never silent: exactly the runs the budget paid for, and then a
    fixed line spoken by the processor. The fourth run this used to expect was an unbudgeted model
    request made after the budget was spent, which is the thing a budget is for.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    probe = Probe(call, budget=TurnBudget(step_runs=3))

    with scripted(
        concierge=outputs_or_says('hand_over', 'Sorry, what was that?', to='complaints', because='loop'),
        complaints=outputs_or_says('hand_over', 'Sorry, what was that?', to='concierge', because='loop'),
    ):
        await probe._turn('hello?')

    assert probe.runs() == 3, probe.runs()
    assert EXHAUSTED in probe.fixed(), probe.fixed()
    assert not probe.spoken(), 'the fallback is fixed speech, not another model run'


async def correction_refuses_stale_slot():
    """The caller changes the day. The offer made for the old one dies with it.

    The slot from the first day, after the caller moved to the second.

    Both days in the caller's own words: `read_diary` refuses a day they did not say, which is N2's
    finding, and a correction is two days said.

    The refusal came back inside the same run, so the caller waited for one turn rather than two. No
    prompt sentence and no model request was involved in noticing the day had changed.

    The first day's slot was read back last turn: `save_booking` is offered only while a read-back
    waits, and a caller who changes the day in answer to one is exactly this case.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(agent='booking', customer_verified='existing', verified_phone=PHONE)
    probe = Probe(call, budget=TurnBudget(step_runs=2))

    first, second = open_days(2)
    today = books.now().date()
    said_first, said_second = diary.spoken_day(first, today), diary.spoken_day(second, today)
    stale = books.find_slots(PHONE, first.isoformat(), 'haircut', books.now())['slots'][0]
    call.ask(Asked(about='booking', value=stale['slot_id'], action='book'))

    with scripted(
        booking=then(
            tool_call('read_diary', when=first.isoformat(), service='haircut'),
            tool_call('read_diary', when=second.isoformat(), service='haircut'),
            outputs(
                'save_booking',
                action='book',
                slot_id=stale['slot_id'],
                booking_id='',
            ),
            outputs('booking_guidance', topic='retry'),
        ),
    ):
        await probe._turn(f'{said_first} please -- actually, make it {said_second}')

    assert call.state.booking_day == second, call.state.booking_day
    assert call.state.appointment is None, 'a slot from the day they moved away from must not book'
    assert books.bookings_for(PHONE, books.now().date()) == [], 'and nothing reached the diary'
    assert call.state.agent == 'booking', 'the step stays open so the model can offer the right day'
    assert probe.spoken() == ''
    assert probe.fixed()[-1] == "I couldn't complete that request. What would you like to change?"
    assert probe.runs() == 1, probe.runs()


async def save_finishes():
    """`offered_to=call.id`, as `read_diary` does it: an offer belongs to a conversation, so priming
    one under the bare number puts it somewhere the tool will not look.

    a Monday run: ask for the next open day instead

    What `read_diary` would have written. A slot is only bookable on the day the diary was last read
    for, and that now holds whatever the day is -- including when the last read found nothing to
    offer and left it empty.

    And what `ask_booking` would have: the readback went out last turn.

    The diary's own phrase, said by this code -- a fixed line, not model text. The model is never
    asked to compose it, so it cannot turn a Friday into "Thursday the 18th" or read 09:00 back as
    "09:00 AM", both of which it did on live calls.

    **One** request. Saving used to hand the caller to the front desk to have the confirmation
    spoken, which cost a second round trip on the one turn a caller is least willing to wait
    through.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    call.commit(agent='booking')
    probe = Probe(call)

    slot = books.find_slots(PHONE, 'monday', 'haircut', books.now(), offered_to=call.id)
    if slot['status'] != 'ok':
        slot = books.find_slots(PHONE, 'tomorrow', 'haircut', books.now(), offered_to=call.id)
    slot_id = slot['slots'][0]['slot_id']
    call.commit(booking_day=slot['date'])
    call.ask(Asked(about='booking', value=slot_id, action='book'))

    with scripted(
        booking=outputs('save_booking', action='book', slot_id=slot_id, booking_id=''),
    ):
        await probe._turn('yes, book it')

    assert call.state.appointment is not None and call.state.appointment.action == 'book'
    assert call.state.appointment.spoken == slot['slots'][0]['spoken']
    assert call.state.agent == 'concierge'
    assert probe.spoken() == '', 'the model says nothing on the turn that saves'
    assert any(slot['slots'][0]['spoken'] in line for line in probe.fixed()), probe.fixed()
    assert probe.runs() == 1, probe.runs()
    assert books.bookings_for(PHONE, books.now().date()), 'the diary really has it'


async def a_handed_over_agent_reads_the_question_without_it_being_asked_twice():
    """The step taking over reads the caller's words, and they appear once.

    Without them its last message is `hand_over`, answered with "Final result
    processed." and nothing to reply to. Replaying the exact 19-message request
    from the call of 2026-09-20 against the real model: Gemini returned a
    candidate with no parts at all in **5 of 8** runs, each costing a round trip
    and pydantic-ai's "Please return text or include your response in a tool
    call" retry on top -- 0.64 s of the 3.94 s that turn took. One of the three
    that did answer answered about the *booking* the caller had just finished,
    because that was the last errand in front of it.

    The fix used to be re-sending the caller's turn as a second user message.
    That worked and it put the same utterance into the conversation twice,
    which C3 forbids: a projection is not a repetition. Every prompt renders
    `{{heard}}` instead, so the words reach the receiving step through its own
    instructions and the conversation holds one of them.

    Once, in the whole conversation the receiving step is sent.

    And in front of it, in its own instructions, however far back that is.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    probe = Probe(call)
    seen: dict = {}

    async def remember(messages, info: AgentInfo):
        seen['messages'] = list(messages)
        async for chunk in outputs('complaint_guidance', topic='resolution')(messages, info):
            yield chunk

    said = 'my last haircut was completely uneven'
    with scripted(
        concierge=outputs('hand_over', to='complaints', because='an uneven haircut'),
        complaints=remember,
    ):
        await probe._turn(said)

    assert call.state.agent == 'complaints', call.state.agent
    messages = seen['messages']
    asked = [
        part
        for message in messages
        for part in message.parts
        if isinstance(part, UserPromptPart) and said in str(part.content)
    ]
    assert len(asked) == 1, [str(p.content) for p in asked]
    instructions = next((m.instructions for m in reversed(messages) if getattr(m, 'instructions', None)), '')
    assert said in instructions, instructions[-400:]


async def the_note_is_said_once():
    """Writing the complaint down says so; the model's turn is the next step.

    A live call had the filler say "Let me get that written down." and the very
    next request say "I have noted that down for you." -- one fact twice, a
    round trip apart, and the second one arrived after the first had already
    told the caller. Same shape as `save_finishes`: the application owns the
    confirmation, so there is nothing left for a filler to duplicate.

    The summary went back to them last turn; this is the turn their yes lands.

    One line, and it carries the reference the book handed back plus the step the policy actually
    promises. A live call took a reference and never told the caller it, then promised a manager
    callback the policy does not give for a redo.

    The finisher stops the run, so there is no generated second receipt.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    call.commit(agent='complaints')
    call.ask(
        Asked(
            about='complaint',
            value=complaints._gist('an uneven haircut'),
            resolution=complaints._gist('a redo'),
            summary='an uneven haircut',
            requested_resolution='a redo',
        )
    )
    probe = Probe(call)

    with scripted(
        complaints=then(
            outputs('record_complaint'),
            says('Would you like me to put that redo in the diary?'),
        ),
    ):
        await probe._turn("yes, that's right")

    assert books.complaints(), 'the complaint really is on the record'
    assert len(probe.fixed()) == 1, probe.fixed()
    said = probe.fixed()[0]
    assert books.complaints()[0]['reference'] in said, said
    assert REPLY_SOON in said and MANAGER_CALLS not in said, said
    assert not probe.spoken(), probe.spoken()
    assert [r.kind for r in call.requests] == ['classifier', 'classifier'], (
        'confirm and closing, side by side'
    )


async def a_tool_result_is_never_a_script():
    """Nothing a tool returns is a sentence the caller could be read verbatim.

    The now-deleted `get_a_manager` used to answer a failed transfer with
    `say: "Tell the caller a direct transfer needs an inbound phone call, and
    offer what you can do here."`, and on the call of 2026-09-20 the model said
    that to the caller word for word, "Tell the caller" included. A tool result
    is content; an instruction inside it reads as content too. Instructions go
    in the docstring, which is the one place the model cannot repeat.

    The tool that taught this is gone, so the check now runs over the ones that
    are left -- which is what it should always have done.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    ctx = RunContext(deps=call, model=None, usage=None)  # ty: ignore[invalid-argument-type]

    call.ask(
        Asked(
            about='complaint',
            value=complaints._gist('the cut was uneven'),
            resolution=complaints._gist('a redo'),
        )
    )
    call.turn_started("yes, that's right")
    results = [
        await read_diary(ctx, 'tomorrow', 'haircut'),
        await _record_complaint(ctx, 'the cut was uneven', 'a redo'),
    ]

    def leaves(value, path='') -> list[tuple[str, str]]:
        if isinstance(value, BaseModel):
            value = value.model_dump()
        if isinstance(value, dict):
            return [pair for k, v in value.items() for pair in leaves(v, f'{path}.{k}')]
        if isinstance(value, list):
            return [pair for i, v in enumerate(value) for pair in leaves(v, f'{path}[{i}]')]
        return [(path, str(value))]

    for result in results:
        for path, value in leaves(result):
            assert not value.lower().startswith(('tell ', 'say ', 'ask ', 'offer ', 'you should')), (
                f'{path} is addressed to the model, and the model will read it out: {value!r}'
            )


async def a_diary_read_from_the_desk_is_the_move():
    """The front desk reads the diary, which moves the caller: it is not offered `hand_over` after it.

    The user's call of 24 September read the diary from the desk, then handed
    over to booking as well, one request more for a move already made.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    probe = Probe(call)
    offered: list[list[str]] = []
    day = diary.spoken_day(open_days(1)[0], books.now().date())

    async def after_read(messages, info: AgentInfo):
        offered.append([t.name for t in info.output_tools])
        yield 'I have nine or ten.'

    with scripted(concierge=then(tool_call('read_diary', when=day, service='haircut'), after_read)):
        await probe._turn(f'What do you have for a haircut {day}?')
    assert offered and 'hand_over' not in offered[-1], offered
    assert call.state.agent == 'booking', 'the diary read moved the caller'


async def refusal_retries():
    """With no read-back waiting, `save_booking` is not offered at all: a save nobody agreed to cannot be tried.

    It used to be offered and refused, which cost a request on the turn: the
    user's call of 24 September tried a move nothing had read back.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    call.commit(agent='booking')
    probe = Probe(call)
    offered: list[list[str]] = []

    async def ask(messages, info: AgentInfo):
        offered.append([t.name for t in info.output_tools])
        yield {0: DeltaToolCall(name='booking_guidance', json_args='{"topic":"time"}')}

    with scripted(booking=ask):
        await probe._turn('monday morning')

    assert offered and all('save_booking' not in names for names in offered), offered
    assert call.state.appointment is None and call.state.agent == 'booking'
    assert probe.spoken() == ''
    assert probe.fixed() == ['Which time would you like?']
    assert probe.runs() == 1


async def hangs_up():
    """The goodbye first, then the frame that closes the line. `EndWorkerFrame` is a control frame, so
    this order is the order the caller hears: they get the goodbye and then the pipeline drains. The
    old wiring awaited `PipelineWorker.end()` from inside the tool, which waits for a flush probe to
    cross the pipeline -- a probe queued behind this very coroutine. The call hung until the browser
    gave up.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    probe = Probe(call)
    with scripted(concierge=outputs('hang_up', goodbye='Take care, bye!')):
        await probe._turn("no, that's all thanks")
    assert 'Take care, bye!' in probe.fixed()
    goodbye = next(
        i for i, f in enumerate(probe.frames) if isinstance(f, TTSSpeakFrame) and f.text == 'Take care, bye!'
    )
    ends = [i for i, f in enumerate(probe.frames) if isinstance(f, EndWorkerFrame)]
    assert ends, [type(f).__name__ for f in probe.frames]
    assert goodbye < ends[0], [type(f).__name__ for f in probe.frames]


def _heard(call) -> list[str]:
    """Attach a collector to the call's line and hand back what reaches it."""
    said: list[str] = []

    async def collect(text: str) -> None:
        said.append(text)

    call._say = collect
    return said


def _context(call):
    return RunContext(deps=call, model=None, usage=None)  # ty: ignore[invalid-argument-type]


async def _refuses(fn, *args) -> str:
    """Run a tool that must hand the model back a retry, and return what it said."""
    try:
        await fn(*args)
    except ModelRetry as retry:
        return str(retry)
    raise AssertionError(f'{getattr(fn, "__name__", fn)} accepted what it must refuse')


async def complain(call, summary, wanted, said="yes, that's right"):
    """Read a complaint summary back, take the caller's yes, and record it."""
    agreed(call, 'complaint', said, value=complaints._gist(summary), resolution=complaints._gist(wanted))
    return await complaints._record_complaint(_context(call), summary, wanted)


async def refuses_complaint(call, summary, wanted, said="yes, that's right"):
    """The same, for a complaint refused for a reason that is not consent."""
    agreed(call, 'complaint', said, value=complaints._gist(summary), resolution=complaints._gist(wanted))
    return await _refuses(complaints._record_complaint, _context(call), summary, wanted)


async def confirm(call, phone, said="yes, that's right"):
    """Read a number back, take the caller's yes, and look them up."""
    agreed(call, 'phone', said, value=phone)
    return await agents.confirm_customer(_context(call), phone)


async def refuses_confirm(call, phone, said="yes, that's right"):
    """The same, for a lookup that must be refused for a reason that is not consent."""
    agreed(call, 'phone', said, value=phone)
    return await _refuses(agents.confirm_customer, _context(call), phone)


async def save(call, action, slot_id='', booking_id='', said="yes, that's right"):
    """Authorise one write the way an agreed readback would, then perform it."""
    agreed(call, 'booking', said, value=slot_id, action=action, booking_id=booking_id)
    return await agents.save_booking(_context(call), action, slot_id, booking_id)


async def refuses_save(call, action, slot_id='', booking_id='', said="yes, that's right"):
    """The same, for a write that must be refused for a reason that is not consent."""
    agreed(call, 'booking', said, value=slot_id, action=action, booking_id=booking_id)
    return await _refuses(agents.save_booking, _context(call), action, slot_id, booking_id)


async def an_empty_number_identifies_nobody():
    """`confirm_customer(phone='')` came back 'created' and verified the call.

    The state's `Phone` has to permit '' -- it is what the field holds before
    anybody has said a number -- and the same type was on the tool argument,
    so an empty string submitted as an identity created an empty customer
    record, the lookup said 'created', and the two empty strings matched the
    invariant that ties verification to a number. A caller verified as
    nobody, with the booking step's only prerequisite met.

    Through the real streaming invocation, because that is where argument
    validation actually runs.

    no caller ID, so `verify` has to ask for one

    And no empty-number customer was created on the way: asking for one now would say 'existing' if
    the refused call had reached the service.
    """
    books.reset()
    call = open_call('', diary=books)
    call.commit(agent='verify')
    probe = Probe(call)

    with scripted(
        verify=then(
            outputs('confirm_customer', phone=''),
            outputs('verification_guidance', topic='number'),
        )
    ):
        await probe._turn('I would rather not say')

    assert call.state.customer_verified == '', call.state
    assert call.state.phone_confirmed is False, call.state
    assert call.state.verified_phone == '' and call.state.customer_phone == ''
    assert call.state.agent == 'verify', 'nobody was identified, so nobody moved'
    held, *asked = probe.fixed()
    assert held in call.locale.holding, 'the refused value costs a second request, covered'
    assert asked == ['Which phone number should I use? Please say it with the country code.'], asked
    assert books.find_or_create_customer('')['customer_status'] == 'created'


async def a_lookup_about_somebody_else_verifies_nobody():
    """The service answers about a different number, and the answer is legal.

    A valid status and a valid phone, and nothing compared the two: the
    committed identity was the number we asked about, not the one the lookup
    was actually performed against. Not something the model can fix by
    rewording, so the refusal says what to do rather than what to try.
    """
    books.reset()
    call = open_call(PHONE, diary=books)

    def answers_about_somebody_else(phone: str) -> dict:
        return {'customer_status': 'existing', 'customer_phone': OTHER}

    books.find_or_create_customer = answers_about_somebody_else  # ty: ignore[invalid-assignment]
    try:
        refusal = await refuses_confirm(call, PHONE)
    finally:
        del books.find_or_create_customer

    assert 'a different number' in refusal, refusal
    assert call.state.customer_verified == '' and call.state.verified_phone == ''


async def a_complaint_the_book_refused_is_not_confirmed():
    """`{'recorded': False}` parses. It is still the book saying no.

    The response validated, so the write was treated as done and the caller
    heard "Okay, that's written down." about a complaint that is on no
    record anywhere. A schema says the service answered in the right shape;
    only the field says what it did.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    said = _heard(call)

    def refuses(phone: str, summary: str, requested_resolution: str, operation_token: str = '') -> dict:
        return {'recorded': False, 'reference': 'CX0001'}

    books.record_complaint = refuses  # ty: ignore[invalid-assignment]
    try:
        refusal = await refuses_complaint(call, 'the colour was wrong', 'a refund')
    finally:
        del books.record_complaint

    assert 'nothing is on record' in refusal, refusal
    assert said == [], f'the caller was told it was written down: {said}'


async def a_malformed_save_response_is_an_unknown_outcome():
    """The wrapper, not the model: a real response substituted at the boundary.

    The test that used to cover this built an invalid `Appointment` by hand,
    which proves the model validates and not that the invocation path does.
    Here the service answers, and answers unreadably -- so the write may well
    have landed, and the honest thing is the same one the lost-response case
    says rather than an error the caller hears as a broken turn.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE)
    read = books.find_slots(PHONE, 'monday', 'haircut', books.now())
    call.commit(booking_day=read['date'])
    said = _heard(call)

    def answers_nonsense(*args, **kwargs) -> dict:
        return {
            'status': 'booked',
            'booking_id': 'BK0001',
            'service': 'nails',
            'date': 'next Tuesday',
            'time': 'half nine',
            'spoken': '',
        }

    books.save_booking = answers_nonsense  # ty: ignore[invalid-assignment]
    try:
        refusal = await refuses_save(call, 'book', read['slots'][0]['slot_id'], '')
    finally:
        del books.save_booking

    assert 'not clear whether that saved' in refusal, refusal
    assert call.state.appointment is None, 'nothing unreadable reached the state'
    assert call.state.writes == 0
    assert said == [], f'and nothing was confirmed out loud: {said}'


async def a_search_withdraws_the_one_before_it():
    """Asking again is the question now on the table, whatever the answer is.

    Three sequences, and the first two used to go through. A closed day comes
    back with no date, so `booking_day` cleared -- and the rule that compared
    the slot's day against it only ran while it was *set*, so the correction
    the caller made was the one thing that switched off the check protecting
    them from it. A day nobody could work out did the same. And a second
    search on the **same day** for a different service defeated a day
    comparison completely: same date, so nothing to disagree about, and the
    caller was bookable for the service they had just corrected.

    back on the table

    The one a day comparison can never see: same caller, same day, and the service corrected
    underneath the offer.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE, agent='booking')

    friday = await read_diary(_context(call), 'friday', 'haircut')
    assert friday.status == 'ok' and call.state.booking_day == friday.date
    haircut = friday.slots[0].slot_id

    async def refused_now() -> str:
        return await refuses_save(call, 'book', haircut, '')

    sunday = await read_diary(_context(call), 'sunday', 'any')
    assert sunday.status == 'closed' and call.state.booking_day is None
    assert 'currently offering' in await refused_now()

    await read_diary(_context(call), 'friday', 'haircut')
    await read_diary(_context(call), 'whenever suits you', 'haircut')
    assert call.state.booking_day is None
    assert 'currently offering' in await refused_now()

    await read_diary(_context(call), 'friday', 'haircut')
    colour = await read_diary(_context(call), 'friday', 'hair-color')
    assert colour.status == 'ok' and call.state.booking_day == friday.date, 'the day did not change'
    assert 'currently offering' in await refused_now()

    assert books.bookings_for(PHONE, books.now().date()) == [], 'and nothing was written by any of it'


async def the_same_slot_can_be_booked_again_after_it_is_cancelled():
    """Book, cancel, book the same slot: one token, and the second write vanished.

    The operation token is derived from what the action is, which is what
    makes a retry safe. Derived from the arguments alone, a genuinely new
    intent with the same arguments derived the same name: the diary returned
    the first booking's outcome, wrote nothing, and the call state held an
    appointment the diary no longer had.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE, agent='booking')
    _heard(call)

    read = await read_diary(_context(call), 'monday', 'haircut')
    slot = read.slots[0].slot_id
    first = await save(call, 'book', slot, '')
    assert 'booked' in first, first
    held = books.bookings_for(PHONE, books.now().date())[0]['booking_id']

    call.commit(agent='booking')
    await save(call, 'cancel', '', held)
    assert books.bookings_for(PHONE, books.now().date()) == []

    call.commit(agent='booking')
    await read_diary(_context(call), 'monday', 'haircut')
    again = await save(call, 'book', slot, '')
    assert 'booked' in again, again
    assert len(books.bookings_for(PHONE, books.now().date())) == 1, 'the second booking really happened'
    assert call.state.writes == 3, call.state.writes


async def an_offer_belongs_to_the_caller_it_was_made_to():
    """One process, two callers, one slot id. It is not first come, everybody served.

    the same day, without being offered it
    """
    books.reset()
    mine = open_call(PHONE, diary=books)
    mine.commit(customer_verified='existing', verified_phone=PHONE, agent='booking')
    read = await read_diary(_context(mine), 'monday', 'haircut')
    slot = read.slots[0].slot_id

    theirs = open_call(OTHER, diary=books)
    theirs.commit(customer_verified='existing', verified_phone=OTHER, agent='booking')
    theirs.commit(booking_day=read.date)
    refusal = await refuses_save(theirs, 'book', slot, '')
    assert 'not a slot this diary is currently offering this caller' in refusal, refusal
    assert books.bookings_for(OTHER, books.now().date()) == []


async def giving_up_on_the_number_gives_up_the_errand():
    """`verify` is told to hand the caller back, and could not.

    Its only exit is guarded on being verified, so the one route the prompt
    names was shut in exactly the case the prompt names it for -- a caller
    who will not confirm, or who asked about something else. The finisher is
    the route, and it takes the pending errand with it: a caller who
    abandoned a booking and is identified an hour later for some other reason
    must not be put back into it.

    And verifying later resumes nothing: the booking was abandoned, not parked.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(agent='verify', pending='booking')

    handoff = await agents.stop_verifying(_context(call), 'the caller asked about opening hours')
    assert handoff.to == 'concierge', handoff
    assert call.state.pending is None, 'the errand went with it'

    call.commit(agent='verify')
    resumed = await confirm(call, PHONE)
    assert resumed.to == 'concierge', resumed


async def an_unknown_write_blocks_the_next_one():
    """The service commits, answers unreadably, and the model tries again differently.

    Reproduced by the recheck of 2026-09-21: the wrapper raised, `writes`
    stayed at zero, nothing recorded that an operation was outstanding, and a
    second call with a *different* slot derived a different token -- so the
    idempotency cache never saw it and the diary ended with two bookings the
    caller asked for once. The only thing in the way was a sentence of retry
    text asking the model not to.

    Here the service really does record the outcome, so the right answer is
    not to block at all: ask the token what happened and tell the caller
    about the booking they already have.

    It asked the token rather than guessing, so the caller is told about the booking that really
    exists instead of being left with an unknown.

    used by the sibling check below
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE, agent='booking')
    said = _heard(call)
    read = await read_diary(_context(call), 'monday', 'haircut')
    first, second = read.slots[0].slot_id, read.slots[1].slot_id

    real = books.save_booking

    def commits_then_garbles(*args, **kwargs) -> dict:
        """The write lands, and is recorded under its token"""
        real(*args, **kwargs)
        return {'status': 'booked', 'booking_id': 'BK0001', 'service': 'nails', 'date': 'soon'}

    books.save_booking = commits_then_garbles  # ty: ignore[invalid-assignment]
    try:
        recovered = await save(call, 'book', first, '')
    finally:
        del books.save_booking

    assert 'booked' in recovered, recovered
    assert call.state.appointment is not None and call.state.unresolved is None
    assert len(books.bookings_for(PHONE, books.now().date())) == 1, books.bookings_for(
        PHONE, books.now().date()
    )
    assert said, 'and it was confirmed out loud'
    assert second


async def an_outcome_nobody_knows_stops_every_other_write():
    """The other half: the service records nothing, so the operation stays open.

    `additional=True` on a different slot is what an erroneous next model
    action looks like -- different arguments, different token, nothing in the
    idempotency cache to stop it. What stops it is that the call itself
    records an operation it never got an answer for.

    A different slot, flagged as an extra appointment: new arguments, so a new token, so nothing the
    idempotency cache can refuse.

    A complaint is a business write too, and meets the same bar.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE, agent='booking')
    read = await read_diary(_context(call), 'monday', 'haircut')
    first, second = read.slots[0].slot_id, read.slots[1].slot_id

    def never_answers(*args, **kwargs) -> dict:
        raise TimeoutError('no answer')

    books.save_booking = never_answers  # ty: ignore[invalid-assignment]
    try:
        assert 'not clear whether that saved' in await refuses_save(call, 'book', first, '')
        assert call.state.unresolved, 'the operation is recorded as outstanding'
        assert call.state.writes == 0

        blocked = await refuses_save(call, 'book', second, '')
        assert 'still unknown' in blocked, blocked
    finally:
        del books.save_booking

    assert books.bookings_for(PHONE, books.now().date()) == [], 'not one booking, let alone two'
    call.commit(agent='complaints')
    assert 'still unknown' in await refuses_complaint(call, 'the colour was wrong', 'a refund')
    assert books.complaints() == []


async def a_result_that_answers_a_different_question_is_not_accepted():
    """`status='cancelled'` to a `book` request, in perfectly legal values.

    Pydantic says every field holds something allowed. It cannot say the
    result is about what was asked for. Accepted, it stored an appointment
    marked `book` carrying a cancellation, and read the cancellation wording
    out to the caller.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE, agent='booking')
    said = _heard(call)
    read = await read_diary(_context(call), 'monday', 'haircut')
    slot = read.slots[0]

    def cancels_instead(*args, **kwargs) -> dict:
        return {
            'status': 'cancelled',
            'booking_id': 'BK9999',
            'service': 'haircut',
            'date': slot_date,
            'time': slot.time,
            'spoken': slot.spoken,
        }

    slot_date = call.state.booking_day
    books.save_booking = cancels_instead  # ty: ignore[invalid-assignment]
    try:
        refusal = await refuses_save(call, 'book', slot.slot_id, '')
    finally:
        del books.save_booking

    assert 'a cancelled result came back from a book request' in refusal, refusal
    assert call.state.appointment is None and call.state.writes == 0
    assert said == [], f'and nothing was confirmed out loud: {said}'
    assert call.state.unresolved, 'what the service actually did is unknown, and recorded as unknown'


async def a_result_about_a_different_slot_is_not_accepted():
    """The right status, the wrong appointment. Still not this request's answer."""
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE, agent='booking')
    read = await read_diary(_context(call), 'monday', 'haircut')
    asked, other = read.slots[0], read.slots[-1]

    def books_something_else(*args, **kwargs) -> dict:
        return {
            'status': 'booked',
            'booking_id': 'BK0001',
            'service': 'haircut',
            'date': read.date,
            'time': other.time,
            'spoken': other.spoken,
        }

    books.save_booking = books_something_else  # ty: ignore[invalid-assignment]
    try:
        refusal = await refuses_save(call, 'book', asked.slot_id, '')
    finally:
        del books.save_booking

    assert 'not the slot that was offered' in refusal, refusal
    assert call.state.appointment is None


async def a_complaint_is_written_once():
    """The same idempotency a booking has. Two attempts, one entry in the book.

    the attempt whose answer went missing
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE, agent='complaints')
    _heard(call)

    first = await complain(call, 'the colour was wrong', 'a refund')
    call.commit(writes=call.state.writes - 1)
    again = await complain(call, 'the colour was wrong', 'a refund')

    assert first.reference == again.reference, (first, again)
    assert len(books.complaints()) == 1, books.complaints()


async def a_lost_complaint_answer_is_recovered_rather_than_written_twice():
    """The complaint book commits, then the answer goes missing on the way back.

    Reproduced by the second recheck: `record_complaint` derived a token and
    never used it. The exception propagated, `writes` stayed at zero, nothing
    recorded an operation outstanding, and the model retrying in different
    words derived a different token -- so the book ended with the same
    grievance in it twice and the call with one accepted write.

    The failure is injected at the service call, not by editing state
    afterwards: the point is that a wrapper which raises after committing is
    what a lost response looks like from in here.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE, agent='complaints')
    said = _heard(call)

    real = books.record_complaint

    def commits_then_loses_the_answer(*args, **kwargs) -> dict:
        """written, and recorded under its token"""
        real(*args, **kwargs)
        raise TimeoutError('no answer')

    books.record_complaint = commits_then_loses_the_answer  # ty: ignore[invalid-assignment]
    try:
        recorded = await complain(call, 'the colour was wrong', 'a refund')
    finally:
        del books.record_complaint

    assert recorded.recorded, recorded
    assert len(books.complaints()) == 1, books.complaints()
    assert call.state.writes == 1 and call.state.unresolved is None, call.state
    assert said, 'and the caller was told'


async def a_complaint_nobody_answered_for_blocks_the_next_write():
    """The other half: nothing written, nothing cached, so the operation stays open.

    And it is settled by the same path a booking is, because it is the same
    kind of thing: an outstanding write is outstanding whichever book it
    would have gone in.

    Rewording it is a different token, so nothing the book's own idempotency can refuse. What
    refuses is the call.

    And a booking may not run past it either, whichever book is outstanding.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE, agent='complaints')
    _heard(call)

    def never_answers(*args, **kwargs) -> dict:
        raise TimeoutError('no answer')

    books.record_complaint = never_answers  # ty: ignore[invalid-assignment]
    try:
        refusal = await refuses_complaint(call, 'the colour was wrong', 'a refund')
        assert 'not clear whether that saved' in refusal, refusal
        assert call.state.unresolved is not None, 'recorded as outstanding'
        assert call.state.unresolved.action == 'complaint', call.state.unresolved
        assert call.state.writes == 0

        blocked = await refuses_complaint(call, 'my colour came out wrong', 'money back')
        assert 'still unknown' in blocked, blocked
    finally:
        del books.record_complaint

    assert books.complaints() == [], books.complaints()
    call.commit(agent='booking')
    assert 'still unknown' in await refuses_save(call, 'book', 'HAIRCUT-1', '')


async def a_recovered_result_still_has_to_answer_the_request():
    """The contradiction refused on the first attempt, offered again on the second.

    Reproduced by the second recheck. `_settle` held only a token, so it took
    the action from whatever status came back: a `cancelled` result correctly
    refused as an answer to `book` was accepted on recovery as an ordinary
    cancellation, stored with `action='cancel'`, and read out to the caller.

    The recovery path, which loads exactly that cached result.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(customer_verified='existing', verified_phone=PHONE, agent='booking')
    said = _heard(call)
    read = await read_diary(_context(call), 'monday', 'haircut')
    slot = read.slots[0]

    def cancels_instead(*args, **kwargs) -> dict:
        """Cached under the token, exactly as a real service would cache the outcome it believes it
        produced.
        """
        return books._record(
            kwargs['operation_token'],
            {
                'status': 'cancelled',
                'booking_id': 'BK9999',
                'service': 'haircut',
                'date': call.state.booking_day,
                'time': slot.time,
                'spoken': slot.spoken,
            },
        )

    books.save_booking = cancels_instead  # ty: ignore[invalid-assignment]
    try:
        first = await refuses_save(call, 'book', slot.slot_id, '')
        assert 'a cancelled result came back from a book request' in first, first
        assert call.state.unresolved is not None

        again = await refuses_save(call, 'book', slot.slot_id, '')
    finally:
        del books.save_booking

    assert 'a cancelled result came back from a book request' in again, again
    assert call.state.appointment is None, call.state.appointment
    assert call.state.writes == 0 and call.state.unresolved is not None
    assert said == [], f'and nothing was read out to the caller: {said}'


async def correcting_the_number_withdraws_the_old_identity_offers():
    """Offers belong to who the caller turned out to be, not only to the call.

    Reproduced by the second recheck. Scoping offers to the call closed the
    cross-call hole and opened this one: the offer's identity field was
    filled with the call id, so the identity check compared the call against
    itself and always agreed. Verify as one number, read the diary, correct
    the number, and the slots shown to the first identity booked under the
    second.

    And the offer is gone for the old number too: it was made to this call under an identity this
    call no longer has.
    """
    books.reset()
    call = open_call(PHONE, diary=books)
    call.commit(agent='verify')
    await confirm(call, PHONE)
    call.commit(agent='booking')
    read = await read_diary(_context(call), 'monday', 'haircut')
    slot = read.slots[0].slot_id

    call.commit(agent='verify')
    await confirm(call, OTHER)
    assert call.state.customer_phone == OTHER, call.state.customer_phone

    call.commit(agent='booking')
    refusal = await refuses_save(call, 'book', slot, '')
    assert 'not a slot this diary is currently offering this caller' in refusal, refusal
    assert books.bookings_for(OTHER, books.now().date()) == []
    assert books.offer_of(call.id, slot, OTHER) == {}


async def a_second_appointment_is_read_back_as_one():
    """Trace fbdd81b6: "Shall I book you in for a haircut, Friday at 11:00?" to a caller holding the 9:00.

    The yes to it could never be carried out, because the diary refuses a
    second booking nobody asked for. Read back as a second appointment, the
    yes is the ask, and it books. A caller with nothing booked hears the
    plain read-back, and a "no" to the second one writes nothing.
    """
    for answer, writes in (('No, move it instead.', 1), ('Yes.', 2)):
        books.reset()
        call = open_call(PHONE, diary=books)
        call.commit(agent='booking', customer_verified='existing', verified_phone=PHONE)
        probe = Probe(call)
        day = open_days()[-1].strftime('%A')
        slots = books.find_slots(PHONE, day, 'haircut', books.now(), offered_to=call.id)['slots']
        first, second = slots[0], slots[-1]
        plain = booking_question(call, 'book', first['slot_id'], '')
        assert plain.spoken.startswith('Shall I book you in for a haircut') and not plain.additional, plain
        books.save_booking(PHONE, 'book', first['slot_id'], '', False, books.now(), offered_to=call.id)

        question = booking_question(call, 'book', second['slot_id'], '')
        assert question.spoken.startswith('Shall I book you a second appointment'), question.spoken
        assert question.additional and 'as well as your haircut' in question.spoken, question.spoken
        call.turn_started('Book another one.')
        await call.speak_now(question.put(call), origin='ask')
        call.note_reached()
        call._burst -= 60
        call.note_drained()
        with scripted(booking=outputs('booking_guidance', topic='time')):
            await probe._turn(answer)
        held = books.bookings_for(PHONE, books.now().date())
        assert len(held) == writes, (answer, held, probe.fixed()[-1])
        assert probe._finished.status == 'answered', probe._finished


async def main():
    """The `additional` flag that this used to check is gone from `save_booking` altogether: a second
    appointment is now decided by the caller's answer to the question the refusal asks, not by an
    argument the model fills in. The replay of both live calls that found it lives in
    `tests.test_consent`, as `n3_a_flipped_flag_is_not_consent`.
    """
    await verify_then_book()
    await a_second_appointment_is_read_back_as_one()
    await the_diary_is_the_edge()
    await the_diary_is_not_lent_before_verification()
    await interpose_costs_one_run()
    await resume_after_finisher()
    await side_question_does_not_move()
    await one_kind_b_per_turn()
    await correction_refuses_stale_slot()
    await save_finishes()
    await an_empty_number_identifies_nobody()
    await a_lookup_about_somebody_else_verifies_nobody()
    await a_complaint_the_book_refused_is_not_confirmed()
    await a_malformed_save_response_is_an_unknown_outcome()
    await a_search_withdraws_the_one_before_it()
    await the_same_slot_can_be_booked_again_after_it_is_cancelled()
    await an_unknown_write_blocks_the_next_one()
    await an_outcome_nobody_knows_stops_every_other_write()
    await a_result_that_answers_a_different_question_is_not_accepted()
    await a_result_about_a_different_slot_is_not_accepted()
    await a_complaint_is_written_once()
    await a_lost_complaint_answer_is_recovered_rather_than_written_twice()
    await a_complaint_nobody_answered_for_blocks_the_next_write()
    await a_recovered_result_still_has_to_answer_the_request()
    await correcting_the_number_withdraws_the_old_identity_offers()
    await an_offer_belongs_to_the_caller_it_was_made_to()
    await giving_up_on_the_number_gives_up_the_errand()
    await a_handed_over_agent_reads_the_question_without_it_being_asked_twice()
    await the_note_is_said_once()
    await a_tool_result_is_never_a_script()
    await refusal_retries()
    await a_diary_read_from_the_desk_is_the_move()
    await hangs_up()
    print('ok')


if __name__ == '__main__':
    with classifier_model():
        asyncio.run(main())
