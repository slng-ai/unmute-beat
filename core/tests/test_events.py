"""Run: uv run python -m tests.test_events

The listener hook (`core.events`) and going back (`core.snapshot`), on the structured clinic.

* a call with a listener delivers every event in order, numbered with no gap, each stamped with its
  turn: the caller's words, the step's requests, what the model called, the state it committed, what
  the bot said and that it was read, how the turn ended, and a snapshot per turn;
* a slow listener never slows a turn, and one too far behind loses events and counts them;
* a snapshot survives JSON, and a call resumed from it sends the model the same history the
  original call sent at that point: the speech ledger came back with the messages;
* a yes never carries across a resume: the read-back is asked again, nothing is booked;
* a business action settled before the snapshot stays settled, and a resumed edit the application's
  types refuse opens nothing;
* a read-back withdrawn before the snapshot stays withdrawn after it.

Scripted models prove runtime properties, never language understanding.
"""

import asyncio
import json
import time

from pydantic import ValidationError

import core
from core import events as ev
from core.snapshot import TurnSnapshot
from tests.clinic_flow.structured import Clinic
from tests.script import Call, Reply
from tests.test_context import HERE
from tests.test_public import CLOCK
from tests.test_structured import MANIFEST, READ_BACK, Script, offered, opened, read_back, turn


class Heard:
    """A listener that keeps every event, optionally taking its time over each."""

    def __init__(self, delay: float = 0.0) -> None:
        self.events: list[ev._Event] = []
        self.delay = delay

    async def event(self, event) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def of(self, kind: type[ev._Event]) -> list:
        return [e for e in self.events if isinstance(e, kind)]


async def every_event_in_order():
    script = Script()
    flow, call, _ = opened(script)
    heard = Heard()
    events = call.listen(heard, run='run_t')
    events.started(
        agent='clinic', channel='text', entry=call.state.agent, state=call.state.facts.model_dump(mode='json')
    )
    await offered(flow, call, script)
    await read_back(flow, call, script)
    events.ended(call.turn, 'hangup')
    await events.close()

    assert [e.seq for e in heard.events] == list(range(len(heard.events))), 'numbered from 0, no gap'
    assert {e.run for e in heard.events} == {'run_t'}
    assert heard.types()[0] == 'run.started' and heard.types()[-1] == 'run.ended'
    first = [e for e in heard.events if e.turn == 1]
    types = [e.type for e in first]
    assert types[0] == 'caller.speech' and first[0].text == 'A cleaning, please. When can I come?', types
    assert types.count('model.request') == 2 and types.count('model.ended') == 2, types
    decided = [e.name for e in first if isinstance(e, ev.ModelDecision)]
    assert decided == ['options', 'reply'], decided
    assert [e.text for e in first if isinstance(e, ev.ModelDelta) and e.kind == 'args'], (
        'arguments as they stream'
    )
    committed = [e for e in first if isinstance(e, ev.StateCommitted)]
    assert committed and committed[0].changed == ['service'] and committed[0].state['service'] == 'cleaning'
    said = [e for e in first if isinstance(e, ev.BotSpeech)]
    assert [(e.phase, e.text) for e in said] == [
        ('queued', 'Nine or ten on Tuesday.'),
        ('delivered', 'Nine or ten on Tuesday.'),
    ], said
    assert said[1].evidence == 'ack', 'a text reader acknowledged it'
    finished = heard.of(ev.TurnFinished)
    assert [(e.turn, e.status, e.requests) for e in finished] == [(1, 'answered', 2), (2, 'answered', 1)]
    ended = heard.of(ev.ModelEnded)
    assert all(e.first_ms is not None and e.ms >= e.first_ms for e in ended), ended
    snapshots = heard.of(ev.TurnSnapshotted)
    assert [s.turn for s in snapshots] == [1, 2], 'one snapshot per turn'
    assert types.index('turn.finished') < types.index('turn.snapshot'), 'the snapshot follows the turn'
    intents = heard.of(ev.IntentChanged)
    assert [(i.operation, i.status) for i in intents if i.operation == 'book'] == [('book', 'prepared')]
    assert intents[-1].args == {'patient': 'P-1234', 'slot': '1000'}, intents[-1]


async def a_slow_listener_never_slows_a_turn():
    script = Script()
    flow, call, _ = opened(script)
    slow = Heard(delay=0.5)
    call.listen(slow)
    started = time.perf_counter()
    await offered(flow, call, script)
    assert time.perf_counter() - started < 0.5, 'the turn did not wait on its listener'
    assert len(slow.events) <= 1
    await call.events.close(timeout=0.2)
    assert len(slow.events) < 5, 'closing is bounded: a listener this slow does not hold the call'

    script = Script()
    flow, call, _ = opened(script)
    behind = Heard(delay=10)
    call._events = ev.Events(behind, 'run_full', size=3)
    await offered(flow, call, script)
    assert call.events.dropped > 0, 'a full queue drops new events and counts them'
    await call.events.close(timeout=0.05)


async def no_listener_builds_nothing():
    script = Script()
    flow, call, _ = opened(script)
    await offered(flow, call, script)
    assert call.events is None
    call.emit(ev.CallerSpeech, text='ignored')
    call.turn_snapshot()


def resumed(snapshot: TurnSnapshot, script: Script, clinic: Clinic, **edit):
    """A new call on the clinic carrying on from `snapshot`, with its facts edited by `edit`."""
    flow = core.load(HERE / MANIFEST).compile(model=script.model)
    facts = snapshot.facts | edit if edit else None
    call = flow.open(
        snapshot_state(flow, snapshot), services=clinic, clock=lambda: CLOCK, resume=snapshot, facts=facts
    )
    return flow, call


def snapshot_state(flow: core.StepFlow, snapshot: TurnSnapshot):
    return flow.state_type.model_validate(snapshot.facts)


async def a_resumed_call_sends_the_same_history():
    script = Script()
    flow, call, clinic = opened(script)
    await offered(flow, call, script)
    saved = TurnSnapshot.model_validate_json(call.snapshot().model_dump_json())
    assert saved.turn == 1 and saved.agent == 'desk' and saved.facts['service'] == 'cleaning'
    assert [line['who'] for line in saved.said()] == ['caller', 'bot']
    await turn(flow, call, script, 'Is it far?', Reply('Ten minutes away.'))
    original = script.payloads[-1]

    again = Script()
    flow2, call2 = resumed(saved, again, clinic)
    again.call = call2
    assert call2.turn == 1 and call2.state.agent == 'desk' and call2.state.facts.service == 'cleaning'
    said, _ = await turn(flow2, call2, again, 'Is it far?', Reply('Ten minutes away.'))
    assert said == ['Ten minutes away.']
    assert json.loads(again.payloads[-1].history) == json.loads(original.history), (
        'the same history reached the model: messages and the speech ledger both came back'
    )
    assert call2.turn == 2


async def a_yes_never_carries_across():
    script = Script()
    flow, call, clinic = opened(script)
    await offered(flow, call, script)
    await read_back(flow, call, script)
    saved = call.snapshot()
    assert [i.status for i in saved.intents if i.operation == 'book'] == ['prepared']

    again = Script()
    flow2, call2 = resumed(saved, again, clinic)
    assert not [i for i in call2.intents if i.operation == 'book'], (
        'a prepared action is dropped: asked again'
    )
    said, _ = await turn(flow2, call2, again, 'Yes.', Call('book'))
    assert said == [READ_BACK['1000']] and clinic.calls == 0, 'the old yes booked nothing: read back fresh'
    said, _ = await turn(flow2, call2, again, 'Yes.', Call('book'))
    assert said == ['Booked for Tuesday at 10:00 AM, reference CL001.'] and clinic.calls == 1, said


async def what_was_done_stays_done_and_a_refused_edit_opens_nothing():
    script = Script()
    flow, call, clinic = opened(script)
    await offered(flow, call, script)
    await read_back(flow, call, script)
    await turn(flow, call, script, 'Yes.', Call('book'))
    saved = call.snapshot()
    assert clinic.calls == 1

    _, call2 = resumed(saved, Script(), clinic)
    assert [i.status for i in call2.intents if i.operation == 'book'] == ['succeeded']
    assert [e.operation for e in call2.evidence if e.operation == 'book'], 'its receipt came back too'
    assert clinic.calls == 1, 'nothing was sent again'

    try:
        resumed(saved, Script(), clinic, visitors=9)
    except ValidationError as error:
        assert error.errors()[0]['loc'] == ('visitors',), error
    else:
        raise AssertionError('an edit the state refuses must open nothing')
    _, edited = resumed(saved, Script(), clinic, visitors=2)
    assert edited.state.facts.visitors == 2, 'an edit the state takes is the resumed state'


async def a_withdrawn_read_back_stays_withdrawn():
    script = Script()
    flow, call, clinic = opened(script)
    await offered(flow, call, script)
    await read_back(flow, call, script)
    intent = next(i for i in call.intents if i.operation == 'book')
    call.withdraw(intent.id)
    saved = call.snapshot()
    _, call2 = resumed(saved, Script(), clinic)
    assert [i.status for i in call2.intents if i.operation == 'book'] == ['withdrawn']


async def a_resumed_call_says_so():
    script = Script()
    flow, call, clinic = opened(script)
    await offered(flow, call, script)
    saved = call.snapshot()
    again = Script()
    flow2 = resumed(saved, again, clinic)[0]
    call2 = flow2.open(snapshot_state(flow2, saved), services=clinic, clock=lambda: CLOCK)
    heard = Heard()
    call2.listen(heard)
    flow2.resume(call2, saved)
    await call2.events.close()
    assert [(e.type, getattr(e, 'via', '')) for e in heard.events] == [('step.entered', 'resume')]


async def main():
    await every_event_in_order()
    await a_slow_listener_never_slows_a_turn()
    await no_listener_builds_nothing()
    await a_resumed_call_sends_the_same_history()
    await a_yes_never_carries_across()
    await what_was_done_stays_done_and_a_refused_edit_opens_nothing()
    await a_withdrawn_read_back_stays_withdrawn()
    await a_resumed_call_says_so()
    print(
        'events ok: in order, off the turn, a snapshot per turn, resumed with the same history, no yes carried'
    )


if __name__ == '__main__':
    asyncio.run(main())
