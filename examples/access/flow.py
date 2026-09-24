"""The access request, loaded from its folder, and the same flow written in Python for comparison.

`flow.yaml` is the flow. Each step names its own prompt in `prompts/` or its
own module in `steps/`; `voice.md` is added to the top of every prompt.
`build()` is the same flow written as Python steps, kept so a test can show
the two behave alike.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_ai.models import infer_model

from core.flowspec import load_file
from core.steps import ActionStep, CollectStep, ConfirmStep, End, FlowCall, StepFlow, confirm_interpreter
from examples.access.steps import eligibility, identify, submit
from examples.access.steps.fields import Employee, Finish, Level, Reason, Start, System, Team

NAME = 'access request'
MODEL = infer_model('google-cloud:gemini-3.5-flash-lite')
SPEC = load_file(Path(__file__).parent / 'flow.yaml')


def from_file(model=MODEL) -> StepFlow:
    """The flow compiled from its folder. Tests pass a scripted model."""
    return SPEC.compile(model=model)


def build(model=MODEL) -> StepFlow:
    """The same flow as Python steps, with prompts written from the fields instead of read from files."""
    interpreter = confirm_interpreter(model)
    submitting = submit.OPERATION(interpreter)
    steps = [
        CollectStep(id='employee', fields=Employee, question='What is your employee ID?'),
        ActionStep(
            id='identify',
            operation=identify.OPERATION(interpreter),
            accepts=identify.accepts,
            on_error='stranger',
        ),
        CollectStep(id='team', fields=Team, question='Which team are you on?'),
        CollectStep(
            id='system', fields=System, question='Which system do you need?', after=frozenset({'team'})
        ),
        CollectStep(
            id='level', fields=Level, question='What access level do you need?', after=frozenset({'system'})
        ),
        CollectStep(id='reason', fields=Reason, question='What do you need it for?'),
        CollectStep(id='start', fields=Start, question='From what date?'),
        CollectStep(id='end', fields=Finish, question='Until what date?', after=frozenset({'start'})),
        ActionStep(
            id='eligibility',
            operation=eligibility.OPERATION(interpreter),
            accepts=eligibility.accepts,
            on_error='refused',
        ),
        ConfirmStep(
            id='confirm', operation=submitting, then='submit', on_reject='cancelled', on_revise='level'
        ),
        ActionStep(id='submit', operation=submitting, on_error='failed', on_unknown='unconfirmed'),
        End(
            id='submitted',
            outcome='completed',
            say='Done: request {submit[request_id]} is submitted. It still needs approval; you will get an email.',
        ),
        End(id='cancelled', outcome='cancelled', say="Okay, I haven't submitted anything."),
        End(
            id='refused',
            outcome='failed',
            say='The access policy does not allow that request, so nothing was filed.',
        ),
        End(id='stranger', outcome='failed', say="I can't find that employee ID, so I can't file a request."),
        End(
            id='failed',
            outcome='failed',
            say='The ticketing system refused the request, so nothing was filed.',
        ),
        End(
            id='unconfirmed',
            outcome='pending',
            say="I couldn't confirm whether it went through. Please don't submit it again; we'll email you.",
        ),
    ]
    return StepFlow(steps, model=model, interpreter=interpreter)


FLOW = from_file()
AGENTS = FLOW.nodes
GREETING = SPEC.greeting


def open_call(caller_id: str = '') -> FlowCall:
    """A fresh request, on its first step. The caller ID is not used: identity is the employee ID."""
    return FLOW.open_call()
