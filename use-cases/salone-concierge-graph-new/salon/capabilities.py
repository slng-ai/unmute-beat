"""The refund policy, loaded on demand: most calls never ask about money back, so its words stay out of the prompt.

A native Pydantic AI capability (`defer_loading=True`): each step that lists
it (`capabilities:` in `agent.yaml`) shows the model one line of catalog,
and the model loads the policy only for a question about refunds. Loaded
once, it stays loaded for the rest of the call (`core.history.conversation`).
"""

from pathlib import Path

from pydantic_ai.capabilities import Capability

refunds = Capability(
    id='refunds',
    description=(
        'The salon refund and compensation policy. Load it only when the caller asks about a refund, '
        'money back or compensation.'
    ),
    instructions=(Path(__file__).parent / 'prompts' / 'knowledge' / 'refunds.md').read_text(),
    defer_loading=True,
)
