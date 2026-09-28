"""Run: uv run python -m tests.test_slice

The salon's identity -> read-back -> lookup path, through core's public
manifest (`tests/slice/agent.yaml`) with the salon's real phone types,
receipt and diary. Scripted model, no network. Every lookup the diary gets is
counted: none before a heard read-back of those exact digits and a yes, one
after, and none that a receipt about another number can count.
"""

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

import core
from tests.slice.slice import Caller

MANIFEST = Path(__file__).parent / 'slice' / 'agent.yaml'


class Script:
    """Each agent request records the next values it is given; each reading answers with `label`."""

    def __init__(self) -> None:
        self.values: list[dict[str, Any]] = []
        self.label = 'unclear'
        self.model = FunctionModel(self.reading, stream_function=self.agent)

    def reading(self, messages: Any, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(json.dumps({'confirmation': self.label}))])

    async def agent(self, messages: Any, info: AgentInfo):
        if self.values:
            yield {0: DeltaToolCall(name='collect', json_args=json.dumps({'values': self.values.pop(0)}))}
        else:
            yield 'Sorry, which number was that?'


def opened() -> tuple[core.StepFlow, core.FlowCall, Script, list[str]]:
    script = Script()
    flow = core.load(MANIFEST).compile(model=script.model)
    call = flow.open(Caller())
    lookups: list[str] = []
    real = call.services.find_or_create_customer

    def counted(phone: str) -> dict:
        lookups.append(phone)
        return real(phone)

    call.services.find_or_create_customer = counted
    return flow, call, script, lookups


async def turn(
    flow, call, script: Script, words: str, *values: dict[str, Any], heard: bool = True
) -> list[str]:
    script.values = list(values)
    reply = await core.text_turn(flow, call, words)
    if heard:
        call.acknowledge(*(s.id for s in reply.segments))
    return [s.text for s in reply.segments]


async def a_number_is_looked_up_once_after_its_heard_read_back():
    for heard in (False, True):
        flow, call, script, lookups = opened()
        said = await turn(
            flow, call, script, 'It is plus one 202 555 0123.', {'phone': '+1 202 555 0123'}, heard=heard
        )
        assert call.state.facts.phone == '+12025550123', 'normalized to E.164 by the salon type'
        assert said == ['Let me read that back to you: +1 202-555-0123. Have I got that right?'], said
        script.label = 'accept'
        said = await turn(flow, call, script, 'Yes.')
        if not heard:
            assert lookups == [] and call.state.facts.verified == '', 'a yes to an unheard read-back'
            continue
        assert lookups == ['+12025550123'], lookups
        assert said == ['Thanks, I have you.', 'How can I help you today?'], said
        assert call.state.facts.verified == 'created' and call.state.outcome == 'completed'


async def a_corrected_number_is_read_back_again_and_looked_up_once():
    flow, call, script, lookups = opened()
    await turn(flow, call, script, '+44 20 7946 0958.', {'phone': '+44 20 7946 0958'})
    script.label = 'revise'
    said = await turn(flow, call, script, 'No, it ends in 0959.')
    assert call.state.facts.phone == '' and lookups == [], 'the old number is forgotten, nothing looked up'
    assert said[-1].startswith('Which phone number'), said
    script.label = 'unclear'
    said = await turn(flow, call, script, '+44 20 7946 0959.', {'phone': '+44 20 7946 0959'})
    assert said == ['Let me read that back to you: +44 20 7946 0959. Have I got that right?'], said
    script.label = 'accept'
    await turn(flow, call, script, 'Yes.')
    assert lookups == ['+442079460959'], lookups


async def digits_with_no_country_are_not_recorded():
    flow, call, script, lookups = opened()
    await turn(flow, call, script, '202 555 0123.', {'phone': '202 555 0123'})
    assert call.state.facts.phone == '' and lookups == [] and call.state.agent == 'phone', call.state


async def a_receipt_about_another_number_verifies_nobody():
    flow, call, script, _ = opened()
    await turn(flow, call, script, '+34 671 273 454.', {'phone': '+34 671 273 454'})
    script.label = 'accept'
    with patch.object(
        type(call.services),
        'find_or_create_customer',
        lambda self, phone: {'customer_status': 'existing', 'customer_phone': '+34600111222'},
    ):
        call.services.__dict__.pop('find_or_create_customer', None)
        said = await turn(flow, call, script, 'Yes.')
    assert call.state.facts.verified == '' and call.state.outcome == 'failed', call.state
    assert said == ['I cannot check who you are just now.'], said
    assert not call.evidence, 'a receipt about someone else is not evidence for this caller'


async def main() -> None:
    await a_number_is_looked_up_once_after_its_heard_read_back()
    await a_corrected_number_is_read_back_again_and_looked_up_once()
    await digits_with_no_country_are_not_recorded()
    await a_receipt_about_another_number_verifies_nobody()
    print('slice ok: the salon identity path on the public manifest, one lookup after a heard yes')


if __name__ == '__main__':
    asyncio.run(main())
