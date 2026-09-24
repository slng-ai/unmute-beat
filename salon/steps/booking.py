"""Appointments: reads the diary, offers real times, and writes only what the caller agreed to."""

from core.graph import Node
from core.steps import AgentStep
from salon.models.state import SalonState
from salon.nodes import build
from salon.tools.booking import OPENINGS, ask_booking, booking_guidance, offer_times, read_diary, save_booking

__all__ = ['node']


def node(step: AgentStep) -> Node[SalonState]:
    """Speaks only through its outputs, so a booking is never said to exist before the diary holds it."""
    return build(
        step,
        tools=[read_diary],
        finishers=[save_booking],
        asks=[ask_booking, offer_times, booking_guidance],
        openings=OPENINGS,
        structured_speech=True,
    )
