"""Run: uv run python -m tests.test_structured

`speech: structured` on the clinic (`clinic_flow/structured.yaml`): one typed
decision per model response, reads that continue, consent and consultations.
Every request is captured at the Pydantic AI model boundary (`Payload`), and
every backend read and write is counted by kind. Asserts, no pytest.

* a local answer with its update is one request, committed once and said once;
* an update keeps what it leaves out, clears only what may be null, records
  False, 0 and '', and commits nothing at all when any part of it is refused;
* a read with the new values it rests on is two requests, the service asked
  once, and a repeat or a refetch with nothing new is a labelled retry that
  reads nothing;
* a read's results are told apart: found, refused, absent, failed, and a
  cancelled read assigns nothing;
* an answer beside a read waits for the read, and one response carries one
  update and one terminal decision, whatever the order;
* consent is unchanged: a read-back in code's words, a yes to exactly it, a
  correction read back fresh, a side question or an ordinary question never a yes;
* a consultation answers once, never blank, comes back on a legal edge, and
  the caller's own words reach the step it came back to, whatever the
  specialist's summary said;
* partial output commits nothing; a committed update stays when its words are cut.

Scripted models prove runtime properties, never language understanding.
"""

import asyncio
import json
import re
import threading
from dataclasses import replace
from typing import Any

from pydantic_ai.messages import ModelRequest, RetryPromptPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo

import core
from core.history import Payload
from core.state import Checkpoint
from core.turn import Barge
from tests import script as scripted
from tests.clinic_flow.consult import CheckedIn
from tests.clinic_flow.structured import Clinic, VisitState
from tests.script import Call, Calls, Record, Reply, Stall
from tests.test_context import HERE
from tests.test_public import CLOCK

MANIFEST = 'structured.yaml'
"""The clinic every test here opens."""
READ_BACK = {
    '0900': 'Shall I book Tuesday at 9:00 AM for P-1234?',
    '1000': 'Shall I book Tuesday at 10:00 AM for P-1234?',
}


class Script(scripted.Script):
    """The shared scripted model; with `probe` set, the adaptive fake.

    A probing request that is offered a redundant path takes it first
    (`redundant`), before any scripted move.
    """

    def __init__(self, probe: bool = False) -> None:
        super().__init__()
        self.probe = probe
        self.probed: list[str] = []

    def next_move(self, messages: list[Any], info: AgentInfo) -> scripted.Move:
        if self.probe and (found := redundant(messages, info)) is not None:
            self.probed.append(found.tool)
            return found
        return super().next_move(messages, info)


def redundant(messages: Any, info: AgentInfo) -> Call | None:
    """A redundant call this request is offered: a refetch with nothing new, or a repeated read.

    A repeat is only probed once per call of it, so the fake does not loop.
    """
    tools = {t.name: t for t in info.function_tools}
    options = tools.get('options')
    if options is not None and 'update' not in options.parameters_json_schema.get('required', []):
        returned = [
            p
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, ToolReturnPart) and p.tool_name == 'options'
        ]
        if returned:
            return Call('options')
    last = messages[-1] if messages else None
    if isinstance(last, ModelRequest):
        for part in last.parts:
            if isinstance(part, ToolReturnPart) and part.tool_name in tools and part.tool_name != 'options':
                return Call(part.tool_name)
    return None


def opened(
    script: Script, *, agent: str = 'desk', **facts: Any
) -> tuple[core.StepFlow, core.FlowCall, Clinic]:
    """A call at `agent`, checked in unless `facts` say otherwise, on the fixed clock."""
    flow = core.load(HERE / MANIFEST).compile(model=script.model)
    clinic = Clinic()
    given: dict[str, Any] = {'patient': 'P-1234', 'checked_in': CheckedIn(patient='P-1234')}
    state = VisitState.model_validate(given | facts)
    call = flow.open(state, services=clinic, clock=lambda: CLOCK)
    call.commit(agent=agent)
    return flow, call, clinic


async def turn(flow, call, script: Script, words: str, *moves, heard: bool = True, barge=None):
    """One caller turn: what was said, and how it ended, with the requests this piece of it sent."""
    script.moves = list(moves)
    before = len(script.payloads)
    reply = await core.text_turn(flow, call, words, barge=barge)
    if heard:
        call.acknowledge(*(segment.id for segment in reply.segments))
    sent = len(script.payloads) - before
    assert reply.finished.requests >= sent, 'a joined turn keeps one tally; this piece sent the rest'
    return [segment.text for segment in reply.segments], replace(reply.finished, requests=sent)


def booking(call: core.FlowCall) -> list[str]:
    """The status of every booking intent on the call, oldest first (a read makes intents of its own)."""
    return [i.status for i in call.intents if i.operation == 'book']


def retries(payload: Payload) -> list[str]:
    """The retry texts a request carries: what the model was told a call it made was refused for."""
    return [
        str(part['content'])
        for message in json.loads(payload.history)
        for part in message['parts']
        if part.get('part_kind') == 'retry-prompt'
    ]


def schema(payload: Payload, name: str, kind: str = 'outputs') -> dict[str, Any]:
    found = json.loads(getattr(payload, kind))
    tools = found['tools'] if kind == 'outputs' else found
    return next(t['parameters_json_schema'] for t in tools if t['name'] == name)


def names(payload: Payload) -> tuple[list[str], list[str]]:
    outputs = json.loads(payload.outputs)
    return [t['name'] for t in json.loads(payload.tools)], [t['name'] for t in outputs['tools']]


async def offered(flow, call, script: Script) -> None:
    """Both times on offer for a cleaning, as the caller asked: one availability read, two requests."""
    said, done = await turn(
        flow,
        call,
        script,
        'A cleaning, please. When can I come?',
        Call('options', update={'service': 'cleaning'}),
        Reply('Nine or ten on Tuesday.'),
    )
    assert said == ['Nine or ten on Tuesday.'] and done.requests == 2, (said, done)


async def read_back(flow, call, script: Script, slot: str = '1000') -> None:
    said, done = await turn(flow, call, script, 'Ten, please.', Call('book', update={'slot': slot}))
    assert said == [READ_BACK[slot]] and done.requests == 1, (said, done)


async def a_local_answer_is_one_request():
    script = Script()
    flow, call, clinic = opened(script)
    version = call._version
    said, done = await turn(
        flow, call, script, 'I come in a wheelchair.', Reply('We will have the ramp ready.', wheelchair=True)
    )
    assert said == ['We will have the ramp ready.'] and done.requests == 1, (said, done)
    assert call.state.facts.wheelchair is True and call._version == version + 1, 'committed once'
    assert [s.kind for s in call.all_spoken if s.text] == ['model'], 'the model wrote it; code said it'
    tools, outputs = names(script.payloads[-1])
    assert 'collect' not in tools and 'decline' not in tools, tools
    assert json.loads(script.payloads[-1].outputs)['text'] is False, (
        'no plain text: every answer is a decision'
    )
    assert {'reply', 'book', 'hand_over', 'goodbye'} <= set(outputs), outputs
    update = schema(script.payloads[-1], 'reply')['$defs']['DeskUpdate']['properties']
    assert set(update) == {'service', 'slot', 'note', 'wheelchair', 'visitors', 'companion'}, update
    assert not any('default' in f or 'examples' in f for f in update.values()), (
        'no state default is an update'
    )
    assert update['visitors'] == {
        'description': 'How many people come with them.',
        'maximum': 3,
        'minimum': 0,
        'type': 'integer',
    }, update['visitors']
    book = schema(script.payloads[-1], 'book')['$defs']['DeskBookUpdate']['properties']
    assert set(book) == {'slot'}, 'an action takes only the fields it rests on'
    said, done = await turn(flow, call, script, 'Is that all set?', Reply('Yes.'))
    after = script.payloads[-1]
    assert '"wheelchair":true' in after.instructions, 'the next request sees the change'
    assert 'We will have the ramp ready.' in after.history, 'what was said is in the history, once'
    assert after.history.count('We will have the ramp ready.') == 1, (
        'as speech; the earlier decision is not sent'
    )
    assert clinic.calls == 0 and not clinic.quotes and not clinic.availability


async def an_update_keeps_what_it_leaves_out():
    script = Script()
    flow, call, _ = opened(script, note='sore tooth', companion={'name': 'Ana', 'relation': 'friend'})
    await turn(flow, call, script, 'Nobody comes with me.', Reply('Noted.', visitors=0, companion=None))
    facts = call.state.facts
    assert facts.visitors == 0 and facts.companion is None and facts.note == 'sore tooth', facts
    await turn(
        flow, call, script, 'No wheelchair, and forget the note.', Reply('Done.', wheelchair=False, note='')
    )
    assert call.state.facts.wheelchair is False and call.state.facts.note == '', call.state.facts
    before, version = call.state.facts, call._version
    said, done = await turn(
        flow,
        call,
        script,
        'Clear the note.',
        Reply('Cleared.', note=None),
        Reply('What should the note say?'),
    )
    assert said == ['What should the note say?'] and done.requests == 2, (
        'null is refused where None is no value'
    )
    assert call.state.facts == before and call._version == version
    assert any('note' in r for r in retries(script.payloads[-1])), retries(script.payloads[-1])


async def a_refused_decision_commits_nothing():
    """Whole-state rule, unknown key, receipt field, another step's field, bad nested value, a pick no offer holds.

    The last is checked on the offer for the state the update ends in: a checkup is only at ten.
    """
    script = Script()
    flow, call, clinic = opened(script, wheelchair=True, visitors=2)
    bad = [
        ({'visitors': 3, 'note': 'bringing family'}, 'room fits two visitors'),
        ({'reference': 'CL999', 'note': 'x'}, 'reference'),
        ({'patient': 'P-9999', 'note': 'x'}, 'patient'),
        ({'companion': {'name': ' ', 'relation': 'boss'}, 'note': 'x'}, 'companion'),
        ({'service': 'checkup', 'slot': '0900', 'note': 'x'}, 'not on offer'),
    ]
    for update, why in bad:
        before, version = call.state.facts, call._version
        said, done = await turn(
            flow,
            call,
            script,
            'Something.',
            Call('reply', say='All set for you.', update=update),
            Reply('Sorry, I could not note that.'),
        )
        assert said == ['Sorry, I could not note that.'] and done.requests == 2, (update, said)
        assert call.state.facts == before and call._version == version, f'{update} committed part of itself'
        assert any(why in r for r in retries(script.payloads[-1])), (why, retries(script.payloads[-1]))
    assert clinic.calls == 0


async def a_read_with_new_values_is_two_requests():
    script = Script()
    flow, call, clinic = opened(script)
    said, done = await turn(
        flow,
        call,
        script,
        'How much is a cleaning?',
        Call('quote', update={'service': 'cleaning'}),
        Reply('A cleaning is 40 euros.'),
    )
    assert said == ['A cleaning is 40 euros.'] and done.requests == 2, (said, done)
    assert clinic.quotes == ['cleaning'] and clinic.availability == [], (
        'the price list saw the new value once'
    )
    assert call.state.facts.service == 'cleaning' and call.state.facts.price.euros == 40, call.state.facts
    answer = script.payloads[-1]
    assert '"status": "found"' in answer.history and '"euros": 40' in answer.history, answer.history
    await offered(flow, call, script)
    assert clinic.availability == ['cleaning'] and clinic.quotes == ['cleaning'], 'each read counted apart'
    said, done = await turn(
        flow,
        call,
        script,
        'And for a checkup instead?',
        Call('options', update={'service': 'checkup'}),
        Reply('Only ten, for a checkup.'),
    )
    assert done.requests == 2 and clinic.availability == ['cleaning', 'checkup'], clinic.availability
    assert call.state.facts.price is None, 'the price quoted for a cleaning rested on the service, and went'
    assert dict(call._offers['slot']) == {'1000': 'Tuesday at 10:00 AM'}


async def no_redundant_path_is_offered():
    """The adaptive fake takes any redundant path a request offers; structured steps offer only labelled dead ends."""
    script = Script(probe=True)
    flow, call, clinic = opened(script)
    await offered(flow, call, script)
    options = schema(script.payloads[-1], 'options', 'tools')
    assert options['required'] == ['update'] and options['properties']['update']['minProperties'] == 1, (
        options
    )
    refs = {r.rsplit('/', 1)[-1] for r in re.findall(r'"\$ref": "([^"]+)"', json.dumps(options))}
    assert refs and refs <= set(options['$defs']), ('every reference resolves', refs, options['$defs'])
    assert set(options['$defs']['DeskOptionsUpdate']['properties']) == {'service'}, (
        'only what the offer rests on'
    )
    assert script.probed == [], 'with every offer standing, a refetch with nothing new is not offered'
    said, done = await turn(
        flow,
        call,
        script,
        'How much is it?',
        Call('quote'),
        Reply('A cleaning is 40 euros.'),
    )
    assert script.probed == ['quote'] and clinic.quotes == ['cleaning'], 'the repeat read nothing'
    assert done.requests == 3 and said == ['A cleaning is 40 euros.'], (said, done)
    assert any('already answered' in r for r in retries(script.payloads[-1]))
    script.probe = False
    said, done = await turn(
        flow,
        call,
        script,
        'Any other times?',
        Call('options'),
        Call('collect', values={'note': 'x'}),
        Reply('Those are the times.'),
    )
    assert clinic.availability == ['cleaning'], 'a hostile refetch read nothing'
    assert done.requests == 3 and call.state.facts.note == '', 'the hidden collect recorded nothing'
    told = retries(script.payloads[-1])
    assert any('still stands' in r for r in told) and any('collect' in r for r in told), told


async def a_read_says_what_it_found():
    script = Script()
    flow, call, clinic = opened(script)
    clinic.closed = True
    await turn(
        flow,
        call,
        script,
        'Any time for a cleaning?',
        Call('options', update={'service': 'cleaning'}),
        Reply('Nothing is free.'),
    )
    assert '"options": {}' in script.payloads[-1].history, 'a valid empty answer'
    await turn(
        flow,
        call,
        script,
        'What does a checkup cost?',
        Call('quote', update={'service': 'checkup'}),
        Reply('A checkup is priced at the visit.'),
    )
    told = script.payloads[-1].history
    assert '"status": "refused"' in told and 'priced at the visit' in told, told
    assert call.state.facts.price is None, 'a no assigns nothing'
    clinic.failing = True
    await turn(
        flow,
        call,
        script,
        'And a cleaning?',
        Call('quote', update={'service': 'cleaning'}),
        Reply('The price list is not answering.'),
    )
    assert '"status": "failed"' in script.payloads[-1].history and call.state.facts.price is None
    await turn(flow, call, script, 'Try again?', Call('quote'), Reply('A cleaning is 40 euros.'))
    assert clinic.quotes == ['checkup', 'cleaning', 'cleaning'], 'a read with no answer may be asked again'
    assert call.state.facts.price is not None
    await turn(flow, call, script, 'A checkup after all.', Reply('Noted.', service='checkup'))
    clinic.during_quote = lambda: call.update(patient='P-9999')
    await turn(
        flow,
        call,
        script,
        'And the cleaning price?',
        Call('quote', update={'service': 'cleaning'}),
        Reply('Who is calling?'),
    )
    assert '"status": "absent"' in script.payloads[-1].history, 'settled for another caller: not told'
    assert call.state.facts.price is None, 'another caller changed nothing here'


async def a_cancelled_read_assigns_nothing():
    script = Script()
    flow, call, clinic = opened(script)
    started, release = threading.Event(), threading.Event()

    def slow() -> None:
        started.set()
        release.wait(5)

    clinic.during_quote = slow
    barge = Barge()
    script.moves = [Call('quote', update={'service': 'cleaning'})]
    task = asyncio.create_task(core.text_turn(flow, call, 'How much is a cleaning?', barge=barge))
    await asyncio.to_thread(started.wait, 5)
    barge.fire()
    done = await task
    release.set()
    assert done.finished.status == 'interrupted', done.finished
    assert call.state.facts.service == 'cleaning' and call.state.facts.price is None, call.state.facts
    assert not [s for s in done.segments if s.text], 'nothing was said'
    clinic.during_quote = None
    _ = await turn(flow, call, script, 'How much?', Call('quote'), Reply('40 euros.'))
    assert clinic.quotes == ['cleaning', 'cleaning'] and call.state.facts.price is not None, clinic.quotes


async def one_decision_per_response():
    for calls in (
        Calls(Call('quote', update={'service': 'cleaning'}), Reply('Forty euros.')),
        Calls(Reply('Forty euros.'), Call('quote', update={'service': 'cleaning'})),
    ):
        script = Script()
        flow, call, clinic = opened(script)
        said, done = await turn(
            flow, call, script, 'How much is a cleaning?', calls, Reply('A cleaning is 40 euros.')
        )
        assert said == ['A cleaning is 40 euros.'] and done.requests == 2, (calls, said)
        assert clinic.quotes == ['cleaning'], 'the read ran once'
        assert any('looks something up' in r for r in retries(script.payloads[-1]))
    script = Script()
    flow, call, clinic = opened(script)
    said, done = await turn(
        flow,
        call,
        script,
        'A cleaning, or a checkup?',
        Calls(Call('quote', update={'service': 'cleaning'}), Call('options', update={'service': 'checkup'})),
        Reply('A cleaning is 40 euros.'),
    )
    assert call.state.facts.service == 'cleaning' and clinic.availability == [], 'one update per response'
    assert any('already carries the update' in r for r in retries(script.payloads[-1]))
    await offered(flow, call, script)
    said, done = await turn(
        flow,
        call,
        script,
        'Ten.',
        Calls(Reply('Booked!'), Call('book', update={'slot': '1000'})),
    )
    assert said == ['Booked!'] and done.requests == 1 and call.state.facts.slot is None, (
        said,
        call.state.facts,
    )
    assert clinic.calls == 0 and call.pending_question() is None, 'the second decision did nothing'


async def consent_is_code_s():
    script = Script()
    flow, call, clinic = opened(script)
    await offered(flow, call, script)
    await read_back(flow, call, script)
    assert clinic.calls == 0 and booking(call) == ['prepared']
    said, done = await turn(
        flow, call, script, 'Yes, but make it nine.', Call('book', update={'slot': '0900'})
    )
    assert said == [READ_BACK['0900']] and done.requests == 1 and clinic.calls == 0, said
    said, _ = await turn(flow, call, script, 'How much is it?', Call('quote'), Reply('40 euros.'))
    assert clinic.calls == 0 and booking(call) == ['prepared', 'prepared'], 'a question is not a no'
    said, _ = await turn(flow, call, script, 'Yes.', Call('book'))
    assert said == [READ_BACK['0900']] and clinic.calls == 0, 'the side question came between: asked afresh'
    said, _ = await turn(flow, call, script, 'Yes.', Call('book'))
    assert said == ['Booked for Tuesday at 9:00 AM, reference CL001.'] and clinic.calls == 1, said
    assert clinic.bookings == {'CL001': ('P-1234', '0900')}, 'only the corrected time'
    script = Script()
    flow, call, clinic = opened(script)
    await offered(flow, call, script)
    await turn(flow, call, script, 'Ten.', Reply('Shall I put you down for ten?', slot='1000'))
    said, _ = await turn(flow, call, script, 'Yes.', Call('book'))
    assert said == [READ_BACK['1000']] and clinic.calls == 0, 'an ordinary question is not a read-back'
    await turn(flow, call, script, 'No, not now.', Reply('All right.', decline=True))
    assert call.declined_this_turn() and clinic.calls == 0
    said, _ = await turn(flow, call, script, 'Actually yes.', Call('book'))
    assert said == [READ_BACK['1000']] and clinic.calls == 0, 'a declined read-back is spent'
    clinic.failing = True
    said, _ = await turn(flow, call, script, 'Yes.', Call('book'))
    assert said == [call.locale.unsettled] and clinic.calls == 1, said
    assert booking(call)[-1] == 'unknown', 'no invented outcome'


async def a_yes_that_repeats_its_value_is_a_yes():
    """A yes whose update sends again the value just read back is carried out; a different value is read back.

    Live, a model answering "yes, that's perfect" often fills the operation's
    update with the value it already sent (session 4, Coval run
    82gmB8t5BHAGjq8bHeTysX). The question it answers is unchanged, so the yes
    stands; only a value that changes, or one the state refuses, is a change.
    """
    script = Script()
    flow, call, clinic = opened(script)
    await offered(flow, call, script)
    await read_back(flow, call, script)
    said, _ = await turn(flow, call, script, 'Yes, ten is perfect.', Call('book', update={'slot': '1000'}))
    assert said == ['Booked for Tuesday at 10:00 AM, reference CL001.'] and clinic.calls == 1, said
    script = Script()
    flow, call, clinic = opened(script)
    await offered(flow, call, script)
    await read_back(flow, call, script)
    said, _ = await turn(flow, call, script, 'Yes, nine then.', Call('book', update={'slot': '0900'}))
    assert said == [READ_BACK['0900']] and clinic.calls == 0, 'a new value is a change: read back fresh'


async def a_consultation_answers_once():
    script = Script()
    flow, call, clinic = opened(script)
    assert flow.nodes.flow.moves['nurse'] >= {'desk', 'front'}, 'the legal way back'
    await offered(flow, call, script)
    await read_back(flow, call, script)
    said, done = await turn(
        flow,
        call,
        script,
        'Wait, can I eat before? Yes, book it.',
        Call('hand_over', to='nurse', because='food'),
        Call('done', answer='   '),
        Call('done', answer='Yes, eat something light.'),
    )
    assert said == ['Yes, eat something light.', READ_BACK['1000']], said
    assert done.requests == 3 and call.state.agent == 'desk' and clinic.calls == 0, 'the nurse books nothing'
    nurse = script.payloads[-1]
    assert 'book' not in names(nurse)[1] and 'reply' in names(nurse)[1], names(nurse)
    assert any('answer' in r for r in retries(nurse)), 'a blank answer is refused'
    said, _ = await turn(flow, call, script, 'Yes.', Call('book'))
    assert clinic.calls == 1 and said == ['Booked for Tuesday at 10:00 AM, reference CL001.'], said


async def a_stale_read_back_is_not_put_again():
    """The read-back waiting when a consultation began is put again only while the state still gives its arguments."""
    script = Script()
    flow, call, clinic = opened(script)
    await offered(flow, call, script)
    await read_back(flow, call, script)
    await turn(
        flow,
        call,
        script,
        'Can I eat before?',
        Call('hand_over', to='nurse', because='food'),
        Reply('Is it in the morning?'),
    )
    call.update(slot='0900')
    said, _ = await turn(flow, call, script, 'Yes, the morning.', Call('done', answer='Then eat lightly.'))
    assert said == ['Then eat lightly.'] and call.state.agent == 'desk', (
        "the ten o'clock read-back is not put again"
    )
    said, _ = await turn(flow, call, script, 'Yes.', Call('book'))
    assert said == [READ_BACK['0900']] and clinic.calls == 0, 'the new time is read back fresh first'


async def the_caller_s_words_come_back_whatever_the_summary():
    for handback in ('They want nine instead of ten.', 'They want Wednesday.', ''):
        script = Script()
        flow, call, clinic = opened(script)
        await offered(flow, call, script)
        await read_back(flow, call, script)
        answer = Call('done', answer='Yes, eat something light.', handback=handback)
        if handback:
            said, done = await turn(
                flow,
                call,
                script,
                'Can I eat before? And make it nine instead.',
                Call('hand_over', to='nurse', because='food'),
                answer,
                Call('book', update={'slot': '0900'}),
            )
            assert said == ['Yes, eat something light.', READ_BACK['0900']], (handback, said)
            assert done.requests == 3 and done.handoffs == ('nurse', 'desk'), done
            desk = script.payloads[-1]
            assert handback in desk.instructions, "the summary is shown as the specialist's reading"
        else:
            said, done = await turn(
                flow,
                call,
                script,
                'Can I eat before? And make it nine instead.',
                Call('hand_over', to='nurse', because='food'),
                answer,
            )
            assert said == ['Yes, eat something light.', READ_BACK['1000']], (
                'still what the state gives: asked again'
            )
            await turn(flow, call, script, 'No, nine!', Call('book', update={'slot': '0900'}))
            desk = script.payloads[-1]
        assert 'make it nine instead' in desk.history, (handback, "the caller's words reach the desk")
        assert clinic.calls == 0 and call.state.agent == 'desk'
        said, _ = await turn(flow, call, script, 'Yes.', Call('book'))
        assert clinic.bookings == {'CL001': ('P-1234', '0900')}, (handback, clinic.bookings)


async def a_consultation_keeps_the_prerequisite_apart():
    script = Script()
    flow, call, _ = opened(script, agent='front', patient=None, checked_in=None)
    call.commit(pending='desk')
    await turn(
        flow,
        call,
        script,
        'Is it safe while pregnant?',
        Call('hand_over', to='nurse', because='safety'),
        Call('done', answer='Yes, it is safe.'),
    )
    assert call.state.agent == 'front' and call.state.pending == 'desk' and call.state.consulting is None
    said, done = await turn(
        flow,
        call,
        script,
        'P-1234.',
        Call('check_in', update={'patient': 'P-1234'}),
        Reply('What can I book for you?'),
    )
    assert said == ['Thanks, I have you checked in.', 'What can I book for you?'], said
    assert call.state.agent == 'desk' and call.state.pending is None and done.requests == 2, done


async def an_offer_on_arrival_is_code_s():
    """`offers: [slot]`: reached from the prerequisite, code asks the choice function for the first line.

    The clinic's `free_times` gives ids and no words, so the offer stands and
    the agent says it in one reply, with no `options` request. Closed, it
    offers nothing, and the agent runs with nothing standing. The clinic has no
    exit into front, so the call starts where a hand-over to desk leaves it
    once front is interposed: at front, desk pending, and the request kept
    (`Checkpoint`, as `turn` records it).
    """
    text = (
        (HERE / MANIFEST)
        .read_text()
        .replace('    operations: [book, quote]\n', '    offers: [slot]\n    operations: [book, quote]\n')
    )
    for closed, told in ((False, 'is on offer now'), (True, 'nothing is on offer')):
        script = Script()
        flow = core.flowspec.load(text, root=HERE).compile(model=script.model)
        clinic = Clinic()
        clinic.closed = closed
        call = flow.open(VisitState(service='cleaning'), services=clinic, clock=lambda: CLOCK)
        call.commit(agent='front', pending='desk')
        call._checkpoint = Checkpoint('desk', 'A cleaning, please.')
        said, done = await turn(
            flow,
            call,
            script,
            'P-1234.',
            Call('check_in', update={'patient': 'P-1234'}),
            Reply('Nine or ten on Tuesday.'),
        )
        assert said == ['Thanks, I have you checked in.', 'Nine or ten on Tuesday.'], said
        assert call.state.agent == 'desk' and done.requests == 2 and clinic.availability == ['cleaning'], (
            done,
            clinic.availability,
        )
        assert told in script.payloads[-1], (closed, script.payloads[-1].instructions[-600:])
        assert ('slot' in call._offers) == (not closed), closed


async def a_pick_is_read_back_and_a_record_offers_again():
    """`offers:` after a `record` as on arrival: one time is picked and read back by code, with no request.

    Two times for a cleaning are ids with no words: the agent says them. The
    caller changes to a checkup, only recorded: the times rest on the service,
    so code asks again, and the one time left is picked and read back.
    """
    text = (
        (HERE / MANIFEST)
        .read_text()
        .replace('structured.py:free_times', 'structured.py:the_only_time')
        .replace('    operations: [book, quote]\n', '    offers: [slot]\n    operations: [book, quote]\n')
    )
    asked = 'Shall I book Tuesday at 10:00 AM for P-1234?'
    script = Script()
    flow = core.flowspec.load(text, root=HERE).compile(model=script.model)
    call = flow.open(VisitState(service='checkup'), services=Clinic(), clock=lambda: CLOCK)
    call.commit(agent='front', pending='desk')
    call._checkpoint = Checkpoint('desk', 'A checkup, please.')
    said, done = await turn(flow, call, script, 'P-1234.', Call('check_in', update={'patient': 'P-1234'}))
    assert said == ['Thanks, I have you checked in.', asked] and done.requests == 1, (said, done)
    assert call.state.facts.slot == '1000'
    script = Script()
    flow = core.flowspec.load(text, root=HERE).compile(model=script.model)
    clinic = Clinic()
    known = VisitState(service='cleaning', patient='P-1234', checked_in=CheckedIn(patient='P-1234'))
    call = flow.open(known, services=clinic, clock=lambda: CLOCK)
    call.commit(agent='desk')
    said, done = await turn(flow, call, script, 'Make it a checkup.', Record(service='checkup'))
    assert said == [asked] and done.requests == 1 and clinic.availability == ['checkup'], (said, done)


async def partial_output_commits_nothing():
    script = Script()
    flow, call, _ = opened(script)
    barge = Barge()
    stall = Stall('reply', '{"say": "Booked you in", "update": {"note": "vip')
    script.moves = [stall]
    task = asyncio.create_task(core.text_turn(flow, call, 'Note that I am a VIP.', barge=barge))
    await stall.started.wait()
    version = call._version
    barge.fire()
    done = await task
    assert done.finished.status == 'interrupted' and call._version == version and call.state.facts.note == ''
    assert not [s for s in call.all_spoken if 'Booked' in s.text], 'no word of an unfinished decision'
    said, _ = await turn(flow, call, script, 'Hello?', Reply('Hello, how can I help?'))
    assert said == ['Hello, how can I help?']


async def a_committed_update_stays_when_its_words_are_cut():
    script = Script()
    flow, call, _ = opened(script)
    barge = Barge()

    async def cut(text: str) -> None:
        barge.fire()

    call._say = cut
    _, done = await turn(
        flow,
        call,
        script,
        'I come in a wheelchair.',
        Reply('We will have the ramp ready.', wheelchair=True),
        heard=False,
        barge=barge,
    )
    assert done.status == 'interrupted' and call.state.facts.wheelchair is True, (done, call.state.facts)
    segment = next(s for s in call.all_spoken if s.text == 'We will have the ramp ready.')
    assert segment.delivered != 'delivered', 'cut words are not claimed as heard'
    assert any(
        isinstance(p, RetryPromptPart) is False and getattr(p, 'tool_name', '') == 'reply'
        for m in call.messages
        for p in m.parts
    ), 'the decision stays in the record'


async def main():
    await a_local_answer_is_one_request()
    await an_update_keeps_what_it_leaves_out()
    await a_refused_decision_commits_nothing()
    await a_read_with_new_values_is_two_requests()
    await no_redundant_path_is_offered()
    await a_read_says_what_it_found()
    await a_cancelled_read_assigns_nothing()
    await one_decision_per_response()
    await consent_is_code_s()
    await a_yes_that_repeats_its_value_is_a_yes()
    await a_consultation_answers_once()
    await a_stale_read_back_is_not_put_again()
    await the_caller_s_words_come_back_whatever_the_summary()
    await a_consultation_keeps_the_prerequisite_apart()
    await an_offer_on_arrival_is_code_s()
    await a_pick_is_read_back_and_a_record_offers_again()
    await partial_output_commits_nothing()
    await a_committed_update_stays_when_its_words_are_cut()
    print(
        'structured ok: one request per local answer, two per read, one decision per response, consent, consultations'
    )


if __name__ == '__main__':
    asyncio.run(main())
