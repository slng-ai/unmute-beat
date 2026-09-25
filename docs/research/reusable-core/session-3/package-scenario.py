"""The installed-wheel scenario, session 3: three clinic manifests and the salon slice, offline, from any directory.

Run by `package-proof.sh` with the working directory at `/`, inside a fresh
virtual environment holding only the core wheel and the phone types. Every
manifest is found through `importlib.resources`, never a relative path. It
asserts `core` is the installed copy, checks and compiles every manifest,
books once in each clinic pattern, looks the salon caller up once, and that
no Pipecat, Langfuse or dotenv module was loaded.
"""

import asyncio
import json
import os
import sys
from importlib.resources import files

import pydantic_ai.models
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

from clinic_flow.clinic import ClinicState  # noqa: E402
from tests.slice.slice import Caller  # noqa: E402

import core  # noqa: E402

assert 'site-packages' in core.__file__ and '.claude' not in core.__file__, core.__file__
assert os.getcwd() == '/', os.getcwd()
print('core from', core.__file__)

script: dict = {'moves': [], 'label': 'unclear'}


def reading(messages, info):
    return ModelResponse(parts=[TextPart(json.dumps({'confirmation': script['label']}))])


async def agent(messages, info):
    if not script['moves']:
        yield 'Sorry?'
        return
    kind, *rest = script['moves'].pop(0)
    if kind == 'collect':
        yield {0: DeltaToolCall(name='collect', json_args=json.dumps({'values': rest[0]}))}
    else:
        yield {0: DeltaToolCall(name=rest[0], json_args=json.dumps(rest[1]))}


async def say(flow, call, words, *moves, label='unclear'):
    script.update(moves=list(moves), label=label)
    reply = await core.text_turn(flow, call, words)
    call.acknowledge(*(s.id for s in reply.segments))
    return [s.text for s in reply.segments]


async def main() -> None:
    model = FunctionModel(reading, stream_function=agent)
    fill = (('call', 'options', {}), ('collect', {'patient': 'P-1234', 'slot': '1000'}), ('call', 'book', {}))
    for manifest in ('agent.yaml', 'one_agent.yaml', 'handoff.yaml'):
        spec = core.load(files('clinic_flow') / manifest)
        assert spec.problems() == [], (manifest, spec.problems())
        flow = spec.compile(model=model)
        call = flow.open(ClinicState())
        if manifest == 'agent.yaml':
            await say(flow, call, 'P-1234.', ('collect', {'patient': 'P-1234'}))
            await say(flow, call, 'Ten.', ('collect', {'slot': '1000'}))
        elif manifest == 'one_agent.yaml':
            await say(flow, call, 'P-1234, ten.', *fill)
        else:
            await say(flow, call, 'P-1234, ten.', ('call', 'hand_over', {'to': 'booking', 'because': 'booking'}), *fill)
        heard = await say(flow, call, 'Yes.', label='accept')
        assert call.services.bookings == {'CL001': ('P-1234', '1000')}, (manifest, call.services.bookings)
        print(manifest, 'booked once:', heard[0])

    flow = core.load(files('tests.slice') / 'agent.yaml').compile(model=model)
    call = flow.open(Caller())
    await say(flow, call, '+1 202 555 0123.', ('collect', {'phone': '+1 202 555 0123'}))
    heard = await say(flow, call, 'Yes.', label='accept')
    assert call.state.facts.verified == 'created' and heard[0] == 'Thanks, I have you.', (call.state, heard)
    print('salon slice looked up once:', call.state.facts.phone)

    blocked = sorted(m for m in sys.modules if m.split('.')[0] in ('pipecat', 'langfuse', 'dotenv'))
    assert blocked == [], blocked
    print('installed scenario ok')


asyncio.run(main())
