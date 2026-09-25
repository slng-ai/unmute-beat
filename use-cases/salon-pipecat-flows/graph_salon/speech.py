"""What code says, and the two processors between the model and TTS.

Every read-back and every receipt is worded here and spoken by the `say` action.
The model never words a question a write depends on, and never announces a
write: the diary's receipt does. The wording is the harness's own
(`salon/tools`), so a difference in a Coval run is not a difference in words.
`test_graph_salon.py` holds the two together.

`EmptyReplyGuard` makes sure every model turn says or does something, and
`ClaimFilter` makes sure what it says claims nothing code did not do.
"""

import re
from collections.abc import Awaitable, Callable
from typing import Literal

from pipecat.frames.frames import (
    Frame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from graph_salon.call import SalonCall
from salon.models.receipts import Saved

GREETING = "Hi, you've reached Sage and Stone. Robin speaking, what can I do for you?"
BUDGET = "Sorry, I'm taking too long over that. Could you say it again for me?"
FAULT = 'Sorry, something went wrong on my side. Could you say that again?'
MISSED = 'Sorry, I missed that. Could you say it again?'
ASK_NUMBER = 'Could I take your phone number, please?'
"""Said by code on the way into verification, so no model request runs until the caller gives the number."""

SAVED = {
    'booked': "You're all set for {spoken}. Anything else I can help with?",
    'moved': "That's moved to {spoken}. Anything else I can help with?",
    'cancelled': "That's cancelled for you. Anything else I can help with?",
}
"""The first of the harness's alternatives for each. ponytail: no rotation; add it if callers who book twice notice."""

NOTED = "That's on record, reference {reference}. {next_step}"
REPLY_SOON = 'The salon will get back to you within two working days.'
MANAGER_CALLS = 'That one goes to the manager, who will call you back.'
_WANTS_MONEY = re.compile(r'\b(?:refund|money back|reimburse|compensat\w*|my money)\b', re.IGNORECASE)


def spoken_service(service: str) -> str:
    return service.replace('hair-color', 'hair colour')


def number_question(phone: str) -> str:
    """The digits grouped the way a person says a number: +34 600 111 222."""
    country, rest = phone[:3], phone[3:]
    return (
        f'Let me read that back to you: {country} {rest[:3]} {rest[3:6]} {rest[6:]}. Have I got that right?'
    )


def booking_question(action: str, service: str, spoken: str) -> str:
    """The read-back of a booking, a move or a cancellation, from the diary's own phrasing of the slot."""
    if action == 'book':
        return f'Shall I book you in for a {spoken_service(service)}, {spoken}?'
    if action == 'move':
        return f'Shall I move your appointment to {spoken}?'
    return f'Shall I cancel your {spoken_service(service)} {spoken}?'


def complaint_question(summary: str, requested_resolution: str) -> str:
    request = f'Your request: {requested_resolution.strip().rstrip(".?!")}. ' if requested_resolution else ''
    return (
        f'Shall I record your complaint in your words: {summary.strip().rstrip(".?!")}. {request}'
        'The salon will review it; no refund or booking change is approved yet. Is that right?'
    )


def saved(receipt: Saved) -> str:
    return SAVED[receipt.status].format(spoken=receipt.spoken, service=spoken_service(receipt.service))


def acknowledgement(reference: str, requested_resolution: str) -> str:
    """The one thing said when a complaint is written down: its reference, then the policy's next step."""
    step = MANAGER_CALLS if _WANTS_MONEY.search(requested_resolution) else REPLY_SOON
    pending = ' Your requested resolution is pending approval.' if requested_resolution else ''
    return NOTED.format(reference=reference, next_step=step) + pending + ' Anything else I can help with?'


CLAIMED = re.compile(
    r"\b(?:i|we)(?:'ve| have|'ll have| had)?\s+(?:just\s+)?"
    r'(?:cancelled|canceled|booked|moved|rebooked)\b'
    r"|\bi(?:'ve| have)\s+(?:got\s+)?you\s+(?:down|booked|in)\b"
    r"|\b(?:i|we)(?:'ve| have)?\s+(?:just\s+)?(?:recorded|logged|noted|written down)\b"
    r"|\b(?:that|it|your complaint)(?:'s| is| has been)\s+(?:on record|recorded|logged)\b",
    re.IGNORECASE,
)
"""A first-person claim that a write was done: the harness's `_CLAIMED` and `_RECORDED`, joined.

A language-specific heuristic, like the harness's. It is a second line: the
first is that writes are announced by their receipts, never by the model.
"""
SCRIPTED = re.compile(
    r'\b(?:let me read that back to you|shall i (?:book you in|move your appointment|cancel your'
    r"|record your complaint)|you're all (?:set|moved|booked)|that's (?:moved|cancelled|booked)|that's on record)\b",
    re.IGNORECASE,
)
"""The openings of the lines only code says: every read-back and every receipt above, and near copies of them.

A receipt's opening matches with any of the three verbs, since the model
reworded "That's moved to Thursday" as "You're all moved to Thursday" in
Langfuse trace 8b6ea51b.

Code's own lines reach TTS as `TTSSpeakFrame`s, which the filter never
sees, so model text that opens like one is a copy: a read-back no proposal
stands behind, or a receipt no write stands behind. In Langfuse trace
2b75413c the model, left without `answer`, said "You're all set for
Saturday at 9:30 AM" and nothing was booked. `test_graph_salon.py` checks every template opens
with one of these, so the list cannot drift from the words.
"""
_CLAUSE = re.compile(r'[.!?;,:]\s')


class EmptyReplyGuard(FrameProcessor):
    """Right after the LLM: a model turn that said nothing and called nothing is run again, once.

    Gemini can end a turn with STOP and no parts: it thought, then answered
    with nothing. Pipecat counts that as a finished response and waits for the
    caller, who waits for the bot. Langfuse trace a9b5ea5c: 25 reasoning
    tokens, 0 output, and 13 seconds of silence after the caller gave their
    number, until they said "Hello?".

    A response is empty when it brought no text and started no tool call.
    The service starts a response's calls before it ends the response
    (`GoogleLLMService` runs them, then pushes the end frame), but
    `FunctionCallsStartedFrame` is a system frame and can overtake a start
    frame still queued here. So text is forgotten at each start, and a call
    only after the end: whichever order they arrive in, the end sees them.
    An interruption counts as text, forgotten at the next start: it cut this
    response short, and the caller is talking. The first empty response is run again with an
    `LLMRunFrame`, queued the way `FlowManager` starts a node's response. A
    second one in a row is a fixed line, so the caller is never left in
    silence. Any real response resets the count.

    Nor is a response empty while the caller is speaking. When they go on
    after a pause, Pipecat cancels the response to the first half, and the
    cancelled response pushes its end frame before the interruption that
    cancelled it reaches here. Rerun, it answered the first half on the old
    node while the whole turn got its own response: two replies, both
    spoken, in Langfuse trace 8b6ea51b ("That's moved to Thursday", then
    "You're all moved to Thursday"). The aggregator announces the caller
    (`UserStartedSpeakingFrame`) before it interrupts, so that frame is here
    first, and their turn brings the next response.

    Args:
        rerun: Queues a new model run. `GraphSalon.attach` sets it once the worker exists.
    """

    def __init__(self, rerun: Callable[[], Awaitable[None]] | None = None) -> None:
        super().__init__()
        self.rerun = rerun
        self._text = False
        self._acted = False
        self._speaking = False
        self._empty = 0

    def verdict(self, said: bool) -> Literal['ok', 'rerun', 'apologise']:
        """What to do when a response ends: nothing, run the model again, or speak the fixed line."""
        if said:
            self._empty = 0
            return 'ok'
        self._empty += 1
        return 'rerun' if self._empty == 1 and self.rerun is not None else 'apologise'

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self._text = False
        elif isinstance(frame, FunctionCallsStartedFrame):
            self._acted = True
        elif isinstance(frame, UserStartedSpeakingFrame | UserStoppedSpeakingFrame):
            self._speaking = isinstance(frame, UserStartedSpeakingFrame)
        elif isinstance(frame, InterruptionFrame) or (isinstance(frame, LLMTextFrame) and frame.text.strip()):
            self._text = True
        await self.push_frame(frame, direction)
        if isinstance(frame, LLMFullResponseEndFrame):
            said, self._acted = self._text or self._acted or self._speaking, False
            match self.verdict(said):
                case 'rerun' if self.rerun is not None:
                    await self.rerun()
                case 'apologise':
                    await self.push_frame(TTSSpeakFrame(MISSED))


class ClaimFilter(FrameProcessor):
    """Right after the LLM: hold the model's words a clause at a time, and drop what claims a write.

    Two rules. A first-person claim (`CLAIMED`) passes only when a receipt
    landed in this caller turn. A copy of a line only code says (`SCRIPTED`)
    never passes, and neither does the rest of that reply: the clauses split
    on commas, so "Shall I book you in for a haircut, tomorrow at 9:30 AM?"
    must not leave "tomorrow at 9:30 AM?" behind. When a copy is dropped
    after other words already went out, the reply ends with the fixed
    `MISSED` line, so the caller is asked again rather than left waiting;
    when nothing went out, `EmptyReplyGuard`, next in line, sees an empty
    reply and runs the model again.

    Clause by clause, so the caller still hears the first words while the
    model is writing the rest.

    ponytail: it holds every reply, not only on steps that can write. Scope it
    to the booking and complaint nodes if the clause wait shows in latency.
    """

    def __init__(self, call: SalonCall) -> None:
        super().__init__()
        self._call = call
        self._held = ''
        self._copied = False
        self._passed = False

    def passes(self, clause: str) -> bool:
        """Whether one clause may be spoken. A code-owned line never may; a claim may once a receipt landed."""
        if SCRIPTED.search(clause):
            return False
        return not CLAIMED.search(clause) or self._call.state.receipt_turn == self._call.state.turn

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            self._held += frame.text
            while match := _CLAUSE.search(self._held):
                clause, self._held = self._held[: match.end()], self._held[match.end() :]
                await self._release(clause, direction)
            return
        if isinstance(frame, LLMFullResponseStartFrame | InterruptionFrame):
            self._held, self._copied, self._passed = '', False, False
        elif isinstance(frame, LLMFullResponseEndFrame):
            if self._held:
                clause, self._held = self._held, ''
                await self._release(clause, direction)
            if self._copied and self._passed:
                await self.push_frame(TTSSpeakFrame(MISSED), direction)
        await self.push_frame(frame, direction)

    async def _release(self, clause: str, direction: FrameDirection) -> None:
        if SCRIPTED.search(clause):
            self._copied = True
        if self._copied or not self.passes(clause):
            return
        self._passed = True
        await self.push_frame(LLMTextFrame(clause), direction)
