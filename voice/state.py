"""Typed call state.

One BaseModel holds the whole call: the variables the agents write, the message
history they read, and a handle to the caller's line. It is the agents'
``deps_type`` and it is the scope their prompt templates render against, so a
`{{placeholder}}` in a prompt and a field here cannot drift apart -- a name that
is not a field fails when the prompt is compiled, at import, rather than
rendering as nothing on a live call.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, Field

from voice import harness

AgentName = Literal['concierge', 'intake']

_E164 = re.compile(r'^\+[1-9]\d{6,14}$')


def _e164(value: str) -> str:
    # Empty is not a wrong value, it is no value yet: it is what the field holds
    # before anyone fills it. Refusing it here would deadlock the one step whose
    # job is to fill it, because it has nothing else to send.
    if value and not _E164.match(value):
        raise ValueError('expected E.164: one leading plus, then 7 to 15 digits, like +34600111222')
    return value


# AfterValidator and never a `pattern=` constraint: a pattern reaches the schema
# the model is sent, and some providers reject it. The description is how the
# shape reaches the model instead -- it travels as prose, so no strict schema
# converter strips it.
Phone = Annotated[
    str,
    AfterValidator(_e164),
    Field(description='A phone number in E.164: one leading plus, then 7 to 15 digits, like +34600111222.'),
]


class Handoff(harness.Handoff):
    to: AgentName


class CallState(harness.VoiceState):
    """Everything this call knows."""

    agent: AgentName = 'concierge'
    caller_phone: Phone = ''
