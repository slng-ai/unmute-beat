"""What Kestrel asks next, and which car the claim is for: one choice function, `next_question`.

While a fact is missing it returns only words, the next question, in a fixed
order. Core says them as the step's next line and takes them away once said,
so the function is asked again after the next `record`. Once every fact is
in, it offers the cars on the policy: with one car it picks it, and code
reads the claim back at once.
"""

from pydantic import BaseModel

from kestrel.operations import Kestrel
from kestrel.state import ClaimState

QUESTIONS = (
    ('incident_day', 'I can take your claim now. On which day did it happen?'),
    ('incident_time', 'Roughly what time was it?'),
    ('place', 'Where did it happen?'),
    ('what_happened', 'In your own words, what happened?'),
    ('injuries', 'Was anyone hurt?'),
    ('police_report', 'Was it reported to the police? If so, do you have a reference?'),
    ('other_party', 'Was another vehicle or driver involved?'),
    ('vehicle_drivable', 'Can your car still be driven?'),
)
"""The incident's facts in the order they are asked, each with code's own question."""
SAFETY = "I'm sorry to hear that. If anyone needs medical help, please call the emergency services first."


class Ask(BaseModel):
    """The cars on offer by plate, what the agent should know, code's line, and the one car when there is one."""

    options: dict[str, str] = {}
    note: str = ''
    say: str = ''
    pick: str = ''


def next_question(kestrel: Kestrel, state: ClaimState) -> Ask:
    """The next question for a missing fact, then the car the claim is for."""
    missing = next(((name, ask) for name, ask in QUESTIONS if getattr(state, name) is None), None)
    if missing is not None:
        name, ask = missing
        if state.injuries and not kestrel.safety_said:
            kestrel.safety_said = True
            ask = f'{SAFETY} {ask}'
        return Ask(note=f'Still missing: {name}. Code asked for it.', say=ask)
    cars = {v.plate: v.words for v in (state.policy.vehicles if state.policy else [])}
    if len(cars) == 1:
        return Ask(options=cars, pick=next(iter(cars)))
    listed = ' or '.join(cars.values())
    return Ask(
        options=cars, note='Record the plate of the car they name.', say=f'Which car was it: {listed}?'
    )
