"""What every salon step can do: core's `Desk`, given the salon's call, hand-over, actions and carried booking.

End the call on the caller's own goodbye, hand the caller on, clarify, or
switch task. The tools and their docstrings are core's (`core.desk`); the
salon adds the booking a front-desk hand-over carries to `booking`, and the
one thing that blocks a switch: a write whose outcome is unknown.
"""

from __future__ import annotations

from core.desk import Carry, Desk
from salon.models.state import BookingRequest, Handoff, SalonCall
from salon.tools.policy import ACTIONS

_BOOKING_ARG = (
    '    booking: Only with to="booking", for a new appointment the caller\n'
    '        has asked for: the service, their own day words, and their\n'
    '        time words if they gave any. Leave it out if they named no\n'
    '        service or no day.'
)
"""The `booking` argument's description, appended to `hand_over`'s Args only where the argument exists."""


def _write_outstanding(call: SalonCall) -> str:
    if call.state.unresolved is not None:
        return 'Nothing was switched: a booking change is still waiting on the diary. Resolve that first.'
    return ''


DESK = Desk[SalonCall](
    call_type=SalonCall,
    handoff=Handoff,
    actions=ACTIONS,
    carry=Carry(to='booking', name='booking', type=BookingRequest, doc=_BOOKING_ARG, field='wanted'),
    blocked=_write_outstanding,
)

hang_up = DESK.hang_up
end_conversation = DESK.end_conversation
switch_task = DESK.switch_task
clarify = DESK.clarify
hand_over_for = DESK.hand_over_for
