"""Identity: takes a number, reads it back, and looks the caller up only once they agree it is right."""

from core.graph import Node
from core.steps import AgentStep
from salon.models.state import SalonState
from salon.nodes import build, verified
from salon.tools.identity import OPENINGS, ask_number, confirm_customer, stop_verifying, verification_guidance

__all__ = ['node', 'verified']


def node(step: AgentStep) -> Node[SalonState]:
    """Speaks only through its outputs, so nothing it says can claim a lookup that did not happen."""
    return build(
        step,
        finishers=[confirm_customer, stop_verifying],
        asks=[ask_number, verification_guidance],
        openings=OPENINGS,
        structured_speech=True,
    )
