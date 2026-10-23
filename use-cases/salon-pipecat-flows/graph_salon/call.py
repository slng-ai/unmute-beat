"""One call's application state and the rules that guard it.

This is where consent lives. The model reads a reply; this code decides
whether that reply may authorise anything. A proposal is put by code, in
code's words, in one caller turn; only a yes in a later turn, from the same
verified caller, to that same proposal, lets its write run, and only once.
"""

from collections.abc import Sequence

from pydantic import TypeAdapter, ValidationError

from graph_salon.models import CallState, Choice, Origin, Proposal
from salon.diary import Diary
from salon.models.shapes import SubmittedPhone

TOOL_CALLS_PER_TURN = 8
"""Core's `TurnBudget.tool_calls`: past it, the turn ends with a fixed line instead of another request."""

_PHONE = TypeAdapter(SubmittedPhone)


class RefusalError(Exception):
    """Nothing was done, and the model is told why so it can say something useful."""


def _text(content: object) -> str:
    """A context message's text, whether it is a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ' '.join(part.get('text', '') for part in content if isinstance(part, dict))
    return ''


class SalonCall:
    """The call: its diary, its identity at the diary, and its state.

    Args:
        diary: The salon.
        call_id: Whose offers these are at the diary. Two calls from one number are two conversations.
        caller: The carrier's caller ID, read back before it is trusted. Anything not a Spanish number is dropped.
    """

    def __init__(self, diary: Diary, call_id: str, caller: str = '') -> None:
        self.diary = diary
        self.id = call_id
        try:
            self.state = CallState(caller_id=_PHONE.validate_python(caller))
        except ValidationError:
            self.state = CallState()

    def commit(self, **updates: object) -> None:
        """Replace the state with one validated candidate, or raise and leave it as it was."""
        self.state = CallState.model_validate(self.state.model_dump() | updates)

    def hear(self, messages: Sequence[object]) -> None:
        """Bring the turn up to date from the context the model is reading.

        The turn is the number of caller messages in the context, so it moves
        exactly when the model's view does. Counting from Pipecat's turn events
        would race the tool call they trigger.

        The context is not all dicts. Pipecat keeps a provider's own records in
        it as `LLMSpecificMessage`: Gemini's function calls, with their thought
        signatures, are stored that way. Reading those as dicts crashed every
        tool call of the first live call (Langfuse trace 56e1b598), and the
        model talked on as if the booking were under way. A caller's words are
        always a standard message: a dict with the role `user`.

        ponytail: assumes the APPEND context strategy, which never drops a
        caller message. A strategy that resets or summarises needs its own count.
        """
        said = [m for m in messages if isinstance(m, dict) and m.get('role') == 'user']
        if len(said) != self.state.turn:
            heard = _text(said[-1].get('content')) if said else ''
            self.commit(turn=len(said), heard=heard, tool_calls=0)

    def over_budget(self) -> bool:
        """Count one tool call against this turn, and say whether the turn has spent its allowance."""
        self.commit(tool_calls=self.state.tool_calls + 1)
        return self.state.tool_calls > TOOL_CALLS_PER_TURN

    def claim(self, node: str) -> None:
        """Refuse a second action on a node the call has not left since the last one.

        Every action moves the call to another node, and Pipecat moves only
        after every call in the reply has finished (`FlowManager` holds the
        transition until then). So a second action on the node the last one
        ran on, with no move in between, is a second action in the same
        reply, and it is refused.

        ponytail: keyed by node name, which holds because no action returns
        to the node it ran on. An action that may stay needs a transition
        counter instead.
        """
        if self.state.claimed_node == node:
            raise RefusalError(
                'Nothing was done: one action per reply, and this reply has already taken one.'
            )

    def acted(self, node: str) -> None:
        """Record the node an action ran on."""
        self.commit(claimed_node=node)

    @property
    def verified(self) -> bool:
        return bool(self.state.phone)

    def propose(self, choice: Choice, question: str, origin: Origin) -> Proposal:
        """Put one question on the table. It replaces any other: there is only ever one."""
        proposal = Proposal(
            choice=choice,
            question=question,
            asked_turn=self.state.turn,
            phone=self.state.phone,
            origin=origin,
        )
        self.commit(proposal=proposal)
        return proposal

    def drop_proposal(self) -> Proposal | None:
        """Take the question off the table, and hand back what it was."""
        proposal = self.state.proposal
        self.commit(proposal=None)
        return proposal

    def approve(self, kind: str) -> Proposal:
        """The proposal a yes may authorise, taken off the table so it can only authorise once.

        Raises:
            RefusalError: If no such question is waiting, it was put in this same turn, or put to somebody else.
        """
        proposal = self.state.proposal
        if proposal is None or proposal.choice.kind != kind:
            raise RefusalError(f'Nothing was done: the caller has not been asked to agree to this {kind}.')
        if proposal.asked_turn >= self.state.turn:
            raise RefusalError('Nothing was done: the caller has not answered the question yet.')
        if proposal.phone != self.state.phone:
            raise RefusalError('Nothing was done: the question was put to a different caller. Ask again.')
        self.commit(proposal=None)
        return proposal

    def facts(self) -> str:
        """What the model is told about this call on entering a node, as `{{ facts }}`: from state, never from the model."""
        state = self.state
        lines = [
            '## Current call',
            'This caller has been identified.' if state.phone else 'Nobody has been identified yet.',
        ]
        if state.appointment:
            lines.append(
                f'The latest appointment record: {state.appointment.service}, {state.appointment.spoken}.'
            )
        if offered := self.diary.offered_to(self.id, state.phone):
            lines.append('Times just offered, with their slot ids:')
            lines.extend(f'- {slot["spoken"]}: `{slot["slot_id"]}`' for slot in offered)
        return '\n'.join(lines)
