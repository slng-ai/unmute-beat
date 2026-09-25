"""Run: uv run python -m tests.test_baseline

The one-prompt Pipecat baseline, with no model: its prompt is built from the
salon's own files, and its tools reach the diary only for an identified caller.

Asserts, no pytest, no fixtures, same as the rest.
"""

import asyncio
from types import SimpleNamespace

from salon import baseline
from salon.diary import Diary


def tool_calls():
    """Each tool run with a fake Pipecat call, returning what it handed back."""
    desk = baseline.Desk(Diary(), 'call-1')
    tools = {tool.__name__: tool for tool in desk.tools()}
    frames: list = []

    async def run(name: str, **arguments):
        results: list = []

        async def result_callback(result, properties=None):
            results.append(result)

        async def push_frame(frame):
            frames.append(frame)

        params = SimpleNamespace(result_callback=result_callback, llm=SimpleNamespace(push_frame=push_frame))
        await tools[name](params, **arguments)
        return results[0]

    return desk, frames, lambda name, **arguments: asyncio.run(run(name, **arguments))


def the_prompt_is_the_salons_own_words():
    prompt = baseline.system_prompt()
    assert 'Robin' in prompt and '## Opening hours' in prompt, 'voice rules and services are in the prompt'
    assert 'ask_booking' not in prompt, 'the baseline names only its own tools'
    assert '## Actions' in prompt and 'runtime supplies' not in prompt, (
        "the harness's Actions section is replaced"
    )


def nothing_is_read_or_written_before_the_number_is_confirmed():
    desk, _, call = tool_calls()
    assert call('read_diary', day='Friday', service='haircut') == {'error': 'Identify the caller first.'}
    assert 'error' in call('confirm_customer', phone='+447700900123'), 'only Spanish numbers'
    assert not desk.phone
    assert call('confirm_customer', phone='+34611111111')['customer_status'] == 'created'
    slots = call('read_diary', day='Friday', service='haircut')['slots']
    booked = call('save_booking', action='book', slot_id=slots[0]['slot_id'])
    assert booked['status'] == 'booked', booked
    assert 'error' in call('save_booking', action='book', slot_id='made-up'), 'a refusal is said, not raised'


def ending_the_call_says_goodbye_then_closes():
    _, frames, call = tool_calls()
    call('end_call', goodbye='Bye now.')
    assert [type(f).__name__ for f in frames] == ['TTSSpeakFrame', 'EndWorkerFrame'], frames


the_prompt_is_the_salons_own_words()
nothing_is_read_or_written_before_the_number_is_confirmed()
ending_the_call_says_goodbye_then_closes()
print('ok')
