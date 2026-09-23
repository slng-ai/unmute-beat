"""The access request as steps: who you are, what you need, why and when, a policy check, your yes, the ticket.

Every rule lives on a Pydantic model or an operation: a system must belong
to the team, a level to the system, the end must follow the start. The steps
only say in which order they are asked. Changing the team forgets the
system, which forgets the level, which forgets the eligibility check.
"""

from __future__ import annotations

from datetime import date, timedelta
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator
from pydantic_ai.models import infer_model

from core.operations import OutcomeUnknownError
from core.proposals import BaseProposal
from core.steps import (
    ActionStep,
    CollectStep,
    ConfirmedOperation,
    ConfirmStep,
    End,
    FlowCall,
    StepFlow,
    confirm_interpreter,
)
from examples.access.backend import CATALOG, DIRECTORY

NAME = 'access request'
MODEL = infer_model('google-cloud:gemini-3.5-flash-lite')


def _facts(info: ValidationInfo) -> dict:
    return (info.context or {}).get('facts', {})


class Employee(BaseModel):
    employee_id: str = Field(pattern=r'^E-\d{3}$', description='Their employee ID, like E-100.')


class Team(BaseModel):
    team: str = Field(description='Their team: engineering or finance.')

    @field_validator('team')
    @classmethod
    def supported(cls, team: str) -> str:
        team = team.strip().lower()
        if team not in CATALOG:
            raise ValueError(f'{team!r} is not a team we handle; choose {", ".join(CATALOG)}')
        return team


class System(BaseModel):
    system: str = Field(description='The system they need access to, e.g. github, aws or ledger.')

    @field_validator('system')
    @classmethod
    def belongs_to_team(cls, system: str, info: ValidationInfo) -> str:
        system, team = system.strip().lower(), _facts(info).get('team', '')
        if system not in CATALOG.get(team, {}):
            raise ValueError(f'{system!r} is not available to {team or "that"} team')
        return system


class Level(BaseModel):
    level: str = Field(description='The access level: read, write or admin.')

    @field_validator('level')
    @classmethod
    def offered_for_system(cls, level: str, info: ValidationInfo) -> str:
        facts = _facts(info)
        level = level.strip().lower()
        if level not in CATALOG.get(facts.get('team', ''), {}).get(facts.get('system', ''), ()):
            raise ValueError(f'{level!r} is not a level offered on {facts.get("system") or "that system"}')
        return level


class Reason(BaseModel):
    reason: str = Field(min_length=5, description='Why they need it, in their own words.')


class Start(BaseModel):
    start: date = Field(description='The first day they need access, as a date.')

    @field_validator('start')
    @classmethod
    def not_in_the_past(cls, start: date) -> date:
        if start < DIRECTORY.today:
            raise ValueError('the start date is in the past')
        return start


class Finish(BaseModel):
    end: date = Field(description='The last day they need access, as a date.')

    @field_validator('end')
    @classmethod
    def after_start(cls, end: date, info: ValidationInfo) -> date:
        start = _facts(info).get('start')
        if start and end <= date.fromisoformat(str(start)):
            raise ValueError('the end date must be after the start date')
        if start and end - date.fromisoformat(str(start)) > timedelta(days=90):
            raise ValueError('access can be requested for at most 90 days')
        return end


class Lookup(BaseModel):
    employee_id: str


class Identified(BaseModel):
    employee_id: str
    name: str
    match: bool


class Request(BaseModel):
    employee_id: str
    team: str
    system: str
    level: str
    reason: str
    start: date
    end: date


class Eligibility(BaseModel):
    eligible: bool
    reason: str


class Submitted(BaseModel):
    request_id: str
    status: Literal['submitted']
    """Filed, not approved: the receipt says only what happened."""


class LookupEmployee(ConfirmedOperation[Lookup, Identified]):
    """Identify the requester. Reading a directory needs no approval."""

    name = 'identify'
    args_type = Lookup
    receipt_type = Identified
    needs_approval = False

    async def _execute(self, call: FlowCall, args: Lookup, grant: None) -> dict:
        return DIRECTORY.lookup(args.employee_id)


class CheckEligibility(ConfirmedOperation[Request, Eligibility]):
    """Ask the policy whether this exact request is allowed. A check, not a change."""

    name = 'eligibility'
    args_type = Request
    receipt_type = Eligibility
    needs_approval = False

    async def _execute(self, call: FlowCall, args: Request, grant: None) -> dict:
        return DIRECTORY.check(args.team, args.system, args.level, args.start, args.end)


class SubmitRequest(ConfirmedOperation[Request, Submitted]):
    """File the request, once the caller agreed to this exact readback of it."""

    name = 'submit'
    args_type = Request
    receipt_type = Submitted
    proposal_about = 'submit'

    def proposal(self, args: Request) -> BaseProposal:
        digest = sha256(args.model_dump_json().encode()).hexdigest()[:12]
        return BaseProposal(
            about='submit',
            value=digest,
            spoken=(
                f'To confirm: {args.level} access to {args.system} for the {args.team} team, '
                f'from {args.start:%d %B} to {args.end:%d %B}, because {args.reason}. Shall I submit it?'
            ),
        )

    async def _execute(self, call: FlowCall, args: Request, grant: object) -> dict:
        try:
            return DIRECTORY.submit(args.model_dump(mode='json'))
        except TimeoutError as lost:
            raise OutcomeUnknownError(str(lost)) from None


def build(model=MODEL) -> StepFlow:
    """The flow, for a model. Tests build their own with a scripted one."""
    interpreter = confirm_interpreter(model)
    submit = SubmitRequest(interpreter)
    steps = [
        CollectStep(id='employee', fields=Employee, question='What is your employee ID?'),
        ActionStep(
            id='identify',
            operation=LookupEmployee(interpreter),
            accepts=lambda r: r['match'],
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
            operation=CheckEligibility(interpreter),
            accepts=lambda r: r['eligible'],
            on_error='refused',
        ),
        ConfirmStep(id='confirm', operation=submit, then='submit', on_reject='cancelled', on_revise='level'),
        ActionStep(id='submit', operation=submit, on_error='failed', on_unknown='unconfirmed'),
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


FLOW = build()
AGENTS = FLOW.nodes
GREETING = 'Hi, I can help you request access to a system. What is your employee ID?'


def open_call(caller_id: str = '') -> FlowCall:
    """A fresh request, on its first step. The caller ID is not used: identity is the employee ID."""
    return FLOW.open_call()
