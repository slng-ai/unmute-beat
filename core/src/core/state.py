"""What every agency shares: the validated state of one call, and the call itself.

`VoiceState` is the business facts of a call and nothing else. `CallContext`
holds that state, the model protocol, the ledger of everything the caller was
sent, and the line to the caller. An agency subclasses both; the turn engine
knows only what is here.
"""

from __future__ import annotations

import contextvars
import json
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, runtime_checkable
from uuid import uuid4

from loguru import logger
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from pydantic_ai.messages import ModelMessage, ModelRequest, SystemPromptPart, UserPromptPart

from core.clock import Today
from core.locale import ENGLISH, Locale

if TYPE_CHECKING:
    from core.conversation import Conversation
    from core.proposals import Proposal, ProposalLedger

SayFn = Callable[[str], Awaitable[None]]
EndFn = Callable[[], Awaitable[None]]
type Entry = Literal['', 'handoff', 'completed', 'unserved']
"""How a step was reached within a turn. '' means it was already running when the turn began."""

turn_generation: contextvars.ContextVar[tuple[str, int] | None] = contextvars.ContextVar(
    'turn_generation', default=None
)
"""Which call, and which accepted caller turn of it, the code running right now belongs to.

A context variable rather than an argument because the thing that has to know
is `CallContext.commit`, and every tool in an agency would otherwise carry a
number it never reads. Tool tasks inherit the context they were spawned in, so
a tool still running after its turn was cancelled still reports that turn's
number while the call has moved on to the next. The call id is part of it
because the number only means anything within one call.
"""

STRICT = ConfigDict(validate_default=True, validate_assignment=True, revalidate_instances='always')
"""What every model holding business facts opts into.

Defaults are validated, so an authored default that breaks its own type fails
at import and not on the call that first reads it; assignments are validated;
and a nested model handed in is revalidated rather than trusted because it is
already an instance of the right class. None of the three is on by default,
and each one alone leaves a hole the other two do not cover.
"""


def present(value: object) -> bool:
    """Whether a prerequisite is filled, under one written-down rule.

    `None` is absent and a permitted empty string is absent. A legitimate
    `False` or `0` is **present**, which plain truthiness gets wrong: a field
    holding `False` because the caller said no has been answered, and a step
    waiting on it should not be interposed a second time.
    """
    return value is not None and value != ''


RECEIPT_REPEATS = 1
"""How often an unheard receipt is said again without being asked (`CallContext.unheard_receipt`)."""
_FILLERS = frozenset({'entry', 'tool_filler'})
_tracer = trace.get_tracer('voice.confirmations')
"""Lines that cover a silence and answer nothing: hearing one does not end the caller's turn."""
SPEECH_CHARS_PER_SECOND = 15.0
"""About how fast a voice says English text. Audio that stopped in under half its words' time was cut off.

ponytail: a fixed rate. Per-segment playout events from the runtime would make it exact.
"""
CUT_OFF_WINDOW = 1.0
"""Seconds after an early stop in which a cut-in is taken to have caused it (`CallContext._stopped_early`)."""
PLAYOUT_SLACK = 1.0
"""Seconds added to a line's own speaking time before it counts as played with no signal. TTS first audio is ~0.2 s."""
_WORD = re.compile(r"[\w']+")


def _words(text: str) -> list[str]:
    """The words of a stretch of speech, for comparing what was sent with what the runtime says went out."""
    return [word.casefold() for word in _WORD.findall(text)]


def _first_words(text: str, count: int) -> str:
    """The leading part of `text` that holds its first `count` words, in its own spelling and punctuation.

    Punctuation stuck to the last word stays with it: "nine," was heard as "nine,".
    """
    if count <= 0:
        return ''
    found = list(_WORD.finditer(text))
    if count >= len(found):
        return text
    end = found[count - 1].end()
    while end < len(text) and not text[end].isspace() and not _WORD.match(text[end]):
        end += 1
    return text[:end]


def _missed(segments: list[Spoken], playback_known: bool = False) -> str:
    """The note for speech the caller did not hear in full: what they heard of it and what they did not.

    With a runtime that reports playback, speech whose audio never started
    is missed too, cut in on or not: in Coval run RXcDFLJX an offer's audio
    never played, the caller spoke into the silence, and nothing told the
    next reply that the offer had not been heard.
    """
    never = [
        s for s in segments if playback_known and s.playback == 'queued' and s.text and not s.interrupted
    ]
    cut = [s for s in segments if s.interrupted and s.generated]
    if never and not cut:
        return f'Your last reply never reached the caller. They did not hear: "{" ".join(s.text for s in never)}".'
    if not cut:
        return ''
    heard = ' '.join(s.text for s in cut if s.text).strip()
    generated = ' '.join(s.generated for s in cut).strip()
    if heard and generated.startswith(heard) and heard != generated:
        missed = generated[len(heard) :].strip()
        return f'The caller spoke over your last reply. They heard: "{heard}". They did not hear: "{missed}".'
    if not heard:
        return f'The caller spoke before your last reply reached them. They did not hear: "{generated}".'
    return f'The caller spoke over your last reply while it was playing: "{generated}". They may not have heard all of it.'


@dataclass
class Spoken:
    """One segment of speech the caller was actually sent, and who wrote it.

    A turn's delivered speech is not one string. An entry line, a question and
    a computed confirmation are written by this code and never appear in the
    model's history, so a record built from the history alone reports a turn
    that said three things as one that said one. `kind` keeps the two apart:
    "the agency answered" and "the model answered" are different claims, and
    only one of them costs a provider request.
    """

    kind: Literal['fixed', 'model']
    text: str
    origin: Literal[
        'entry',
        'tool_filler',
        'ask',
        'confirmation',
        'model',
        'goodbye',
        'error',
        'guidance',
        'clarify',
        'switch',
    ] = 'model'
    """Where this segment came from, so a trace can tell a question from a confirmation."""
    id: str = ''
    """Stable within the call: turn, then position in it. Matches a submitted segment to the audio that played it."""
    attempt: int = 0
    """Which run of the agency asked for it. A turn can have more than one."""
    tool_call_id: str = ''
    """The tool call it was covering, where there was one."""
    at: float = field(default_factory=time.perf_counter)
    """When this segment started going out, on the same clock as `Request.started`.

    Text handed to TTS, not audio the caller heard: playout is a boundary
    nothing here can see. Every segment is timed and not just the model's,
    because "let me check" going out quickly is not an answer going out quickly.
    """
    interrupted: bool = False
    """Cut down to what the caller actually heard, once the runtime says so."""
    turn: int = 0
    response: str = ''
    generated: str = ''
    delivery: Literal['submitted', 'runtime-estimate'] = 'submitted'
    playback: Literal['queued', 'playing', 'played'] = 'queued'
    """How far its audio got, from the runtime's own start and stop signals. Only read when the runtime reports playback.

    `played` means the audio ran out after this segment began playing and
    before the caller cut in: the only case in which the caller heard all of
    it. A segment still `queued` or `playing` when the caller cuts in was not
    heard in full, however the runtime's later text report lines up.
    """
    heard_by: Literal['', 'signal', 'ack', 'estimate', 'mark', 'local'] = ''
    """What says it played.

    `mark`: the transport returned this segment's own mark after its last
    audio (Twilio's `mark` event), with no clear sent while it was pending.
    `local`: its last audio was written out, or had time to play, with no
    mark from the far end (a development transport, a carrier that returns
    none). It is an estimate: it delivers only in a call the launcher
    explicitly opened for simulated playout (`simulated`).
    `ack`: a text consumer acknowledged it: simulated delivery, for text runs.
    `signal` (the runtime's aggregate stop) and `estimate` (elapsed time) only
    ever decide turn-taking, never delivery (`delivered`).
    """
    simulated: bool = False
    """Its `local` record came in a call opened for simulated playout (`CallContext.simulate_playout`)."""
    proposal: int = 0
    """The serial of the question this segment carried (`ProposalLedger.bind`), 0 for none."""
    mark: str = ''
    """The name of the mark sent after this segment's last audio (`CallContext.mark_for`), '' when none was."""
    played_at: float | None = None
    """When its mark came back (or its acknowledgement arrived), on the segments' clock (`time.perf_counter`)."""
    cleared: bool = False
    """A clear was sent while its mark was still pending: the audio may have been cut, so it never counts as heard."""
    lost: Literal['', 'cleared', 'silent', 'failed'] = ''
    """Why `cleared` was set, from the transport's own record: a clear went out (the caller cut in), the segment
    had no audio at all, or synthesis failed while it was pending. Evidence it did not play, never a guess."""

    @property
    def delivered(self) -> Literal['delivered', 'interrupted', 'unknown']:
        """Whether the caller got all of it, as far as evidence goes.

        Only this segment's own mark, or a text consumer's acknowledgement,
        makes it delivered. Handing text to TTS, a run finishing, the
        runtime's aggregate stop signal, bytes written to a socket or enough
        time passing prove nothing about what reached the caller, so a segment
        with no such evidence is unknown, and unknown never authorises
        anything. The one exception is chosen, never defaulted: in a call the
        launcher opened for simulated playout, a `local` write counts too.
        Transport playout is still not comprehension: the caller may not have
        been listening.
        """
        if self.interrupted:
            return 'interrupted'
        if self.cleared:
            return 'unknown'
        evidence = self.heard_by in ('mark', 'ack') or (self.heard_by == 'local' and self.simulated)
        if self.playback == 'played' and evidence and self.played_at is not None:
            return 'delivered'
        return 'unknown'


@dataclass
class Request:
    """One provider request, timed at the model boundary.

    `first_output` is the first part that came back, whatever it was, and
    `kind` says which: a tool call answers the request without the caller
    hearing anything, and a time to first token that cannot say which of the
    two it measured is not a number anybody can compare.
    """

    started: float
    first_output: float | None = None
    kind: Literal['', 'text', 'tool', 'classifier'] = ''
    duration: float | None = None
    fallback: bool = False
    """The interpretation request failed or its answer was invalid, so nothing was understood."""
    outcome: Literal['', 'action', 'refused', 'none'] = ''
    """What the interpretation led to, set by the turn engine on the hop that asked for it.

    `action` ran the application action it chose, `refused` chose one the
    executor refused, `none` chose nothing and the step carried on. Kept apart
    from `fallback` so a correct answer followed by ordinary conversation is
    never counted as a failure to understand.
    """
    labels: str = ''
    """The validated interpretation as JSON, for traces. Never the caller's words."""

    @property
    def ttft(self) -> float | None:
        """Seconds from the request going out to its first part, or None if none came."""
        return None if self.first_output is None else round(self.first_output - self.started, 3)


@dataclass(frozen=True)
class Interpretation:
    """One interpreter's answer for one turn, and the snapshot it answered. Held on the call, cleared each turn."""

    turn: int
    input: BaseModel
    output: BaseModel


@dataclass(frozen=True)
class Checkpoint:
    """Where a step that needed a prerequisite was, so it can be resumed when the prerequisite is met.

    One owner at a time: the graph forbids a satisfier with prerequisites of
    its own, so checkpoints never nest.
    """

    owner: str
    messages: list[ModelMessage]
    speech_ids: frozenset[str]
    heard: str
    turn: int = 0
    """The caller turn the detour began on. Everything the caller said after it was said to the prerequisite."""


type IntentStatus = Literal['prepared', 'dispatched', 'unknown', 'succeeded', 'rejected', 'withdrawn']
"""Where one business intent stands.

`prepared`: known not dispatched. `dispatched`: handed to the backend and not
answered yet. `unknown`: it may or may not have landed (cancelled, timed out,
lost, or answered in a shape or about a subject that cannot be trusted).
`succeeded` and `rejected`: a validated receipt, or a validated
reconciliation, said which. `withdrawn`: never dispatched, and taken back by
core because its read-back could not be confirmed (`CallContext.withdraw`).
Those three are terminal. A withdrawn intent also keeps its arguments: the
same operation, arguments and caller find it again for the rest of the call,
so retrying never buys a fresh read-back allowance.
"""
OPEN_INTENT: frozenset[str] = frozenset({'prepared', 'dispatched', 'unknown'})
type Hearing = Literal['heard', 'early', 'cut', 'failed', 'unconfirmed']
"""What the evidence says about one copy of a question (`CallContext.hearing`). Only `heard` can be agreed to."""
HELD_INTENT: frozenset[str] = OPEN_INTENT | {'withdrawn'}
"""The statuses under which the same operation, arguments and caller find the same intent (`CallContext.intent_for`)."""


class Intent(BaseModel):
    """One semantic business action the caller was asked for: this operation, these arguments, this subject.

    Created by core before anything can be dispatched, and kept through
    retries, reconciliation, a read-back said again and a receipt cut off
    while it was said: all of those are the same intent. Only a terminal
    outcome ends it, so a new, separately authorised action with the same
    arguments gets a new one. The id is opaque (a random UUID), never derived
    from the arguments, so it is safe to persist and to hand a backend as its
    idempotency key; a backend that dedups by it may be sent it again.

    Strict and frozen: it crosses into application code and back out of a
    durable store (`StepFlow.open(intents=...)`), so nothing coerces its
    fields, and a change is a new, validated copy (`CallContext.set_intent`).
    """

    model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

    id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    args: str
    """The validated arguments as canonical JSON: the exact thing this intent is about."""
    subject: str = ''
    """Who the call took the caller to be when the intent was made (`CallContext.identity`)."""
    status: IntentStatus = 'prepared'
    readbacks: int = Field(0, ge=0)
    """Read-backs of this intent not heard in full on a line that had proven no delivery yet (`note_unheard_readbacks`)."""
    cut_readbacks: int = Field(0, ge=0)
    """Read-backs of this intent the caller did not hear in full on a line that had proven delivery: they talked over them."""


@dataclass(frozen=True)
class Evidence:
    """What a service did, as its validated receipt said, and the answer that authorised it.

    Filed by `Operation.run` the moment the receipt validates, and never
    removed: a correction, a new topic or a stale generation can make it no
    longer count for the current request, but the effect happened and stays
    on record for reconciliation.
    """

    operation: str
    args: dict[str, Any]
    receipt: dict[str, Any]
    identity: str
    """Who the call took the caller to be when it ran."""
    turn: int
    generation: int
    """The turn generation whose work produced it: a superseded one still counts as an effect."""
    proposal: int = 0
    """The serial of the question whose answer authorised it, 0 when none was needed."""
    segments: tuple[str, ...] = ()
    """The segments that carried that question to the caller."""
    intent: str = ''
    """The `Intent` this receipt answered."""
    succeeded: bool = True
    """Whether the receipt said it worked (`succeeds`). A validated refusal is evidence too, of nothing done."""
    changes: bool = True
    """Whether the operation changes a business resource. A lookup does not, so it can never back a claim of one."""
    resource: str = ''
    """The resource the receipt is about (its declared `subject` field), '' when the operation names none."""
    kind: str = ''
    """The kind of that resource (the operation's `resource`, else its `subject` field): ids are unique within it."""
    version: int | None = None
    """The resource's version after this change (the operation's `version` field), None when it declares none."""


@dataclass(frozen=True)
class Held:
    """Something the caller agreed to, or was told was done, that a later reply can still take back.

    A booking just made, which the caller may still undo; an approval kept
    while a prerequisite runs. The application holds it on the call by kind
    (`CallContext.hold`), and a reading reads the caller's reply against what
    they were told (`told`).
    """

    key: str
    """What it is, as the application names it: a booking id, the digest of approved wording."""
    told: str
    """What the caller heard about it, word for word. A reading of their reply is read against this."""
    owner: str = ''
    """The caller identity it belongs to. Empty when it belongs to whoever is on the line."""
    turn: int = 0
    """The caller turn it was agreed on."""
    item: Any = None
    """The application's own record of it, such as the question that was approved."""
    lasts: Callable[[Any], bool] | None = None
    """Checked on every caller turn: once it says no, the call lets go of it. None lasts until released."""


class Handoff(BaseModel):
    """Where the caller goes next. Agencies return this from their `hand_over` action and their finishers."""

    to: str
    because: str = Field(default='', description='One short sentence: what the caller wants. Never spoken.')
    status: Literal['handoff', 'completed', 'unserved'] = 'handoff'
    """A plain handoff, or a prerequisite step reporting that it finished, or that it gave up."""
    ends_turn: bool = False
    """Whether the move itself answers the caller, so the destination does not run a model this turn.

    Routing, prerequisites and the checkpoint are exactly those of any
    handoff. The destination's opening still wins when it has one, because
    those words are the application's and cost nothing; with no opening and
    no `reply`, the destination runs as usual.
    """
    reply: str = ''
    """What to say when the move ends the turn. Held back if it claims a change nothing made."""


@runtime_checkable
class Question(Protocol):
    """An agent output that is a question for the caller, worded by the application.

    The turn engine says it and ends the turn. It exists so the one sentence the
    next consequential action will be checked against is the sentence the
    caller actually heard, not a paraphrase the model wrote and then treated as
    having been agreed to.
    """

    def put(self, call: CallContext) -> str:
        """Record the question as pending, and hand back the words to say."""
        ...


class VoiceState(BaseModel):
    """The business facts of one call, and nothing else.

    Every supported write path is validated: authored defaults on the way in,
    assignments on the way back, and nested models every time they are handed
    over rather than only when first built. That is three separate opt-ins in
    Pydantic and none of them is on by default.

    Conversation history and the runtime's callbacks are deliberately **not**
    here. They live on `CallContext`, which holds this object and replaces it
    wholesale. Keeping them out is what makes a whole-candidate commit possible:
    a state that carried a message list and two bound methods could not be
    rebuilt from its own values.

    This is also the scope the prompt templates render against, so a
    `{{state.placeholder}}` naming no field fails when the prompt is compiled,
    at import, rather than rendering as nothing on a live call.
    """

    model_config = STRICT

    @classmethod
    def flow_fields(cls) -> frozenset[str]:
        """The fields that say where the caller is, rather than what is true of them.

        Read by `CallContext.commit` to decide what a superseded turn may still
        write. A business fact it established really happened; the step it was
        going to send the caller to next is an answer to a question they have
        moved on from.
        """
        return frozenset({'agent', 'pending'})

    agent: str
    pending: str | None = None
    """The step waiting to resume.

    A step whose prerequisite was not met is recorded here and the graph enters
    the step that satisfies it. One slot, not a list: no satisfier has
    prerequisites of its own, so `route` cannot chain, and a list invited
    in-place mutation that no validator would ever see.
    """


class CallContext(BaseModel):
    """One call: its validated state, its history, and the line to the caller.

    The agents' `deps`. This object is stable for the whole call and `state` is
    replaced inside it, so a tool holding `ctx.deps` always reads the current
    state; there is no way to capture a snapshot and write to it later.

    Everything below `heard` is a private attribute rather than a field: the
    prompt templates are compiled against this model's JSON schema, and a
    callable or a ledger has no place in it.
    """

    model_config = ConfigDict(validate_assignment=True)

    default_locale: ClassVar[Locale] = ENGLISH
    """The words the turn engine says itself, in this call's language. A use case in another language sets its own."""
    timezone: ClassVar[tzinfo] = UTC
    """Where this call's day is counted. A business in one place sets its own (the salon: Europe/Madrid)."""
    calendar_days: ClassVar[int] = 0
    """How many days after today each step's model is shown, labelled (`core.clock.Today`). 0: today only."""

    state: VoiceState
    messages: list[ModelMessage] = Field(default_factory=list)
    heard: str = ''
    """What the caller actually said to open this turn, as the runtime accepted it.

    A field, because the prompts render it, and because everything an action
    claims the caller agreed to has to be checkable against something the
    model did not write. Every consent fact used to enter as a model-authored
    argument, and a model supplied all of them for things the caller never said.
    """

    _say: SayFn | None = PrivateAttr(default=None)
    _end: EndFn | None = PrivateAttr(default=None)
    _covered: bool = PrivateAttr(default=False)
    _reminders: int = PrivateAttr(default=0)
    """Silence reminders since the caller last spoke (`core.turn.remind`)."""
    _continued_from: list[str] = PrivateAttr(default_factory=list)
    """For a turn joined to the one before: each of that piece's segments as origin, playback, cut, blank. For the trace."""
    _answers: int = PrivateAttr(default=0)
    """How many lines `speak_now` has put to the caller, heard or not. The turn engine's evidence that an action answered."""
    """Whether the current silence already has a fixed line in it. `say` allows one per gap."""
    _generation: int = PrivateAttr(default=0)
    """Which accepted caller turn is current.

    Bumped once per turn and read back before a move is committed: a turn that
    was cancelled and then finished its unwinding must not still move the
    caller, and a late result is not evidence about where the caller is now.
    """
    _claim: str | None = PrivateAttr(default=None)
    """The one tool call in the current model response allowed to change the flow or write to the business.

    Set by `one_action_per_reply` from the whole response before any tool runs;
    read by every action that could be the second one. `None` means this
    response proposed no such action.
    """
    _changing: set[str] = PrivateAttr(default_factory=set)
    """Fields a model tried to change in this caller turn, accepted or not. See `one_action_per_reply`."""
    _unapplied: set[str] = PrivateAttr(default_factory=set)
    """Fields the current model response changes after its action: not yet run when the action does."""
    _tries: Counter[str] = PrivateAttr(default_factory=Counter)
    """How many times a model response in this caller turn tried to record each field (`one_action_per_reply`)."""
    _kept: Counter[str] = PrivateAttr(default_factory=Counter)
    """How many of those tries recorded the field with the value it already had (`collect`).

    `_changing` also holds a field recorded again unchanged: the caller saying
    the read-back's value back. A read-back is confirmed by that
    (`core.graph.Restated`) only while every try at its fields kept the value.
    A try the tool's schema refused never reaches `collect`, so it counts as
    a try and not as kept: "half nine, well, ten then" is a change, and
    "ten, no, nine, no, ten" too, never a yes to ten.
    """
    _proposals: ProposalLedger[Any] = PrivateAttr(default_factory=lambda: _ledger())
    """The one question this call is waiting on an answer to. See `core.proposals`."""
    _told: tuple[int, str, str, int] = PrivateAttr(default=(0, '', '', 0))
    """The turn, words and intent of the latest receipt speech (`note_receipt`), and how often it was said again."""
    _approved: tuple[int, int] = PrivateAttr(default=(0, -1))
    """The question serial the active step's agent approved, and the turn generation that did. See `approve`."""
    _interpretations: dict[str, Interpretation] = PrivateAttr(default_factory=dict)
    """Each interpreter's answer for this turn, by interpreter name. See `core.interpreters`."""
    _requests: list[Request] = PrivateAttr(default_factory=list)
    """Every provider request of the turn now running, timed at the model boundary by `request_clock`."""
    _spoken: list[Spoken] = PrivateAttr(default_factory=list)
    """Everything the caller was sent this turn, in order. Fixed lines are here and nowhere else."""
    _open: Spoken | None = PrivateAttr(default=None)
    """The model segment currently streaming, held open until something else speaks or the run ends.

    Kept open so that a fixed line spoken by a tool mid-run lands after the
    words that preceded it and before the words that follow: the order of the
    ledger is the order the caller heard, and a timestamp cannot put back a
    boundary that was never kept.
    """
    _spoke: bool = PrivateAttr(default=False)
    """Whether the *model* streamed text this turn."""
    _id: str = PrivateAttr(default_factory=lambda: uuid4().hex[:12])
    """This call, told apart from every other one sharing the process.

    Part of the name of every business operation it starts, so one call's
    write can never be answered out of another call's cache.
    """
    _turn: int = PrivateAttr(default=0)
    """How many caller turns this call has accepted.

    A question put to the caller is stamped with it, so "they answered the
    thing we asked" is a comparison and not an assumption.
    """
    _ended: str = PrivateAttr(default='')
    """Why this call ended, set by whoever established it. Empty while it has not."""
    _segments: int = PrivateAttr(default=0)
    _attempt: int = PrivateAttr(default=0)
    """Which run of the agency is going on inside this turn. More than one when a caller speaks again before the agency answers."""
    _speech_history: list[Spoken] = PrivateAttr(default_factory=list)
    _inputs: dict[int, str] = PrivateAttr(default_factory=dict)
    _response: str = PrivateAttr(default='')
    _protocol: list[ModelMessage] = PrivateAttr(default_factory=list)
    """Every message that executed, retained for audit and never projected away."""
    _hidden_speech: set[str] = PrivateAttr(default_factory=set)
    _checkpoint: Checkpoint | None = PrivateAttr(default=None)
    _held: dict[str, Held] = PrivateAttr(default_factory=dict)
    """What the caller could still take back, by kind. See `hold`."""
    _clock: Callable[[], datetime] | None = PrivateAttr(default=None)
    """The clock a session was opened with, a service like any other. None reads the wall clock."""
    _began_as: dict[int, str] = PrivateAttr(default_factory=dict)
    """The identity each turn generation began under, so late work can tell it now speaks for somebody else."""
    _evidence: list[Evidence] = PrivateAttr(default_factory=list)
    """Every validated receipt of the call, oldest first. Append-only. See `note_evidence`."""
    _intents: dict[str, Intent] = PrivateAttr(default_factory=dict)
    """Every business intent of the call, by id, oldest first. See `intent_for`."""
    _unknown: dict[str, tuple[Any, BaseModel]] = PrivateAttr(default_factory=dict)
    """Intents whose outcome is unknown, by id, with the operation and arguments that reconcile them.

    Settled before anything else: at the start of the next turn, by code
    (`settlement`), and again before any retry (`Operation._before`).
    """
    _unpresented: dict[str, tuple[Any, BaseModel, dict[str, Any]]] = PrivateAttr(default_factory=dict)
    """Succeeded intents whose local recording (state, speech) failed after the receipt: repaired, never re-sent."""
    _simulated_playout: bool = PrivateAttr(default=False)
    """Whether an estimated playback may count as heard. Off unless a launcher chose it (`simulate_playout`)."""
    _transport: str = PrivateAttr(default='')
    """Which transport carries the call's speech, as its launcher wired it (`note_transport`); '' for none.

    Recorded in every blocked, withdrawn and dispatched confirmation, so an
    audit says which kind of evidence it could have had.
    """
    _claimed: set[str] = PrivateAttr(default_factory=set)
    """Intents a run of `Operation.run` is sending right now. A second run of one of them is refused."""
    _versions: dict[tuple[str, str], int] = PrivateAttr(default_factory=dict)
    """The newest version known for each resource (kind, id): a receipt below it is history. See `is_latest`."""
    _last_asked_intent: str = PrivateAttr(default='')
    """The intent of the last read-back put to the caller, whatever happened to the question since."""
    _last_asked_serial: int = PrivateAttr(default=0)
    """The serial of that read-back (`ProposalLedger.serial`)."""
    _declined: dict[int, int] = PrivateAttr(default_factory=dict)
    """Read-backs the active step's agent read the caller as turning down (`decline`), by serial, with the turn.

    A joined turn that rewinds drops the ones recorded on it: they were read from half a sentence.
    """
    _judged: set[str] = PrivateAttr(default_factory=set)
    """Question copies already counted as heard or not (`note_unheard_readbacks`)."""
    _turn_usage: tuple[int, Any] | None = PrivateAttr(default=None)
    """The caller turn and the usage tally its pieces share, so a joined turn spends one budget (`core.turn.Turn`)."""
    _departed: bool = PrivateAttr(default=False)
    """The caller said goodbye: no new consequential intent starts after it. See `note_departure`."""
    _pending_marks: dict[str, str] = PrivateAttr(default_factory=dict)
    _sent_marks: set[str] = PrivateAttr(default_factory=set)
    """Pending marks the transport has sent: all their segment's audio went out before them."""
    """Marks sent and not yet returned, by name, with the segment each one follows. See `mark_for`."""
    _clears: int = PrivateAttr(default=0)
    """How many clears this call sent: part of every mark's name, so a mark from before one is told apart."""
    _marks_known: bool = PrivateAttr(default=False)
    """The transport returns marks (or writes locally): only then can speech be delivered by one."""
    _onsets: list[float] = PrivateAttr(default_factory=list)
    """When the caller was heard starting to speak, newest last, bounded (`note_onset`)."""
    _answer_lead: float = PrivateAttr(default=0.0)
    """How much earlier than its report caller speech may have begun (the adapter's VAD start window)."""
    _origin: tuple[float, datetime] = PrivateAttr(
        default_factory=lambda: (time.perf_counter(), datetime.now(UTC))
    )
    """The segments' clock and UTC wall time at one instant: how a monotonic time is written for a report (`wall`)."""
    _audits: int = PrivateAttr(default=0)
    _endings: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    """How the call ended, as each part of it was observed or intended, in order (`note_ending`)."""
    _playback_log: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    """Every mark sent, returned, ignored or cleared, in order, for the write audit (`playback_for`)."""
    _request_kept: bool = PrivateAttr(default=False)
    """Whether a reading of this turn found the detour's request unchanged. See `note_request_kept`."""
    _return_status: str = PrivateAttr(default='')
    _entered: Entry = PrivateAttr(default='')
    """How the step now running was reached within this turn. Set with `_return_status`, cleared with it."""
    _pieces: list[str] = PrivateAttr(default_factory=list)
    """What the caller said in this turn, in the pieces the runtime delivered it. `heard` is them joined.

    One utterance often arrives as several: the runtime closes the caller's
    turn on every pause, and a long sentence has several. A piece that arrives
    before any of the agent's answer reached the caller continues the same
    turn rather than opening a new one. See `continues_turn`.
    """
    _retained: set[tuple[int, int]] = PrivateAttr(default_factory=set)
    """Which pieces, as (turn, index), are already in the model history. The rest still have to be sent."""
    _acted: bool = PrivateAttr(default=False)
    """Whether a business action was attempted in this caller turn, in any of its pieces."""
    _interrupted: bool = PrivateAttr(default=False)
    """Whether the last run of this turn was cut off by the caller speaking again."""
    _reached: bool = PrivateAttr(default=False)
    """Whether any of this turn's speech started playing to the caller. Only meaningful when `_playback_known`."""
    _burst: float = PrivateAttr(default=float('inf'))
    """When the audio now going out started, on the segments' clock. Infinite while nothing is playing."""
    _stopped_early: tuple[float, list[Spoken]] = PrivateAttr(default=(float('-inf'), []))
    """When the audio last stopped far sooner than its words could be said, and what it was playing.

    The runtime's stop signal and the caller's cut-in can arrive in either
    order, because the audio stops *because* the caller cut in. Stop first,
    and the segments were already `played` when the cut-in came, so a closing
    question heard for 56 ms read as heard in full and "That's perfect." hung
    up on it (Coval run Z56CoZax, K5). A cut-in within `CUT_OFF_WINDOW` of such
    a stop takes those segments back to `playing`, so it marks them cut off.
    """
    _playback_known: bool = PrivateAttr(default=False)
    _leaving: bool = PrivateAttr(default=False)
    """The caller said goodbye in the same reply as the answer this turn runs (`core.readings.Acts`)."""
    _today: Today | None = PrivateAttr(default=None)
    """The call's clock, read once (`today`): a call that crosses midnight keeps its day."""
    _spoken_over: str = PrivateAttr(default='')
    """What the caller missed of the last turn's speech, said as a fact for this turn's runs. See `spoken_over`."""
    """Whether the runtime reports playback (`note_reached`). Without it, speech handed to TTS counts as heard."""

    @property
    def locale(self) -> Locale:
        """This call's fixed lines: the class's `default_locale`, unless a call picks its own, turn by turn."""
        return self.default_locale

    def now(self) -> datetime:
        """The time now, where this call's day is counted: the injected clock (`clock`), or the wall clock."""
        return self._clock() if self._clock is not None else datetime.now(self.timezone)

    @property
    def today(self) -> Today:
        """Today's date for this call, read from `now` the first time it is asked for, then kept."""
        if self._today is None:
            self._today = Today(self.now(), self.calendar_days)
        return self._today

    @property
    def entered(self) -> Entry:
        """How the current step was reached in this turn: by a handoff, by a prerequisite's return, or not at all."""
        return self._entered

    @property
    def fresh(self) -> str:
        """The words of this turn that are not in the model history yet, in order. What a run is sent."""
        return ' '.join(p for i, p in enumerate(self._pieces) if (self._turn, i) not in self._retained and p)

    @property
    def leaving(self) -> bool:
        """Whether the caller is going: a confirmation said now asks nothing more, because the goodbye follows it."""
        return self._leaving

    @property
    def input_retained(self) -> bool:
        """Whether everything the caller said in this turn is already in the model history."""
        return not self.fresh

    def mark_retained(self) -> None:
        """Every piece of this turn so far is now in the model history."""
        self._retained.update((self._turn, i) for i in range(len(self._pieces)))

    @property
    def spoken_over(self) -> str:
        """What the caller did not hear of the agent's last turn, as an application note, or ''.

        The history already holds only what was heard, but "We do" on its own
        reads like an answer given, and the model moved on without the
        balayage answer the caller never got. Said as a fact, nothing more:
        what to do about it is the step's prompt's business.
        """
        return self._spoken_over

    @property
    def continuing(self) -> bool:
        """Whether this turn has had more than one piece: the caller carried on before the agent's answer reached them."""
        return len(self._pieces) > 1

    @property
    def acted_this_turn(self) -> bool:
        """Whether a business action was attempted in this caller turn. Its answer is then spent."""
        return self._acted

    def note_business_attempt(self) -> None:
        """A business action is about to execute. Called by `Operation.run`, whether it then succeeds or not."""
        self._acted = True

    def note_reached(self) -> None:
        """The runtime has started playing speech to the caller. Wired to the transport's own signal.

        Whatever was queued is now playing: audio goes out in the order it was
        handed to TTS, so the segments already submitted are the ones this
        burst of sound is made of.
        """
        self._playback_known = True
        self._reached = True
        self._burst = time.perf_counter()
        for segment in self.all_spoken:
            if segment.playback == 'queued' and segment.text:
                segment.playback = 'playing'

    def note_drained(self) -> None:
        """The runtime's audio ran out: everything that was playing, or joined the sound while it played, was heard.

        Speech handed to TTS while audio was already going out never gets a
        start signal of its own, because the sound never stopped; it counts
        as part of the stretch it joined, but only once its own words have had
        time to play since it was handed on. Session 4's Coval run GYZdoiie
        queued a number read-back behind a holding line; the holding line's
        audio ended half a second later, and the read-back, of which the
        caller heard almost nothing before cutting in, was counted as heard.
        Their next words then agreed to it. Now it stays queued: its own start
        signal, or a later end once its words could have played, decides.

        ponytail: time is estimated from the text (`SPEECH_CHARS_PER_SECOND`),
        not measured per segment; the upgrade path is per-segment playout events.

        A segment the caller already cut off stays cut off: the audio stopping
        after a cut-in is the cut-in, not the end of the words. Audio that
        stopped in under half the time its words take is remembered, so a
        cut-in arriving just after the stop still cuts it (`_stopped_early`).
        """
        now = time.perf_counter()
        ended = [
            segment
            for segment in self.all_spoken
            if not segment.interrupted
            and (
                segment.playback == 'playing'
                or (
                    segment.playback == 'queued'
                    and segment.text
                    and segment.at >= self._burst
                    and now - segment.at >= len(segment.text) / SPEECH_CHARS_PER_SECOND
                )
            )
        ]
        for segment in ended:
            segment.playback, segment.heard_by = 'played', 'signal'
        spoken_for = sum(len(s.text) for s in ended) / SPEECH_CHARS_PER_SECOND
        if ended and now - self._burst < spoken_for / 2:
            self._stopped_early = (now, ended)
        else:
            self._stopped_early = (float('-inf'), [])
        self._burst = float('inf')

    def acknowledge(self, *segment_ids: str, heard: str | None = None) -> bool:
        """A text consumer received these segments: the text runtime's playback signal.

        Nothing else marks text as heard: a turn returning or a model finishing
        is not the caller reading it. `heard` is what actually reached them,
        when that was less than all of it; the segments are then cut to it, as
        a runtime's interrupted report cuts them. Unknown ids change nothing.
        Returns whether the acknowledgement applied.
        """
        by_id = {s.id: s for s in self.all_spoken}
        if not segment_ids or any(i not in by_id for i in segment_ids):
            return False
        if heard is not None and not self.note_heard(heard, segment_ids=segment_ids, interrupted=True):
            return False
        self._playback_known = True
        if self._open is not None and self._open.id in segment_ids:
            self.close_model_text()
        now = time.perf_counter()
        for segment in (by_id[i] for i in segment_ids):
            if not segment.interrupted:
                segment.playback, segment.heard_by, segment.played_at = 'played', 'ack', now
        return True

    def mark_for(self, segment_id: str) -> str:
        """Name the mark to send after this segment's last audio, and wait for it. '' for an unknown segment.

        The name carries the segment's id (call, turn, position) and the
        number of clears sent so far, so a mark that comes back after a
        clear, from another call, or twice, is recognised as not this one.
        """
        segment = next((s for s in self.all_spoken if s.id == segment_id), None)
        if segment is None or not segment.text:
            return ''
        self._marks_known = True
        segment.mark = f'{segment_id}~{self._clears}'
        self._pending_marks[segment.mark] = segment_id
        return segment.mark

    def note_mark_sent(self, name: str, at: float, *, media: int | None = None) -> None:
        """The transport sent this mark after the segment's last audio chunk. For the record; it proves nothing yet.

        `media` is how many audio chunks went out since the mark before it,
        when the transport counts them. None means it does not. Zero means the
        segment had no audio at all (TTS produced none), so its mark will come
        back at once and prove nothing: the segment is never delivered.
        """
        self._log_playback('sent', name, at)
        if name in self._pending_marks:
            self._sent_marks.add(name)
        if media == 0 and (segment := self._marked(name)) is not None:
            segment.cleared, segment.lost = True, 'silent'
            self._log_playback('silent', name, at, segment.proposal)

    def note_speech_failed(self, at: float) -> None:
        """Speech synthesis reported an error while marks were pending: those segments may be partial or silent."""
        for name in list(self._pending_marks):
            if (segment := self._marked(name)) is not None:
                segment.cleared, segment.lost = True, 'failed'
                self._log_playback('failed', name, at, segment.proposal)

    def _marked(self, name: str) -> Spoken | None:
        segment_id = self._pending_marks.get(name)
        return next((s for s in self.all_spoken if s.id == segment_id), None) if segment_id else None

    def note_mark_returned(self, name: str, at: float, *, local: bool = False) -> bool:
        """The transport says the audio up to this mark has played (or, `local`, was written). Returns whether it counted.

        A mark counts once, for the segment it was sent for, if no clear was
        sent while it was pending: Twilio returns a mark both when its audio
        has played and when a clear dropped it, and the message does not say
        which, so every mark pending at a clear was made invalid then.
        Unrelated, duplicate and stale names change nothing.
        """
        segment_id = self._pending_marks.pop(name, None)
        self._sent_marks.discard(name)
        segment = next((s for s in self.all_spoken if s.id == segment_id), None) if segment_id else None
        if segment is None or segment.interrupted or segment.cleared:
            self._log_playback('ignored', name, at)
            return False
        segment.playback, segment.heard_by, segment.played_at = 'played', 'local' if local else 'mark', at
        segment.simulated = local and self._simulated_playout
        self._log_playback('written' if local else 'played', name, at, segment.proposal)
        return True

    def note_transport(self, name: str) -> None:
        """Name the transport this call's speech goes out on (`core.processor.playout`, or a text runner)."""
        self._transport = name

    @property
    def transport(self) -> str:
        """The transport named by `note_transport`, '' when none was."""
        return self._transport

    def simulate_playout(self) -> None:
        """Let an estimated playback (`local`) count as heard on this call. For a test or development launcher only.

        Only `core.processor.playout(..., simulate=True)` calls it, and only a
        launcher that names the mode passes that: no transport turns it on by
        itself. Every `write` audit says whether it was on.
        """
        self._simulated_playout = True

    @property
    def simulated_playout(self) -> bool:
        """Whether estimated playback counts as heard on this call (`simulate_playout`)."""
        return self._simulated_playout

    def note_cleared(self, at: float, *, sent: bool = True) -> None:
        """The transport is about to clear the audio it holds: every mark still pending may now come back for nothing.

        Recorded before the clear goes out, so a mark returned in answer to
        it can never pass for playback. A segment whose mark already came back
        keeps it: clearing later audio does not unplay earlier audio.

        `sent=False` is for a far end that keeps all it was sent (a browser has
        no clear): only the marks still queued behind the dropped audio are
        voided. A segment whose mark was already sent had all its audio sent,
        and stays pending for the far end's own report.
        """
        for name, segment_id in list(self._pending_marks.items()):
            if not sent and name in self._sent_marks:
                continue
            if (segment := next((s for s in self.all_spoken if s.id == segment_id), None)) is not None:
                segment.cleared, segment.lost = True, 'cleared'
                self._log_playback('cleared', name, at, segment.proposal)
            del self._pending_marks[name]
            self._sent_marks.discard(name)
        self._clears += 1

    def note_onset(self, at: float) -> None:
        """The caller was heard starting to speak (the adapter's voice detector), on the segments' clock."""
        self._onsets = [*self._onsets[-31:], at]

    def answered_in_time(self, segment: Spoken) -> bool:
        """Whether this segment had played to its end before the caller began the reply it is being answered by.

        The reply began at the earliest speech onset after the segment was
        queued, less the adapter's detection window (`_answer_lead`): both
        times are this process's `time.perf_counter`, taken where each report
        arrives, never a provider's clock. With marks and no onset after the
        segment, nothing says when the reply began, so it did not count. A
        text run has no onsets; its acknowledgement always comes first.
        """
        if segment.played_at is None:
            return False
        onsets = [t for t in self._onsets if t >= segment.at]
        if not onsets:
            return not self._marks_known or segment.heard_by == 'ack'
        return segment.played_at <= min(onsets) - self._answer_lead

    def wall(self, at: float) -> str:
        """A time on the segments' clock (`time.perf_counter`) as UTC wall time, for a record read elsewhere.

        One origin per call, taken when the call was made, so the order of
        events is the monotonic clock's and never a wall clock's jump.
        """
        perf, wall = self._origin
        return (wall + timedelta(seconds=at - perf)).isoformat()

    def playback_for(self, serial: int) -> dict[str, Any] | None:
        """A question's transport playback evidence, in the shape `load_report.Playback` reads, or None.

        Each of its marks that came back (`played`), and each that proves it
        did not play (`cleared`: voided by a clear, no media before it, or
        synthesis failed), with when the caller's reply began. None when the
        reply's start is unknown (no onset after the copy) or nothing was
        marked: that is no evidence, not a pass. A text acknowledgement is
        simulated delivery and is never written here (`simulated_for`).
        """
        copies = [s for s in self.all_spoken if serial and s.proposal == serial]
        kinds = {'played': 'played', 'cleared': 'cleared', 'silent': 'cleared', 'failed': 'cleared'}
        marks = [
            {'proposal': serial, 'segment': e['segment'], 'kind': kinds[e['kind']], 'at': self.wall(e['at'])}
            for e in self._playback_log
            if e.get('proposal') == serial and e['kind'] in kinds
        ]
        onsets = [t for t in self._onsets if copies and t >= copies[-1].at]
        if not marks or not onsets:
            return None
        return {'marks': marks, 'answered_at': self.wall(min(onsets) - self._answer_lead)}

    def simulated_for(self, serial: int) -> list[dict[str, Any]]:
        """A question's delivery that is not a transport mark: a text acknowledgement, or audio written at real time.

        `kind` says which (`text`, or `written`: a transport that returns no
        marks, settled from the media sent, see `core.processor.TwilioMarks`).
        Never written as `playback`, so an evaluator does not read it as one.
        """
        return [
            {
                'segment': s.id,
                'kind': 'text' if s.heard_by == 'ack' else 'written',
                'at': self.wall(s.played_at),
            }
            for s in self.all_spoken
            if serial and s.proposal == serial and s.heard_by in ('ack', 'local') and s.played_at is not None
        ]

    def next_audit(self) -> int:
        """The next number in this call's sequence of audited events, so a reader can order them without clocks."""
        self._audits += 1
        return self._audits

    def _log_playback(self, kind: str, name: str, at: float, proposal: int | None = None) -> None:
        segment = name.split('~', 1)[0]
        if proposal is None:
            proposal = next((s.proposal for s in self.all_spoken if s.id == segment), 0)
        self._playback_log = [
            *self._playback_log[-255:],
            {'proposal': proposal, 'segment': segment, 'mark': name, 'kind': kind, 'at': at},
        ]

    def turn_ended(self, *, interrupted: bool) -> None:
        """The run for this piece is over. `interrupted` is whether the caller cut it off by speaking again."""
        self._interrupted = self._interrupted or interrupted

    def note_barge(self) -> None:
        """The caller started speaking again, while this turn ran or after it, before the next one began.

        With a runtime that reports playback, nothing that had not played to
        its end was heard in full, so it is marked interrupted now rather than
        left to a text report that may never be matched to it: a read-back cut
        off 200 ms in used to stay "heard", and the caller's next words were
        read as agreement to a question they never heard. What never started
        is recorded as heard by nobody; what was playing keeps its text until
        the runtime says how much of it went out.

        A segment still queued whose words had all the time they need to play
        (`PLAYOUT_SLACK`), one after another, was heard: its start signal went
        missing, and a missing signal errs towards a new turn.

        That estimate decides whether the caller's words start a new turn. It
        never authorises: an estimated segment is `unknown`, not `delivered`
        (`Spoken.delivered`). The audio burst ends here too, so a stop signal
        arriving late for the audio the caller cut off cannot count a line
        queued after the cut-in as played.

        ponytail: time since queuing is a stand-in for a playout event per
        segment. A line whose audio started far later than it was queued is
        taken as heard too early. Per-segment playout ids are the upgrade.
        """
        self._interrupted = True
        if not self._playback_known:
            return
        now = time.perf_counter()
        stopped, early = self._stopped_early
        if now - stopped <= CUT_OFF_WINDOW:
            for segment in early:
                segment.playback = 'playing'
        self._stopped_early = (float('-inf'), [])
        ends = float('-inf')
        for segment in self.all_spoken:
            if segment.playback != 'queued' or not segment.text:
                continue
            ends = max(ends, segment.at) + len(segment.text) / SPEECH_CHARS_PER_SECOND + PLAYOUT_SLACK
            if ends < now:
                segment.playback, segment.heard_by = 'played', 'estimate'
        for segment in self.all_spoken:
            if segment.playback == 'queued' and segment.text:
                segment.text, segment.interrupted, segment.delivery = '', True, 'runtime-estimate'
            elif segment.playback == 'playing':
                segment.interrupted = True
        self._burst = float('inf')

    def continues_turn(self) -> bool:
        """Whether the caller's next words continue this turn: they cut in before any answer to it had played to its end.

        When the caller stops is the runtime's call (its endpointing), and it
        stops on every pause. What the harness owns is what happens when they
        go on: the pieces accumulate into one utterance, so "+34" and "611 111
        111" are one number and "Yes, please." and "That sounds perfect." are
        one reply to the question before them. A caller who talks over the
        agent's answer to a piece is still talking; a caller who speaks after
        an answer has played out is answering it.

        Both halves are required: the caller must have started speaking again
        (`note_barge`, or a run cut off), and no answer may have played to its
        end. With a runtime that reports playback, that is a non-filler
        segment whose audio ran out before the caller cut in. Without one,
        anything handed to TTS counts as heard, so only a piece cut off before
        it said anything continues. Either way a missing signal errs towards a
        new turn, which is how every turn was counted before.
        """
        if self._turn == 0 or not self._pieces or not self._interrupted:
            return False
        said = [s for s in self.spoken if s.text]
        if self._playback_known:
            return not any(s.playback == 'played' and s.origin not in _FILLERS for s in said)
        return not said

    def retain_input(self) -> None:
        """Accept a model-free turn once, without inventing a provider exchange."""
        if self.input_retained:
            return
        message = ModelRequest(parts=[UserPromptPart(self.fresh)], metadata={'caller_turn': self.turn})
        self.messages.append(message)
        self._protocol.append(deepcopy(message))
        self.mark_retained()

    @property
    def detour_request(self) -> str:
        """What the caller asked for when a prerequisite interrupted it. Empty while no step is waiting."""
        return self._checkpoint.heard if self._checkpoint is not None else ''

    def heard_since(self, turn: int) -> str:
        """Everything the caller said from that caller turn to this one, in order, one turn after another."""
        return ' '.join(
            words for t in range(max(turn, 1), self._turn + 1) if (words := self._inputs.get(t, '').strip())
        )

    def detour_words(self, checkpoint: Checkpoint | None = None) -> list[str]:
        """What the caller has said to a prerequisite since its detour began, a turn per item, oldest first.

        That includes the rest of the turn the detour began on: a caller who
        went on in that same turn (`continues_turn`) said it to the
        prerequisite, and it is not in the request the checkpoint holds.
        """
        checkpoint = checkpoint or self._checkpoint
        if checkpoint is None:
            return []
        said = []
        for turn in range(checkpoint.turn, self.turn + 1):
            words = self._inputs.get(turn, '').strip()
            if turn == checkpoint.turn:
                words = words.removeprefix(checkpoint.heard.strip()).strip()
            if words:
                said.append(words)
        return said

    def said_last_turn(self) -> str:
        """What the agent said on the turn before this one: the question a reply answers."""
        previous = [segment for segment in self.all_spoken if segment.turn < self.turn]
        last_turn = max((segment.turn for segment in previous), default=-1)
        return ' '.join(s.text for s in previous if s.turn == last_turn).strip()

    def detour_speech(self) -> str:
        """What the agent said to the caller since a prerequisite's detour began, in order, as far as it was heard."""
        checkpoint = self._checkpoint
        if checkpoint is None:
            return ''
        return ' '.join(s.text for s in self.all_spoken if s.id not in checkpoint.speech_ids and s.text)

    def note_request_kept(self) -> None:
        """Record that a reading of this turn found the detour's request unchanged.

        A return opening acts on the request the checkpoint holds, with no
        model to read what the caller said meanwhile. "And make it Saturday
        instead", said with the number, made that request stale, so a return
        opening runs only on this evidence (`Opening.action`). Only a reading
        of the whole detour gives it, and only while the detour is open;
        every new piece of the caller's turn takes it away again.
        """
        if self._checkpoint is not None:
            self._request_kept = True

    @property
    def request_kept(self) -> bool:
        """Whether this turn has evidence that the caller kept the request the detour interrupted."""
        return self._request_kept

    def keep_detour_words(self, checkpoint: Checkpoint, step: str, *, sent: str = '') -> str:
        """Keep what the caller said during a prerequisite's detour, when the waiting step's history is restored.

        The restore puts back the history from before the detour, so the
        prerequisite's own exchanges stay out of the waiting step's view. The
        caller's words must not go with them: "and make it at ten instead",
        said while the number was being checked, was gone by the time the
        booking step ran. They are kept as one application note, in order, in
        the restored history (so a model-free opening cannot lose them for a
        later run) and returned for this hop's status. `sent` is left out,
        because it is about to reach the model as the caller's own prompt.
        """
        said = [words for words in self.detour_words(checkpoint) if words != sent]
        if not said:
            return ''
        note = (
            f'While {step} ran, the caller said, in order: ' + '; '.join(f'"{words}"' for words in said) + '.'
        )
        message = ModelRequest(
            parts=[SystemPromptPart(content=note)], metadata={'application_note': 'detour'}
        )
        self.messages.append(message)
        self._protocol.append(deepcopy(message))
        return note

    def retain_action(self, name: str, receipt: dict) -> None:
        """Retain a validated application result before speech can be cancelled.

        Model tools already retain their SDK exchanges. A local action has no
        model tool call, so its receipt is explicitly an application instruction.
        """
        if not self._response.endswith(':application'):
            return
        message = ModelRequest(
            parts=[SystemPromptPart(content=f'Application result {name}: {json.dumps(receipt)}')],
            metadata={'application_action': name, 'caller_turn': self.turn},
        )
        self.messages.append(message)
        self._protocol.append(deepcopy(message))

    @property
    def all_spoken(self) -> list[Spoken]:
        """Every segment of the call so far, including old turns that can still receive late playback."""
        return [*self._speech_history, *self.spoken]

    @property
    def id(self) -> str:
        """This call's own identity, stable for its whole life."""
        return self._id

    @property
    def turn(self) -> int:
        """Which accepted caller turn this is, counting from one."""
        return self._turn

    def identity(self) -> str:
        """Who the caller is taken to be right now. A question put to one identity is not answered by another."""
        return ''

    def ask(self, proposal: Proposal) -> None:
        """Record that this question has gone out to the caller, on this turn."""
        self._proposals.ask(self, proposal)
        if intent := getattr(proposal, 'intent', ''):
            self._last_asked_intent, self._last_asked_serial = intent, self._proposals.serial

    def last_asked_left_undone(self) -> bool:
        """Whether the change last read back to the caller was left waiting: never made, and never turned down.

        Never made: its intent was never dispatched, or core withdrew it
        (`withdraw`: the caller may have talked over that line too). Never turned down: the
        active step's agent never read a reply to it as a no (`decline`). A
        read-back the caller turned down ("No, don't book Friday at two") is
        not left waiting: telling them "I haven't made that last change" as
        they leave says what they asked for as if it were a failure (session
        4d, live call 7). A reply that only asked something ("Before I answer,
        what are your hours?") left it waiting, and they are told (session
        4f): when a reply began says nothing about what it meant. A revision
        puts a fresh read-back, which is then the last one asked. The agent's
        typed call is the decision; no word of the reply is read here.
        """
        intent = self._intents.get(self._last_asked_intent)
        return (
            intent is not None
            and intent.status in ('prepared', 'withdrawn')
            and self._last_asked_serial not in self._declined
        )

    def decline(self) -> bool:
        """The active step's agent read the caller's reply as a no to the pending read-back: record it, and spend it.

        Only the question this turn can answer counts (`pending_question`:
        last turn's, same identity, heard in full), and only a read-back of an
        operation. It is consumed, so no later yes can approve it. False, with
        nothing recorded, when there is no such question: a caller cannot turn
        down what they did not hear.
        """
        question = self._proposals.pending(self)
        if question is None or not getattr(question, 'intent', ''):
            return False
        self._declined[self._proposals.serial] = self._turn
        self.answered()
        return True

    def declined_this_turn(self) -> bool:
        """Whether a read-back was turned down on this caller turn: a rendered step says so, not "pardon"."""
        return self._turn in self._declined.values()

    def hearing(self, segment: Spoken | None) -> Hearing:
        """What the evidence says about one copy of a question, for the model's context and the audits.

        `heard`: delivered (`Spoken.delivered`) and played out before the reply
        began. `early`: delivered, but the reply began before its playback
        ended. `cut`: the caller cut in on it, or a clear went out while it was
        pending. `failed`: it had no audio, or synthesis failed. `unconfirmed`:
        no evidence either way, which is not evidence the caller missed it.
        Only `heard` can be agreed to.
        """
        if segment is None:
            return 'unconfirmed'
        if segment.interrupted or segment.lost == 'cleared':
            return 'cut'
        if segment.lost:
            return 'failed'
        if segment.delivered == 'delivered':
            return 'heard' if self.answered_in_time(segment) else 'early'
        return 'unconfirmed'

    def last_hearing(self) -> Hearing | None:
        """`hearing` for the latest copy of last turn's question, or None when there is none to answer."""
        if self._proposals.last(self) is None:
            return None
        copies = self._proposals.copies(self)
        return self.hearing(copies[-1] if copies else None)

    def delivery_record(self, serial: int) -> dict[str, Any]:
        """How a question's copies reached the caller, as far as evidence goes: for a blocked or withdrawn read-back.

        The transport (`transport`), whether estimates count (`simulated_playout`),
        and per copy what said it played (`heard_by`: `mark` is the far end's
        own acknowledgement), what stopped it (`lost`) and the verdict
        (`hearing`). Observed facts only: no copy is summarised as unheard.
        """
        copies = [s for s in self.all_spoken if serial and s.proposal == serial]
        return {
            'transport': self.transport,
            'simulated_playout': self._simulated_playout,
            'copies': [
                {
                    'segment': s.id,
                    'heard_by': s.heard_by or 'none',
                    'lost': s.lost,
                    'hearing': self.hearing(s),
                    'simulated': s.simulated,
                }
                for s in copies
            ],
        }

    def note_confirmation(self, event: Literal['blocked', 'withdrawn'], about: str, reason: str) -> None:
        """One `confirmation` span for a read-back that could not be agreed to, or was taken back. Nothing was sent.

        A `write` span exists only for a dispatched action; this is the record
        of the ones that never got that far, and why: the hearing of its last
        copy (`hearing`), or what else stood in the way.
        """
        record = {'event': event, 'about': about, 'reason': reason, 'call': self._id, 'turn': self._turn}
        record |= self.delivery_record(self._confirmation_serial())
        with _tracer.start_as_current_span('confirmation') as span:
            span.set_attribute('confirmation.audit', json.dumps(record, default=str))
        logger.info(
            'confirmation {} for {}: {} on {}', event, about, reason, self.transport or 'no transport'
        )

    def _confirmation_serial(self) -> int:
        """The serial of the read-back a confirmation record is about: the one asked, else the last one asked."""
        return self._proposals.serial if self._proposals.asked is not None else self._last_asked_serial

    def withdraw(self, intent_id: str) -> None:
        """Take back a prepared intent whose read-back could not be confirmed: terminal, never dispatched, nothing pending.

        The question about it is spent, so no later reply can agree to it; the
        intent stays withdrawn, so the same operation, arguments and caller
        find it again (`intent_for`) and never get a fresh read-back
        allowance. Nothing is owed: it never reached a service. A goodbye
        still says `left_undone` for it (`last_asked_left_undone`).

        The audit's reason is the hearing of the read-back's last copy
        (`hearing`): `unconfirmed` when evidence is missing, `cut` only when
        the caller did cut in. Which retry count ran out says neither: a line
        that proved one segment heard counts every later unacknowledged copy
        as talked over.
        """
        intent = self._intents.get(intent_id)
        if intent is None or intent.status != 'prepared':
            return
        serial = self._confirmation_serial()
        copies = [s for s in self.all_spoken if serial and s.proposal == serial]
        reason = self.hearing(copies[-1] if copies else None)
        self.note_confirmation('withdrawn', intent.operation, reason)
        self.set_intent(intent_id, status='withdrawn')
        if getattr(self._proposals.asked, 'intent', '') == intent_id:
            self._proposals.consume()
            self._approved = (0, -1)

    def pending_question(self) -> Any:
        """The question this turn's reply can answer: last turn's, same identity, heard in full."""
        return self._proposals.pending(self)

    def pending(self, about: str, **fields: str) -> Any:
        """The question the caller is answering right now, if it is this one."""
        return self._proposals.pending_about(self, about, **fields)

    def open_question(self) -> Any:
        """The question put to the caller on this turn, if any: what a reminder says again."""
        asked = self._proposals.asked
        return asked if asked is not None and asked.turn == self._turn else None

    def last_proposal(self) -> tuple[Any, bool] | None:
        """Last turn's question and whether it was heard in full. For interpretation only, never authorisation."""
        return self._proposals.last(self)

    def answered(self) -> None:
        """Consume the pending question. One answer authorises one action."""
        self._proposals.consume()
        self._approved = (0, -1)

    def approve(self, about: str, **fields: str) -> bool:
        """The active step's agent read the caller's reply as a yes to the pending question: bind it to that question.

        Only the question this turn can answer counts (`pending`: last turn's,
        same identity, heard in full), with these exact field values. What is
        kept is the question's serial and the turn generation that approved
        it, both the runtime's: nothing the model sends can name either. An
        operation's `_agreed` reads it back (`approved`), so a yes from a turn
        the caller has moved on from, to a question since replaced, or to
        different arguments authorises nothing. False when there is nothing
        this reply can approve.
        """
        if self._proposals.pending_about(self, about, **fields) is None:
            return False
        running = turn_generation.get()
        generation = running[1] if running is not None and running[0] == self._id else self._generation
        self._approved = (self._proposals.serial, generation)
        return True

    def note_receipt(self, words: str, intent: str = '') -> None:
        """These words just told the caller what the business did. Kept so they can be said again if unheard."""
        self._told = (self.turn, words, intent, 0)

    def said_again(self) -> None:
        """The latest receipt was just said again, on this turn: the same receipt, for the same intent, once more."""
        _turn, words, intent, repeats = self._told
        self._told = (self.turn, words, intent, repeats + 1)

    def unheard_receipt(self, repeats: int = RECEIPT_REPEATS) -> str:
        """The latest receipt speech, if it is known not to have reached the caller and may be said again.

        Last turn's, or this turn's when the caller spoke before any of it
        played: then their words continue the turn it was said in
        (`continues_turn`), and it is still the thing they have not heard.

        Known means evidence, as for the missed-speech note: a copy the caller
        cut in on, or one whose audio never started on a runtime that reports
        playback. No record of the words is not evidence, and gives ''.

        Said again at most `repeats` times (`RECEIPT_REPEATS` without asking):
        a caller who talks over it once more is heard, not talked over in turn
        (session 4d, live call 6: the move's receipt said six times). Past
        that it is still unheard, never marked delivered: the evidence says
        what it says. A receipt a later change superseded is never said again.
        """
        turn, words, intent, said = self._told
        if not words or said >= repeats:
            return ''
        if turn not in (self.turn - 1, self.turn) or (turn == self.turn and not self.continuing):
            return ''
        filed = next((e for e in self._evidence if e.intent and e.intent == intent), None)
        if filed is not None and filed.changes and filed not in self.effective():
            return ''
        copies = [s for s in self.all_spoken if s.turn == turn and s.generated == words]
        if not copies:
            return ''
        last = copies[-1]
        return words if last.interrupted or (self._playback_known and last.playback == 'queued') else ''

    def approved(self) -> bool:
        """Whether the pending question was approved by the active step, in the current turn generation."""
        serial, generation = self._approved
        return (
            bool(serial)
            and serial == self._proposals.serial
            and generation == self._generation
            and self.pending_question() is not None
        )

    @property
    def conversation(self) -> Conversation[Any]:
        """The conversation whose readings read this call's replies (`core.conversation`).

        An agency with readings overrides this; a call without any has none,
        and nothing that needs one (a goodbye read by `core.desk`) can run on it.
        """
        raise NotImplementedError(f'{type(self).__name__} reads its replies with no conversation')

    def answering(self) -> tuple[int, tuple[str, ...]]:
        """The recorded question's serial and the ids of the segments that carried it, for an audit."""
        return self._proposals.serial, tuple(s.id for s in self._proposals.copies(self))

    def note_evidence(
        self,
        operation: str,
        args: dict[str, Any],
        receipt: dict[str, Any],
        *,
        proposal: int = 0,
        segments: tuple[str, ...] = (),
        intent: str = '',
        succeeded: bool = True,
        changes: bool = True,
        resource: str = '',
        identity: str | None = None,
        kind: str = '',
        version: int | None = None,
    ) -> None:
        """File a validated receipt. Called by `Operation.run` before anything can be cancelled.

        `identity` is whom it was done for, when that is not who is on the
        line now: an intent reconciled in a fresh process, before the caller
        is known again, was still done for the caller it was made for.
        """
        running = turn_generation.get()
        generation = running[1] if running is not None and running[0] == self._id else self._generation
        if succeeded and changes and resource and version is not None:
            self.note_version(kind, resource, version)
        self._evidence.append(
            Evidence(
                operation,
                deepcopy(args),
                deepcopy(receipt),
                self.identity() if identity is None else identity,
                self.turn,
                generation,
                proposal,
                segments,
                intent,
                succeeded,
                changes,
                resource,
                kind,
                version,
            )
        )

    def note_version(self, kind: str, resource: str, version: int) -> None:
        """The application says this resource is at `version` (or later): anything filed below it is history."""
        key = (kind, resource)
        self._versions[key] = max(version, self._versions.get(key, version))

    def is_latest(self, kind: str, resource: str, version: int | None) -> bool:
        """Whether a change at `version` is the newest this call knows of for the resource. Unversioned: yes."""
        return version is None or version >= self._versions.get((kind, resource), version)

    @property
    def evidence(self) -> tuple[Evidence, ...]:
        """Every validated receipt on this call, oldest first, whatever happened since."""
        return tuple(self._evidence)

    def effective(self) -> tuple[Evidence, ...]:
        """The receipts that still stand: each resource's latest successful change, for the caller now on the line.

        A later successful change to the same resource supersedes an earlier
        one, whichever operation made it: a move replaces the booking it
        moved, a cancellation replaces the booking with itself, a reopening
        replaces the resolution. A receipt about no declared resource stands
        for its own operation and arguments. A lookup (`changes: false`) and a
        validated refusal never stand, so neither can back a claim that
        something changed. Nothing here reads a receipt's meaning: which field
        names the resource is the application's declaration.

        A resource is its kind and its id: ticket 1 and address 1 are two.
        "Latest" is the application's version when the operation declares one,
        never the order receipts reached this call, so an old receipt found
        late by reconciliation cannot stand over a newer change. A resource
        the application says has moved past every receipt here has none
        standing: its current state is not one this call can recap.
        """
        latest: dict[tuple[str, ...], Evidence] = {}
        for filed in self._evidence:
            if not (filed.changes and filed.succeeded) or filed.identity != self.identity():
                continue
            key = (
                ('resource', filed.kind, filed.resource)
                if filed.resource
                else (filed.operation, json.dumps(filed.args))
            )
            held = latest.get(key)
            if (
                held is not None
                and held.version is not None
                and filed.version is not None
                and filed.version < held.version
            ):
                continue
            latest.pop(key, None)
            latest[key] = filed
        return tuple(f for f in latest.values() if self.is_latest(f.kind, f.resource, f.version))

    def open_intent(self, operation: str, args: str) -> Intent | None:
        """The intent for this operation and these arguments under the caller now on the line, if one is held.

        Held means open, or withdrawn (`HELD_INTENT`): a read-back core took
        back is found again, never replaced by a fresh intent.
        """
        subject = self.identity()
        return next(
            (
                i
                for i in reversed(self._intents.values())
                if i.operation == operation
                and i.args == args
                and i.subject == subject
                and i.status in HELD_INTENT
            ),
            None,
        )

    def note_unheard_readbacks(self) -> None:
        """The caller spoke: every copy of the question they are answering that they did not hear in full counts once.

        Counted against the question's intent, each copy once, so a caller who
        goes on in pieces over one read-back spends one, and a question asked
        again gets no fresh allowance.

        Two counts, because two different things lose a copy. On a line that
        has never proven one segment heard (a carrier or simulator that
        returns no marks), no copy can ever be heard, so asking again only
        loops: `Intent.readbacks`, bounded by `core.steps.READBACKS`. On a line
        that has proven delivery, an unheard copy is one the caller talked
        over, and saying it again is how they get to hear it:
        `Intent.cut_readbacks`, bounded by the larger `CUT_READBACKS`. Live
        call hzSdfMNn (session 4c) spent the first limit on two copies an
        interruptive caller cut, while every other mark came back, and gave
        up on a caller who was still trying.
        """
        asked = self._proposals.asked
        intent = self._intents.get(getattr(asked, 'intent', '')) if asked is not None else None
        if intent is None or intent.status != 'prepared':
            return
        unheard = 0
        for copy in self._proposals.copies(self):
            if copy.id in self._judged:
                continue
            self._judged.add(copy.id)
            if not (copy.delivered == 'delivered' and self.answered_in_time(copy)):
                unheard += 1
        if not unheard:
            return
        if any(s.delivered == 'delivered' for s in self.all_spoken):
            self.set_intent(intent.id, cut_readbacks=intent.cut_readbacks + unheard)
        else:
            self.set_intent(intent.id, readbacks=intent.readbacks + unheard)

    def intent_for(self, operation: str, args: str) -> Intent:
        """The open intent for this operation, these arguments and this caller, or a new one.

        Open means not terminal: a read-back said again, an answer lost to a
        cancellation and a retry after a timeout all find the same intent, so
        none of them can manufacture a fresh identity past the retry guard.
        Once it succeeded or was refused, the same arguments make a new one.
        Once it was withdrawn, they find it again (`open_intent`): it is never
        read back or dispatched on this call.
        """
        if (held := self.open_intent(operation, args)) is not None:
            return held
        made = Intent(id=uuid4().hex, operation=operation, args=args, subject=self.identity())
        self._intents[made.id] = made
        return made

    def set_intent(self, intent_id: str, **changes: Any) -> Intent:
        """Replace one intent with a validated copy carrying these changes. A terminal intent never reopens."""
        current = self._intents[intent_id]
        if current.status not in OPEN_INTENT and changes.get('status', current.status) != current.status:
            raise ValueError(f'intent {intent_id} is {current.status}; it does not change status again')
        updated = Intent.model_validate(current.model_dump() | changes)
        self._intents[intent_id] = updated
        return updated

    @property
    def intents(self) -> tuple[Intent, ...]:
        """Every intent of the call, oldest first."""
        return tuple(self._intents.values())

    def unresolved(self) -> tuple[Intent, ...]:
        """The intents that may have reached the backend with no answer that settles them: what a fresh process reconciles."""
        return tuple(i for i in self._intents.values() if i.status in ('dispatched', 'unknown'))

    def restore(self, intents: list[Intent], reconcile_with: dict[str, tuple[Any, type[BaseModel]]]) -> None:
        """Take back unresolved intents a previous process persisted, so this call reconciles them before any retry.

        Each is revalidated strictly. An id already here with a different
        operation, arguments or subject is refused: one id names one intent.
        A dispatched intent comes back unknown, because the process that was
        waiting for its answer is gone. `reconcile_with` maps an operation
        name to its operation and argument model.
        """
        for given in intents:
            intent = Intent.model_validate(given.model_dump() if isinstance(given, Intent) else given)
            held = self._intents.get(intent.id)
            if held is not None and (held.operation, held.args, held.subject) != (
                intent.operation,
                intent.args,
                intent.subject,
            ):
                raise ValueError(f'intent {intent.id} is already held for different arguments')
            if intent.status not in ('dispatched', 'unknown') or (
                held is not None and held.status not in OPEN_INTENT
            ):
                continue
            if intent.operation not in reconcile_with:
                raise ValueError(
                    f'intent {intent.id} names {intent.operation}, which this flow does not declare'
                )
            operation, args_type = reconcile_with[intent.operation]
            args = args_type.model_validate_json(intent.args)
            self._intents[intent.id] = intent.model_copy(update={'status': 'unknown'})
            self._unknown[intent.id] = (operation, args)

    def correction(self, line: str) -> str:
        """A fixed line that says nothing changed, unless an intent's outcome is unknown: then the unsettled line.

        "Nothing has been changed" is a claim too, and a false one while a
        write may have landed.
        """
        return self.locale.unsettled if self.unresolved() else line

    @property
    def departed(self) -> bool:
        """Whether the caller has said goodbye. After it, no new consequential intent starts."""
        return self._departed

    def note_departure(self) -> None:
        """The caller said goodbye. Work already dispatched is still reconciled; nothing new begins."""
        self._departed = True

    def hold(self, kind: str, held: Held) -> None:
        """Keep this, of this kind, until it is released or its `lasts` says no. One of each kind at a time."""
        self._held = {**self._held, kind: held}

    def held(self, kind: str) -> Held | None:
        """What is held of this kind, if anything."""
        return self._held.get(kind)

    def release(self, kind: str) -> None:
        """Let go of what is held of this kind: done, withdrawn, or no longer the caller's."""
        self._held = {name: held for name, held in self._held.items() if name != kind}

    @property
    def _asked(self) -> Any:
        """The recorded question, whatever its age. Read-only: record one with `ask`."""
        return self._proposals.asked

    @property
    def requests(self) -> list[Request]:
        """This turn's provider requests, oldest first."""
        return self._requests

    @property
    def spoken(self) -> list[Spoken]:
        """This turn's delivered speech segments, in the order they went out.

        The segment still streaming is included, so a reader does not have to
        know whether a run has finished to see what it said.
        """
        return self._spoken if self._open is None else [*self._spoken, self._open]

    def note_said(
        self,
        kind: Literal['fixed', 'model'],
        text: str,
        at: float | None = None,
        origin: str = '',
        tool_call_id: str = '',
    ) -> None:
        """One more segment the caller was sent, in the order it went out.

        Every route to the caller comes through here, so the ledger holds the
        model's half as well as the fixed lines: a transcript missing either
        cannot show whether consent was asked for. `at` is when the segment
        *started* going out, which for a streamed answer is its first token;
        the default is only for a line written and sent in one go. Whatever the
        model was in the middle of saying is closed first, because it went out
        first.
        """
        if text:
            self.close_model_text()
            segment = Spoken(
                kind,
                text,
                at=time.perf_counter() if at is None else at,
                origin=origin or ('model' if kind == 'model' else 'confirmation'),  # ty: ignore[invalid-argument-type]
                id=self.next_segment(),
                attempt=self._attempt,
                turn=self._turn,
                response=self._response,
                generated=text,
                tool_call_id=tool_call_id,
            )
            self._proposals.bind(self, segment)
            self._spoken.append(segment)

    def next_segment(self) -> str:
        """One id for the next stretch of speech, stable within this call."""
        self._segments += 1
        return f'{self._id}:{self._turn}:{self._segments}'

    def attempt_started(self) -> int:
        """One more run of the agency inside this caller turn. Returns which."""
        self._attempt += 1
        return self._attempt

    @property
    def attempt(self) -> int:
        """Which run of the agency is going on, counting from one within the turn."""
        return self._attempt

    def note_model_text(self, text: str) -> None:
        """One more piece of the model segment now streaming, opening it if it is the first.

        Kept open rather than appended per delta, because a segment is what
        the caller hears as one stretch of speech and `at` has to be its
        first token. Closed by the next fixed line or by the end of the run,
        whichever comes first.
        """
        if not text:
            return
        if self._open is not None and self._open.response != self._response:
            self.close_model_text()
        if self._open is None:
            self._open = Spoken(
                'model',
                '',
                id=self.next_segment(),
                attempt=self._attempt,
                turn=self._turn,
                response=self._response,
            )
            self._proposals.bind(self, self._open)
        self._open.text += text
        self._open.generated += text

    def close_model_text(self) -> None:
        """End the model segment now streaming, if there is one."""
        if self._open is not None and self._open.text:
            self._spoken.append(self._open)
        self._open = None

    def note_heard(
        self, delivered: str, *, segment_ids: tuple[str, ...] = (), interrupted: bool = False
    ) -> bool:
        """Apply a runtime-estimated delivered prefix to the identified speech segments only.

        Matched word by word, not character by character: the runtime's text
        comes back from TTS with its own spacing and punctuation. When the
        words do not line up at all, a plain report changes nothing, because an
        estimate that does not line up is not evidence. An *interrupted* one
        still is: the caller certainly did not hear everything, so the
        segments are cut to as many words as the runtime says went out, rather
        than left looking as if every word was heard. Returns whether anything
        was applied.

        A report never clears a cut-in the call already recorded. The report
        holds what TTS was handed, and TTS is handed a whole line at once: in
        Coval run 4bLgLR4t (case 05) the report of an "Anything else?" cut off
        0.2 s in held every word, cleared the cut-in's mark, and the caller's
        next words answered a question they never heard.

        Nor does a report cut a segment whose audio ran out before any cut-in
        (`playback == 'played'`). The runtime's playback signal is the stronger
        evidence. Pipecat's aggregator reports any assistant turn still open
        as interrupted when the caller next speaks, with the words it happened
        to collect. In trace fbdd81b6 a move-or-add question the caller
        answered 6.5 s later was taken as never heard, and the answer was lost.
        """
        if not segment_ids:
            segment_ids = tuple(s.id for s in self.spoken[-1:])
        by_id = {s.id: s for s in self.all_spoken}
        if not segment_ids or any(i not in by_id for i in segment_ids):
            return False
        selected = [by_id[i] for i in segment_ids]
        if len(set(segment_ids)) != len(segment_ids):
            return False
        heard = _words(delivered)
        remaining = heard
        updates: list[str] | None = []
        for segment in selected:
            text = segment.generated or segment.text
            words = _words(text)
            if remaining[: len(words)] == words:
                updates.append(text)
                remaining = remaining[len(words) :]
            elif words[: len(remaining)] == remaining:
                updates.append(_first_words(text, len(remaining)))
                remaining = []
            else:
                updates = None
                break
        if updates is None or remaining:
            if not interrupted:
                return False
            count, updates = len(heard), []
            for segment in selected:
                text = segment.generated or segment.text
                updates.append(_first_words(text, count))
                count = max(0, count - len(_words(text)))
        if any(
            s.delivery == 'runtime-estimate' and not s.text.strip().startswith(text.strip())
            for s, text in zip(selected, updates, strict=True)
        ):
            return False
        if self._open is not None and self._open.id in segment_ids:
            self.close_model_text()
        for segment, text in zip(selected, updates, strict=True):
            if segment.playback == 'played' and not segment.interrupted:
                continue
            segment.text = text
            segment.interrupted = segment.interrupted or text != segment.generated
            segment.delivery = 'runtime-estimate'
        return True

    def unbacked_claim(self) -> str:
        """Model speech this turn asserting something only the business can assert. '' when clean.

        The claim itself, when there is one, so it can be logged and traced.
        This base has no business to write to and so nothing to check against;
        an agency that does overrides it. The turn engine says a correction out
        loud rather than leaving the caller with it.
        """
        return ''

    def wrote_this_turn(self) -> bool:
        """Whether this turn has already changed the business. Base: it has no business."""
        return False

    def settlement(self) -> Callable[[], Awaitable[object]] | None:
        """What settles an earlier write whose outcome is unknown, run before any model this turn. Base: none."""
        return None

    def delivered(self, heard: str, excluding: frozenset[int] = frozenset()) -> bool:
        """Whether this utterance reached a model and is in the history it keeps.

        Input that never entered an SDK run still needs carrying into the next
        turn; identity checks distinguish a newly accepted repeated utterance
        from the same words in an older turn.
        """
        return any(
            isinstance(part, UserPromptPart) and part.content == heard and id(part) not in excluding
            for message in self.messages
            for part in message.parts
        )

    def transcript(self) -> list[dict[str, str]]:
        """What was actually said on this call, in order, as a record rather than a protocol.

        `messages` is the model and tool protocol that executed. This is the
        conversation the *caller* had: their own utterances, and every segment
        that went out to them, fixed lines and model speech alike, cut down to
        what they heard where the runtime could tell us.
        """
        result = []
        for turn in sorted(set(self._inputs) | {s.turn for s in self.all_spoken}):
            if text := self._inputs.get(turn):
                result.append({'said': 'caller', 'text': text})
            result.extend(
                {'said': s.kind, 'text': s.text} for s in self.all_spoken if s.turn == turn and s.text
            )
        return result

    def claims_completion(self, clause: str) -> bool:
        """Whether this clause, on its own, tells the caller the business has changed.

        Asked of each clause **before** it goes out, on a step that can write,
        so the answer can be to not say it. `unbacked_claim` asks the same
        question of a whole turn afterwards, which is all that can be done once
        the words are at TTS. This base has no business to check against.
        """
        return False

    def note_first_output(self, kind: Literal['text', 'tool']) -> None:
        """The newest step request has produced its first part.

        A classifier's request is timed by its interpreter, and one can be in
        flight beside a step's (`core.turn._Early`), so it is passed over. A second part in the same response does not overwrite the first: time
        to *first* output is the number, and `kind` says whether that first
        thing was speech the caller can hear or a tool call they cannot.
        """
        pending = [r for r in self._requests if r.kind != 'classifier']
        if pending and pending[-1].first_output is None:
            pending[-1].first_output = time.perf_counter()
            pending[-1].kind = kind

    def begin_turn(self) -> int:
        """Open a new accepted caller turn, and say which one it is.

        The number only has to differ from the last one; nothing reads it as a
        count. Work started under an older turn can tell that it is stale
        without knowing why.
        """
        self._generation += 1
        self._began_as[self._generation] = self.identity()
        return self._generation

    def is_current(self, generation: int) -> bool:
        """Whether work from that turn may still change where the caller is."""
        return generation == self._generation

    def commit(self, **updates) -> None:
        """Replace the whole state with one validated candidate, or change nothing.

        Built from values rather than `model_copy(update=...)`, which copies
        updates in without validating them, and rather than a sequence of
        assignments, which commits the first field before the second is
        checked. A tool writing a phone number and a verification status
        together gets both or neither.

        A keyword naming no field is refused rather than dropped: Pydantic
        ignores an extra keyword by default, so a typo in a tool would become a
        state write that silently did not happen.

        The updates are copied first, so a list or model the caller keeps and
        changes later cannot reach into the saved state, and the candidate is
        validated with unknown nested keys refused, not dropped.

        A commit from a turn the caller has moved on from keeps its business
        facts and loses its flow fields, and loses everything if the caller is
        now somebody else: their visible state is not the old caller's to
        write. What the service did is on record either way (`evidence`). Where the caller *is* belongs to the
        current turn and a stale answer about it is wrong by definition; a
        booking that really happened is a fact whichever turn made it, and
        refusing to record it would leave the diary holding an appointment the
        call would book again.
        """
        if unknown := set(updates) - set(type(self.state).model_fields):
            raise ValueError(f'no such field on {type(self.state).__name__}: {", ".join(sorted(unknown))}')
        running = turn_generation.get()
        if running is not None and running[0] == self._id and running[1] != self._generation:
            if self._began_as.get(running[1], self.identity()) != self.identity():
                logger.info('fenced a late commit from turn {}: the caller is now somebody else', running[1])
                return
            if late := set(updates) & self.state.flow_fields():
                logger.info('fenced a late commit of {} from turn {}', ', '.join(sorted(late)), running[1])
                updates = {name: value for name, value in updates.items() if name not in late}
            if not updates:
                return
        candidate = self.state.model_dump() | deepcopy(updates)
        self.state = type(self.state).model_validate(
            candidate, extra='forbid', context=self.validation_context()
        )

    def validation_context(self) -> dict[str, Any]:
        """What an application's validators may read (`ValidationInfo.context`) whenever core validates its state.

        The call's own clock, read once per call (`today`), so a validator can
        turn "Thursday" into one date, the same date all call long. Plain
        values, no services and nothing that changes under a validator: a
        validator does no I/O and needs no core import to read it.
        """
        return {'today': self.today.date, 'now': self.today.at}

    async def say(self, text: str, origin: str = 'entry', tool_call_id: str = '') -> None:
        """Speak one fixed line now, without waiting for the model.

        At most one per gap: an entry line and a tool's line cover the same
        silence, and a caller should not hear both back to back. The turn
        engine opens a new gap at the start of each turn and closes this one as
        soon as the model's own words start.
        """
        if self._covered or self._say is None:
            return
        self._covered = True
        self.note_said('fixed', text, origin=origin, tool_call_id=tool_call_id)
        await self._say(text)

    def resume(self, fallback: str) -> str:
        """Where the caller goes now this step is done: back to what they were doing.

        A finisher calls this instead of asking the model where to go next. The
        step that was waiting recorded itself on the way in, so the application
        already knows the answer.

        Never the step that is finishing. A step entered and then left without
        finishing leaves its record behind, and resuming into it would hand the
        caller to the agent they are already with: a whole model request that
        says nothing.
        """
        step = self.state.pending
        if step is not None:
            self.commit(pending=None)
        return step if step is not None and step != self.state.agent else fallback

    def turn_started(self, heard: str = '') -> None:
        """The caller has said something. Either a new turn, or more of the one the agent has not answered yet.

        Last turn's speech moves from the per-turn ledger to the per-call
        record first. `heard` is kept for the whole turn, because an action
        three hops later still has to be able to ask what the caller actually
        asked for rather than take the model's word for it.

        A continuation (`continues_turn`) keeps the turn's number, so the
        question the caller is answering is still the one before it, and joins
        the words, so everything that reads `heard` reads the whole reply. The
        answer to the cut-off piece is kept as far as the caller heard it:
        what never started playing is recorded as heard by nobody, what was
        playing keeps the runtime's estimate, and the generated text stays for
        audit. The question ledger is rewound to where the turn began. Nothing
        the caller said is dropped: every piece is in `_pieces`, and `fresh`
        is what the model has not been sent yet. Either way, what the caller
        missed of the last speech is said to this piece's runs (`spoken_over`).
        """
        self.note_unheard_readbacks()
        continuing = self.continues_turn()
        self.close_model_text()
        missed = _missed(self._spoken, self._playback_known)
        self._continued_from = (
            [
                f'{s.origin}:{s.playback}{":cut" if s.interrupted else ""}{"" if s.text else ":blank"}'
                for s in self._spoken
            ]
            if continuing
            else []
        )
        if continuing:
            for segment in self._spoken:
                if segment.playback != 'played':
                    segment.interrupted = True
                if segment.playback == 'queued':
                    segment.text, segment.delivery = '', 'runtime-estimate'
        self._speech_history.extend(self._spoken)
        if continuing:
            logger.info(
                'turn {} continues with piece {}: the caller went on before any answer played out ({})',
                self._turn,
                len(self._pieces) + 1,
                ', '.join(self._continued_from) or 'nothing said',
            )
            self._pieces.append(heard)
            self._proposals.rewind(self)
            self._declined = {serial: turn for serial, turn in self._declined.items() if turn != self._turn}
        else:
            self._turn += 1
            self._pieces = [heard]
            self._attempt = 0
            self._acted = False
            self._proposals.age(self)
            self._proposals.open_turn()
        self._spoken_over = missed
        self.heard = ' '.join(p for p in self._pieces if p)
        self._inputs[self._turn] = self.heard
        self._interrupted = False
        self._reached = False
        self._leaving = False
        self._changing = set()
        self._tries = Counter()
        self._kept = Counter()
        self._request_kept = False
        self._response = ''
        self._entered = ''
        self._interpretations.clear()
        self._requests.clear()
        self._spoken.clear()
        self._open = None
        self._spoke = False
        self._reminders = 0
        self._held = {
            name: held for name, held in self._held.items() if held.lasts is None or held.lasts(self)
        }

    async def speak_now(self, text: str, origin: str = 'confirmation') -> None:
        """Say this whatever else was said. For a confirmation, a question or a goodbye, never a filler.

        Recorded even with no runtime attached: the words were decided and
        handed on, and whether anyone received them is a separate fact
        (`Spoken.delivered`), unknown until a signal or an acknowledgement.
        """
        self._answers += 1
        self.note_said('fixed', text, origin=origin)
        if self._say is not None:
            await self._say(text)

    def note_ending(self, kind: str, reason: str, *, observed: bool, at: float | None = None) -> None:
        """One piece of how this call ended: what happened, why, and whether it was seen or only intended.

        The pieces are kept apart because they are different facts: the
        application deciding to close (`observed=False`), the pipeline
        shutting down, the media stream stopping, and the carrier confirming
        the call completed. Only the carrier's own report is carrier closure;
        an `EndFrame` or a failed hang-up request is not. A cause nobody
        observed stays unknown.
        """
        self._endings.append(
            {
                'kind': kind,
                'reason': reason,
                'observed': observed,
                'at': time.perf_counter() if at is None else at,
            }
        )

    @property
    def endings(self) -> tuple[dict[str, Any], ...]:
        """Every piece of ending evidence, in the order it arrived."""
        return tuple(dict(e) for e in self._endings)

    def end_requested(self, why: Literal['caller', 'abandoned', 'transport', 'error']) -> None:
        """Record *why* the call is ending, before anything acts on it.

        Queuing a goodbye and observing a disconnect are different facts, and a
        record that cannot tell "the caller rang off" from "the agency hung up
        on them" scores the second as answered.
        """
        self._ended = why
        self.note_ending('intent_to_close', why, observed=False)

    @property
    def ended(self) -> str:
        """Why this call is ending, or '' while it is not."""
        return self._ended

    async def end_call(self) -> None:
        """Hang up, once the caller has said the call is over."""
        if not self._ended:
            self._ended = 'caller'
        if self._end is not None:
            await self._end()


def _ledger() -> ProposalLedger[Any]:
    """A fresh ledger. Imported here because `core.proposals` builds on this module."""
    from core.proposals import ProposalLedger

    return ProposalLedger()
