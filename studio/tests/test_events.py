"""Run: uv run python -m tests.test_events

The run format is strict: one discriminated union, numbered, no extra keys. What goes on the
stream comes back the same, and the JSON schema the web types mirror names every event.
"""

from pydantic import ValidationError

from studio.events import EVENT, BotSpeech, IntentChanged, ModelDelta, RunStarted, StateCommitted


def an_event_comes_back_as_it_went():
    sent = [
        RunStarted(
            run='run_1',
            seq=0,
            turn=0,
            t_ms=0,
            agent='desk',
            config='a41c',
            channel='text',
            entry='front',
            state={},
        ),
        ModelDelta(run='run_1', seq=1, turn=1, t_ms=812.5, step='front', text='Hello'),
        StateCommitted(run='run_1', seq=2, turn=1, t_ms=900, state={'topic': 'billing'}, changed=['topic']),
        IntentChanged(
            run='run_1', seq=3, turn=2, t_ms=1500, intent='i_1', operation='book', status='prepared'
        ),
        BotSpeech(
            run='run_1', seq=4, turn=2, t_ms=1600, segment='s_1', phase='delivered', evidence='mark'
        ),
    ]
    for event in sent:
        back = EVENT.validate_json(event.model_dump_json())
        assert back == event and type(back) is type(event), (event, back)


def a_stray_key_or_a_gap_is_refused():
    line = ModelDelta(run='r', seq=0, turn=1, t_ms=0, step='front', text='x').model_dump()
    for broken in ({**line, 'tool_args': '{}'}, {**line, 'seq': -1}, {**line, 'type': 'model.thought'}):
        try:
            EVENT.validate_python(broken)
        except ValidationError:
            continue
        raise AssertionError(f'accepted {broken}')


def the_schema_names_every_event():
    schema = EVENT.json_schema()
    types = {
        definition['properties']['type']['const']
        for definition in schema['$defs'].values()
        if 'type' in definition.get('properties', {})
    }
    expected = {
        'run.started', 'caller.speech', 'step.entered', 'model.request', 'model.delta', 'model.decision',
        'model.ended', 'state.committed', 'intent.changed', 'receipt', 'bot.speech', 'turn.finished',
        'turn.snapshot', 'run.ended',
    }  # fmt: skip
    assert types == expected, types ^ expected


if __name__ == '__main__':
    an_event_comes_back_as_it_went()
    a_stray_key_or_a_gap_is_refused()
    the_schema_names_every_event()
    print('events ok: round trip, strict, every type in the schema')
