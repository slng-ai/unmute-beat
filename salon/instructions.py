"""Each step's instructions, compiled against the call at import.

`salon/flow.yaml` names one Handlebars prompt per step in `prompts/`, the
voice rules every step shares, the diary rules two steps share, and the
salon's own documents. They are typed against `SalonCall`, so a
`{{placeholder}}` naming no field raises here and the process never starts,
rather than rendering as an empty string into a live call.

`<!--diary-->`, `<!--services-->` and `<!--refunds-->` are plain
substitutions (the flow's `includes`) rather than Handlebars partials: one
line each, nothing to register, and the rules they carry are fixed in one file
instead of in four prompts that slowly disagree. The documents used to be a
retrieval tool, and a caller moving an appointment cost seven consecutive
lookups rewording one question. Seven hundred words is small enough to simply
be in the prompt, and a document already in front of the model cannot be
searched for badly.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic_ai import TemplateStr

from core.flowspec import load_file
from salon.models.state import SalonCall

FOLDER = Path(__file__).resolve().parent
SPEC = load_file(FOLDER / 'flow.yaml')
"""The salon as a flow file: every step's prompt, module, exits and requirements."""

GREETING = SPEC.greeting

COMPLAINT_GUIDANCE: dict[str, str] = json.loads(
    (FOLDER / 'prompts/knowledge/complaint-guidance.json').read_text()
)
"""The approved sentences the complaints step speaks for each policy topic."""


def instructions(name: str) -> TemplateStr[SalonCall]:
    """Shared voice guidance, then the step's own role, then the caller's latest words."""
    return SPEC.prompt(name)
