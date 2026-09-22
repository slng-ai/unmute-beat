"""Sage and Stone: four agents, one call, one shared state.

What the salon does differently from the same agency built on a node graph:

* **The prompt is already branched.** Every `{{#if}}` is resolved before the
  model reads it, so an agent never sees an instruction that does not apply
  to this call.
* **Anything code can work out, code works out.** Which day "next Friday" is,
  whether a slot has already passed, what a booking sounds like said out loud.
* **A step that finishes its job ends its own run.** `confirm_customer` and
  `save_booking` are output functions: one model request does the work,
  records it, and the graph works out who takes the caller next. A failure
  raises `ModelRetry` instead, which keeps the step open with the real reason
  in front of the model.
* **Consent is the application's.** A question the caller must answer is
  worded here and recorded on the call; the caller's next words are checked
  against it before anything consequential runs, and a clear answer runs the
  same executor with no model request at all.
* **A refusal carries the alternatives.** The diary says what the caller
  actually holds, or which slots are really free, so the model's next turn is
  a correction rather than a guess.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import replace

from pydantic_ai import Agent, RunContext, Tool, ToolOutput
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.models import infer_model
from pydantic_ai.models.google import GoogleModelSettings
from pydantic_ai.tools import ToolDefinition

from core.graph import Exit, LocalAction, Node, lent
from core.policies import one_action_per_reply, request_clock, speak_by
from salon.instructions import GREETING, instructions
from salon.models.consent import agrees
from salon.models.receipts import BookingCommand
from salon.models.shapes import AgentName
from salon.models.state import SalonCall, SalonState
from salon.tools.booking import (
    ask_booking,
    booking_guidance,
    offer_times,
    read_diary,
    save_booking,
    write_booking,
)
from salon.tools.complaints import (
    ask_complaint,
    complaint_goodbye,
    complaint_guidance,
    record_complaint,
    write_complaint,
)
from salon.tools.desk import hand_over_for, hang_up
from salon.tools.identity import (
    ask_number,
    confirm_customer,
    lookup_customer,
    readback_number,
    stop_verifying,
    verification_guidance,
)
from salon.tools.policy import ACTIONS, SPEAK_BY

__all__ = ['AGENTS', 'GREETING', 'MODEL', 'SETTINGS', 'advance', 'build']

MODEL = infer_model('google-cloud:gemini-3.5-flash-lite')
"""One model for now. `build` takes it per agent, so giving one step a different model is a one-word edit."""
SETTINGS: object = GoogleModelSettings(
    service_tier='priority',
    google_thinking_config={'thinking_level': 'MINIMAL'},  # ty: ignore[invalid-argument-type]
)
"""Thinking pinned off rather than left out.

Left out, no `thinking_config` is sent and whatever Google currently defaults
this model to is what a call gets, a setting that can change under us on the
one dimension a voice turn cannot absorb.
"""

_CLEAR_APPROVAL = re.compile(
    r'(?:yes|yeah|yep|yup|sure|okay|ok|right|correct|exactly|absolutely|go for it|go ahead|do it|'
    r"that(?:'s| is) (?:right|correct)|yes[, ]+(?:that(?:'s| is) (?:right|correct|my number)))"
    r'(?:[, ]+please)?[.! ]*',
    re.IGNORECASE,
)
"""An answer that is nothing but agreement.

ponytail: only complete, familiar answers bypass the model. Extend from
observed misses.
"""
_PHONE_CONTENT = re.compile(
    r'[+\d]|\b(?:phone|number|digits?|zero|one|two|three|four|five|six|seven|eight|nine|'
    r'actually|instead|correction|wrong|wait)\b',
    re.IGNORECASE,
)
"""Anything in an utterance that might be a number or a correction to one."""


def advance(call: SalonCall) -> LocalAction | None:
    """A known next action that needs no model, or None to run the agent.

    Only a clear approval of the one question this call is waiting on, under
    the identity it was asked for: the readback of a number, of a booking, or
    of a complaint. Each runs the same executor its model tool runs, so every
    guard still applies. The one other case is a caller who has just been
    handed to verification with a prefetched number and has said nothing about
    any number: the readback is put to them without asking a model to decide
    to ask it. Ambiguous input still goes to the agent.
    """
    if _CLEAR_APPROVAL.fullmatch(call.heard.strip()) and agrees(call.heard):
        if (
            call.state.agent == 'verify'
            and (question := call.pending('phone'))
            and (not question.owner or question.owner == call.state.customer_phone)
        ):
            phone = question.value
            return LocalAction('confirm_customer', lambda: lookup_customer(call, phone))
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
            return LocalAction('save_booking', lambda: write_booking(call, **command.model_dump()))
        if call.state.agent == 'complaints' and call.complaint_approved and call.pending('complaint'):
            approved = call._complaint
            assert approved is not None
            return LocalAction(
                'record_complaint',
                lambda: write_complaint(call, approved.summary, approved.requested_resolution),
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
        return LocalAction('ask_number', lambda: readback_number(call, call.state.customer_phone))
    return None


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
    """One step: its agent, its legal exits, and what has to hold before it runs.

    Every output tool is named explicitly. Pydantic AI calls a lone output
    function `final_result` and renames a bare `hand_over` to
    `final_result_hand_over` as soon as there is more than one output, which
    the per-request narrowing then fails to find by name. Everything that
    writes is `sequential`, so two of them can never overlap; the conflict
    policy refusing the second is the brace to that belt.

    A question ends the run the same way finishing the step does, and for the
    same reason: the words are already decided, so a further model request
    would only be a chance to word them differently. Questions are not in
    `ACTIONS`, because asking changes nothing and writes nothing.

    `str` in the output list is what keeps speech streaming to TTS. The three
    steps that can write or verify speak only through their output functions,
    so they do not get it and provider text never reaches TTS from them.

    The whole output set is always declared; what varies per request is which
    of it is *offered*, decided by the two prepare hooks below.
    """
    borrowed = lent(exits)
    named = [
        ToolOutput(
            complaint_goodbye if name == 'complaints' and fn is hang_up else fn,
            name=fn.__name__,
            sequential=fn.__name__ in ACTIONS,
        )
        for fn in finishers
    ]
    questions = [ToolOutput(fn, name=fn.__name__) for fn in asks]
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
        """What this step may return: the superset `narrow` picks from at each request."""
        return declared

    async def only_reachable(ctx: RunContext[SalonCall], defs: list[ToolDefinition]) -> list[ToolDefinition]:
        """Take a borrowed tool back out while the edge it stands for is shut.

        An unidentified caller's front desk is not holding the diary, so the
        model cannot read it and be refused, which costs the round trip the
        lending was there to save. Re-run before every model request, so a tool
        that ran a moment ago and changed the state is reflected in the very
        next schema.
        """
        return [d for d in defs if d.name not in borrowed or borrowed[d.name].guard(ctx.deps.state)]

    async def narrow(ctx: RunContext[SalonCall], defs: list[ToolDefinition]) -> list[ToolDefinition]:
        """Offer only the destinations that are open at this request.

        The output-tool half of `only_reachable`, and a separate hook because
        Pydantic AI prepares ordinary and output tools through different ones.
        An exit whose guard has stopped holding leaves the enum the model is
        sent, so it cannot be chosen rather than being refused after it was;
        with none left, `hand_over` goes entirely. `record_complaint` is
        offered only once a readback has been approved.
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
            name=name,
            deps_type=SalonCall,
            output_type=declared,  # type: ignore[arg-type]
            instructions=instructions(name),
            tools=[
                *(
                    fn if isinstance(fn, Tool) else Tool(fn, sequential=fn.__name__ in ACTIONS)
                    for fn in tools
                ),
                *(Tool(fn, sequential=fn.__name__ in ACTIONS) for exit in exits for fn in exit.via),
            ],
            capabilities=[
                one_action_per_reply(ACTIONS),
                Hooks(prepare_tools=only_reachable, prepare_output_tools=narrow),
                *([speak_by(SPEAK_BY)] if not structured_speech else []),
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


def verified(state: SalonState) -> bool:
    """Identified already, so `verify` is a correction rather than a first pass.

    Guards are facts on the state, never judgments. "The caller corrected
    their number" is a judgment, and it stays in the prompts.
    """
    return bool(state.customer_verified)


AGENTS: dict[str, Node[SalonState]] = {
    'concierge': build(
        'concierge',
        finishers=[hang_up],
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
        exits=(Exit('concierge', verified),),
    ),
    'booking': build(
        'booking',
        tools=[read_diary],
        finishers=[save_booking],
        asks=[ask_booking, offer_times, booking_guidance],
        guarded=True,
        exits=(Exit('concierge'), Exit('complaints')),
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
"""The graph. If an agent's words should be heard, it is a step in here; if not, it is a delegation from inside a tool.

`booking` appears twice among the front desk's exits, and the pair is the
point: unguarded, it is a `hand_over` the model may choose whoever is calling,
and whether anyone has to be identified first is the graph's business;
guarded on `verified`, it also lends `read_diary`, so an identified caller
moves into `booking` by the diary being read rather than by a request that
exists only to say "booking". `verify` is an exit only once somebody has been
identified, because before that `requires` interposes it. The verify step's
own exit is guarded too: an incomplete number is a reason to ask again, not a
reason to leave.
"""
