"""Appointments: reads the diary, offers real times, and writes only what the caller agreed to."""

from core.graph import Node
from core.steps import AgentStep
from salon.models.state import SalonCall, SalonState
from salon.nodes import build
from salon.tools.booking import OPENINGS, ask_booking, booking_guidance, offer_times, read_diary, save_booking

__all__ = ['node']


def _read_back_waiting(call: SalonCall) -> bool:
    """`save_booking` is offered only while a booking read-back, or the move-or-add question, waits on an answer.

    Without one it is always refused, and the refusal cost a request: the
    user's call of 24 September called it for a move nothing had read back,
    was refused, then asked. A tool the model cannot call is never refused.
    """
    return call.pending('booking') is not None or call.pending('move_or_add') is not None


def node(step: AgentStep) -> Node[SalonState]:
    """Speaks only through its outputs, so a booking is never said to exist before the diary holds it."""
    return build(
        step,
        tools=[read_diary],
        finishers=[save_booking],
        asks=[ask_booking, offer_times, booking_guidance],
        openings=OPENINGS,
        structured_speech=True,
        offered_when={'save_booking': _read_back_waiting},
    )
