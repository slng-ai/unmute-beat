"""Sage and Stone: four agents, one call, one shared state.

What this does differently from the same agency built on a node graph:

* **The prompt is already branched.** Every `{{#if}}` is resolved before the
  model reads it, so an agent never sees an instruction that does not apply to
  this call. The node-graph version had to write both branches as prose and ask
  the model to pick ("If verification so far is empty, run verify first").
* **Anything code can work out, code works out.** Which day "next Friday" is,
  whether a slot has already passed, what a booking sounds like said out loud.
  Three of the six call variables disappeared with it.
* **A step that finishes its job ends its own run.** `confirm_customer` and
  `save_booking` are output functions: one model request does the work, records
  it and names who takes the caller next. A failure raises `ModelRetry` instead,
  which keeps the step open with the real reason in front of the model.
* **A refusal carries the alternatives.** The diary says what the caller
  actually holds, or which slots are really free, so the model's next turn is a
  correction rather than a guess.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Literal, NoReturn

from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic_ai import Agent, ModelRetry, RunContext, TemplateStr, Tool, ToolOutput
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models import infer_model
from pydantic_ai.models.google import GoogleModelSettings
from pydantic_ai.tools import ToolDefinition

from salon import diary
from salon.state import (
    AgentName,
    Appointment,
    Asked,
    Date,
    Handoff,
    Id,
    Phone,
    SalonCall,
    SalonState,
    Service,
    SubmittedPhone,
    Time,
    Unresolved,
    agrees,
    ends,
    move_or_add,
)
from voice.harness import (
    STRICT,
    Exit,
    LocalAction,
    Node,
    claim_or_refuse,
    one_action_per_reply,
    request_clock,
    speak_by,
)
from voice.harness import lent as _lent
from voice.harness import pick as _pick
from voice.harness import present as _present

# --- the service boundary ----------------------------------------------------

# What the salon service is allowed to have said. `salon.diary` stands in for
# an HTTP service and hands back plain JSON-shaped dicts, so every one of them
# is validated here before a single field is read. A return annotation on the
# service function is not validation: Python does not check it, and the one
# place a malformed response could reach the state is the place that trusted
# the annotation.


class Held(BaseModel):
    model_config = STRICT
    booking_id: Id
    service: Service
    date: Date
    time: Time
    spoken: str


class Slot(BaseModel):
    model_config = STRICT
    slot_id: str
    service: Service
    time: Time
    spoken: str


class Slots(BaseModel):
    model_config = STRICT
    status: Literal['ok', 'full', 'need_day', 'past', 'closed']
    bookings: list[Held] = []
    slots: list[Slot] = []
    day: str = ''
    date: Date | None = None
    note: str = ''
    price_euros: int | None = None


class Saved(BaseModel):
    model_config = STRICT
    status: Literal['booked', 'moved', 'cancelled']
    booking_id: Id
    service: Service
    date: Date
    time: Time
    spoken: str


class BookingCommand(BaseModel):
    model_config = STRICT
    action: Literal['book', 'move', 'cancel']
    slot_id: str
    booking_id: str


_PHONE = TypeAdapter(SubmittedPhone)


class CustomerLookup(BaseModel):
    model_config = STRICT
    # No '' here. An empty status is what the state holds before a lookup has
    # run; it is not something a lookup may come back with.
    customer_status: Literal['existing', 'created']
    customer_phone: Phone


class ComplaintRecord(BaseModel):
    model_config = STRICT
    recorded: bool
    reference: Id


def operation_token(*parts: object) -> str:
    """One stable name for a business action, across every attempt at it.

    Derived rather than generated, so a retry of the same action derives the
    same token without anything having to remember it between attempts. What
    it identifies is the action -- this caller, moving this booking to this
    slot -- and not the request that carried it.
    """
    return hashlib.sha256('|'.join(str(part) for part in parts).encode()).hexdigest()[:16]


# What the diary calls the outcome of each action it was asked to perform. A
# response that reports one of the other two is not this request's answer,
# however well-formed it is.
DID = {'book': 'booked', 'move': 'moved', 'cancel': 'cancelled'}

# Said by every entry point while an earlier write is outstanding. One string,
# because a complaint and a booking are refused for the same reason and two
# copies of a reason drift.
OUTSTANDING = (
    'There is a write on this call whose outcome is still unknown, so nothing else may be '
    'written until somebody checks it. Tell the caller you will confirm what happened, and '
    'do not try to book, move or cancel anything.'
)

UNKNOWN = (
    'It is not clear whether that saved: {why}. Nothing else can be written on this call until '
    'somebody checks. Tell the caller you will confirm it and not to book again, and do not call '
    'this a second time.'
)


# --- asking the caller, in words this code chose -----------------------------

# "The same time" names a time exactly, without naming any digits: the one the
# caller already holds. `prompts/salon/_diary.md` has said so for a while; this
# is the half of that rule the application enforces.
_SAME_TIME = re.compile(r'\b(?:same|usual)[\s\u2014\u2013-]+time\b', re.IGNORECASE)


class Ask(Asked):
    """A question for the caller, worded here and said by the core.

    An output type, so putting the question ends the run: one model request
    decides *that* a question is needed and this code decides what it says.
    The alternative -- letting the model word it -- is what the calls of
    2026-09-21 did, and a model that writes the question also gets to decide
    afterwards what it was: it asked which country the caller was ringing from
    and then treated the yes as agreement to a phone number.

    `Asked` is the record the next turn checks against; this adds the sentence
    the caller hears, so the two can never be about different things.
    """

    model_config = STRICT

    spoken: str

    def put(self, call: SalonCall) -> str:
        """Record the question as pending on the call, and hand back what to say."""
        call.ask(self.model_copy(update={'owner': call.state.customer_phone}))
        return self.spoken


def _readback(phone: str) -> str:
    """A number as digits to read out, grouped the way a person says one."""
    country, rest = phone[:3], phone[3:]
    return ' '.join([country, rest[:3], rest[3:6], rest[6:]]).strip()


async def ask_number(ctx: RunContext[SalonCall], phone: SubmittedPhone) -> Ask:
    """Read a number back to the caller and wait. The only way to reach `confirm_customer`.

    Nothing is looked up until they have agreed to these exact digits. Their
    answer next turn is what decides it, so say nothing after calling this.

    Args:
        phone: The number to read back, in E.164, exactly as they gave it.
    """
    return await _ask_number(ctx.deps, phone)


async def _ask_number(call: SalonCall, phone: str) -> Ask:
    phone = _PHONE.validate_python(phone)
    return Ask(
        about='phone',
        value=phone,
        spoken=f'Let me read that back to you: {_readback(phone)}. Have I got that right?',
    )


async def verification_guidance(
    ctx: RunContext[SalonCall],
    topic: Literal['number', 'country', 'incomplete', 'correction', 'unsupported'],
) -> str:
    """Ask for missing phone information. For a complete number, use ask_number instead."""
    text = {
        'number': 'What phone number should I use, including the country code?',
        'country': 'Which country is that phone number from?',
        'incomplete': 'Could you give me the remaining digits?',
        'correction': 'Could you repeat the full number, including the country code?',
        'unsupported': 'I can look up Spanish phone numbers. Do you have a Spanish number I can use?',
    }[topic]
    ctx.deps.answered()
    await ctx.deps.speak_now(text, origin='guidance')
    return text


async def ask_booking(
    ctx: RunContext[SalonCall],
    action: Literal['book', 'move', 'cancel'],
    slot_id: str = '',
    booking_id: str = '',
) -> Ask:
    """Say back what you are about to write, and wait for their yes. Nothing is saved by this.

    The sentence is built here from the diary's own record of the slot, so it
    cannot describe a time the caller was never offered. Say nothing after
    calling it: their next answer is what decides.

    Args:
        action: book a new time, move an existing booking, or cancel one.
        slot_id: The slot exactly as the diary returned it. Empty when cancelling.
        booking_id: The booking exactly as the diary returned it. Empty when booking.
    """
    call = ctx.deps
    phone = call.state.customer_phone
    if action in ('book', 'move'):
        offered = call.diary.offer_of(call.id, slot_id, phone)
        if not offered:
            raise ModelRetry(
                'Nothing was asked. That slot is not one this caller is currently being offered; '
                'read the diary again and offer a time it actually returned.'
            )
        service = offered['service'].replace('hair-color', 'hair colour')
        words = (
            f'So that is a {service}, {offered["spoken"]}. Shall I book that in?'
            if action == 'book'
            else f'So I will move you to {offered["spoken"]}. Shall I do that?'
        )
    else:
        held = {b['booking_id']: b for b in call.diary.bookings_for(phone, call.diary.now().date())}
        if booking_id not in held:
            raise ModelRetry(
                'Nothing was asked. That is not a booking this caller holds; read the diary to see '
                'what they actually have.'
            )
        one = held[booking_id]
        service = one['service'].replace('hair-color', 'hair colour')
        words = f'So that is your {service} {one["spoken"]} cancelled. Shall I go ahead?'
    return Ask(about='booking', value=slot_id, action=action, booking_id=booking_id, spoken=words)


# The words in "I'd like to raise a complaint" and nothing else. A summary
# made only of these describes wanting to complain, not what went wrong, and
# reading it back asks the caller to confirm their own request.
#
# ponytail: a closed word list, like the consent matchers in `salon/state.py`,
# and it fails the same way -- towards asking what happened. Upgrade it with
# them.
_COMPLAINT_REQUEST_WORDS = (
    'a about an are be been caller can client complain complaining complaint complaints could customer '
    'er file files filing formal had has have he hello her hi his i id im in is it leave leaving like '
    'lodge lodges lodging make makes making may maybe me might my need official our please put raise '
    'raises raising register registering she someone something submit submitting the their they to uh '
    'um us want wants was we were wish would yeah yes you your '
)
_ASKING_TO_COMPLAIN = frozenset(_COMPLAINT_REQUEST_WORDS.split())


def _describes_nothing(summary: str) -> bool:
    """Whether this summary says only that they want to complain."""
    words = [word.strip("'") for word in re.findall(r"[a-zA-Z']+", summary.lower())]
    return not words or all(word in _ASKING_TO_COMPLAIN for word in words)


async def ask_complaint(ctx: RunContext[SalonCall], summary: str, requested_resolution: str) -> Ask:
    """Ask for approval once the complaint details and requested outcome are known.

    Use this instead of asking whether to record their complaint in ordinary
    speech. It speaks the readback and waits; nothing is recorded or approved yet.

    Args:
        summary: The shortest complete excerpt of the caller's words describing the faulty service.
            Exclude filler and the requested outcome. Do not paraphrase or add dates from an upcoming
            booking. Punctuation and case may differ.
        requested_resolution: An exact excerpt of the caller's requested outcome, or empty if unspecified.
            Never infer a redo. Recording a request does not approve it.
    """
    if not summary.strip():
        raise ModelRetry('Nothing was asked: a complaint summary cannot be empty.')
    if _describes_nothing(summary):
        # On 2026-09-21 14:59 a caller said "maybe raising a complaint" and
        # heard "So, Raising a complaint. Have I got that right?" back before
        # anyone had asked what happened -- and then had to be asked again.
        raise ModelRetry(
            f'Nothing was asked: {summary.strip()!r} is them asking to complain, not what went '
            'wrong. Ask them what happened, and put it to them once they have said.'
        )
    for label, excerpt in (('summary', summary), ('requested_resolution', requested_resolution)):
        if excerpt and not caller_excerpt(ctx.deps, excerpt):
            raise ModelRetry(
                f'Nothing was asked: {label} must quote the caller, not an assistant or a booking record. '
                'Copy their words exactly; leave requested_resolution empty if they have not requested one.'
            )
    return Ask(
        about='complaint',
        value=_gist(summary),
        resolution=_gist(requested_resolution),
        summary=summary,
        requested_resolution=requested_resolution,
        spoken=f"I'll note your words: {summary.strip().rstrip('.?!')}. "
        + (f'Your request: {requested_resolution.strip().rstrip(".?!")}. ' if requested_resolution else '')
        + 'The salon will review this; no refund or booking change is approved. Shall I record that?',
    )


def caller_excerpt(call: SalonCall, excerpt: str) -> bool:
    """Ground readback facts in caller speech, never model-authored history.

    Extractive readbacks trade paraphrasing for inspectable source evidence.
    Case and punctuation may differ because STT inserts its own punctuation.
    """

    def words(text: str) -> str:
        return ' '.join(re.findall(r'\w+', text.casefold()))

    sources = [row['text'] for row in call.transcript() if row['said'] == 'caller']
    sources.extend(
        part.content
        for message in call.messages
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    )
    wanted = words(excerpt)
    return bool(wanted) and any(f' {wanted} ' in f' {words(source)} ' for source in sources)


def _gist(summary: str) -> str:
    """One stable name for a complaint summary, so agreement is to the summary that was read out."""
    return hashlib.sha256(' '.join(summary.casefold().split()).encode()).hexdigest()[:12]


def _disagrees(action: str, booking_id: str, offered: dict, written: Saved) -> str:
    """Why a well-formed result is not an answer to the request, or '' when it is.

    Pydantic says every field holds a legal value. It cannot say the result is
    about the thing that was asked for, and a service answering `cancelled`
    to a booking request answers in perfectly legal values: on the call this
    check was written for, the wrapper accepted it, stored an appointment
    marked `book`, and read the cancellation wording out to the caller.
    """
    if written.status != DID[action]:
        return f'a {written.status} result came back from a {action} request'
    if action in ('move', 'cancel') and booking_id and written.booking_id != booking_id:
        return f'it is about booking {written.booking_id}, not {booking_id}'
    if action in ('book', 'move') and offered:
        got = {
            # Back to the wire shape the offer was recorded in: the offer is a
            # dict the service handed over, and comparing a `date` against the
            # string it was written as is a comparison that always disagrees.
            'date': written.date.isoformat(),
            'time': written.time.strftime('%H:%M'),
            'service': written.service,
        }
        # Only the three that identify the slot. `offer_of` also hands back the
        # one phrasing to say it out loud, which is presentation and is not
        # something a result has to agree with.
        was = {key: offered[key] for key in got if key in offered}
        if got != was:
            return f'it is {got}, not the slot that was offered: {was}'
    return ''


def _unknown(call: SalonCall, pending: Unresolved, why: str) -> NoReturn:
    """Record this write as outstanding and refuse, which is the only thing that blocks the next one.

    On the state, not in a sentence. Retry text asking the model not to try
    again is not a guard, and changing one argument changes the token, so the
    service's own idempotency cache never sees the second attempt.
    """
    call.commit(unresolved=pending)
    raise ModelRetry(UNKNOWN.format(why=why))


def _answered[Result: BaseModel](
    call: SalonCall,
    pending: Unresolved,
    shape: type[Result],
    attempt: Callable[[], object],
    book: str,
) -> Result:
    """Perform one write and read its answer, or record the outcome as unknown and refuse.

    One place, because a booking and a complaint fail the same three ways: the
    service does not answer, it answers in a shape nothing can read, or it
    answers about something else. Only the third is specific to what was
    written, so only the third is left to the caller of this.

    A lost answer is never "nothing happened". The write may well have
    committed, and the token is asked what it actually did before anything
    concludes otherwise -- which is what the complaint book had a token for
    and no code to use it, so a lost response there wrote the grievance down
    twice.
    """
    try:
        raw = attempt()
    except diary.RefusedError:
        # The service saying no, in words the caller can hear. Not this
        # function's business: an answer, and a clear one.
        raise
    except Exception:
        recovered = call.diary.outcome_of(pending.token)
        if recovered is None:
            _unknown(call, pending, f'the {book} did not answer')
        raw = recovered
    try:
        return shape.model_validate(raw)
    except ValidationError:
        # The same question the lost-answer path asks, and for the same
        # reason: the service may have recorded a perfectly good outcome
        # under this token and merely told us about it badly.
        recovered = call.diary.outcome_of(pending.token)
        if recovered is None or recovered is raw:
            _unknown(call, pending, f'the {book} answered in a shape this cannot read')
        try:
            return shape.model_validate(recovered)
        except ValidationError:
            _unknown(call, pending, f'the {book} answered in a shape this cannot read')


async def _settle(call: SalonCall) -> str:
    """Finish an earlier write whose outcome was never established, or refuse to move past it.

    An operation the service never answered for is not "nothing happened".
    Until it is settled, a second write is how one caller gets two
    appointments -- and the only thing standing in the way used to be a
    sentence of retry text asking the model not to. Changing an argument
    changes the token, so the idempotency cache never saw the second attempt.

    What comes back is checked against what was *asked for*, by the same rule
    the first answer was checked by. It used to be checked against nothing:
    only a token was retained, so the action was read off whatever status
    came back, and a `cancelled` result refused on the first attempt as an
    answer to a `book` request was accepted on the second as an ordinary
    cancellation -- stored, confirmed out loud, and the caller's booking gone.

    Returns what to say when the earlier write turns out to have landed, and
    raises when it is still unknown or still does not answer the question.
    """
    pending = call.state.unresolved
    assert pending is not None, 'only reached while a write is outstanding'
    outcome = call.diary.outcome_of(pending.token)
    if outcome is None:
        raise ModelRetry(OUTSTANDING)
    if pending.action == 'complaint':
        try:
            record = ComplaintRecord.model_validate(outcome)
        except ValidationError:
            raise ModelRetry(UNKNOWN.format(why='the complaint book answered unreadably')) from None
        if not record.recorded:
            # Settled, and settled as a refusal: the book answered, it said
            # no, and nothing is outstanding any more.
            call.commit(unresolved=None)
            raise ModelRetry(
                'The complaint was never taken -- the book refused it. Tell the caller it is not '
                'logged and that someone will call them back.'
            )
        call.commit(writes=call.state.writes + 1, unresolved=None)
        call.retain_action('record_complaint', record.model_dump(mode='json'))
        await call.speak_now(acknowledgement(record.reference, pending.wanted))
        return f'complaint recorded: {record.reference}'
    try:
        written = Saved.model_validate(outcome)
    except ValidationError:
        raise ModelRetry(UNKNOWN.format(why='the diary answered in a shape this cannot read')) from None
    # The request, not the reply, says what this was. Reading the action off
    # the status is what let a contradiction in as a completion.
    action = pending.action
    if why := _disagrees(action, pending.booking_id, pending.offered, written):
        raise ModelRetry(UNKNOWN.format(why=why)) from None
    call.commit(
        appointment=Appointment(
            booking_id=written.booking_id,
            service=written.service,
            date=written.date,
            time=written.time,
            spoken=written.spoken,
            action=action,
        ),
        agent=call.resume('concierge'),
        writes=call.state.writes + 1,
        unresolved=None,
    )
    call.retain_action('save_booking', written.model_dump(mode='json'))
    await call.speak_now(
        _pick(f'saved:{written.status}', SAVED[written.status]).format(
            spoken=written.spoken, service=written.service.replace('hair-color', 'hair colour')
        )
    )
    return f'{written.status}: {written.spoken}'


# Actions that change where the caller is, or write to the salon. At most one
# of these runs per model response; see `one_action_per_reply`.
ACTIONS = frozenset(
    {
        'hand_over',
        'confirm_customer',
        'save_booking',
        'hang_up',
        'record_complaint',
        'read_diary',
        'offer_times',
        'stop_verifying',
    }
)

# The request at which a run runs out of ordinary tools and has to answer with what
# it holds. Three, because the longest legitimate chain is two tool-bearing requests:
# read the diary, then correct what it refused. See `voice.harness.speak_by`.
SPEAK_BY = 3

# What the caller hears the moment the diary is written, said by this code. The
# `{spoken}` phrase is the diary's own, computed once, so two turns cannot
# describe one appointment two different ways. Alternatives because a caller who
# books and then moves it hears this twice inside a minute.
SAVED: dict[str, tuple[str, ...]] = {
    'booked': (
        "You're all set for {spoken}. Anything else I can help with?",
        "Lovely, that's your {service} booked for {spoken}. What else can I do?",
        'Done, {spoken}. Anything else?',
    ),
    'moved': (
        "That's moved to {spoken}. Anything else I can help with?",
        "Right, you're down for {spoken} now. What else can I do?",
        'Moved to {spoken}. Anything else?',
    ),
    'cancelled': (
        "That's cancelled for you. Anything else I can help with?",
        "Right, that's cancelled. What else can I do?",
        'Cancelled. Anything else?',
    ),
}

# What the caller hears the moment the complaint is written, for the same reason
# `SAVED` exists: the model's own next turn used to say it too. A live call had
# the filler say "Let me get that written down." and the following request say
# "I have noted that down for you." -- one fact, twice, a round trip apart. Said
# here it is said once, and the model's turn is free to be the next step.
NOTED = "That's on record, reference {reference}. {next_step}"
# The two next steps the policy actually states, and nothing else. A live call
# promised a manager would ring back within two working days -- which is two
# commitments welded together, neither of them what `knowledge/refunds.md`
# says. Said here rather than by the model, for the same reason a booking
# confirmation is: there is one right sentence and a round trip spent asking
# for it is a round trip that can get it wrong.
REPLY_SOON = 'The salon will get back to you within two working days.'
MANAGER_CALLS = 'That one goes to the manager, who will call you back.'
# What sends a complaint to the manager. Straight out of the policy document:
# a half or a full refund. Anything else gets the ordinary reply.
_WANTS_MONEY = re.compile(r'\b(?:refund|money back|reimburse|compensat\w*|my money)\b', re.IGNORECASE)


def acknowledgement(reference: str, requested_resolution: str) -> str:
    """The one thing said when a complaint is written down: its reference, then the policy step."""
    step = MANAGER_CALLS if _WANTS_MONEY.search(requested_resolution) else REPLY_SOON
    pending = ' Your requested resolution is pending approval.' if requested_resolution else ''
    return NOTED.format(reference=reference, next_step=step) + pending + ' Anything else I can help with?'


PROMPTS = Path(__file__).resolve().parent.parent / 'prompts' / 'salon'

# One model for now; `build` takes it per agent, so giving `complaints` a
# stronger one or `verify` a cheaper one is a one-word edit.
MODEL = infer_model('google-cloud:gemini-3.5-flash-lite')
# Thinking pinned off rather than left out. Left out, no `thinking_config` is
# sent at all and whatever Google currently defaults this model to is what a
# call gets -- a setting that can change under us, on the one dimension a voice
# turn cannot absorb. The live trace showed no thinking tokens, so this is
# writing down what is already true rather than changing it.
SETTINGS: object = GoogleModelSettings(
    service_tier='priority',
    google_thinking_config={'thinking_level': 'MINIMAL'},  # ty: ignore[invalid-argument-type]
)


GREETING = "Hi, you've reached Sage and Stone. Robin speaking, what can I do for you?"

KNOWLEDGE = Path(__file__).resolve().parent.parent / 'knowledge'

_VOICE = (PROMPTS / '_voice.md').read_text()
_DIARY = (PROMPTS / '_diary.md').read_text()
# The salon's own documents, read once at import and substituted into the two
# prompts that answer from them. They were a retrieval tool until the call of
# 2026-09-21; see `prompt` below.
_DOCS = {name: (KNOWLEDGE / f'{name}.md').read_text() for name in ('services', 'refunds')}
COMPLAINT_GUIDANCE = json.loads((KNOWLEDGE / 'complaint-guidance.json').read_text())


def prompt(name: str) -> TemplateStr[SalonCall]:
    """Shared voice guidance, the role's task, then the current caller message.

    Compiled against SalonState at import: a `{{placeholder}}` naming no field
    raises here and the process never starts, rather than rendering as an empty
    string into a live call. The shared block lives in one file, so a voice rule
    is fixed once instead of in four prompts that slowly disagree.

    `<!--diary-->` expands the same way, and for the same reason: two steps hold
    `read_diary` now -- the front desk borrows it once the caller is identified
    -- and the rules for using it are hard-won enough that two copies would
    drift. A plain substitution rather than a Handlebars partial, because it is
    one line and needs nothing registered.

    `<!--services-->` and `<!--refunds-->` are the salon's documents themselves.
    They used to be a retrieval tool, and on the call of 2026-09-21 a caller
    moving an appointment cost seven consecutive lookups: the first one asked
    for the "cancellation policy" and word overlap returned nothing, so the
    model kept rewording the question until the turn's whole request budget was
    gone and it apologised. Seven hundred words is small enough to simply be in
    the prompt, and a document that is already in front of the model cannot be
    searched for badly -- it also takes a silent model request out of every
    turn that answers a question about the salon.
    """
    text = (PROMPTS / f'{name}.md').read_text().replace('<!--diary-->', _DIARY)
    for topic, document in _DOCS.items():
        text = text.replace(f'<!--{topic}-->', document)
    return TemplateStr(
        f'{_VOICE}\n\n{text}\n\n## Latest caller message\n\n'
        'Use this with the conversation above, including earlier speech fragments.\n\n> {{heard}}',
        deps_type=SalonCall,
    )


# --- what the front desk can do ---------------------------------------------


async def complaint_guidance(
    ctx: RunContext[SalonCall],
    topic: Literal['details', 'resolution', 'review', 'refunds', 'products', 'followup'],
) -> str:
    """Speak approved salon guidance and finish this turn. No booking or approval.

    Args:
        topic: details asks what happened; resolution asks what outcome they want;
            review explains that requested free services or booking conversions need salon review;
            refunds explains approval tiers; products explains return conditions;
            followup explains the recorded complaint's response time.
            Use ask_complaint instead when their complaint and requested outcome are already known.
    """
    ctx.deps.answered()
    text = COMPLAINT_GUIDANCE[topic]
    await ctx.deps.speak_now(text, origin='guidance')
    return text


async def record_complaint(ctx: RunContext[SalonCall]) -> ComplaintRecord:
    """Record the exact complaint and resolution the caller already approved, then stop.

    It speaks the receipt itself. Do not reword the approved complaint or say
    another acknowledgement. It uses the approved wording without changes.
    """
    approved = ctx.deps._complaint
    if approved is None:
        raise ModelRetry('Use ask_complaint and wait for approval before recording anything.')
    return await _record_complaint(ctx, approved.summary, approved.requested_resolution)


async def _record_complaint(
    ctx: RunContext[SalonCall], summary: str, requested_resolution: str
) -> ComplaintRecord:
    """Write the caller's complaint down, once they have agreed to your summary of it.

    Recording what they asked for is not approving it.

    Args:
        summary: What went wrong and when, addressed to the caller. Exclude the requested resolution.
        requested_resolution: What they want the salon to do, or an empty string.
    """
    claim_or_refuse(ctx, ACTIONS)
    return await _record_approved_complaint(ctx.deps, summary, requested_resolution)


async def _record_approved_complaint(
    call: SalonCall, summary: str, requested_resolution: str
) -> ComplaintRecord:
    """Shared execution boundary for model tools and application steps."""
    phone = call.state.customer_phone
    if call.state.unresolved:
        # The same bar a booking meets. A write nobody has settled blocks
        # every other one, whichever book it would be written in.
        raise ModelRetry(OUTSTANDING)
    if not _present(call.state.customer_verified):
        raise ModelRetry(
            'Nobody is identified yet. Return to concierge so verification can run before complaints.'
        )
    approved = call._complaint
    if (
        approved is None
        or approved.value != _gist(summary)
        or approved.resolution != _gist(requested_resolution)
    ):
        raise ModelRetry(
            'Nothing was written down. Read this summary and requested resolution back with '
            '`ask_complaint` and wait for their answer. Use the exact wording they approved.'
        )
    if approved.owner and approved.owner != phone:
        call._complaint = None
        raise ModelRetry('The caller identity changed. Confirm the complaint again with ask_complaint.')
    call._complaint = None
    call.answered()
    # One token per complaint, so an attempt that lost its answer and is
    # retried leaves one entry in the book rather than two. Same derivation
    # as a booking's: the call, the writes it has already had accepted, and
    # what this one is about.
    token = operation_token(call.id, call.state.writes, phone, summary, requested_resolution)
    # Through the same boundary a booking goes through. The token alone was
    # never the guard: a lost response propagated straight out of here, so
    # the complaint that had in fact been written was written again under a
    # paraphrase -- a new token, a new entry, one accepted write and two
    # records in the book.
    pending = Unresolved(token=token, action='complaint', wanted=requested_resolution)
    recorded = _answered(
        call,
        pending,
        ComplaintRecord,
        lambda: call.diary.record_complaint(phone, summary, requested_resolution, operation_token=token),
        'complaint book',
    )
    # Validated, then *read*. A well-formed `{'recorded': False}` is the
    # complaint book saying it did not take it, and this used to say "Okay,
    # that's written down." to it anyway: the response parsed, so the write
    # was called a success. A schema says the service answered in the right
    # shape; only the field says what it did.
    if not recorded.recorded:
        raise ModelRetry(
            'The complaint book refused it and nothing is on record. Tell the caller you could '
            'not log it. Do not promise a callback or call this again.'
        )
    call.commit(writes=call.state.writes + 1)
    # Before the next request, not after it, so this covers the wait the filler
    # used to cover -- and says the one thing worth saying rather than
    # announcing that something is about to happen. The reference is in it: a
    # live call took one from the book and never told the caller.
    call.retain_action('record_complaint', recorded.model_dump(mode='json'))
    await call.speak_now(acknowledgement(recorded.reference, requested_resolution))
    return recorded


async def read_diary(
    ctx: RunContext[SalonCall],
    when: str,
    service: Literal['any', 'haircut', 'hair-color', 'blowout'],
    at: str = '',
) -> Slots:
    """Read the diary: what this caller already holds, and what is free on the day they named.

    One call answers both, so you never need a second. It works the day and the
    time out from the caller's own words, never returns a time that has already
    gone past, and never writes anything.

    Args:
        when: The day in the caller's own words -- "tomorrow", "next Friday",
            "the 25th" -- or an empty string if they have not named one.
        service: Narrow the free times to one service, or `any` to see them all.
        at: The time or part of day they asked for, in their own words --
            "three", "half past two", "the afternoon" -- or empty if they said
            none. It decides which free times you are shown, so leaving it out
            when they named one is how a caller asking for the afternoon gets
            read the morning.
    """
    claim_or_refuse(ctx, ACTIONS)
    if not _present(ctx.deps.state.customer_verified):
        raise ModelRetry(
            'The diary is read under a confirmed phone number and nobody is identified yet. Hand to the verify agent.'
        )
    # The caller's words for the day, against the ones this call was given.
    # The resolver is right; on the live call of 2026-09-21 12:58 it was given
    # the wrong input -- the caller said "Friday", the lookup asked for "next
    # Friday", and under that Monday clock those are a week apart. The whole
    # difference is a modifier the model added, so the check is on the words
    # and not on the date they resolve to.
    if (theirs := ctx.deps.day_words) and (mine := diary.day_phrases(when)):
        today = ctx.deps.diary.now().date()
        wanted = {diary.resolve_day(words, today) for words in theirs} - {None}
        asked_for = diary.resolve_day(mine[-1], today)
        if wanted and asked_for not in wanted:
            said = '" or "'.join(theirs)
            raise ModelRetry(
                f'Nothing was looked up. The caller said "{said}" and this asked for "{mine[-1]}", '
                f'which is a different day. Call this again with their words. If you think they '
                'meant another day, ask them which one.'
            )
    found = Slots.model_validate(
        # `offered_to` is this call, not this number: what was put in front of
        # one conversation was never put in front of another, even from the
        # same phone. Asking again also withdraws whatever the last search
        # offered -- see `diary.find_slots`.
        ctx.deps.diary.find_slots(
            ctx.deps.state.customer_phone,
            when,
            service,
            ctx.deps.diary.now(),
            offered_to=ctx.deps.id,
            # Their words, resolved here rather than by the model. Applied
            # before the twelve-slot cap, so a preference is the answer and
            # not a filter over one.
            prefer=diary.resolve_time(at or ctx.deps.heard),
        )
    )
    # The day this look confirmed. `save_booking` refuses a slot from any other
    # one, so a caller who changes their mind about the day cannot be booked
    # onto the day they changed away from. The model is not asked whether the
    # change invalidated the slot: it is an application invariant and it is held
    # here, in code, whatever the model does next.
    ctx.deps.commit(booking_day=found.date)
    return found


async def offer_times(
    ctx: RunContext[SalonCall],
    when: str,
    service: Service,
    at: str = '',
) -> str:
    """Read availability and speak up to three real choices, ending this reply.

    Use when the caller needs to choose a time. For an already authorized same-time
    move or clear selection, use read_diary then save_booking instead.

    Args:
        when: The caller's own day words.
        service: The requested service.
        at: The caller's time or daypart, if given.
    """
    found = await read_diary(ctx, when, service, at)
    if found.slots:
        times = ', '.join(s.time.strftime('%I:%M %p').lstrip('0') for s in found.slots[:3])
        text = f'For a {service.replace("hair-color", "hair colour")} on {found.day}, I have {times}. Which time works for you?'
    else:
        text = {
            'need_day': 'Which day would you like?',
            'past': 'That date has already passed. Which future day would you like?',
            'closed': 'The salon is closed that day. Which other day works for you?',
        }.get(found.status, 'There are no available times for that request. Which other day works for you?')
    ctx.deps.answered()
    await ctx.deps.speak_now(text, origin='guidance')
    return text


async def booking_guidance(
    ctx: RunContext[SalonCall], topic: Literal['day', 'service', 'time', 'move_or_add', 'existing', 'retry']
) -> str:
    """Ask for missing booking details or recount the caller's actual appointments.

    Use offer_times for availability, ask_booking for an exact readback, and
    save_booking for an authorized change. This tool never claims a new write.
    """
    if topic == 'existing' and not ctx.deps.state.customer_verified:
        raise ModelRetry('Identify the caller before discussing their bookings.')
    if topic == 'existing':
        held = [
            Held.model_validate(row)
            for row in ctx.deps.diary.bookings_for(ctx.deps.state.customer_phone, ctx.deps.diary.now().date())
        ]
        text = (
            'You have '
            + '; '.join(f'a {b.service.replace("hair-color", "hair colour")} {b.spoken}' for b in held)
            + '. What would you like to do?'
            if held
            else 'There are no upcoming appointments under this number. Would you like to make one?'
        )
    elif topic == 'move_or_add':
        question = ctx.deps._asked
        if (
            question is None
            or question.about != 'move_or_add'
            or question.turn not in (ctx.deps.turn, ctx.deps.turn - 1)
        ):
            raise ModelRetry(
                'Attempt the requested save first; its refusal identifies the booking and choice.'
            )
        ctx.deps.ask(question)
        text = 'Would you like to move the appointment you already have, or add another one?'
    else:
        text = {
            'day': 'Which day would you like?',
            'service': 'Would you like a haircut, hair colour, or a blowout?',
            'time': 'Which time would you like?',
            'retry': "I couldn't complete that request. What would you like to change?",
        }[topic]
    if topic != 'move_or_add':
        ctx.deps.answered()
    await ctx.deps.speak_now(text, origin='guidance')
    return text


def _can_end(call: SalonCall) -> bool:
    """A bare 'no thanks' declines the last offer, not necessarily the whole call."""
    if not ends(call.heard):
        return False
    if not re.fullmatch(r'no[ ,]*(?:thanks|thank you)[.! ]*', call.heard.strip(), re.IGNORECASE):
        return True
    previous = [r['text'] for r in call.transcript() if r['said'] != 'caller']
    if not previous and not call.all_spoken:
        previous = [
            p.content
            for m in call.messages
            if isinstance(m, ModelResponse)
            for p in m.parts
            if isinstance(p, TextPart)
        ]
    return bool(
        previous and re.search(r'anything else|anything more|something else', previous[-1], re.IGNORECASE)
    )


async def hang_up(ctx: RunContext[SalonCall], goodbye: str) -> str:
    """End the call, once the caller's own words say it is over: "no, that's all", "I'm good,
    thanks", "goodbye". A question is never one of those, however tired it sounds: answer it and wait.

    Args:
        goodbye: One short goodbye in your own words. It is the last thing they hear.
    """
    claim_or_refuse(ctx, ACTIONS)
    # The caller's own words, not a goodbye string the model wrote. On the
    # live call of 2026-09-21 12:57 the caller asked "Can you help me moving
    # it to Saturday, the same time?", the agent read Saturday's availability,
    # and then hung up on them -- `answered`, no move, no goodbye from the
    # caller. A model-chosen farewell is not evidence the call is over.
    if not _can_end(ctx.deps):
        raise ModelRetry(
            f'The line is still open: the caller said "{ctx.deps.heard}", which is not them ending '
            'the call. Answer what they asked. Hang up only on their own goodbye.'
        )
    ctx.deps.end_requested('caller')
    await ctx.deps.speak_now(goodbye, origin='goodbye')
    await ctx.deps.end_call()
    return goodbye


async def complaint_goodbye(ctx: RunContext[SalonCall]) -> str:
    """End the call only when the caller is finished. Speaks a fixed goodbye."""
    return await hang_up(ctx, 'Thanks for calling. Goodbye.')


@lru_cache
def hand_over_for(exits: tuple[Exit, ...], questions: tuple[str, ...] = ()):
    """One step's `hand_over`, carrying only the steps that step may actually reach.

    A shared `hand_over` over every step in the agency offers the model routes
    that are wrong on this call, and the self-hop it then has to be told not to
    take. Narrowing the `Literal` per step deletes both: an illegal transition
    is not refused at runtime, it is unrepresentable in the schema the model is
    sent, and pydantic-ai hands back its own retry if a provider ignores the
    enum anyway.

    A readback keeps this step waiting for consent. Refuse a paired handoff
    so the SDK can try that output instead of silently skipping the question.
    """
    names = tuple(dict.fromkeys(exit.to for exit in exits))

    async def hand_over(ctx, to, because) -> Handoff:
        """Give the caller to another agent, right now, for something that is not yours to do.

        This is the only way to move a caller. Writing a sentence about transferring
        them does nothing at all: the caller hears you say it and then sits there,
        still with you, while nobody does the thing. So do this instead of saying
        it, and say nothing about it afterwards -- they hear one short line and
        carry straight on in the same conversation.

        Args:
            to: The agent that takes it from here.
            because: One short sentence: what the caller wants. Never spoken.
        """
        if (
            ctx.messages
            and isinstance(reply := ctx.messages[-1], ModelResponse)
            and any(isinstance(part, ToolCallPart) and part.tool_name in questions for part in reply.parts)
        ):
            raise ModelRetry('Ask the readback here and wait for the caller. Do not hand over in that reply.')
        claim_or_refuse(ctx, ACTIONS)
        # Checked again here, against the state as it is now. The schema that
        # offered this destination was built before the tools in this same
        # reply ran, and one of them can have closed the edge. A schema that
        # omits a destination is a convenience for the model, never the
        # enforcement.
        if not any(exit.to == to and exit.guard(ctx.deps.state) for exit in exits):
            raise ModelRetry(
                f'{to} is not somewhere this caller can go from here right now. '
                'Nothing was done. Answer them yourself, or pick a destination that is open.'
            )
        return Handoff(to=to, because=because)

    # Built from a value, so it cannot be written as an annotation: under
    # `from __future__ import annotations` every annotation is a string, and
    # pydantic-ai resolves `Literal[exits]` in a scope where `exits` is gone.
    hand_over.__annotations__ = {
        'ctx': RunContext[SalonCall],
        'to': Literal[names],  # ty: ignore[invalid-type-form]
        'because': str,
        'return': Handoff,
    }
    return hand_over


# --- the two steps that finish themselves ------------------------------------


async def confirm_customer(ctx: RunContext[SalonCall], phone: SubmittedPhone) -> Handoff:
    """Look the caller up by the number they have just agreed to, and finish this step.

    Only after a clear yes to the digits you read back. A caller giving
    different digits is correcting you, not agreeing.

    Args:
        phone: The exact number they confirmed, in E.164.
    """
    claim_or_refuse(ctx, ACTIONS)
    return await _confirm_customer(ctx.deps, phone)


async def _confirm_customer(call: SalonCall, phone: SubmittedPhone) -> Handoff:
    """Shared execution boundary for model tools and application steps."""
    phone = _PHONE.validate_python(phone)
    # The caller has to have agreed to *these digits*, last turn, in answer to
    # a readback this code put to them. On the live call of 2026-09-21 13:02
    # the agent asked "are you calling from Spain?", the caller said yes, and
    # that yes was used as agreement to a number nobody had read back. The
    # number was structurally fine; the procedure was skipped, and the whole
    # of what `phone_confirmed` means is that the procedure happened.
    question = call.pending('phone', value=phone)
    if question is None or (question.owner and question.owner != call.state.customer_phone):
        raise ModelRetry(
            'Nothing was looked up: those digits have not been read back to this caller and agreed '
            f'to. Call `ask_number` with {phone} and wait for their answer.'
        )
    if not agrees(call.heard):
        raise ModelRetry(
            'Nothing was looked up: the caller did not agree to the number that was read back. '
            f'They said "{call.heard}". If those are different digits it is a correction -- read '
            'the new number back with `ask_number`; otherwise ask again.'
        )
    call.answered()
    found = CustomerLookup.model_validate(call.diary.find_or_create_customer(phone))
    # The lookup has to have been about the number we asked about. Ignored,
    # a customer service answering for somebody else verified this caller as
    # them: the status was legal, the phone was legal, and nothing compared
    # the two. Not a `ModelRetry` the model can fix by rewording -- it is the
    # service disagreeing with itself -- so it says what to do instead.
    if found.customer_phone != phone:
        raise ModelRetry(
            'The customer lookup came back about a different number, so nobody was identified '
            'and nothing was saved. Tell the caller you cannot check who they are just now, and '
            'do not call this again.'
        )
    # One commit, not two assignments. Written separately, a status the diary
    # could not produce would fail *after* the phone number had already been
    # saved, leaving a caller whose number is on the state and whose
    # verification never happened -- which every downstream guard reads as
    # "identified enough to have a number". Both or neither.
    #
    # `phone_confirmed` records only that the caller said yes to the digits
    # read back. It is not proof the number is theirs: a caller who agrees to a
    # wrong readback gets a structurally valid number belonging to somebody
    # else, and that stays a conversational risk this field does not launder.
    call.commit(
        customer_phone=phone,
        phone_confirmed=True,
        customer_verified=found.customer_status,
        # The number the lookup was actually performed against. Committed with
        # the status, never after it: the two are one fact, and a status with
        # no identity attached is cleared by the state's own invariant.
        verified_phone=phone,
    )
    # Straight on to what they actually rang about -- which the graph already
    # knows, because the step that needed them identified pushed itself on the
    # way in. The node-graph version came back to the front desk first, which
    # cost a model request and a turn in which the caller heard nothing new.
    # This version does not even ask the model where they were heading.
    # ty: `resume` hands back a step name as `str`; `Handoff.to` narrows it
    # to the four this agency has, and validates it here rather than trusting it.
    call.retain_action('confirm_customer', found.model_dump(mode='json'))
    return Handoff(to=call.resume('concierge'), because='the caller is identified', status='completed')  # ty: ignore[invalid-argument-type]


async def stop_verifying(ctx: RunContext[SalonCall], because: str) -> Handoff:
    """Give the caller back to the front desk without identifying them, and finish this step.

    For a caller who will not confirm a number, or who asks about something
    else entirely. Whatever they were waiting to do that needed identifying is
    given up with it, so use it when they have moved on and not while they are
    still correcting the digits.

    Args:
        because: One short sentence: why this stopped. Never spoken.
    """
    claim_or_refuse(ctx, ACTIONS)
    # The errand goes too. `verify` is interposed in front of a step that
    # needs an identity, and that step is recorded so it can be resumed --
    # so leaving it behind means a caller who verifies an hour later for some
    # other reason is silently put back into a booking they abandoned.
    ctx.deps._complaint = None
    ctx.deps.commit(pending=None)
    return Handoff(to='concierge', because=because, status='unserved')


def _authorised(call: SalonCall, action: str, slot_id: str, booking_id: str) -> bool:
    """Whether the caller authorised this exact write, and whether it is a second appointment.

    Refuses by raising, so a write that reaches past here has caller evidence
    behind it. Three ways in, and the model supplies none of them:

    * They agreed to an `ask_booking` readback of this same action, slot and
      booking, on the turn before this one.
    * They named a time, and exactly one of the times currently on the table
      is that time. This is the fast path the booking policy is built on: an
      unambiguous choice of an offered slot authorises that slot. It is not
      available while they already hold something that day, because then the
      unanswered question is whether this replaces it.
    * They answered the move-or-add question in their own words.

    The boolean it returns is the *second appointment* decision, and it comes
    from what the caller said, never from an argument. Reproduced on the live
    calls of 2026-09-21 13:01 and 13:03: refused with `additional=false`, sent
    again one second later with `additional=true`, same caller, no turn in
    between. The flag was the model's own permission slip.
    """
    phone = call.state.customer_phone
    held = call.diary.bookings_for(phone, call.diary.now().date()) if phone else []

    if question := call.pending('booking', value=slot_id, action=action, booking_id=booking_id):
        if question.owner and question.owner != phone:
            raise ModelRetry('The caller identity changed. Ask for approval again under this number.')
        if not agrees(call.heard):
            raise ModelRetry(
                'Nothing was saved: the caller did not agree to what was read back to them. They '
                f'said "{call.heard}". Answer what they actually asked.'
            )
        call.answered()
        # They agreed to a sentence that named this one appointment, so a
        # second one is not what they agreed to unless they said so.
        return action == 'book' and move_or_add(call.heard) == 'add'

    if action == 'cancel':
        raise ModelRetry(
            'Nothing was cancelled: nobody has agreed to cancel anything. Call `ask_booking` with '
            "action='cancel' and that booking, and wait for their answer."
        )

    if (decision := call.pending('move_or_add', value=slot_id)) is not None:
        match move_or_add(call.heard):
            case 'add':
                call.answered()
                return True
            case 'move':
                call.answered()
                if action != 'move' or not booking_id:
                    raise ModelRetry(
                        'The caller asked to move the booking they already have, not to add one. '
                        f"Call this again with action='move' and booking_id={decision.booking_id!r}."
                    )
                return False
            case _:
                raise ModelRetry(
                    f'Nothing was saved. The caller said "{call.heard}", which does not answer '
                    'whether to move the appointment they already have or add another one. Ask '
                    'them again in those words.'
                )

    # The fast path, and the reason this agency books a turn sooner than one
    # that reads everything back: an unambiguous choice of a time it has just
    # offered is the caller authorising that time. Unambiguous means exactly
    # one slot on the table is at the time they named -- so "three" with a
    # haircut and a colour both free at three is two appointments and a
    # question, not a booking.
    offered = call.diary.offer_of(call.id, slot_id, phone)
    named = diary.times_named(call.heard)
    on_the_table = call.diary.offered_to(call.id, phone)
    at_that_time = [slot for slot in on_the_table if slot['time'] in named]
    picked = bool(offered) and offered['time'] in named and len(at_that_time) == 1
    if action == 'book' and picked and not held:
        # Nothing to displace, so choosing a time is the whole decision.
        return False
    if action == 'move' and offered and move_or_add(call.heard) == 'move' and booking_id:
        moving = next((b for b in held if b['booking_id'] == booking_id), None)
        # "Move it to Saturday, the same time" names no digits and still names
        # one time exactly: the one they already hold. The live call of
        # 2026-09-21 12:57 answered that sentence by hanging up.
        same = (
            bool(moving)
            and _SAME_TIME.search(call.heard)
            and not diary.NEGATED_TIME.search(call.heard)
            and offered['time'] == moving['time']
        )
        if moving and (picked or same):
            return False

    if held and action == 'book':
        # The one refusal with a question behind it. Recorded as that
        # question, so the answer to it is the caller's and not a retry's.
        one = held[0]
        call.ask(Asked(about='move_or_add', value=slot_id, action='move', booking_id=one['booking_id']))
        raise ModelRetry(
            f'Nothing was saved: this caller already has a {one["service"]} {one["spoken"]}. Ask '
            'them, in one question, whether to move that one or add another appointment on top of '
            'it, and call this again with their answer. Do not decide it for them.'
        )
    raise ModelRetry(
        'Nothing was saved: the caller has not said they want this. Read it back with '
        '`ask_booking` and call this again once they have agreed.'
    )


async def save_booking(
    ctx: RunContext[SalonCall],
    action: Literal['book', 'move', 'cancel'],
    slot_id: str,
    booking_id: str,
) -> str:
    """Write the diary, and finish this step. Nothing is booked, moved or cancelled until this runs.

    Only after the caller has authorised this exact appointment -- by naming
    one of the times just offered to them, or by agreeing to an `ask_booking`
    readback of it. It speaks the confirmation itself, so say nothing after
    calling it.

    Args:
        action: book a new time, move an existing booking, or cancel one.
        slot_id: The slot exactly as the diary returned it. Empty when cancelling.
        booking_id: The booking exactly as the diary returned it. Empty when booking.
    """
    claim_or_refuse(ctx, ACTIONS)
    return await _save_booking(ctx.deps, action, slot_id, booking_id)


async def _save_booking(
    call: SalonCall, action: Literal['book', 'move', 'cancel'], slot_id: str, booking_id: str
) -> str:
    """Shared execution boundary for model tools and application steps."""
    command = BookingCommand(action=action, slot_id=slot_id, booking_id=booking_id)
    action, slot_id, booking_id = command.action, command.slot_id, command.booking_id
    # Before anything else. A write whose outcome was never established is
    # not a write that did not happen, and until it is settled every other
    # one is a second appointment waiting to be made.
    if call.state.unresolved:
        return await _settle(call)
    additional = _authorised(call, action, slot_id, booking_id)
    if not _present(call.state.customer_verified):
        raise ModelRetry('Nothing was saved: nobody is identified yet. Hand to the verify agent.')
    # What this caller is being offered under that id, asked of the diary
    # rather than read out of the string: the id is the service's own opaque
    # token. Empty means it is offering them nothing of the sort, and the
    # diary refuses it below in its own words.
    #
    # There used to be a second rule here, comparing the slot's day against
    # `state.booking_day`. It was the weaker copy of this one and it could be
    # walked around: read Friday for a haircut, read Friday for a colour, and
    # the day still matched while the service the caller had just corrected
    # did not. An offer belongs to one question, so the diary withdraws the
    # last one on every new search and there is a single enforcement of it.
    #
    # Asked under the caller's *current* identity as well as this call: a
    # number corrected mid-call makes the caller somebody else, and the slots
    # the previous identity was shown are not theirs to book.
    offered = call.diary.offer_of(call.id, slot_id, call.state.customer_phone)
    # One token per business action, not per attempt. Derived from what the
    # action *is*, so a retry after a lost response derives the same one and
    # the diary returns the outcome it already recorded rather than booking a
    # second time. Nothing has to carry it between turns.
    #
    # The call and the number of writes it has already had accepted are part
    # of the name, and they are what make it the *action's* name rather than
    # the arguments'. Book a slot, cancel it, book it again: same caller, same
    # arguments, and the old token returned the first booking's outcome
    # without writing anything -- the diary empty and the call state holding
    # an appointment that no longer existed. An attempt at one intent happens
    # with the count unchanged and still reconciles; a new intent follows an
    # accepted write and cannot.
    #
    # ponytail: derived rather than handed out by the service. A real one
    # would issue it, and then this line goes.
    token = operation_token(
        call.id,
        call.state.writes,
        call.state.customer_phone,
        action,
        slot_id,
        booking_id,
        additional,
    )
    # The whole request, not just its name. What a recovery has to be checked
    # against cannot be read back out of a hash, and `_settle` used to have
    # only the hash.
    pending = Unresolved(token=token, action=action, booking_id=booking_id, offered=offered)
    try:
        # Validated before a single field is read, so a malformed response
        # cannot reach the state through an annotation nobody checks -- and
        # an unreadable answer is an *unknown* outcome, not a failure. The
        # write may well have landed; only the answer is broken.
        written = _answered(
            call,
            pending,
            Saved,
            lambda: call.diary.save_booking(
                call.state.customer_phone,
                action,
                slot_id,
                booking_id,
                additional,
                call.diary.now(),
                operation_token=token,
                offered_to=call.id,
            ),
            'diary',
        )
    except diary.AlreadyHoldsError as refused:
        # The diary refusing what `_authorised` let through: a booking this
        # caller was authorised to make that the diary itself will not take
        # alongside one they hold. Recorded as the question it raises, so the
        # only way past it is the caller's answer to that question.
        call.ask(Asked(about='move_or_add', value=slot_id, action='move'))
        raise ModelRetry(str(refused)) from None
    except diary.RefusedError as refused:
        # The diary's own words, which name what is actually possible instead of
        # leaving the model to guess a new slot.
        raise ModelRetry(str(refused)) from None
    # Legal values are not an answer to this request. Checked before anything
    # is committed or said, because both of those are irreversible in front of
    # a caller.
    if why := _disagrees(action, booking_id, offered, written):
        _unknown(call, pending, why)
    appointment = Appointment(
        booking_id=written.booking_id,
        service=written.service,
        date=written.date,
        time=written.time,
        spoken=written.spoken,
        action=action,
    )
    # Said here rather than handed to a model to say. The diary already computed
    # the one right phrasing, so a second request only risks getting it wrong:
    # a weekday worked out from a date turned a Friday booking into "Thursday
    # the 18th", and a 24 hour time was read back as "09:00 AM". It also costs a
    # whole round trip in which the caller hears nothing, which is what a
    # confirmation is least able to afford.
    # The appointment and the step the caller lands in are one decision, so
    # they commit together: a booking recorded without the move would leave
    # the caller inside `booking` with nothing left to book, and a move
    # recorded without the booking would lose the only record that it happened.
    # `resume` clears the pending slot, so it runs on its own line: relying on
    # argument evaluation order to sequence two writes is the kind of thing
    # that reads fine and breaks under a later edit.
    going_back_to = call.resume('concierge')
    call.commit(appointment=appointment, agent=going_back_to, writes=call.state.writes + 1, unresolved=None)
    call.retain_action('save_booking', written.model_dump(mode='json'))
    await call.speak_now(
        _pick(f'saved:{written.status}', SAVED[written.status]).format(
            spoken=written.spoken,
            service=written.service.replace('hair-color', 'hair colour'),
        )
    )
    return f'{written.status}: {written.spoken}'


# ponytail: only complete, familiar answers bypass the model. Extend from observed misses.
_CLEAR_APPROVAL = re.compile(
    r'(?:yes|yeah|yep|yup|sure|okay|ok|right|correct|exactly|absolutely|go for it|go ahead|do it|'
    r"that(?:'s| is) (?:right|correct)|yes[, ]+(?:that(?:'s| is) (?:right|correct|my number)))"
    r'(?:[, ]+please)?[.! ]*',
    re.IGNORECASE,
)
_PHONE_CONTENT = re.compile(
    r'[+\d]|\b(?:phone|number|digits?|zero|one|two|three|four|five|six|seven|eight|nine|'
    r'actually|instead|correction|wrong|wait)\b',
    re.IGNORECASE,
)


def advance(call: SalonCall) -> LocalAction | None:
    """Resolve only a known next step; ambiguous input still goes to the agent."""
    if _CLEAR_APPROVAL.fullmatch(call.heard.strip()) and agrees(call.heard):
        if (
            call.state.agent == 'verify'
            and (question := call.pending('phone'))
            and (not question.owner or question.owner == call.state.customer_phone)
        ):
            phone = question.value
            return LocalAction('confirm_customer', lambda: _confirm_customer(call, phone))
        if (
            call.state.customer_verified
            and call.state.agent == 'booking'
            and (question := call.pending('booking'))
            and question.action in ('book', 'move', 'cancel')
            and (not question.owner or question.owner == call.state.customer_phone)
        ):
            command = BookingCommand.model_validate(
                {'action': question.action, 'slot_id': question.value, 'booking_id': question.booking_id}
            )
            return LocalAction('save_booking', lambda: _save_booking(call, **command.model_dump()))
        if call.state.agent == 'complaints' and call.complaint_approved and call.pending('complaint'):
            approved = call._complaint
            assert approved is not None
            return LocalAction(
                'record_complaint',
                lambda: _record_approved_complaint(call, approved.summary, approved.requested_resolution),
            )
    if (
        call.state.agent == 'verify'
        and call.state.pending
        and call.pending('phone') is None
        and not call.state.customer_verified
        and call.state.customer_phone
        and call._return_status.startswith('Application handoff:')
        and not any(_PHONE_CONTENT.search(words) for words in call._inputs.values())
    ):
        return LocalAction('ask_number', lambda: _ask_number(call, call.state.customer_phone))
    return None


# --- the table ---------------------------------------------------------------


def build(
    name: AgentName,
    tools=(),
    finishers=(),
    asks=(),
    exits: tuple[Exit[SalonState], ...] = (),
    requires: tuple[str, ...] = (),
    satisfied_by: str = '',
    guarded: bool = False,
    model=MODEL,
) -> Node[SalonState]:
    """One step: its agent, its legal exits, and what has to hold before it runs."""
    # `dict.fromkeys`, not `set`: two exits can name one step -- `booking` is
    # reachable by `hand_over` whoever is calling, and reachable by lending
    # `read_diary` only once somebody is identified -- and the `Literal` the
    # model sees should say it once, in the order it was written.
    borrowed = _lent(exits)
    # Named, always. Pydantic AI calls a lone output function `final_result`
    # ("If not specified and only one output is provided, `final_result` will be
    # used") and only uses the real name once there are several. So a step whose
    # exits are all shut -- `verify` before anyone is identified -- would offer
    # the model a tool called `final_result` and nothing else to tell it what
    # the tool does. The name is the shortest description a tool has.
    # `sequential` on everything that writes: two of them must never overlap,
    # and the conflict policy refuses the second before its service runs. This
    # is the belt to that brace, and it is one keyword.
    named = [
        ToolOutput(
            complaint_goodbye if name == 'complaints' and fn is hang_up else fn,
            name=fn.__name__,
            sequential=fn.__name__ in ACTIONS,
        )
        for fn in finishers
    ]
    # Putting a question to the caller ends the run the same way finishing the
    # step does, and for the same reason: the words are already decided, so a
    # further model request would only be a chance to word them differently.
    # Not in `ACTIONS` -- asking changes nothing and writes nothing, and a turn
    # that reads the diary and then asks about what it found is one action.
    questions = [ToolOutput(fn, name=fn.__name__) for fn in asks]
    # The whole set, always declared. What varies per request is which of them
    # is *offered*, which `narrow` below decides -- so a step whose exits all
    # shut does not silently change the name of its remaining output tool.
    # `hand_over` named explicitly, like the finishers. Left as a bare
    # function in the output list, pydantic-ai renames it `final_result_hand_over`
    # as soon as there is more than one output -- which the per-request
    # narrowing below then failed to find by name, silently offering every
    # destination whatever the guards said.
    handoff = (
        [
            ToolOutput(
                hand_over_for(exits, tuple(fn.__name__ for fn in asks)), name='hand_over', sequential=True
            )
        ]
        if exits
        else []
    )
    structured_speech = name in ('verify', 'booking', 'complaints')
    declared = [*([str] if not structured_speech else []), *handoff, *named, *questions]

    def outputs(state: SalonState) -> list:
        """What this step may return, declared once for the whole turn.

        Narrowing used to happen here, once per turn. That was too early: a
        run makes several model requests, and a tool in the first one can shut
        an edge the second one is still being offered. `narrow` does it per
        request instead; this stays the superset it picks from.
        """
        return declared

    async def only_reachable(ctx: RunContext[SalonCall], defs: list[ToolDefinition]) -> list[ToolDefinition]:
        """Take a borrowed tool back out while the edge it stands for is shut.

        The same narrowing `narrow` does to the exits, done to the tools: an
        unidentified caller's front desk is not holding the diary, so the model
        cannot read it and be refused, which costs the round trip the lending
        was there to save.

        Re-run before every model request, not once per turn: `for_run_step`
        in pydantic-ai 2.46 re-resolves the toolset at each request node, so a
        tool that ran a moment ago and changed the state is reflected in the
        very next schema.
        """
        return [d for d in defs if d.name not in borrowed or borrowed[d.name].guard(ctx.deps.state)]

    async def narrow(ctx: RunContext[SalonCall], defs: list[ToolDefinition]) -> list[ToolDefinition]:
        """Offer only the destinations that are open at this request.

        The output-tool half of `only_reachable`, and a separate hook because
        pydantic-ai prepares ordinary and output tools through different ones:
        filtering the normal tools does nothing to the handoff alternatives.

        An exit whose guard has stopped holding leaves the enum the model is
        sent, so it cannot be chosen rather than being refused after it was.
        With none left, `hand_over` goes entirely. Verification can still ask
        for missing information through its guidance output.
        """
        legal = [e.to for e in exits if e.guard(ctx.deps.state)]
        legal = list(dict.fromkeys(legal))
        kept = []
        for tool_def in defs:
            if tool_def.name == 'record_complaint' and (
                not ctx.deps.state.customer_verified or not ctx.deps.complaint_approved
            ):
                continue
            if tool_def.name != 'hand_over':
                kept.append(tool_def)
                continue
            if not legal:
                continue
            schema = deepcopy(tool_def.parameters_json_schema)
            if (to := schema.get('properties', {}).get('to')) is not None:
                to.pop('const', None)
                to['enum'] = legal
            kept.append(replace(tool_def, parameters_json_schema=schema))
        return kept

    return Node(
        Agent[SalonCall, object](  # ty: ignore[no-matching-overload]
            model,
            name=name,  # labels this agent's span in Langfuse
            deps_type=SalonCall,
            # `str` keeps speech streaming to TTS. Everything else ends the run:
            # `hand_over` names who takes the caller, a finisher does this step's
            # work and the graph works out who takes them next. An action rather
            # than a schema on purpose -- given a `Handoff` model to fill in,
            # both models wrote "let me put you through to our complaints team"
            # and then did nothing.
            #
            # The unguarded superset, so a run that does not come through the
            # processor -- `talk.py`, a delegation -- still has every exit. The
            # processor narrows it per turn through `Node.outputs`.
            output_type=declared,  # type: ignore[arg-type]
            instructions=prompt(name),
            # Its own tools, plus the entry tools of the steps it can reach.
            # `only_reachable` hides the borrowed ones while their edge is shut.
            tools=[
                # Already a `Tool` when it was authored from a schema; a bare
                # function otherwise, wrapped here so every action is
                # `sequential` and two of them can never overlap.
                *(
                    fn if isinstance(fn, Tool) else Tool(fn, sequential=fn.__name__ in ACTIONS)
                    for fn in tools
                ),
                *(Tool(fn, sequential=fn.__name__ in ACTIONS) for exit in exits for fn in exit.via),
            ],
            capabilities=[
                one_action_per_reply(ACTIONS),
                Hooks(prepare_tools=only_reachable, prepare_output_tools=narrow),
                # Last, so what it empties is the set the guards already narrowed.
                *([speak_by(SPEAK_BY)] if not structured_speech else []),
                # Measurement, not behaviour: it stamps each provider request
                # on the way out so time to first token is per request.
                request_clock(),
            ],
            model_settings=SETTINGS,
        ),
        exits=exits,
        requires=requires,
        satisfied_by=satisfied_by,
        outputs=outputs,
        guarded=guarded,
        model_speech=not structured_speech,
        advance=advance,
    )


# --- the guards --------------------------------------------------------------

# Facts on the state, never judgments. "The caller corrected their number" and
# "this step is about to write something down" are judgments, and they stay in
# the prompts, where judgments belong.


def verified(state: SalonState) -> bool:
    """Identified already, so `verify` is a correction rather than a first pass."""
    return bool(state.customer_verified)


# --- the table ---------------------------------------------------------------

# If its words should be heard, it is an agent in here. If they should not -- a
# classifier, an extractor, a summariser -- it is delegation:
# `await worker.run(..., deps=ctx.deps, usage=ctx.usage)` from inside a tool.
AGENTS: dict[str, Node[SalonState]] = {
    'concierge': build(
        'concierge',
        finishers=[hang_up],
        # `booking` twice, and the pair is the whole point. Unguarded, it is a
        # `hand_over` the model may choose whoever is calling: the caller wanting
        # to book is what the front desk knows, and whether anyone has to be
        # identified first is the graph's business. Guarded on `verified`, it
        # also lends `read_diary`, so a caller who is already identified moves
        # into `booking` by the diary being read rather than by a request that
        # exists only to say "booking". That request was measured at 0.85s of
        # silence, twice in one twelve-turn call.
        #
        # `verify` only once somebody has been identified, because before that
        # `requires` interposes it and naming it here is a route the model can
        # only get wrong.
        exits=(
            Exit('booking'),
            Exit('booking', verified, via=(read_diary,)),
            Exit('complaints'),
            Exit('verify', verified),
        ),
    ),
    'verify': build(
        'verify',
        finishers=[confirm_customer, stop_verifying],
        asks=[ask_number, verification_guidance],
        # Guarded, because an incomplete number is a reason to ask again and not
        # a reason to leave. Unguarded, a live call on 2026-09-20 went
        # `verify -> concierge ("The caller gave an incomplete phone number and
        # cannot confirm")`, the front desk sent them straight back, `requires`
        # interposed verify a second time, and the caller was asked for the same
        # number twice -- once as "which country are you calling from?" when
        # they had already said +34. Two requests and two questions for nothing.
        # With no exit, its guidance output asks for the missing information.
        exits=(Exit('concierge', verified),),
    ),
    'booking': build(
        'booking',
        tools=[read_diary],
        finishers=[save_booking],
        asks=[ask_booking, offer_times, booking_guidance],
        # This step writes, so its speech goes out a clause at a time until
        # it has. See `voice.harness.Node.guarded`.
        guarded=True,
        exits=(Exit('concierge'), Exit('complaints')),
        # The ordering rule, as a fact instead of a sentence in a prompt.
        requires=('customer_verified',),
        satisfied_by='verify',
    ),
    'complaints': build(
        'complaints',
        finishers=[hang_up, record_complaint],
        asks=[ask_complaint, complaint_guidance],
        guarded=True,
        exits=(Exit('concierge'),),
        requires=('customer_verified',),
        satisfied_by='verify',
    ),
}
