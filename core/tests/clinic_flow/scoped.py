"""What the scoped clinic (`scoped.yaml`) adds to `consult.py`: a booking with a recap, and a trusted callback.

For `tests.test_context`: a recap is shown only to the step that owns the
booking, and a native capability's instructions are application code, which
the scoped view does not sandbox: what it writes reaches the model.
"""

from typing import Any

from pydantic_ai.capabilities import Capability

from tests.clinic_flow.actions import Book


class RecappedBook(Book):
    """The clinic's booking, with the words a goodbye may recap it in."""

    recap = 'your visit {when}'


def _notes(ctx: Any) -> str:
    return f'Front desk notes on file: {ctx.deps.state.facts.note or "none"}.'


notes = Capability(instructions=_notes)
"""Trusted code with the whole call: it reads `note`, which `front` is not shown, and says it anyway."""
