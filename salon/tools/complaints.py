"""The complaints step's tools: guidance from the policy, a readback in the caller's words, the record.

This step speaks only through these. Nothing is written until the caller has
approved a readback made of their own words, and recording a request is not
approving it.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.usage import RunUsage, UsageLimits

from core.operations import Operation
from core.policies import claim_or_refuse
from core.state import present
from salon.classification import classify_reply
from salon.instructions import COMPLAINT_GUIDANCE
from salon.models.receipts import ComplaintRecord
from salon.models.state import SalonCall, Unresolved
from salon.tools.asking import Ask
from salon.tools.desk import hang_up
from salon.tools.policy import ACTIONS
from salon.tools.writes import OUTSTANDING, _answered, acknowledgement, close_with, operation_token

_COMPLAINT_REQUEST_WORDS = (
    'a about an are be been caller can client complain complaining complaint complaints could customer '
    'er file files filing formal had has have he hello her hi his i id im in is it leave leaving like '
    'lodge lodges lodging make makes making may maybe me might my need official our please put raise '
    'raises raising register registering she someone something submit submitting the their they to uh '
    'um us want wants was we were wish would yeah yes you your '
)
_ASKING_TO_COMPLAIN = frozenset(_COMPLAINT_REQUEST_WORDS.split())
"""The words in "I'd like to raise a complaint" and nothing else.

A summary made only of these describes wanting to complain, not what went
wrong, and reading it back asks the caller to confirm their own request.

ponytail: a closed word list and it fails the same
way: towards asking what happened. Upgrade it with them.
"""


def _describes_nothing(summary: str) -> bool:
    """Whether this summary says only that they want to complain."""
    words = [word.strip("'") for word in re.findall(r"[a-zA-Z']+", summary.lower())]
    return not words or all(word in _ASKING_TO_COMPLAIN for word in words)


def _gist(summary: str) -> str:
    """One stable name for a complaint summary, so agreement is to the summary that was read out."""
    return hashlib.sha256(' '.join(summary.casefold().split()).encode()).hexdigest()[:12]


def caller_excerpt(call: SalonCall, excerpt: str) -> bool:
    """Whether these words are the caller's, from their speech and never from model-authored history.

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


async def record_complaint(ctx: RunContext[SalonCall]) -> ComplaintRecord:
    """Record the exact complaint and resolution the caller already approved, then stop.

    It speaks the receipt itself. Do not reword the approved complaint or say
    another acknowledgement. It uses the approved wording without changes.
    """
    await classify_reply(ctx.deps, usage=ctx.usage, usage_limits=ctx.usage_limits)
    approved = ctx.deps._complaint
    if approved is None:
        raise ModelRetry('Use ask_complaint and wait for approval before recording anything.')
    return await _record_complaint(ctx, approved.summary, approved.requested_resolution)


async def _record_complaint(
    ctx: RunContext[SalonCall], summary: str, requested_resolution: str
) -> ComplaintRecord:
    """The tool's body with explicit wording: claim the action, then write."""
    claim_or_refuse(ctx, ACTIONS)
    return await write_complaint(
        ctx.deps, summary, requested_resolution, usage=ctx.usage, usage_limits=ctx.usage_limits
    )


class ComplaintArgs(BaseModel):
    """The exact wording the caller approved: what went wrong, and what they asked for."""

    summary: str
    requested_resolution: str


class WriteComplaint(Operation[SalonCall, ComplaintArgs, ComplaintRecord, ComplaintRecord]):
    """Write the complaint into the book, once the caller has approved these exact words.

    The wording has to be the wording that was read back and approved, under
    the identity it was approved for. One token per complaint, derived like a
    booking's, so an attempt that lost its answer and is retried leaves one
    entry in the book rather than two, and the write goes through the same
    boundary a booking does. The receipt is validated and then *read*: a
    well-formed `recorded: False` is the book saying it did not take it. The
    acknowledgement, reference included, is spoken here before the next
    request rather than by the model after it.
    """

    name = 'record_complaint'
    args_type = ComplaintArgs
    receipt_type = ComplaintRecord

    async def _authorise(
        self,
        call: SalonCall,
        args: ComplaintArgs,
        *,
        usage: RunUsage | None,
        usage_limits: UsageLimits | None,
    ) -> None:
        if call.state.unresolved:
            raise ModelRetry(OUTSTANDING)
        if not present(call.state.customer_verified):
            raise ModelRetry(
                'Nobody is identified yet. Return to concierge so verification can run before complaints.'
            )
        await classify_reply(call, usage=usage, usage_limits=usage_limits)
        approved = call._complaint
        if (
            approved is None
            or approved.value != _gist(args.summary)
            or approved.resolution != _gist(args.requested_resolution)
        ):
            raise ModelRetry(
                'Nothing was written down. Read this summary and requested resolution back with '
                '`ask_complaint` and wait for their answer. Use the exact wording they approved.'
            )
        if approved.owner and approved.owner != call.state.customer_phone:
            call._complaint = None
            raise ModelRetry('The caller identity changed. Confirm the complaint again with ask_complaint.')
        call._complaint = None
        call.answered()

    async def _execute(self, call: SalonCall, args: ComplaintArgs, grant: None) -> ComplaintRecord:
        phone = call.state.customer_phone
        token = operation_token(call.id, call.state.writes, phone, args.summary, args.requested_resolution)
        pending = Unresolved(token=token, action='complaint', wanted=args.requested_resolution)
        return _answered(
            call,
            pending,
            ComplaintRecord,
            lambda: call.diary.record_complaint(
                phone, args.summary, args.requested_resolution, operation_token=token
            ),
            'complaint book',
        )

    async def _record(
        self, call: SalonCall, args: ComplaintArgs, receipt: ComplaintRecord
    ) -> ComplaintRecord:
        if not receipt.recorded:
            raise ModelRetry(
                'The complaint book refused it and nothing is on record. Tell the caller you could '
                'not log it. Do not promise a callback or call this again.'
            )
        call.commit(writes=call.state.writes + 1)
        call.retain_action('record_complaint', receipt.model_dump(mode='json'))
        await close_with(call, acknowledgement(receipt.reference, args.requested_resolution))
        return receipt


WRITE_COMPLAINT = WriteComplaint()


async def write_complaint(
    call: SalonCall,
    summary: str,
    requested_resolution: str,
    *,
    usage: RunUsage | None = None,
    usage_limits: UsageLimits | None = None,
) -> ComplaintRecord:
    """The write itself, once the caller has approved these exact words. Shared by the tool and by `advance`."""
    args = ComplaintArgs(summary=summary, requested_resolution=requested_resolution)
    return await WRITE_COMPLAINT.run(call, args, usage=usage, usage_limits=usage_limits)


async def complaint_goodbye(ctx: RunContext[SalonCall]) -> str:
    """End the call only when the caller is finished. Speaks a fixed goodbye."""
    return await hang_up(ctx, 'Thanks for calling. Goodbye.')
