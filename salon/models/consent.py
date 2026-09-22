"""What the caller actually said, and the one question they are answering.

Closed word lists over the accepted utterance, not language understanding.
Every one of them fails towards *asking*: an utterance none of these
recognises is a reason to put the question again, never a reason to act. So
the cost of a miss is one clarifying turn, and the cost of the false positive a
permissive "does it contain yes" check would buy is a booking nobody asked for.

ponytail: regex, not a classifier. Upgrade path, if live calls show too many
clarifications: run a small model *only* on the utterances these return
nothing for, and keep them as the fast path.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from core.state import STRICT

_YES = re.compile(
    r'^(?:go for it(?=[.!]*$)|'
    r'yes|yeah|yep|yup|aye|sure|please|perfect|great|exactly|correct|right|'
    r'absolutely|certainly|that would be (?:great|good|fine|perfect)|ok|okay|k|go ahead|do it|book it|sounds (?:good|right|fine|great)|'
    r"that(?:'s| is|s) (?:right|correct|it|fine|great|perfect)|that works|"
    r"i(?:'m| am) happy (?:with that|enough))\b",
    re.IGNORECASE,
)
"""Anchored at the start: "yes" has to be the answer, not a word inside one."""
NOT_YES = re.compile(
    r"\b(?:no|nope|nah|not|don'?t|but|actually|instead|change|wait|hold on|rather|"
    r'different|another|else|sorry|maybe|unless|though)\b',
    re.IGNORECASE,
)
"""Anywhere in it: a yes with one of these attached is answering some other question, or changing this one."""
_BYE = re.compile(
    r"\b(?:bye|goodbye|good ?bye|see you|that(?:'s| is|s) (?:all|everything|it)|"
    r"no,? thanks|no thank you|nothing else|no more|i(?:'m| am) (?:good|fine|all set|done|done thanks)|"
    r"we(?:'re| are) done|thanks?,? (?:that(?:'s| is) )?all)\b",
    re.IGNORECASE,
)
_STILL_ASKING = re.compile(
    r'\?|\b(?:book|booking|move|moving|switch|change|cancel|reschedule|complain|complaint|'
    r'can you|could you|would you|what|when|where|how|why|which|who|also|another|instead|'
    r'but|actually|still|help|about)\b',
    re.IGNORECASE,
)
"""A goodbye with any of these in it is not only a goodbye: a question is never the end of a call."""
_MOVE_IT = re.compile(
    r'\b(?:move|moving|switch|swap|change|reschedule|instead of|rather than|replace)\b', re.IGNORECASE
)
_ADD_ONE = re.compile(
    r'\b(?:another|second|additional|extra|as well|also|on top|both|two appointments|'
    r'keep (?:the|that|it|both))\b',
    re.IGNORECASE,
)


def agrees(heard: str) -> bool:
    """Whether this utterance is the caller agreeing to the question they were asked.

    Not "does it contain a yes": a yes to "which country are you calling from"
    is not agreement to a phone number nobody read back. Bounded in length, so
    a yes buried at the front of a paragraph of new instructions is not read as
    an answer to anything.
    """
    text = heard.strip()
    return bool(text) and len(text.split()) <= 12 and bool(_YES.match(text)) and not NOT_YES.search(text)


def ends(heard: str) -> bool:
    """Whether the caller's own words say the call is over.

    A question is never one, however tired it sounds, and neither is a goodbye
    with a request attached to it.
    """
    text = heard.strip()
    return bool(text) and bool(_BYE.search(text)) and not _STILL_ASKING.search(text)


def move_or_add(heard: str) -> Literal['move', 'add', '']:
    """Which of the two the caller asked for, or neither, which means ask them."""
    moving, adding = bool(_MOVE_IT.search(heard)), bool(_ADD_ONE.search(heard))
    if moving == adding:
        return ''
    return 'move' if moving else 'add'


class Asked(BaseModel):
    """A question this call put to the caller, and is waiting on the answer to.

    Application-owned. A tool may *propose* the question; nothing a model
    writes can create one, and nothing it writes can claim one was answered.
    The turn it was asked on is part of it, so an agreement two turns later is
    not an answer to it.
    """

    model_config = STRICT

    about: Literal['phone', 'booking', 'move_or_add', 'complaint']
    value: str = ''
    """The exact thing agreement would be agreement *to*: the number read back, the slot offered, or the complaint summary's digest."""
    resolution: str = ''
    summary: str = ''
    requested_resolution: str = ''
    owner: str = ''
    """The caller identity the question was put under. A question put to one number is not answered by another."""
    action: Literal['book', 'move', 'cancel', ''] = ''
    booking_id: str = ''
    turn: int = 0
    """The caller turn it went out on. Set by `SalonCall.ask`, never by a tool."""
