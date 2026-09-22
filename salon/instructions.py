"""Each step's instructions, compiled against the call at import.

`prompts/` holds one Handlebars template per step, the voice rules every step
shares, the diary rules two steps share, and the salon's own documents. They
are typed against `SalonCall`, so a `{{placeholder}}` naming no field raises
here and the process never starts, rather than rendering as an empty string
into a live call.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic_ai import TemplateStr

from salon.models.state import SalonCall

PROMPTS = Path(__file__).resolve().parent.parent / 'prompts'
KNOWLEDGE = PROMPTS / 'knowledge'

GREETING = "Hi, you've reached Sage and Stone. Robin speaking, what can I do for you?"

_VOICE = (PROMPTS / '_voice.md').read_text()
_DIARY = (PROMPTS / '_diary.md').read_text()
_DOCS = {name: (KNOWLEDGE / f'{name}.md').read_text() for name in ('services', 'refunds')}
"""The salon's own documents, substituted into the prompts that answer from them.

They used to be a retrieval tool, and a caller moving an appointment cost
seven consecutive lookups rewording one question. Seven hundred words is small
enough to simply be in the prompt, and a document already in front of the
model cannot be searched for badly.
"""
COMPLAINT_GUIDANCE: dict[str, str] = json.loads((KNOWLEDGE / 'complaint-guidance.json').read_text())
"""The approved sentences the complaints step speaks for each policy topic."""


def instructions(name: str) -> TemplateStr[SalonCall]:
    """Shared voice guidance, then the step's own role, then the caller's latest words.

    `<!--diary-->`, `<!--services-->` and `<!--refunds-->` are plain
    substitutions rather than Handlebars partials: one line each, nothing to
    register, and the rules they carry are fixed in one file instead of in
    four prompts that slowly disagree.
    """
    text = (PROMPTS / f'{name}.md').read_text().replace('<!--diary-->', _DIARY)
    for topic, document in _DOCS.items():
        text = text.replace(f'<!--{topic}-->', document)
    return TemplateStr(
        f'{_VOICE}\n\n{text}\n\n## Latest caller message\n\n'
        'Use this with the conversation above, including earlier speech fragments.\n\n> {{heard}}',
        deps_type=SalonCall,
    )
