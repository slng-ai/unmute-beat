"""The front desk: answers from the salon's documents and hands the caller to whoever can help."""

from core.graph import Node
from core.steps import AgentStep
from salon.models.state import SalonState
from salon.nodes import build, verified
from salon.tools.booking import read_diary
from salon.tools.desk import hang_up

__all__ = ['node', 'read_diary', 'verified']


def node(step: AgentStep) -> Node[SalonState]:
    """Speaks freely; its only way to finish is to say goodbye. `read_diary` and `verified` are its exits' own."""
    return build(step, finishers=[hang_up])
