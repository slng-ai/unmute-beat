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


# Deliberately *not* the salon's Spain-only type, which is a business rule
# belonging to one agency and not to this two-agent demo. This one stays a
# generic E.164 shape check: it is what the demo's own tests pin, and there is
# no country here to validate a national plan against. The two look alike and
# are not the same rule -- the salon's refuses +348340464 because Spanish
# numbers have nine national digits, which is a fact about Spain and nothing
# this file can know.
#
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
    """The business facts of this call."""

    agent: AgentName = 'concierge'
    pending: AgentName | None = None
    caller_phone: Phone = ''


class Call(harness.CallContext):
    """This call's state, history and line. The agents' `deps`."""

    state: CallState = Field(default_factory=CallState)
