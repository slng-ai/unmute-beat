"""Wasla's payment policy, loaded on demand: most calls never ask how to pay or about a plan.

A native Pydantic AI capability (`defer_loading=True`): the model sees one
catalog line and loads the text only when the caller asks. Once loaded, it
stays loaded for the rest of the call.
"""

from pathlib import Path

from pydantic_ai.capabilities import Capability

payment_policy = Capability(
    id='payment_policy',
    description=(
        'How to pay, payment plans, a new due date, hardship, and the support contact. Load it only when the '
        'caller asks about one of these.'
    ),
    instructions=(Path(__file__).parent / 'prompts' / 'knowledge' / 'payment-policy.md').read_text(),
    defer_loading=True,
)
