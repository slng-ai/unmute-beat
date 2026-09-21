"""Look things up in the salon's own documents.

# ponytail: word overlap over `##` sections, not embeddings. Two documents, a
# few dozen sections, and every query is one of maybe thirty things a caller
# asks. Swap in pydantic_ai.embeddings when a third document arrives or when a
# real query misses -- the tool's contract (ordered best first, each with a
# score) is already the one a vector store would fill.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / 'knowledge'

# Words that match everything and therefore rank nothing.
_NOISE = frozenset(
    [
        'a',
        'an',
        'and',
        'are',
        'as',
        'at',
        'be',
        'by',
        'can',
        'do',
        'does',
        'for',
        'from',
        'get',
        'has',
        'have',
        'how',
        'i',
        'if',
        'in',
        'is',
        'it',
        'me',
        'my',
        'of',
        'on',
        'or',
        'our',
        'so',
        'that',
        'the',
        'their',
        'there',
        'they',
        'this',
        'to',
        'we',
        'what',
        'when',
        'where',
        'which',
        'who',
        'why',
        'will',
        'with',
        'you',
        'your',
    ]
)


def _words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z']+", text.casefold()) if word not in _NOISE}


@cache
def _sections(topic: str) -> list[tuple[str, str, frozenset[str]]]:
    """Every `## heading` section of one document, with its searchable words.

    Cached per topic, so the documents are read and split once per process
    rather than once per lookup. A missing document raises here, on the first
    call, rather than answering a caller with silence.
    """
    text = (DOCS / f'{topic}.md').read_text()
    sections = []
    for block in re.split(r'^## ', text, flags=re.MULTILINE)[1:]:
        heading, _, body = block.partition('\n')
        heading, body = heading.strip(), body.strip()
        # The heading counts twice: "Refunds" in a heading is a stronger signal
        # than "refunds" once in a paragraph about something else.
        sections.append((heading, body, frozenset(_words(heading + ' ' + heading + ' ' + body))))
    return sections


def look_up(topic: str, query: str, limit: int = 3) -> dict:
    """The sections of one document that best answer a question, best first."""
    asked = _words(query)
    scored = []
    for heading, body, words in _sections(topic):
        hits = asked & words
        if hits:
            # Share of the question this section covers. Bounded 0..1, so the
            # number means the same thing across documents and query lengths.
            scored.append((round(len(hits) / len(asked), 2), heading, body))
    scored.sort(reverse=True)
    return {
        'results': [
            {'score': score, 'heading': heading, 'text': body} for score, heading, body in scored[:limit]
        ]
    }
