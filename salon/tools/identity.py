"""The verify step's tools: read a number back, look it up once agreed, or give up.

Two of them are output functions that finish the step: `confirm_customer`
does the lookup and the graph works out who takes the caller next, and
`stop_verifying` hands them back unidentified. The others are questions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import TypeAdapter
from pydantic_ai import ModelRetry, RunContext

from core.policies import claim_or_refuse
from salon.models.consent import agrees
from salon.models.receipts import CustomerLookup
from salon.models.shapes import SubmittedPhone
from salon.models.state import Handoff, SalonCall
from salon.tools.asking import Ask
from salon.tools.policy import ACTIONS

_PHONE = TypeAdapter(SubmittedPhone)


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
    return await readback_number(ctx.deps, phone)


async def readback_number(call: SalonCall, phone: str) -> Ask:
    """The readback question for a number, validated first. Shared by the tool and by `advance`."""
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


async def confirm_customer(ctx: RunContext[SalonCall], phone: SubmittedPhone) -> Handoff:
    """Look the caller up by the number they have just agreed to, and finish this step.

    Only after a clear yes to the digits you read back. A caller giving
    different digits is correcting you, not agreeing.

    Args:
        phone: The exact number they confirmed, in E.164.
    """
    claim_or_refuse(ctx, ACTIONS)
    return await lookup_customer(ctx.deps, phone)


async def lookup_customer(call: SalonCall, phone: str) -> Handoff:
    """The lookup, once the caller has agreed to these digits. Shared by the tool and by `advance`.

    The caller has to have agreed to *these digits*, last turn, in answer to a
    readback this code put to them; a yes to some other question is not that,
    and `phone_confirmed` means only that the procedure happened. The lookup
    has to have been about the number asked about, or a customer service
    answering for somebody else would verify this caller as them.

    One commit, not two assignments: written separately, a status the service
    could not produce would fail *after* the phone number had already been
    saved, and every downstream guard reads a saved number as "identified
    enough". The graph already knows where the caller was heading, so the
    finisher does not ask the model.
    """
    phone = _PHONE.validate_python(phone)
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
    if found.customer_phone != phone:
        raise ModelRetry(
            'The customer lookup came back about a different number, so nobody was identified '
            'and nothing was saved. Tell the caller you cannot check who they are just now, and '
            'do not call this again.'
        )
    call.commit(
        customer_phone=phone,
        phone_confirmed=True,
        customer_verified=found.customer_status,
        verified_phone=phone,
    )
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
    ctx.deps._complaint = None
    ctx.deps.commit(pending=None)
    return Handoff(to='concierge', because=because, status='unserved')
