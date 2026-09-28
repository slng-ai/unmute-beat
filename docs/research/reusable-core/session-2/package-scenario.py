"""The installed-wheel scenario: the clinic slice, copied next to this file, books once offline.

Run by `package-proof.sh` inside a fresh virtual environment that holds only
the wheel and its declared dependencies. It asserts `core` is the installed
copy, checks the flow, and books one appointment with a scripted model: no
key, no network, no Pipecat, no Langfuse.
"""

import asyncio
import json
import sys
from pathlib import Path

import pydantic_ai.models
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

from clinic_flow.clinic import Backend, ClinicState  # noqa: E402

import core  # noqa: E402

assert 'site-packages' in core.__file__ and '.claude' not in core.__file__, core.__file__
print('core from', core.__file__)

script = {'values': {}, 'label': 'unclear'}


def reading(messages, info):
    return ModelResponse(parts=[TextPart(json.dumps({'confirmation': script['label']}))])


async def agent(messages, info):
    values, script['values'] = script['values'], {}
    if values:
        yield {0: DeltaToolCall(name='collect', json_args=json.dumps({'values': values}))}
    else:
        yield 'Sorry?'


async def main() -> None:
    spec = core.load(Path(__file__).parent / 'clinic_flow' / 'flow.yaml')
    assert spec.problems() == [], spec.problems()
    flow = spec.compile(model=FunctionModel(reading, stream_function=agent))
    backend = Backend()
    call = flow.open(ClinicState(), services=backend)
    for words, values, label in (
        ('P-1234.', {'patient': 'P-1234'}, 'unclear'),
        ('Ten.', {'slot': '1000'}, 'unclear'),
        ('Yes.', {}, 'accept'),
    ):
        script.update(values=values, label=label)
        reply = await core.text_turn(flow, call, words)
        call.acknowledge(*(s.id for s in reply.segments))
    assert backend.bookings == {'CL001': ('P-1234', '1000')}, backend.bookings
    assert call.state.outcome == 'completed' and len(call.evidence) == 1
    blocked = sorted(m for m in sys.modules if m.split('.')[0] in ('pipecat', 'langfuse', 'dotenv'))
    assert blocked == [], blocked
    print('installed scenario ok:', reply.segments[-1].text)


asyncio.run(main())
