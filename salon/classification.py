"""The salon's semantic decision boundary, independent of its conversational agents.

Two interpreters read a caller's reply, and one of them is live at a time:

* `CONSENT`, the default: six narrow labels (confirmation, appointment
  choice, end of call, retained complaint approval, whether the reply takes
  back the booking just made, and which time on the table it picks). A
  correction or a change of task is not something it can say, so those fall
  through to an agent.
* `SOLVER`, behind `SALON_INTERPRETER=solver`: one decision per reply from a
  wider set (accept, correct the number, switch task, clarify, ...), so a
  correction or a switch can be answered without a second request.

Whichever is live, the tool guards read the same thing: `SalonCall.classification`,
the consent reading bound to the proposal it answers. The solver writes its
decision into that reading too, so no guard changes and no write path is new.

`DETOUR` is separate from both: while the identity check runs, it reads
whether the caller kept the request that check interrupted, the evidence a
return opening needs (`CallContext.request_kept`). It runs beside the consent
reading, not inside it, and only with the classifier live; with the solver, a
return from the check always runs the step's model.
"""

import os
from typing import TYPE_CHECKING, Literal

from pydantic_ai.models import infer_model
from pydantic_ai.models.google import GoogleModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits

from core.classifiers import Classifier
from core.decisions import Accept, Conditional, EndCall, NoDecision, Reject, Revise
from core.interpreters import ClassifierInterpreter
from salon.models.consent import (
    Asked,
    ChooseAppointment,
    ConsentInput,
    CorrectPhone,
    Destination,
    DetourInput,
    DetourReading,
    ReplyClassification,
    SalonSolution,
    SalonSwitch,
    SolverInput,
)

if TYPE_CHECKING:
    from salon.models.state import SalonCall


MODEL = infer_model('google-cloud:gemini-3.5-flash-lite')
"""One model for now. `build` takes it per agent, so giving one step a different model is a one-word edit."""
SETTINGS = GoogleModelSettings(
    service_tier='priority',
    google_thinking_config={'thinking_level': 'MINIMAL'},  # ty: ignore[invalid-argument-type]
)
"""Thinking pinned off rather than left out.

Left out, no `thinking_config` is sent and whatever Google currently defaults
this model to is what a call gets, a setting that can change under us on the
one dimension a voice turn cannot absorb.
"""

INTERPRETER: Literal['classifier', 'solver'] = (
    'solver' if os.environ.get('SALON_INTERPRETER') == 'solver' else 'classifier'
)
"""Which interpreter reads replies. Read at call time, so a test can switch it with `patch.object`.

ponytail: the solver stays opt-in until `evals/compare.py` has run live on
the reviewed cases and its false acceptances are no worse than the
classifier's. Flip the default here once they are.
"""


CONTEXT = (
    'A caller is on the phone with the receptionist of Sage and Stone, a hair salon. The receptionist asked '
    'them something and is waiting for the answer. The salon acts only on a clear class; an unclear one changes '
    'nothing.'
)
"""The salon's context for its consent reading: who is talking, and what an answer is for. Prompt text."""

CLASSIFIER = Classifier(MODEL, ConsentInput, ReplyClassification, context=CONTEXT, model_settings=SETTINGS)

SOLVER_INSTRUCTIONS = (
    'Read the caller utterance as exactly one decision about the pending proposal, using the class '
    'descriptions in the output schema. Accept only when delivered is true and the caller clearly '
    'approves that exact proposal. Use switch_task only for one of the listed destinations. '
    'Never invent digits.'
)
"""The solver's whole prompt. The decisions' own descriptions carry the rest, in the schema.

ponytail: a new prompt, checked only offline. It must pass the live
comparison before `INTERPRETER` defaults to the solver.
"""

SOLVER_CLASSIFIER = Classifier(
    MODEL, SolverInput, SalonSolution, context=SOLVER_INSTRUCTIONS, model_settings=SETTINGS
)

DESTINATIONS = (
    Destination(
        name='booking',
        purpose='Book, move or cancel an appointment.',
        needs='The caller is identified by phone number first.',
    ),
    Destination(
        name='complaints',
        purpose='Record a complaint about a past visit.',
        needs='The caller is identified by phone number first.',
    ),
    Destination(name='concierge', purpose='General questions: opening hours, prices, services, location.'),
)
"""Where a caller can switch to, as the solver is told. Legality right now is checked again when a switch is applied."""


def _retain_complaint(call: SalonCall, reading: ReplyClassification) -> None:
    """Keep an approved complaint only while the caller has not withdrawn it, and bind a new approval to its owner."""
    if call._complaint is not None and reading.retained_approval != 'keep':
        call._complaint = None
    if (question := call.pending('complaint')) and reading.confirmation == 'accept':
        call._complaint = question.model_copy(update={'owner': call.state.customer_phone})


class ConsentInterpreter(ClassifierInterpreter['SalonCall', ConsentInput, ReplyClassification]):
    """The four-label consent classifier: what the caller agreed to, chose, or ended."""

    def _input(self, call: SalonCall) -> ConsentInput:
        return call.consent_snapshot()

    def _still_valid(self, call: SalonCall, value: ConsentInput) -> bool:
        """The same reply, under the same identity, to the same question, with the same times on the table.

        Field by field, not the whole snapshot: `_settle` itself changes the
        retained complaint, and a reading must not go stale by being recorded.
        """
        now = call.consent_snapshot()
        return (
            value.answer == now.answer
            and value.owner == now.owner
            and value.pending == now.pending
            and value.offered == now.offered
            and value.holding == now.holding
        )

    def _rereadable(self, call: SalonCall, value: ConsentInput) -> bool:
        """Only the times on the table or the caller's holdings moved: the same reply, to a wider question.

        A diary read inside the turn is what does this, and without a new
        reading a caller who named a time ("Friday at nine") could never be
        matched to the slot the read just found. Identity, question and words
        must all be unchanged, or the stale reading stays the safe default.
        """
        now = call.consent_snapshot()
        return value.answer == now.answer and value.owner == now.owner and value.pending == now.pending

    def _settle(
        self, call: SalonCall, value: ConsentInput, result: ReplyClassification
    ) -> ReplyClassification:
        call.record_classification(value, result)
        _retain_complaint(call, call.classification)
        return result


def consent_of(solution: SalonSolution, retained: bool) -> ReplyClassification:
    """The consent reading a decision amounts to, so every existing guard reads the solver unchanged."""
    decision = solution.decision
    confirmation = {
        Accept: 'accept',
        Reject: 'reject',
        Revise: 'revise',
        CorrectPhone: 'revise',
        Conditional: 'conditional',
    }.get(type(decision), 'unclear')
    withdrawn = isinstance(decision, SalonSwitch | EndCall)
    return ReplyClassification.model_validate(
        {
            'confirmation': confirmation,
            'appointment_choice': decision.choice if isinstance(decision, ChooseAppointment) else 'unclear',
            'conversation': 'end' if isinstance(decision, EndCall) else 'continue',
            'retained_approval': ('withdraw' if withdrawn else 'keep') if retained else 'unclear',
        }
    )


class SalonSolver(ClassifierInterpreter['SalonCall', SolverInput, SalonSolution]):
    """One decision per reply, from the proposal, the step, and where the caller could go instead."""

    def _input(self, call: SalonCall) -> SolverInput:
        last = call.last_proposal()
        return SolverInput(
            answer=call.heard,
            question=call.consent_snapshot().question,
            step=call.state.agent,
            owner=call.state.customer_phone,
            proposal=last[0] if last else None,
            delivered=bool(last and last[1]),
            retained_complaint=call._complaint,
            destinations=tuple(d for d in DESTINATIONS if d.name != call.state.agent),
        ).model_copy(deep=True)

    def _still_valid(self, call: SalonCall, value: SolverInput) -> bool:
        last = call.last_proposal()
        proposal: Asked | None = last[0] if last else None
        return (
            value.answer == call.heard
            and value.owner == call.state.customer_phone
            and value.step == call.state.agent
            and value.proposal == proposal
        )

    def _settle(self, call: SalonCall, value: SolverInput, result: SalonSolution) -> SalonSolution:
        """An undelivered proposal cannot be agreed to, whatever the model read; then record the consent reading."""
        if not value.delivered and isinstance(result.decision, Accept | ChooseAppointment):
            result = SalonSolution(decision=NoDecision())
        call.record_classification(call.consent_snapshot(), consent_of(result, call._complaint is not None))
        _retain_complaint(call, call.classification)
        return result


CONSENT = ConsentInterpreter('consent', CLASSIFIER, ReplyClassification())


class DetourInterpreter(ClassifierInterpreter['SalonCall', DetourInput, DetourReading]):
    """Whether the caller kept the request the identity check interrupted, read from everything said since."""

    def _input(self, call: SalonCall) -> DetourInput:
        return DetourInput(
            question=call.detour_speech(), answer=' '.join(call.detour_words()), request=call.detour_request
        )

    def _still_valid(self, call: SalonCall, value: DetourInput) -> bool:
        return value == self._input(call)

    def _settle(self, call: SalonCall, value: DetourInput, result: DetourReading) -> DetourReading:
        if result.request_change == 'kept':
            call.note_request_kept()
        return result


DETOUR_CONTEXT = (
    'A caller is on the phone with Sage and Stone, a hair salon. They asked for something, then the salon '
    'stopped to check who they are. `question` is what the salon said during that check and `answer` is what '
    'the caller said.'
)
"""The context for reading whether the caller kept their request. Prompt text."""

DETOUR_CLASSIFIER = Classifier(
    MODEL, DetourInput, DetourReading, context=DETOUR_CONTEXT, model_settings=SETTINGS
)
DETOUR = DetourInterpreter('detour', DETOUR_CLASSIFIER, DetourReading())
SOLVER = SalonSolver('solver', SOLVER_CLASSIFIER, SalonSolution(decision=NoDecision()))


async def classify_reply(
    call: SalonCall,
    *,
    usage: RunUsage | None = None,
    usage_limits: UsageLimits | None = None,
) -> ReplyClassification:
    """This turn's consent reading, from whichever interpreter is live. At most one request per turn.

    Consumers recheck the reading against the live call through
    `SalonCall.classification`; a late or rebound answer is never published.
    """
    live = SOLVER if INTERPRETER == 'solver' else CONSENT
    await live.interpret(call, usage=usage, usage_limits=usage_limits)
    return call.classification
