"""One call a runner carries, from admission to its end, and the listener that sends its events to the studio."""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

from core.events import Event
from voice_runner.link import Link
from voice_runner.settings import CallRequest


@dataclass
class Carried:
    """One call this process carries, from admission to its end.

    `conversation` is a text call's root span, so its turns hang from it and one run is one trace. `flow`
    is the flow it runs on, kept for its whole life even if the agent is compiled again.
    """

    id: str
    agent: str
    channel: Literal['voice', 'text']
    request: CallRequest
    created: float = field(default_factory=time.monotonic)
    status: Literal['waiting', 'active', 'ended'] = 'waiting'
    call: Any = None
    task: asyncio.Task[None] | None = None
    connection: SmallWebRTCConnection | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    conversation: Any = None
    flow: Any = None
    step: str = ''
    outcome: str = ''
    last_seq: int = -1
    ended_sent: bool = False

    def note(self, event: Event) -> None:
        """What an event says about the call: its last number, its step, its end."""
        self.last_seq = max(self.last_seq, event.seq)
        if event.type == 'step.entered':
            self.step = event.step
        elif event.type == 'run.ended':
            self.outcome, self.ended_sent = event.outcome, True

    def summary(self) -> dict[str, Any]:
        """The call as `/stats` lists it."""
        return {
            'id': self.id,
            'agent': self.agent,
            'channel': self.channel,
            'status': self.status,
            'step': self.step,
        }


class Forward:
    """A call's listener: each event goes on the runner's link to the studio, and into the call's own record."""

    def __init__(self, link: Link, carried: Carried) -> None:
        self.link = link
        self.carried = carried

    async def event(self, event: Event) -> None:
        """Send one event on, and note it."""
        self.link.send(event)
        self.carried.note(event)
