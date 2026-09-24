"""A floor to beat: the reply read with a handful of exact words and no model.

Never used on a call. It answers the question "how much of this does a model
actually earn?": every case it gets right is one an interpreter must not get
wrong, and every case it cannot say is where the model has to add value.
Deliberately literal, so it is safe rather than clever: anything but an exact
match is `NoDecision`, a yes with a question mark is not a yes, and a yes
cannot choose between moving and adding.
"""

import re

from core.decisions import Accept, EndCall, NoDecision, Reject
from salon.models.consent import SalonSolution, SolverInput

YES = frozenset(
    {'yes', 'yes please', 'yeah', 'sí', 'si', 'correct', 'correcto', 'sí correcto', 'go ahead', 'go for it'}
)
NO = frozenset({'no', 'no thanks', 'no thank you', 'no gracias'})
BYE = frozenset(
    {'bye', 'goodbye', 'take care bye', 'adiós', "that's everything thank you", 'no gracias eso es todo'}
)
_PUNCTUATION = re.compile(r"[^\w\s']")


def words(utterance: str) -> str:
    """The utterance lower-cased with punctuation and extra spaces removed."""
    return ' '.join(_PUNCTUATION.sub(' ', utterance.casefold()).split())


def decide(value: SolverInput) -> SalonSolution:
    """One exact-word decision, or nothing."""
    said = words(value.answer)
    closing = value.proposal is not None and value.proposal.about == 'closing'
    if said in BYE or (closing and said in NO):
        return SalonSolution(decision=EndCall())
    if value.proposal is None or not value.delivered:
        return SalonSolution(decision=NoDecision())
    choice = value.proposal.about in ('closing', 'move_or_add')
    if said in YES and not choice and '?' not in value.answer:
        return SalonSolution(decision=Accept())
    if said in NO:
        return SalonSolution(decision=Reject())
    return SalonSolution(decision=NoDecision())
