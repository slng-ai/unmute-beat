"""Faro: a bilingual language-learning companion that remembers learners across calls and restarts.

One agent step on `core` alone, with its learners, plans and sessions in a
local SQLite file (`store.py`). It exposes what `bot.py` needs from an
example, plus the two optional names Faro uses: `close_call`, run when the
line drops, and `STT_LANGUAGE` and `TTS_LANGUAGE`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.flowspec import load_file
from core.steps import Prompted
from faro import curriculum
from faro.operations import finish
from faro.state import FaroCall, open_call

NAME = 'faro'
STT_LANGUAGE = 'es'
"""Speech recognition hint: the answers to exercises are Spanish. A live call showed it transcribing English too."""
TTS_LANGUAGE = 'en'
"""Faro speaks the learner's own language, English by default. Spanish feedback is read with an English voice."""
SPEC = load_file(Path(__file__).parent / 'flow.yaml')


def __getattr__(name: str) -> Any:
    """`FLOW` and `AGENTS`, compiled on first use, so the database tools (`__main__.py`) need no model key."""
    if name not in ('FLOW', 'AGENTS'):
        raise AttributeError(name)
    from faro.conversation import MODEL, SETTINGS

    flow = SPEC.compile(model=MODEL, settings=SETTINGS)
    globals().update(FLOW=flow, AGENTS=flow.nodes)
    return globals()[name]


def GREETING(call: FaroCall) -> str | Prompted:  # noqa: N802
    """The opening line. With a caller ID it is a read-back, so the caller's "sí" answers a real question."""
    caller_id = call.state.caller_id
    words = curriculum.greeting(caller_id)
    return Prompted(about='phone', spoken=words, value=caller_id) if caller_id else words


async def close_call(call: FaroCall) -> None:
    """The line dropped: close the session with the same finaliser a goodbye uses, and no model request."""
    await finish(call, 'disconnected')


__all__ = ['AGENTS', 'FLOW', 'GREETING', 'NAME', 'STT_LANGUAGE', 'TTS_LANGUAGE', 'close_call', 'open_call']
