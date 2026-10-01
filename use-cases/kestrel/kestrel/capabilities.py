"""Kestrel's coverage guide, loaded on demand: most claim calls never ask what the policy covers.

A native Pydantic AI capability (`defer_loading=True`): the model sees one
catalog line and loads the text only when the caller asks.
"""

from pathlib import Path

from pydantic_ai.capabilities import Capability

coverage = Capability(
    id='coverage',
    description=(
        'What a policy covers, the excess, a courtesy car, recovery, and what happens after a claim. Load it '
        'only when the caller asks about one of these.'
    ),
    instructions=(Path(__file__).parent / 'prompts' / 'knowledge' / 'coverage.md').read_text(),
    defer_loading=True,
)
