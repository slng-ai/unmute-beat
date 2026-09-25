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
step's prompt, exits and requirements come from `salon/flow.yaml`. How a step's
agent is put together is core's (`core.steps.AgentBuilder`), the same for every
use case.
"""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter, ValidationError
from pydantic_ai.usage import RunUsage, UsageLimits

from core.decisions import Clarify
from core.graph import Exit, LocalAction, Node
from core.desk import AgentBuilder
from core.steps import AgentStep
from salon import classification
from salon.classification import MODEL, SALON, SETTINGS, SOLVER, classify_reply
from salon.models.consent import CorrectPhone, SalonSwitch
from salon.models.shapes import SubmittedPhone
from salon.models.state import SalonCall, SalonState
from salon.tools.desk import DESK, clarify, switch_task
from salon.tools.identity import readback_number
from salon.tools.policy import ACTIONS, SPEAK_BY
from salon.tools.writes import settle

__all__ = ['BUILDER', 'EXITS', 'advance', 'build', 'complaint_ready', 'decided', 'solve', 'verified']

EXITS: dict[str, tuple[Exit[SalonState], ...]] = {}
"""Each built step's exits, by name, so `solve` can hand a caller on without importing the table that holds the steps."""


async def advance(
    call: SalonCall, usage: RunUsage | None = None, usage_limits: UsageLimits | None = None
) -> LocalAction | None:
    """Resolve a pending decision before spending a conversational request.

    The readings that apply to this turn run side by side (`classify_reply`),
    and the salon's acts decide on them in core's fixed order (`SALON`). A turn
    no reading applies to costs no request here. Classification is shared
    with the tool guards; an unresolved answer falls through to conversation,
    while application guards still own every write. With the solver live,
    `solve` reads the reply instead and can also answer a correction or a
    change of task.

    A write whose outcome is still unknown is settled first, by code, on the
    caller's next words, whatever they are: the diary is asked what it did,
    and a write that landed is confirmed in the diary's words. One the diary
    still cannot answer is refused, and the step's model is told why. Live
    text T4.v2, session 3: settling waited for the model to try the write
    again, and a model that did as it was told never did.
    """
    if call.state.unresolved is not None and call.heard.strip():
        action = 'record_complaint' if call.state.unresolved.action == 'complaint' else 'save_booking'
        return LocalAction(action, lambda: settle(call))
    if classification.INTERPRETER == 'solver':
        return await solve(call, usage, usage_limits)
    await classify_reply(call, usage=usage, usage_limits=usage_limits, before_model=True)
    return decided(call)


def decided(call: SalonCall) -> LocalAction | None:
    """The action this turn's readings decide on their own, or None to let the step's agent answer."""
    return SALON.decide(call)


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
    if last is None and call.held('complaint') is None:
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


BUILDER = AgentBuilder[SalonCall](
    call_type=SalonCall,
    model=MODEL,
    settings=SETTINGS,
    actions=ACTIONS,
    desk=DESK,
    advance=advance,
    speak_by=SPEAK_BY,
)
"""Core's builder, given the salon's call, model, actions, desk and `advance`: every salon step is built by it."""


def build(step: AgentStep, **options: Any) -> Node[SalonState]:
    """One salon step, built by `BUILDER`. Each step's module passes what only its code can say (`AgentBuilder.build`)."""
    EXITS[step.id] = step.exits
    return BUILDER.build(step, **options)


def complaint_ready(call: SalonCall) -> bool:
    """`record_complaint` is offered only once an identified caller has approved a readback."""
    return bool(call.state.customer_verified) and call.complaint_approved


def verified(state: SalonState) -> bool:
    """Identified already, so `verify` is a correction rather than a first pass.

    Guards are facts on the state, never judgments. "The caller corrected
    their number" is a judgment, and it stays in the prompts.
    """
    return bool(state.customer_verified)
