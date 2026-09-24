"""The verify step's tools: read a number back, look it up once agreed, or give up.

Two of them are output functions that finish the step: `confirm_customer`
does the lookup and the graph works out who takes the caller next, and
`stop_verifying` hands them back unidentified. The others are questions.

The step's two openings are here too: entered to verify somebody for a
waiting request, it reads back the carrier's number or asks for one without
a model deciding to, because nothing the caller has said yet could change
which of the two it is.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.usage import RunUsage, UsageLimits

from core.graph import Opening
from core.operations import Operation
from core.policies import claim_or_refuse
from salon.classification import classify_reply
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
    return await guide(ctx.deps, topic)


async def guide(
    call: SalonCall, topic: Literal['number', 'country', 'incomplete', 'correction', 'unsupported']
) -> str:
    """Say the fixed question for a missing piece of the number. Shared by the tool and the step's opening."""
    text = _GUIDANCE[topic]
    call.answered()
    await call.speak_now(text, origin='guidance')
    return text


_GUIDANCE = {
    'number': 'Which phone number should I use? Please say it with the country code.',
    'country': 'Which country is that phone number from?',
    'incomplete': 'Please give me the rest of the digits.',
    'correction': 'Please say the full number again, with the country code.',
    'unsupported': 'I can look up Spanish phone numbers. Do you have a Spanish number I can use?',
}
"""The fixed lines for a missing piece of the number.

A request for digits is worded as a request, never as a yes/no question: asked
"What phone number should I use, including the country code?", four callers in
five answered "Yes." first, and that yes cost a turn every time.
"""


async def confirm_customer(ctx: RunContext[SalonCall], phone: SubmittedPhone) -> Handoff:
    """Look the caller up by the number they have just agreed to, and finish this step.

    Only after a clear yes to the digits you read back. A caller giving
    different digits is correcting you, not agreeing.

    Args:
        phone: The exact number they confirmed, in E.164.
    """
    claim_or_refuse(ctx, ACTIONS)
    return await lookup_customer(ctx.deps, phone, usage=ctx.usage, usage_limits=ctx.usage_limits)


class PhoneArgs(BaseModel):
    """The one argument a lookup takes: the number the caller agreed to."""

    phone: SubmittedPhone


class LookupCustomer(Operation[SalonCall, PhoneArgs, CustomerLookup, Handoff]):
    """Identify the caller by the number they have just agreed to, and finish the verify step.

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

    name = 'confirm_customer'
    args_type = PhoneArgs
    receipt_type = CustomerLookup

    async def _authorise(
        self, call: SalonCall, args: PhoneArgs, *, usage: RunUsage | None, usage_limits: UsageLimits | None
    ) -> None:
        question = call.pending('phone', value=args.phone)
        if question is None or (question.owner and question.owner != call.state.customer_phone):
            raise ModelRetry(
                'Nothing was looked up: those digits have not been read back to this caller and agreed '
                f'to. Call `ask_number` with {args.phone} and wait for their answer.'
            )
        reply = await classify_reply(call, usage=usage, usage_limits=usage_limits)
        if reply.confirmation != 'accept':
            raise ModelRetry(
                'Nothing was looked up: the caller did not agree to the number that was read back. '
                f'They said "{call.heard}". If those are different digits it is a correction -- read '
                'the new number back with `ask_number`; otherwise ask again.'
            )
        call.answered()

    async def _execute(self, call: SalonCall, args: PhoneArgs, grant: None) -> dict:
        return call.diary.find_or_create_customer(args.phone)

    async def _record(self, call: SalonCall, args: PhoneArgs, receipt: CustomerLookup) -> Handoff:
        if receipt.customer_phone != args.phone:
            raise ModelRetry(
                'The customer lookup came back about a different number, so nobody was identified '
                'and nothing was saved. Tell the caller you cannot check who they are just now, and '
                'do not call this again.'
            )
        call.commit(
            customer_phone=args.phone,
            phone_confirmed=True,
            customer_verified=receipt.customer_status,
            verified_phone=args.phone,
        )
        call.retain_action('confirm_customer', receipt.model_dump(mode='json'))
        return Handoff(to=call.resume('concierge'), because='the caller is identified', status='completed')  # ty: ignore[invalid-argument-type]


LOOKUP = LookupCustomer()


async def lookup_customer(
    call: SalonCall, phone: str, *, usage: RunUsage | None = None, usage_limits: UsageLimits | None = None
) -> Handoff:
    """The lookup, once the caller has agreed to these digits. Shared by the tool and by `advance`."""
    return await LOOKUP.run(call, PhoneArgs(phone=phone), usage=usage, usage_limits=usage_limits)


async def stop_verifying(ctx: RunContext[SalonCall], because: str) -> Handoff:
    """Give the caller back to the front desk without identifying them, and finish this step.

    For a caller who will not confirm a number, or who asks about something
    else entirely. Whatever they were waiting to do that needed identifying is
    given up with it, so use it when they have moved on and not while they are
    still correcting the digits. A change to what they asked for, such as
    another day, time or service, is not moving on: carry on with the number,
    and what they said is passed on.

    Args:
        because: One short sentence: why this stopped. Never spoken.
    """
    claim_or_refuse(ctx, ACTIONS)
    ctx.deps._complaint = None
    ctx.deps.commit(pending=None, wanted=None)
    return Handoff(to='concierge', because=because, status='unserved')


_PHONE_CONTENT = re.compile(
    r'[+\d]|\b(?:phone|number|digits?|zero|one|two|three|four|five|six|seven|eight|nine|'
    r'actually|instead|correction|wrong|wait)\b',
    re.IGNORECASE,
)
"""Anything in an utterance that might be a number or a correction to one."""


_SPANISH_RUN = re.compile(r'\+\s*3\s*4(?:[\s.,-]*\d)+')
"""A "+34" and the digit groups after it, however the pauses split them: "+34 6. 71. 2. 73. 4. 5. 4."."""


def number_heard(words: str) -> str:
    """The Spanish number the caller said whole, as the last "+34" and exactly nine digits after it, or ''.

    Coval run L5RWCS7b, K3: the caller said "+34 6. 71. 2. / 73. 4. / 5. /
    4." and then "+34. / 671. / 273. 45. / 4.", nine digits both times, and
    the model counted neither and asked for "the rest of the digits" twice.
    Speech-to-text writes digits as numerals, so counting them is code's job.
    Anything else (ten digits, digits in words, no "+34", a number that is
    not a real Spanish one) is '' and the model handles it, as before.
    """
    runs = _SPANISH_RUN.findall(words)
    if not runs:
        return ''
    digits = re.sub(r'\D', '', runs[-1])
    if len(digits) != 11:
        return ''
    try:
        return _PHONE.validate_python(f'+{digits}')
    except ValidationError:
        return ''


def _waiting_for_a_number(call: SalonCall) -> bool:
    """Interposed for a waiting request, nobody identified, no readback out, and nothing number-like said yet.

    Anything the caller has said that might be digits or a correction sends
    the turn to the model instead, which is the safe side: combining fragments
    is the model's job, and a wrong readback costs a whole extra turn.
    """
    return (
        bool(call.state.pending)
        and not call.state.customer_verified
        and call.pending('phone') is None
        and not any(_PHONE_CONTENT.search(words) for words in call._inputs.values())
    )


async def read_back_caller_id(call: SalonCall) -> Ask:
    """Read back the number the carrier supplied."""
    return await readback_number(call, call.state.customer_phone)


async def ask_for_number(call: SalonCall) -> str:
    """Ask for a number, when the carrier supplied none."""
    return await guide(call, 'number')


OPENINGS = (
    Opening(
        'ask_number',
        'handoff',
        lambda call: _waiting_for_a_number(call) and bool(call.state.customer_phone),
        read_back_caller_id,
    ),
    Opening(
        'verification_guidance',
        'handoff',
        lambda call: _waiting_for_a_number(call) and not call.state.customer_phone,
        ask_for_number,
    ),
)
"""The verify step's arrival: read back the carrier's number if there is one, else ask for it."""
