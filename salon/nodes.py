"""What every salon step is built from: `build`, and the reading of a reply before any step speaks.

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
  same executor after one structured classification request.
* **A refusal carries the alternatives.** The diary says what the caller
  actually holds, or which slots are really free, so the model's next turn is
  a correction rather than a guess.

Each step's own module in `salon/steps/` calls `build` with its tools; the
step's prompt, exits and requirements come from `salon/flow.yaml`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import replace

from pydantic import TypeAdapter, ValidationError
from pydantic_ai import Agent, RunContext, Tool, ToolOutput
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage, UsageLimits

from core.decisions import Clarify
from core.graph import Exit, LocalAction, Node, Opening, lent
from core.policies import one_action_per_reply, request_clock, speak_by
from core.steps import AgentStep
from salon import classification
from salon.classification import DETOUR, MODEL, SETTINGS, SOLVER, classify_reply
from salon.models.consent import CorrectPhone, SalonSwitch
from salon.models.receipts import BookingCommand
from salon.models.shapes import SubmittedPhone
from salon.models.state import SalonCall, SalonState
from salon.tools.asking import Ask
from salon.tools.booking import cancel_question, write_booking
from salon.tools.complaints import write_complaint
from salon.tools.desk import clarify, end_conversation, hand_over_for, switch_task
from salon.tools.identity import lookup_customer, readback_number
from salon.tools.policy import ACTIONS, SPEAK_BY

__all__ = ['EXITS', 'advance', 'build', 'complaint_ready', 'decided', 'solve', 'verified']

EXITS: dict[str, tuple[Exit[SalonState], ...]] = {}
"""Each built step's exits, by name, so `solve` can hand a caller on without importing the table that holds the steps."""


async def advance(
    call: SalonCall, usage: RunUsage | None = None, usage_limits: UsageLimits | None = None
) -> LocalAction | None:
    """Resolve a pending decision before spending a conversational request.

    Classification is shared with the tool guards. An unresolved answer falls
    through to conversation, while application guards still own every write.
    While the identity check runs, whether the caller kept the request it
    interrupted is read at the same time, as its own request, so it adds no
    wait and cannot blur the consent reading.
    With the solver live, `solve` reads the reply instead and can also answer
    a correction or a change of task.
    """
    if classification.INTERPRETER == 'solver':
        return await solve(call, usage, usage_limits)
    if call.awaits_reading:
        readings = [classify_reply(call, usage=usage, usage_limits=usage_limits)]
        if call.detour_request:
            readings.append(DETOUR.interpret(call, usage=usage, usage_limits=usage_limits))
        await asyncio.gather(*readings)
    return decided(call)


def decided(call: SalonCall) -> LocalAction | None:
    """The action this turn's consent reading decides on its own, or None to let the step's agent answer.

    In order of what the caller would lose if it were missed: a booking made
    in this call that they take back is offered for cancelling, on whatever
    turn they say so (Coval run Y47Ut7oC, K2: "Leave it, thanks" two turns
    after the booking was read as the goodbye it also was, and hung up on it)
    unless that cancel is the question already waiting, which the answer then
    answers ("Yes, please cancel it" is a withdrawal too, and re-asked it
    three times, Coval run gdw96FkH), a plain pick of an offered
    time books it (the write's own guard still checks the slot was on the
    table, and asks move-or-add if they already hold one), an answer that
    agrees to something runs it, and only then does a goodbye end the call. "Yes, go
    ahead, bye" used to hang up on the yes, and "Leave it, thanks, goodbye"
    used to hang up on the booking.
    """
    question = call.pending_question()
    reply = call.classification
    cancelling = (
        question is not None and question.action == 'cancel' and question.booking_id == call.undo_booking
    )
    if (undo := call.undo_booking) and reply.last_change == 'withdraw' and not cancelling:
        return LocalAction('ask_booking', lambda: _cancel_question(call, undo))
    if (slot := call.picked_offer()) and call.state.customer_verified:
        return LocalAction('save_booking', lambda: write_booking(call, 'book', slot, ''))
    if question is not None and question.about == 'move_or_add' and call.state.customer_verified:
        choice_question = question
        if reply.appointment_choice == 'add':
            return LocalAction('save_booking', lambda: write_booking(call, 'book', choice_question.value, ''))
        if reply.appointment_choice == 'move' and question.booking_id:
            return LocalAction(
                'save_booking',
                lambda: write_booking(call, 'move', choice_question.value, choice_question.booking_id),
            )
    if reply.confirmation == 'accept':
        if call.state.agent == 'verify' and (question := call.pending('phone')):
            phone = question.value
            return LocalAction('confirm_customer', lambda: lookup_customer(call, phone))
        if (
            call.state.customer_verified
            and (question := call.pending('booking'))
            and question.action in ('book', 'move', 'cancel')
            and (call.state.agent == 'booking' or question.action == 'cancel')
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
    if question is not None and reply.conversation == 'end':
        return LocalAction('hang_up', lambda: end_conversation(call, 'Thanks for calling. Goodbye.'))
    return None


async def _cancel_question(call: SalonCall, booking_id: str) -> Ask:
    """The application's own cancel read-back for a booking the caller has just taken back.

    A cancel question can be answered from whichever step the caller is in,
    because the question is the authority and the write's own guard checks it:
    the caller who took back a picked time is back at the front desk by then.
    """
    return cancel_question(call, booking_id)


async def solve(
    call: SalonCall, usage: RunUsage | None = None, usage_limits: UsageLimits | None = None
) -> LocalAction | None:
    """Read the reply as one decision and map it to the executor that owns it.

    Only where there is something to decide: last turn's proposal, heard in
    full or spoken over, or a complaint approval kept through verification.
    Every other turn goes straight to the step's agent with no request here.

    The three kinds the four-label classifier cannot say are dispatched here;
    everything else is the consent reading the solver already recorded, so
    `decided` and every tool guard treat it exactly as they treat the
    classifier's. A clarifying question is only asked about a readback: for
    anything else the step's own agent, which has the salon's knowledge,
    answers better than a one-line question would.
    """
    last = call.last_proposal()
    if last is None and call._complaint is None:
        return None
    solution = await SOLVER.interpret(call, usage=usage, usage_limits=usage_limits)
    proposal = last[0] if last else None
    readback = proposal is not None and proposal.about == 'phone' and call.state.agent == 'verify'
    match solution.decision:
        case CorrectPhone(digits=digits) if readback:
            try:
                phone = _PHONE.validate_python(digits)
            except ValidationError:
                return None
            return LocalAction('ask_number', lambda: readback_number(call, phone))
        case SalonSwitch(target=target, details=details):
            exits = EXITS[call.state.agent]
            return LocalAction('hand_over', lambda: switch_task(call, exits, target, details, solution.reply))
        case Clarify(question=words) if readback:
            return LocalAction('clarify', lambda: clarify(call, words))
    return decided(call)


_PHONE = TypeAdapter(SubmittedPhone)


def build(
    step: AgentStep,
    tools=(),
    finishers=(),
    asks=(),
    openings: tuple[Opening[SalonCall], ...] = (),
    structured_speech: bool = False,
    offered_when: Mapping[str, Callable[[SalonCall], bool]] | None = None,
    stand_ins: Mapping[Callable, Callable] | None = None,
    model=MODEL,
    shares_detour: bool = False,
) -> Node[SalonState]:
    """One step: its agent, its legal exits, and what has to hold before it runs.

    The step's name, prompt, exits and requirements come from its entry in
    `flow.yaml`; the arguments here are what only its code can say. Everything
    that differs between steps is an argument, so this function
    names no step and no tool: `structured_speech` for a step that speaks only
    through its outputs, `offered_when` for an output offered only while a
    fact holds, and `stand_ins` for an output whose name the model knows but
    whose implementation is this step's own.

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
    name, exits = step.id, step.exits
    EXITS[name] = exits
    borrowed = lent(exits)
    named = [
        ToolOutput(
            (stand_ins or {}).get(fn, fn),
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
    offered_when = offered_when or {}
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
        with none left, `hand_over` goes entirely. An output with an
        `offered_when` fact is offered only while it holds.
        """
        legal = [e.to for e in exits if e.guard(ctx.deps.state)]
        legal = list(dict.fromkeys(legal))
        kept = []
        for tool_def in defs:
            if (offered := offered_when.get(tool_def.name)) is not None and not offered(ctx.deps):
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
        Agent[SalonCall, object](
            model,
            name=name,
            deps_type=SalonCall,
            output_type=declared,  # type: ignore[arg-type]
            instructions=step.instructions,
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
        requires=step.requires,
        satisfied_by=step.satisfied_by,
        outputs=outputs,
        guarded=step.guarded,
        shares_detour=shares_detour,
        model_speech=not structured_speech,
        advance=advance,
        openings=openings,
    )


def complaint_ready(call: SalonCall) -> bool:
    """`record_complaint` is offered only once an identified caller has approved a readback."""
    return bool(call.state.customer_verified) and call.complaint_approved


def verified(state: SalonState) -> bool:
    """Identified already, so `verify` is a correction rather than a first pass.

    Guards are facts on the state, never judgments. "The caller corrected
    their number" is a judgment, and it stays in the prompts.
    """
    return bool(state.customer_verified)
