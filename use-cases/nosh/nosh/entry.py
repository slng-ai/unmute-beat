"""Nosh, as every launcher needs it (`core.Entry`): its flow, its place tools, how a call opens, what it says first."""

from loguru import logger

import core
from nosh import app
from nosh.tools import Tools

TOOLS = Tools()
"""The place tools on Render, shared by every call of this process: a client holds no call's state."""


def wake() -> None:
    """Wake the Render service before the caller's first answer needs it: its free tier sleeps when idle.

    One cached lookup, which Google is not asked again for. A failure here is
    only logged: the call's own lookups say what went wrong in their own words.
    """
    try:
        TOOLS.post('resolve-location', {'query': 'Barcelona'})
    except Exception as error:
        logger.warning(f'the place tools did not answer the wake-up: {error}')


ENTRY = core.Entry(
    id=app.SPEC.id or 'nosh',
    name='nosh call',
    flow=lambda: app.FLOW,
    open=lambda flow, opening: app.open_call(flow, TOOLS),
    greeting=app.greeting,
    started=wake,
)
