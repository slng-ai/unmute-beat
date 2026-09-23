"""What each collect step accepts, and the whole request as the operations receive it.

Every rule is a validator: a system must belong to the team, a level to the
system, the end must follow the start. Validators read the facts collected so
far from the validation context, so a rule can depend on an earlier answer.
"""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from examples.access.backend import CATALOG, DIRECTORY


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


class Request(BaseModel):
    """The whole request, as the eligibility check and the submission receive it."""

    employee_id: str
    team: str
    system: str
    level: str
    reason: str
    start: date
    end: date
