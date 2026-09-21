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
import os
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Literal, NoReturn

from pydantic import BaseModel, ValidationError
from pydantic_ai import Agent, ModelRetry, RunContext, TemplateStr, Tool, ToolOutput
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.models.google import GoogleModelSettings
from pydantic_ai.tools import ToolDefinition

from salon import diary
from salon.state import (
    AgentName,
    Appointment,
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
)
from voice.harness import (
    STRICT,
    Exit,
    Node,
    claim_or_refuse,
    filler_hook,
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
    date: Date = ''
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
        got = {'date': written.date, 'time': written.time, 'service': written.service}
        if got != offered:
            return f'it is {got}, not the slot that was offered: {offered}'
    return ''


def _unknown(ctx: RunContext[SalonCall], pending: Unresolved, why: str) -> NoReturn:
    """Record this write as outstanding and refuse, which is the only thing that blocks the next one.

    On the state, not in a sentence. Retry text asking the model not to try
    again is not a guard, and changing one argument changes the token, so the
    service's own idempotency cache never sees the second attempt.
    """
    ctx.deps.commit(unresolved=pending)
    raise ModelRetry(UNKNOWN.format(why=why))


def _answered[Result: BaseModel](
    ctx: RunContext[SalonCall],
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
        recovered = diary.outcome_of(pending.token)
        if recovered is None:
            _unknown(ctx, pending, f'the {book} did not answer')
        raw = recovered
    try:
        return shape.model_validate(raw)
    except ValidationError:
        # The same question the lost-answer path asks, and for the same
        # reason: the service may have recorded a perfectly good outcome
        # under this token and merely told us about it badly.
        recovered = diary.outcome_of(pending.token)
        if recovered is None or recovered is raw:
            _unknown(ctx, pending, f'the {book} answered in a shape this cannot read')
        try:
            return shape.model_validate(recovered)
        except ValidationError:
            _unknown(ctx, pending, f'the {book} answered in a shape this cannot read')


async def _settle(ctx: RunContext[SalonCall]) -> str:
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
    pending = ctx.deps.state.unresolved
    assert pending is not None, 'only reached while a write is outstanding'
    outcome = diary.outcome_of(pending.token)
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
            ctx.deps.commit(unresolved=None)
            raise ModelRetry(
                'The complaint was never taken -- the book refused it. Tell the caller it is not '
                'logged and that someone will call them back.'
            )
        ctx.deps.commit(writes=ctx.deps.state.writes + 1, unresolved=None)
        await ctx.deps.speak_now(_pick('noted', NOTED))
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
    ctx.deps.commit(
        appointment=Appointment(
            booking_id=written.booking_id,
            service=written.service,
            date=written.date,
            time=written.time,
            spoken=written.spoken,
            action=action,
        ),
        agent=ctx.deps.resume('concierge'),
        writes=ctx.deps.state.writes + 1,
        unresolved=None,
    )
    await ctx.deps.speak_now(
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
NOTED: tuple[str, ...] = (
    "Okay, that's written down.",
    "Right, I've got that on the record.",
    "That's noted for you.",
)

PROMPTS = Path(__file__).resolve().parent.parent / 'prompts' / 'salon'

# One model for now; `build` takes it per agent, so giving `complaints` a
# stronger one or `verify` a cheaper one is a one-word edit.
MODEL: object = 'google-cloud:gemini-3.5-flash-lite'
# Thinking pinned off rather than left out. Left out, no `thinking_config` is
# sent at all and whatever Google currently defaults this model to is what a
# call gets -- a setting that can change under us, on the one dimension a voice
# turn cannot absorb. The live trace showed no thinking tokens, so this is
# writing down what is already true rather than changing it.
SETTINGS: object = GoogleModelSettings(
    service_tier='priority', google_thinking_config={'thinking_level': 'MINIMAL'}
)

if os.getenv('BENCH_PINNED'):
    # ponytail: one env var, because only the benchmark needs this. It pins the
    # POC to configuration A's exact binding, read off its own
    # `build/pipecat/compile-report.json`: gemini-3.1-flash-lite, Vertex, the
    # `eu` multi-region, thinking_level MINIMAL, and no service tier. A
    # benchmark that runs two architectures on two different models measures
    # the models -- 3.5 is the version that lost 18.5 s to
    # MALFORMED_FUNCTION_CALL aborts on a live call, which on its own would
    # decide the result.
    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.providers.google_cloud import GoogleCloudProvider

    # ponytail: the region is pinned only when Application Default Credentials
    # exist, because naming a `location` forces ADC and an Express-Mode
    # `GOOGLE_API_KEY` has none -- it routes wherever Google sends it. With no
    # project set the model and the thinking level are pinned and **the region
    # is not**, which the write-up has to say out loud. To pin it too:
    # `gcloud auth application-default login` and export GOOGLE_CLOUD_PROJECT.
    project = os.getenv('GOOGLE_CLOUD_PROJECT')
    MODEL = GoogleModel(
        'gemini-3.1-flash-lite',
        provider=GoogleCloudProvider(project=project, location='eu') if project else GoogleCloudProvider(),
    )
    SETTINGS = GoogleModelSettings(google_thinking_config={'thinking_level': 'MINIMAL'})

GREETING = "Hi, you've reached Sage and Stone. Robin speaking, what can I do for you?"

KNOWLEDGE = Path(__file__).resolve().parent.parent / 'knowledge'

_VOICE = (PROMPTS / '_voice.md').read_text()
_DIARY = (PROMPTS / '_diary.md').read_text()
# The salon's own documents, read once at import and substituted into the two
# prompts that answer from them. They were a retrieval tool until the call of
# 2026-09-21; see `prompt` below.
_DOCS = {name: (KNOWLEDGE / f'{name}.md').read_text() for name in ('services', 'refunds')}


def prompt(name: str) -> TemplateStr[SalonCall]:
    """One agent's prompt, with the rules every agent shares appended.

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
    return TemplateStr(f'{text}\n\n{_VOICE}', deps_type=SalonCall)


# --- what the front desk can do ---------------------------------------------


async def record_complaint(
    ctx: RunContext[SalonCall], summary: str, requested_resolution: str
) -> ComplaintRecord:
    """Write the caller's complaint down, once they have agreed to your summary of it.

    Recording what they asked for is not approving it.

    Args:
        summary: Short, factual summary of what went wrong, in their own terms.
        requested_resolution: What they want the salon to do, or an empty string.
    """
    claim_or_refuse(ctx, ACTIONS)
    if ctx.deps.state.unresolved:
        # The same bar a booking meets. A write nobody has settled blocks
        # every other one, whichever book it would be written in.
        raise ModelRetry(OUTSTANDING)
    if not _present(ctx.deps.state.customer_verified):
        raise ModelRetry(
            'A complaint record needs an owner and nobody is identified yet. Hand to the verify agent first.'
        )
    # One token per complaint, so an attempt that lost its answer and is
    # retried leaves one entry in the book rather than two. Same derivation
    # as a booking's: the call, the writes it has already had accepted, and
    # what this one is about.
    token = operation_token(
        ctx.deps.id, ctx.deps.state.writes, ctx.deps.state.customer_phone, summary, requested_resolution
    )
    # Through the same boundary a booking goes through. The token alone was
    # never the guard: a lost response propagated straight out of here, so
    # the complaint that had in fact been written was written again under a
    # paraphrase -- a new token, a new entry, one accepted write and two
    # records in the book.
    pending = Unresolved(token=token, action='complaint')
    recorded = _answered(
        ctx,
        pending,
        ComplaintRecord,
        lambda: diary.record_complaint(
            ctx.deps.state.customer_phone, summary, requested_resolution, operation_token=token
        ),
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
            'not log it and that someone will call them back, and do not call this again.'
        )
    ctx.deps.commit(writes=ctx.deps.state.writes + 1)
    # Before the next request, not after it, so this covers the wait the filler
    # used to cover -- and says the one thing worth saying rather than
    # announcing that something is about to happen.
    await ctx.deps.speak_now(_pick('noted', NOTED))
    return recorded


async def read_diary(
    ctx: RunContext[SalonCall], when: str, service: Literal['any', 'haircut', 'hair-color', 'blowout']
) -> Slots:
    """Read the diary: what this caller already holds, and what is free on the day they named.

    One call answers both, so you never need a second. It works the day out from
    the caller's own words, never returns a time that has already gone past, and
    never writes anything.

    Args:
        when: The day in the caller's own words -- "tomorrow", "next Friday",
            "the 25th" -- or an empty string if they have not named one.
        service: Narrow the free times to one service, or `any` to see them all.
    """
    claim_or_refuse(ctx, ACTIONS)
    if not _present(ctx.deps.state.customer_verified):
        raise ModelRetry(
            'The diary is read under a confirmed phone number and nobody is identified yet. Hand to the verify agent.'
        )
    found = Slots.model_validate(
        # `offered_to` is this call, not this number: what was put in front of
        # one conversation was never put in front of another, even from the
        # same phone. Asking again also withdraws whatever the last search
        # offered -- see `diary.find_slots`.
        diary.find_slots(ctx.deps.state.customer_phone, when, service, diary.now(), offered_to=ctx.deps.id)
    )
    # The day this look confirmed. `save_booking` refuses a slot from any other
    # one, so a caller who changes their mind about the day cannot be booked
    # onto the day they changed away from. The model is not asked whether the
    # change invalidated the slot: it is an application invariant and it is held
    # here, in code, whatever the model does next.
    ctx.deps.commit(booking_day=found.date)
    return found


async def hang_up(ctx: RunContext[SalonCall], goodbye: str) -> str:
    """End the call, once the caller's own words say it is over: "no, that's all", "I'm good,
    thanks", "goodbye". A question is never one of those, however tired it sounds: answer it and wait.

    Args:
        goodbye: One short goodbye in your own words. It is the last thing they hear.
    """
    claim_or_refuse(ctx, ACTIONS)
    await ctx.deps.speak_now(goodbye)
    await ctx.deps.end_call()
    return goodbye


@lru_cache
def hand_over_for(exits: tuple[Exit, ...]):
    """One step's `hand_over`, carrying only the steps that step may actually reach.

    A shared `hand_over` over every step in the agency offers the model routes
    that are wrong on this call, and the self-hop it then has to be told not to
    take. Narrowing the `Literal` per step deletes both: an illegal transition
    is not refused at runtime, it is unrepresentable in the schema the model is
    sent, and pydantic-ai hands back its own retry if a provider ignores the
    enum anyway.
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
    found = CustomerLookup.model_validate(diary.find_or_create_customer(phone))
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
    ctx.deps.commit(
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
    return Handoff(to=ctx.deps.resume('concierge'), because='the caller is identified')


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
    ctx.deps.commit(pending=None)
    return Handoff(to='concierge', because=because)


async def save_booking(
    ctx: RunContext[SalonCall],
    action: Literal['book', 'move', 'cancel'],
    slot_id: str,
    booking_id: str,
    confirmed: bool,
    additional: bool,
) -> str:
    """Write the diary, and finish this step. Nothing is booked, moved or cancelled until this runs.

    It speaks the confirmation itself, so say nothing after calling it.

    Args:
        action: book a new time, move an existing booking, or cancel one.
        slot_id: The slot exactly as the diary returned it. Empty when cancelling.
        booking_id: The booking exactly as the diary returned it. Empty when booking.
        confirmed: True only after the caller said yes to the sentence you read back.
        additional: True only when they asked for a second appointment on top of one they hold.
    """
    claim_or_refuse(ctx, ACTIONS)
    # Before anything else. A write whose outcome was never established is
    # not a write that did not happen, and until it is settled every other
    # one is a second appointment waiting to be made.
    if ctx.deps.state.unresolved:
        return await _settle(ctx)
    if not confirmed:
        raise ModelRetry(
            'Nothing was saved. Say the service, the day and the time back in one sentence, ask one yes-or-no question, and call this again on their yes.'
        )
    if not _present(ctx.deps.state.customer_verified):
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
    offered = diary.offer_of(ctx.deps.id, slot_id, ctx.deps.state.customer_phone)
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
        ctx.deps.id,
        ctx.deps.state.writes,
        ctx.deps.state.customer_phone,
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
            ctx,
            pending,
            Saved,
            lambda: diary.save_booking(
                ctx.deps.state.customer_phone,
                action,
                slot_id,
                booking_id,
                additional,
                diary.now(),
                operation_token=token,
                offered_to=ctx.deps.id,
            ),
            'diary',
        )
    except diary.RefusedError as refused:
        # The diary's own words, which name what is actually possible instead of
        # leaving the model to guess a new slot.
        raise ModelRetry(str(refused)) from None
    # Legal values are not an answer to this request. Checked before anything
    # is committed or said, because both of those are irreversible in front of
    # a caller.
    if why := _disagrees(action, booking_id, offered, written):
        _unknown(ctx, pending, why)
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
    going_back_to = ctx.deps.resume('concierge')
    ctx.deps.commit(
        appointment=appointment, agent=going_back_to, writes=ctx.deps.state.writes + 1, unresolved=None
    )
    await ctx.deps.speak_now(
        _pick(f'saved:{written.status}', SAVED[written.status]).format(
            spoken=written.spoken,
            service=written.service.replace('hair-color', 'hair colour'),
        )
    )
    return f'{written.status}: {written.spoken}'


# --- the table ---------------------------------------------------------------


# Spoken while a tool runs, by us rather than by the model. One line per
# silence, so a handoff line already spoken suppresses these.
FILLER: dict[str, tuple[str, ...]] = {
    'read_diary': ("Let me see what's free.", 'One moment, checking the diary.'),
    # No line for `record_complaint`: it speaks `NOTED` itself when it returns,
    # which covers the same gap and is the only sentence the caller needs.
}


def build(
    name: AgentName,
    tools=(),
    finishers=(),
    exits: tuple[Exit, ...] = (),
    enter: tuple[str, ...] = (),
    requires: tuple[str, ...] = (),
    satisfied_by: str = '',
    model=MODEL,
) -> Node:
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
    named = [ToolOutput(fn, name=fn.__name__, sequential=fn.__name__ in ACTIONS) for fn in finishers]
    # The whole set, always declared. What varies per request is which of them
    # is *offered*, which `narrow` below decides -- so a step whose exits all
    # shut does not silently change the name of its remaining output tool.
    # `hand_over` named explicitly, like the finishers. Left as a bare
    # function in the output list, pydantic-ai renames it `final_result_hand_over`
    # as soon as there is more than one output -- which the per-request
    # narrowing below then failed to find by name, silently offering every
    # destination whatever the guards said.
    handoff = [ToolOutput(hand_over_for(exits), name='hand_over', sequential=True)] if exits else []
    declared = [str, *handoff, *named]

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
        With none left, `hand_over` goes entirely and the step still has `str`
        -- which for `verify` is exactly right: an incomplete number is a
        reason to ask again, not a reason to leave.
        """
        legal = [e.to for e in exits if e.guard(ctx.deps.state)]
        legal = list(dict.fromkeys(legal))
        kept = []
        for tool_def in defs:
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
                *(Tool(fn, sequential=fn.__name__ in ACTIONS) for fn in tools),
                *(Tool(fn, sequential=fn.__name__ in ACTIONS) for exit in exits for fn in exit.via),
            ],
            capabilities=[
                filler_hook(FILLER),
                one_action_per_reply(ACTIONS),
                Hooks(prepare_tools=only_reachable, prepare_output_tools=narrow),
                # Last, so what it empties is the set the guards already narrowed.
                speak_by(SPEAK_BY),
                # Measurement, not behaviour: it stamps each provider request
                # on the way out so time to first token is per request.
                request_clock(),
            ],
            model_settings=SETTINGS,
        ),
        enter=enter,
        exits=exits,
        requires=requires,
        satisfied_by=satisfied_by,
        outputs=outputs,
    )


# --- the guards --------------------------------------------------------------

# Facts on the state, never judgments. "The caller corrected their number" and
# "this step is about to write something down" are judgments, and they stay in
# the prompts, where judgments belong.


def verified(state: SalonState) -> bool:
    """Identified already, so `verify` is a correction rather than a first pass."""
    return bool(state.customer_verified)


def unverified(state: SalonState) -> bool:
    """Not identified yet, so there is still something for `verify` to do."""
    return not state.customer_verified


# --- the table ---------------------------------------------------------------

# If its words should be heard, it is an agent in here. If they should not -- a
# classifier, an extractor, a summariser -- it is delegation:
# `await worker.run(..., deps=ctx.deps, usage=ctx.usage)` from inside a tool.
AGENTS: dict[str, Node] = {
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
        # Guarded, because an incomplete number is a reason to ask again and not
        # a reason to leave. Unguarded, a live call on 2026-09-20 went
        # `verify -> concierge ("The caller gave an incomplete phone number and
        # cannot confirm")`, the front desk sent them straight back, `requires`
        # interposed verify a second time, and the caller was asked for the same
        # number twice -- once as "which country are you calling from?" when
        # they had already said +34. Two requests and two questions for nothing.
        # With no exit, this step still has `str`: it asks again, which is what
        # it was always meant to do.
        exits=(Exit('concierge', verified),),
        enter=(
            "Let me confirm who I'm speaking to.",
            "One second, let me check who I've got.",
            'Right, let me make sure I have you.',
        ),
    ),
    'booking': build(
        'booking',
        tools=[read_diary],
        finishers=[save_booking],
        exits=(Exit('concierge'), Exit('complaints')),
        # The ordering rule, as a fact instead of a sentence in a prompt.
        requires=('customer_verified',),
        satisfied_by='verify',
        enter=(
            'Let me have a look at the diary.',
            "One second, I'll check the diary.",
            "Right, let me see what's free.",
        ),
    ),
    'complaints': build(
        'complaints',
        tools=[record_complaint],
        finishers=[hang_up],
        # `record_complaint` needs an owner, so this step sends them to be
        # identified -- and only while there is anybody to identify.
        exits=(Exit('concierge'), Exit('verify', unverified)),
    ),
}
