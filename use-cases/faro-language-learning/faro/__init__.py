"""Faro: a bilingual language-learning companion that remembers learners across calls and restarts.

A flow folder on core's public contract, with its learners, plans and
sessions in a local SQLite file (`store.py`):

    flow = faro.FLOW                                   # agent.yaml, compiled on first use
    call = faro.open_call(flow, '+34600111222', store)  # one session per call
    core.greet(call, faro.greeting(flow, call))        # a caller ID is read back in the greeting
    ...
    await faro.close_call(call)                        # when the line drops: the session is closed once

What Faro wrote is `agent.yaml`, `state.py`, `functions.py`, `prompts/`,
`curriculum.py`, `models.py` and `store.py`. How a turn runs, how a reply
is read, what authorises a write and what is said about it are core's.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import core
from faro import curriculum
from faro.functions import Faro, finish
from faro.models import phone_in
from faro.state import FaroState
from faro.store import Store, store

NAME = 'faro'
STT_LANGUAGE = 'es'
"""Speech recognition hint: the answers to exercises are Spanish. A live call showed it transcribing English too."""
TTS_LANGUAGE = 'en'
"""Faro speaks the learner's own language, English by default. Spanish feedback is read with an English voice."""
SPEC = core.load(Path(__file__).with_name('agent.yaml'))
"""The manifest, checked whole at import: an unknown key, a missing file or a field the state lacks fails here."""


def __getattr__(name: str) -> Any:
    """`FLOW`, compiled on first use, so the database tools (`__main__.py`) need no model key."""
    if name != 'FLOW':
        raise AttributeError(name)
    globals()['FLOW'] = SPEC.compile()
    return globals()['FLOW']


def open_call(
    flow: core.StepFlow,
    caller_id: str = '',
    learning: Store | None = None,
    clock: Callable[[], datetime] | None = None,
) -> core.FlowCall:
    """A new connection. A caller ID that is not a valid Spanish number is dropped, never guessed at.

    The carrier's number is only the phone to read back: nothing is looked up
    until the caller says yes to it. The services read the call's own facts,
    so the words code says follow the language the call is in.
    """
    faro = Faro(learning if learning is not None else store())
    call = flow.open(FaroState(phone=phone_in(caller_id)), services=faro, clock=clock)
    faro.facts = lambda: call.state.facts
    return call


def greeting(flow: core.StepFlow, call: core.FlowCall) -> str | Any:
    """The opening line: Faro's greeting, then the carrier's number read back, or the question for a number."""
    return flow.greeting(call, curriculum.GREETING)


async def close_call(call: core.FlowCall) -> bool:
    """The call ended: close the session with the summary code writes, once, and no model request."""
    outcome = 'goodbye' if call.ended == 'caller' else 'disconnected'
    return await finish(call.services, call.state.facts, outcome)


__all__ = ['NAME', 'SPEC', 'STT_LANGUAGE', 'TTS_LANGUAGE', 'close_call', 'greeting', 'open_call']
