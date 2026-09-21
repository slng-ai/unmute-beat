"""Run: uv run --env-file .env python -m tests.test_salon

Asserts, no pytest. The diary and the knowledge base are exercised directly; the
agents are driven through the processor with a scripted model, so nothing here
touches the network.
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
from pydantic_ai import ModelResponse, RunContext, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from salon import diary, knowledge
from salon.agents import AGENTS, FILLER, NOTED, get_a_manager, prompt
from salon.state import Appointment, SalonCall, SalonState, open_call, prefetch_number
from voice.harness import TurnBudget
from voice.processor import EXHAUSTED, PydanticAgentProcessor

PHONE = '+34600111222'
MONDAY = date(2026, 9, 21)
AT = datetime(2026, 9, 19, 10, 0, tzinfo=diary.TZ)  # a Saturday morning

# --- the diary works the day out, so the model never has to -----------------

TODAY = AT.date()
assert diary.resolve_day('today', TODAY) == TODAY
assert diary.resolve_day('tomorrow', TODAY) == TODAY + timedelta(days=1)
assert diary.resolve_day('friday', TODAY) == date(2026, 9, 25)
# "next Friday" is the week after, when this Friday has not happened yet.
assert diary.resolve_day('next friday', TODAY) == date(2026, 10, 2)
# A weekday named on that weekday means the next one, not this morning.
assert diary.resolve_day('saturday', TODAY) == date(2026, 9, 26)
assert diary.resolve_day('2026-10-01', TODAY) == date(2026, 10, 1)
assert diary.resolve_day('whenever suits', TODAY) is None
assert diary.resolve_day('', TODAY) is None

assert diary.spoken(TODAY, __import__('datetime').time(15, 0), TODAY) == 'today at 3:00 PM'
assert diary.spoken(MONDAY, __import__('datetime').time(9, 0), TODAY) == 'Monday at 9:00 AM'

# --- reading it --------------------------------------------------------------

diary.reset()
assert diary.find_slots(PHONE, '', 'haircut', AT)['status'] == 'need_day'
assert diary.find_slots(PHONE, 'sunday', 'haircut', AT)['status'] == 'closed'
assert diary.find_slots(PHONE, 'yesterday', 'haircut', AT)['status'] in ('need_day', 'past')

monday = diary.find_slots(PHONE, 'monday', 'haircut', AT)
assert monday['status'] == 'ok' and monday['day'] == 'Monday' and monday['price_euros'] == 28
assert monday['slots'][0]['spoken'] == 'Monday at 9:00 AM'

# A time already gone past on the salon clock is not offered, however empty the
# diary looks: at 10:00 the 9:00 slot today is not a slot.
today_free = diary.find_slots(PHONE, 'today', 'haircut', AT)
assert all(slot['time'] > '10:00' for slot in today_free['slots']), today_free['slots'][:3]

# --- writing to it -----------------------------------------------------------

booked = diary.save_booking(PHONE, 'book', monday['slots'][0]['slot_id'], '', False, AT)
assert booked['status'] == 'booked' and booked['spoken'] == 'Monday at 9:00 AM'
assert diary.bookings_for(PHONE, TODAY)[0]['booking_id'] == booked['booking_id']
# The diary hands back what the caller holds, so the refusal is actionable.
assert diary.find_slots(PHONE, 'monday', 'haircut', AT)['bookings']

for why, call in {
    'a second booking without asking for one': lambda: diary.save_booking(
        PHONE, 'book', monday['slots'][1]['slot_id'], '', False, AT
    ),
    'a slot the diary never returned': lambda: diary.save_booking(
        PHONE, 'book', 'made-up-slot', '', True, AT
    ),
    'a booking reference nobody holds': lambda: diary.save_booking(PHONE, 'cancel', '', 'BK9999', False, AT),
}.items():
    try:
        call()
    except diary.RefusedError as refused:
        assert str(refused), why
    else:
        raise AssertionError(f'the diary must refuse {why}')

moved = diary.save_booking(PHONE, 'move', monday['slots'][3]['slot_id'], booked['booking_id'], False, AT)
assert moved['status'] == 'moved' and moved['booking_id'] == booked['booking_id']
assert diary.save_booking(PHONE, 'cancel', '', booked['booking_id'], False, AT)['status'] == 'cancelled'
assert diary.bookings_for(PHONE, TODAY) == []

# --- the documents -----------------------------------------------------------

assert knowledge.look_up('services', 'are you open on sunday')['results'][0]['heading'] == 'Opening hours'
assert 'refund' in knowledge.look_up('refunds', 'can I have my money back')['results'][0]['text'].casefold()
# Nothing plausible beats the closest wrong thing: the agent is told to say so.
assert knowledge.look_up('services', 'do you sell concert tickets')['results'] == []

# --- the carrier's caller ID -------------------------------------------------

assert prefetch_number('+34600111222') == '+34600111222'
for withheld in ('', 'anonymous', '+266696687', '34600111222'):
    assert prefetch_number(withheld) == '', withheld
# A real number that merely starts like a withheld spelling is kept.
assert prefetch_number('+8656696123') == '+8656696123'

# --- the prompt is branched before the model reads it ------------------------

unverified = prompt('concierge').render(SalonCall(state=SalonState(customer_phone=PHONE)))
verified = prompt('concierge').render(
    SalonCall(state=SalonState(customer_phone=PHONE, customer_verified='existing'))
)
assert 'Nobody has been identified' in unverified
assert 'Nobody has been identified' not in verified
assert 'has been identified, for the rest of the call' in verified
# The front desk never holds a number, whatever state it is in.
assert PHONE not in unverified and PHONE not in verified
# Only the verification step renders one.
assert PHONE in prompt('verify').render(SalonCall(state=SalonState(customer_phone=PHONE)))

appointment = Appointment(
    booking_id='BK0001',
    service='haircut',
    date='2026-09-21',
    time='09:00',
    spoken='Monday at 9:00 AM',
    action='book',
)
with_booking = prompt('concierge').render(
    SalonCall(state=SalonState(customer_verified='existing', appointment=appointment))
)
assert 'Monday at 9:00 AM' in with_booking
assert 'Their booking' not in verified, 'no appointment, no section about one'

# --- a scripted model --------------------------------------------------------


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
    today = diary.now().date()
    days, ahead = [], 1
    while len(days) < count:
        day = today + timedelta(days=ahead)
        if diary.HOURS[day.weekday()] is not None:
            days.append(day)
        ahead += 1
    return days


class Probe(PydanticAgentProcessor):
    def __init__(self, call, **kwargs):
        super().__init__(call, AGENTS, **kwargs)
        self.frames = []
        # `call._end` is deliberately left alone: hanging up is the processor's
        # own `hang_up`, and what it pushes is the thing worth asserting.

    async def push_frame(self, frame, direction=None):
        self.frames.append(frame)

    def spoken(self):
        return ''.join(f.text for f in self.frames if isinstance(f, LLMTextFrame))

    def fixed(self):
        return [f.text for f in self.frames if isinstance(f, TTSSpeakFrame)]

    def runs(self):
        return sum(isinstance(f, LLMFullResponseStartFrame) for f in self.frames)


def scripted(**by_agent):
    stack = ExitStack()
    for name, fn in by_agent.items():
        stack.enter_context(AGENTS[name].agent.override(model=FunctionModel(stream_function=fn)))
    return stack


# --- the graph puts verification in the way, and takes it back out -----------


async def verify_then_book():
    """The whole round trip in one turn, and the front desk never named `verify`."""
    diary.reset()
    call = open_call(PHONE)
    probe = Probe(call, budget=TurnBudget(step_runs=4))

    day = diary.spoken_day(diary.now().date() + timedelta(days=2), diary.now().date())
    with scripted(
        # It asks for `booking`, which is all the model knows: what the caller
        # wants. That verification comes first is the graph's business.
        concierge=then(
            outputs('hand_over', to='booking', because='wants a haircut'),
            says("You're all set. Anything else?"),
        ),
        # The readback already happened; this is the turn after their yes. No
        # `then:` -- the step that was interposed pushed where to come back to.
        verify=outputs('confirm_customer', phone=PHONE),
        booking=then(
            tool_call('read_diary', when=day, service='haircut'), says('I can do that. Shall I book it?')
        ),
    ):
        await probe._turn('can I get a haircut, and my number is right')

    assert call.state.customer_verified == 'existing', call.state.customer_verified
    assert call.state.customer_phone == PHONE
    # verify handed straight to booking, never back through the front desk.
    assert call.state.agent == 'booking', call.state.agent
    assert call.state.pending is None, 'the stack was pushed on the way in and popped on the way out'
    assert probe.spoken() == 'I can do that. Shall I book it?'
    # One fixed line per silence: the verify hop spoke, the booking hop and the
    # diary tool did not pile another two on top.
    assert len(probe.fixed()) == 1, probe.fixed()
    assert probe.fixed()[0] in AGENTS['verify'].enter


async def the_diary_is_the_edge():
    """A verified caller changing a booking never pays for a routing request.

    This is the turn the whole change is for. It used to cost four requests --
    the front desk saying "booking" and nothing else, then the booking step
    reading the diary, then speaking -- measured at 3.37s on a live call, the
    worst turn in it. The front desk holds the diary once somebody is
    identified, so reading it *is* the move: two requests, and the caller is in
    `booking` from the next turn on with no request having said so.
    """
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing')
    probe = Probe(call)

    day = diary.spoken_day(diary.now().date() + timedelta(days=2), diary.now().date())
    with scripted(
        concierge=then(
            tool_call('read_diary', when=day, service='haircut'), says('I can do that. Which suits you?')
        ),
    ):
        await probe._turn('can you move my appointment to Saturday?')

    assert probe.spoken() == 'I can do that. Which suits you?'
    # The move happened, and nothing was spent announcing it.
    assert call.state.agent == 'booking', call.state.agent
    # One run, and two model requests inside it: the tool call and the answer.
    # Before, this was two runs and four requests -- the front desk spending one
    # to say "booking" and the booking step spending three more.
    assert probe.runs() == 1, probe.runs()
    assert sum(isinstance(m, ModelResponse) for m in call.messages) == 2, call.messages
    # The diary's own filler covers the lookup, and that is the only fixed line:
    # no hop happened, so no `enter:` line piled on top of it. Before, the
    # caller heard the booking step's entry line *and* this one.
    assert probe.fixed() == [probe.fixed()[0]], probe.fixed()
    assert probe.fixed()[0] in FILLER['read_diary'], probe.fixed()
    assert probe.fixed()[0] not in AGENTS['booking'].enter


async def the_diary_is_not_lent_before_verification():
    """An unidentified caller's front desk cannot read the diary, so it hands over.

    The guard is what keeps the free edge honest: lent unconditionally, the
    model would call the diary, be refused for want of an identified caller, and
    pay a retry -- the round trip the lending exists to save.
    """
    diary.reset()
    call = open_call(PHONE)
    probe = Probe(call)

    with scripted(
        concierge=outputs('hand_over', to='booking', because='wants to book'),
        verify=says('Is that the right number?'),
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
    diary.reset()
    call = open_call(PHONE)
    probe = Probe(call, budget=TurnBudget(step_runs=4))

    with scripted(
        concierge=outputs('hand_over', to='booking', because='wants a haircut'),
        verify=says('Is that plus three four, six zero zero, one one one, two two two?'),
    ):
        await probe._turn('can I book a haircut on Monday')

    assert probe.runs() == 2, probe.runs()
    assert call.state.agent == 'verify', call.state.agent
    assert call.state.pending == 'booking', call.state.pending
    assert probe.fixed() == [probe.fixed()[0]] and probe.fixed()[0] in AGENTS['verify'].enter, (
        "the line covering the hop is the interposed step's, not the destination's"
    )


async def resume_after_finisher():
    """The pop, on its own: verify finishes and the caller lands where they were going."""
    diary.reset()
    call = open_call(PHONE)
    call.commit(agent='verify', pending='booking')
    probe = Probe(call, budget=TurnBudget(step_runs=3))

    with scripted(
        verify=outputs('confirm_customer', phone=PHONE),
        booking=says('Which day were you thinking?'),
    ):
        await probe._turn("yes, that's right")

    assert call.state.agent == 'booking', call.state.agent
    assert call.state.pending is None
    assert probe.spoken() == 'Which day were you thinking?'
    # Two runs: the one that verified and the one that spoke. Nothing in between
    # asked the model where the caller had been heading.
    assert probe.runs() == 2, probe.runs()


async def side_question_does_not_move():
    """A turn that only speaks takes no edge, so there is nothing to suspend or resume."""
    diary.reset()
    call = open_call(PHONE)
    call.commit(agent='booking', customer_verified='existing')
    probe = Probe(call)

    with scripted(booking=says('Parking is free for the first hour round the back.')):
        await probe._turn('quick one -- where do I park?')

    assert call.state.agent == 'booking', call.state.agent
    assert call.state.pending is None, 'no edge was taken, so nothing was stacked'
    assert probe.runs() == 1


async def one_kind_b_per_turn():
    """Model-chosen edges are budgeted, never chained until the caller gives up."""
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing')
    probe = Probe(call, budget=TurnBudget(step_runs=3))

    with scripted(
        concierge=outputs_or_says('hand_over', 'Sorry, what was that?', to='complaints', because='loop'),
        complaints=outputs_or_says('hand_over', 'Sorry, what was that?', to='concierge', because='loop'),
    ):
        await probe._turn('hello?')

    # Bounded rather than hung, and never silent: exactly the runs the budget
    # paid for, and then a fixed line spoken by the processor. The fourth run
    # this used to expect was an unbudgeted model request made after the
    # budget was spent, which is the thing a budget is for.
    assert probe.runs() == 3, probe.runs()
    assert EXHAUSTED in probe.fixed(), probe.fixed()
    assert not probe.spoken(), 'the fallback is fixed speech, not another model run'


# --- a correction the application holds, not the prompt ----------------------


async def correction_refuses_stale_slot():
    """The caller changes the day. The offer made for the old one dies with it."""
    diary.reset()
    call = open_call(PHONE)
    call.commit(agent='booking', customer_verified='existing')
    probe = Probe(call, budget=TurnBudget(step_runs=2))

    first, second = open_days(2)
    stale = diary.find_slots(PHONE, first.isoformat(), 'haircut', diary.now())['slots'][0]

    with scripted(
        booking=then(
            tool_call('read_diary', when=first.isoformat(), service='haircut'),
            tool_call('read_diary', when=second.isoformat(), service='haircut'),
            # The slot from the first day, after the caller moved to the second.
            outputs(
                'save_booking',
                action='book',
                slot_id=stale['slot_id'],
                booking_id='',
                confirmed=True,
                additional=False,
            ),
            says('That one has gone, I am afraid. Shall I look at the other day?'),
        ),
    ):
        await probe._turn('monday please -- actually, make it the day after')

    assert call.state.booking_day == second.isoformat(), call.state.booking_day
    assert call.state.appointment is None, 'a slot from the day they moved away from must not book'
    assert diary.bookings_for(PHONE, diary.now().date()) == [], 'and nothing reached the diary'
    assert call.state.agent == 'booking', 'the step stays open so the model can offer the right day'
    assert probe.spoken() == 'That one has gone, I am afraid. Shall I look at the other day?'
    # The refusal came back inside the same run, so the caller waited for one
    # turn rather than two. No prompt sentence and no model request was involved
    # in noticing the day had changed.
    assert probe.runs() == 1, probe.runs()


# --- saving finishes the step in one request ---------------------------------


async def save_finishes():
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing')
    call.commit(agent='booking')
    probe = Probe(call)

    slot = diary.find_slots(PHONE, 'monday', 'haircut', diary.now())
    if slot['status'] != 'ok':  # a Monday run: ask for the next open day instead
        slot = diary.find_slots(PHONE, 'tomorrow', 'haircut', diary.now())
    slot_id = slot['slots'][0]['slot_id']

    with scripted(
        booking=outputs(
            'save_booking', action='book', slot_id=slot_id, booking_id='', confirmed=True, additional=False
        ),
    ):
        await probe._turn('yes, book it')

    assert call.state.appointment is not None and call.state.appointment.action == 'book'
    assert call.state.appointment.spoken == slot['slots'][0]['spoken']
    assert call.state.agent == 'concierge'
    # The diary's own phrase, said by this code -- a fixed line, not model text.
    # The model is never asked to compose it, so it cannot turn a Friday into
    # "Thursday the 18th" or read 09:00 back as "09:00 AM", both of which it did
    # on live calls.
    assert probe.spoken() == '', 'the model says nothing on the turn that saves'
    assert any(slot['slots'][0]['spoken'] in line for line in probe.fixed()), probe.fixed()
    # **One** request. Saving used to hand the caller to the front desk to have
    # the confirmation spoken, which cost a second round trip on the one turn a
    # caller is least willing to wait through.
    assert probe.runs() == 1, probe.runs()
    assert diary.bookings_for(PHONE, diary.now().date()), 'the diary really has it'


async def a_handed_over_agent_is_asked_the_question():
    """The step taking over reads the caller's words, not a spent errand.

    Without them its last message is `hand_over`, answered with "Final result
    processed." and nothing to reply to. Replaying the exact 19-message request
    from the call of 2026-09-20 against the real model: Gemini returned a
    candidate with no parts at all in **5 of 8** runs, each costing a round trip
    and pydantic-ai's "Please return text or include your response in a tool
    call" retry on top -- 0.64 s of the 3.94 s that turn took. One of the three
    that did answer answered about the *booking* the caller had just finished,
    because that was the last errand in front of it. With the question put back,
    0 of 8. The caller's words are in the history two messages up either way, so
    this is about which message is last, not about what the model can see.
    """
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing')
    probe = Probe(call)
    seen: dict = {}

    async def remember(messages, info: AgentInfo):
        seen['last'] = messages[-1]
        async for chunk in says('Oh, that is not okay.')(messages, info):
            yield chunk

    with scripted(
        concierge=outputs('hand_over', to='complaints', because='an uneven haircut'),
        complaints=remember,
    ):
        await probe._turn('my last haircut was completely uneven')

    assert call.state.agent == 'complaints', call.state.agent
    asked = [p for p in seen['last'].parts if isinstance(p, UserPromptPart)]
    assert any('uneven' in p.content for p in asked), seen['last'].parts


# --- one fact, said once -----------------------------------------------------


async def the_note_is_said_once():
    """Writing the complaint down says so; the model's turn is the next step.

    A live call had the filler say "Let me get that written down." and the very
    next request say "I have noted that down for you." -- one fact twice, a
    round trip apart, and the second one arrived after the first had already
    told the caller. Same shape as `save_finishes`: the application owns the
    confirmation, so there is nothing left for a filler to duplicate.
    """
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing')
    call.commit(agent='complaints')
    probe = Probe(call)

    with scripted(
        complaints=then(
            tool_call('record_complaint', summary='an uneven haircut', requested_resolution='a redo'),
            says('Would you like me to put that redo in the diary?'),
        ),
    ):
        await probe._turn('yes, please write it down')

    assert diary.complaints(), 'the complaint really is on the record'
    assert 'record_complaint' not in FILLER, (
        'a filler here announces what NOTED then states; that pair is the defect'
    )
    assert len(probe.fixed()) == 1 and probe.fixed()[0] in NOTED, probe.fixed()
    # And the model's own turn is free to be something new.
    assert 'redo in the diary' in probe.spoken(), probe.spoken()


async def a_tool_result_is_never_a_script():
    """Nothing a tool returns is a sentence the caller could be read verbatim.

    `get_a_manager` used to answer a failed transfer with
    `say: "Tell the caller a direct transfer needs an inbound phone call, and
    offer what you can do here."`, and on the call of 2026-09-20 the model said
    that to the caller word for word, "Tell the caller" included. A tool result
    is content; an instruction in it reads as content too. Instructions go in
    the docstring, which is the one place the model cannot repeat.
    """
    call = open_call(PHONE)
    result = await get_a_manager(RunContext(deps=call, model=None, usage=None))  # type: ignore[arg-type]
    assert not result['transferred']
    assert 'say' not in result, result
    for key, value in result.items():
        assert not str(value).lower().startswith(('tell ', 'say ', 'ask ', 'offer ')), (
            f'{key} is addressed to the model, and the model will read it out: {value!r}'
        )


# --- a refusal keeps the step open, with the reason ---------------------------


async def refusal_retries():
    diary.reset()
    call = open_call(PHONE)
    call.commit(customer_verified='existing')
    call.commit(agent='booking')
    probe = Probe(call)
    seen = []

    async def unconfirmed_then_ask(messages, info: AgentInfo):
        if not seen:
            seen.append('tried')
            yield {
                0: DeltaToolCall(
                    name=_output_tool(info, 'save_booking'),
                    json_args=json.dumps(
                        {
                            'action': 'book',
                            'slot_id': 'x',
                            'booking_id': '',
                            'confirmed': False,
                            'additional': False,
                        }
                    ),
                )
            }
        else:
            for chunk in ('Shall I ', 'book it?'):
                yield chunk

    with scripted(booking=unconfirmed_then_ask):
        await probe._turn('monday morning')

    assert call.state.appointment is None, 'an unconfirmed save must write nothing'
    assert call.state.agent == 'booking', 'the step stays open so the model can fix it'
    assert probe.spoken() == 'Shall I book it?'
    assert probe.runs() == 1, 'a retry is the same run, not a second turn'


# --- hanging up ---------------------------------------------------------------


async def hangs_up():
    diary.reset()
    call = open_call(PHONE)
    probe = Probe(call)
    with scripted(concierge=outputs('hang_up', goodbye='Take care, bye!')):
        await probe._turn("no, that's all thanks")
    # The goodbye first, then the frame that closes the line. `EndWorkerFrame` is
    # a control frame, so this order is the order the caller hears: they get the
    # goodbye and then the pipeline drains. The old wiring awaited
    # `PipelineWorker.end()` from inside the tool, which waits for a flush probe
    # to cross the pipeline -- a probe queued behind this very coroutine. The
    # call hung until the browser gave up.
    assert 'Take care, bye!' in probe.fixed()
    goodbye = next(
        i for i, f in enumerate(probe.frames) if isinstance(f, TTSSpeakFrame) and f.text == 'Take care, bye!'
    )
    ends = [i for i, f in enumerate(probe.frames) if isinstance(f, EndWorkerFrame)]
    assert ends, [type(f).__name__ for f in probe.frames]
    assert goodbye < ends[0], [type(f).__name__ for f in probe.frames]


async def main():
    await verify_then_book()
    await the_diary_is_the_edge()
    await the_diary_is_not_lent_before_verification()
    await interpose_costs_one_run()
    await resume_after_finisher()
    await side_question_does_not_move()
    await one_kind_b_per_turn()
    await correction_refuses_stale_slot()
    await save_finishes()
    await a_handed_over_agent_is_asked_the_question()
    await the_note_is_said_once()
    await a_tool_result_is_never_a_script()
    await refusal_retries()
    await hangs_up()
    print('ok')


asyncio.run(main())
