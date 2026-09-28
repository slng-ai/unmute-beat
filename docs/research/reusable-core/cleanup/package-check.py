"""The cleanup session's addition to the package proof, run in the environment the wheel was installed into.

    docs/research/reusable-core/session-4d-correctness/package-proof.sh WORK
    cd / && env -i PATH=... PYTHONPATH=WORK/app WORK/app/.venv/bin/python <this file>

Checks what `package-scenario.py` does not: that the wheel carries core's
browser page (`serve_client` serves it at /voice/), and that the flow rules
session 5 added load and run from an installed core, on the support desk's
routed manifest, with no provider key and no checkout on the path.
"""

import asyncio
import importlib.resources
import sys
import tempfile
from pathlib import Path

import pydantic_ai.models
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

import core  # noqa: E402

assert 'site-packages' in core.__file__, f'core came from {core.__file__}, not the installed wheel'
client = importlib.resources.files('core') / 'client'
for name in ('index.html', 'playout.js', 'sdk.js'):
    assert (client / name).is_file(), f'the wheel has no client/{name}'
assert b'setAboutClient' in (client / 'playout.js').read_bytes(), 'playout.js does not declare the capability'
retired = ('classifiers', 'readings', 'conversation', 'interpreters', 'desk', 'decisions')
assert not any((importlib.resources.files('core') / f'{name}.py').is_file() for name in retired), (
    'a retired module shipped'
)

app = next(Path(p) for p in sys.path if (Path(p) / 'ticket_flow' / 'routed.yaml').is_file())
from ticket_flow.tickets import Desk, RoutedState  # noqa: E402

moves = [[('collect', {'values': {'account_id': 'ACC-1234', 'ticket_id': 'T-101'}})]]


async def stream(messages, info):
    yield {
        i: DeltaToolCall(name=n, json_args=str(a).replace("'", '"')) for i, (n, a) in enumerate(moves.pop(0))
    }


async def main() -> None:
    flow = core.load(app / 'ticket_flow' / 'routed.yaml').compile(model=FunctionModel(stream_function=stream))
    desk = Desk(Path(tempfile.mkdtemp()) / 'desk.db')
    desk.seed('ACC-1234', 'T-101')
    call = flow.open(RoutedState(), services=desk)
    desk.language = lambda: call.state.facts.idioma
    reply = await core.text_turn(flow, call, 'Cuenta ACC-1234, ticket T-101.')
    assert [s.text for s in reply.segments] == ['Tengo el ticket T-101.'], reply.segments
    assert call.state.agent == 'resolve', 'the receipt moved the caller, by code, from an installed core'


asyncio.run(main())
print(f'package check ok: client page in the wheel ({client}), no retired module, routed flow runs')
