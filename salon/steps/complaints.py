"""Complaints: reads the complaint back, and records it only once the caller approves that readback."""

from core.graph import Node
from core.steps import AgentStep
from salon.models.state import SalonState
from salon.nodes import build, complaint_ready
from salon.tools.complaints import ask_complaint, complaint_goodbye, complaint_guidance, record_complaint
from salon.tools.desk import hang_up

__all__ = ['node']


def node(step: AgentStep) -> Node[SalonState]:
    """`record_complaint` is offered only once approved; its goodbye is its own, under the shared name."""
    return build(
        step,
        finishers=[hang_up, record_complaint],
        asks=[ask_complaint, complaint_guidance],
        structured_speech=True,
        offered_when={'record_complaint': complaint_ready},
        stand_ins={hang_up: complaint_goodbye},
    )
