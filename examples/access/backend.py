"""The access service, in memory: who works here, what they may ask for, and the requests filed.

ponytail: a dictionary standing in for an identity provider, a policy engine
and a ticketing system. A real deployment replaces each method with a
connector; the operations in `flow.py` validate whatever comes back either way.
"""

from __future__ import annotations

from datetime import date, timedelta

EMPLOYEES = {'E-100': 'Ada Lovelace', 'E-200': 'Alan Turing'}
CATALOG: dict[str, dict[str, tuple[str, ...]]] = {
    'engineering': {'github': ('read', 'write', 'admin'), 'aws': ('read', 'admin')},
    'finance': {'ledger': ('read', 'write')},
}
"""Which systems each team may request, and at which levels."""
ADMIN_DAYS = 30
"""The longest admin access the policy allows."""


class Directory:
    """One organisation's access desk."""

    def __init__(self, today: date = date(2026, 9, 21)) -> None:
        self.today = today
        self.requests: list[dict] = []
        self.lose_next_answer = False
        """Set to make the next submission's answer go missing, as a timeout would."""

    def lookup(self, employee_id: str) -> dict:
        name = EMPLOYEES.get(employee_id, '')
        return {'employee_id': employee_id, 'name': name, 'match': bool(name)}

    def check(self, team: str, system: str, level: str, start: date, end: date) -> dict:
        if level not in CATALOG.get(team, {}).get(system, ()):
            return {'eligible': False, 'reason': f'{team} cannot request {level} on {system}'}
        if level == 'admin' and end - start > timedelta(days=ADMIN_DAYS):
            return {'eligible': False, 'reason': f'admin access is limited to {ADMIN_DAYS} days'}
        return {'eligible': True, 'reason': ''}

    def submit(self, request: dict) -> dict:
        receipt = {'request_id': f'AR-{len(self.requests) + 1:04d}', 'status': 'submitted'}
        self.requests.append(request | receipt)
        if self.lose_next_answer:
            self.lose_next_answer = False
            raise TimeoutError('the ticketing system did not answer')
        return receipt


DIRECTORY = Directory()
"""One desk per process."""
