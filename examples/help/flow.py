"""The library's help line: one conversation step, no interpreter, no business actions.

Each answer finishes the turn and the step stays where it is for the next
question. Nothing is written anywhere, so nothing needs a readback or a
consent check; a business action added later would bring its own. The flow is
`flow.yaml`; `build()` is the same step in Python, for comparison.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_ai.models import infer_model

from core.flowspec import load_file
from core.steps import ConversationStep, FlowCall, StepFlow

NAME = 'library help'
MODEL = infer_model('google-cloud:gemini-3.5-flash-lite')
FOLDER = Path(__file__).parent
SPEC = load_file(FOLDER / 'flow.yaml')


def from_file(model=MODEL) -> StepFlow:
    """The flow compiled from its folder. Tests pass a scripted model."""
    return SPEC.compile(model=model)


def build(model=MODEL) -> StepFlow:
    """The same step as Python, with the same prompt text."""
    prompt = (FOLDER / 'voice.md').read_text() + '\n\n' + (FOLDER / 'prompts' / 'answer.md').read_text()
    return StepFlow(
        [ConversationStep(id='answer', instructions=prompt, knowledge=(FOLDER / 'knowledge.md').read_text())],
        model=model,
    )


FLOW = from_file()
AGENTS = FLOW.nodes
GREETING = SPEC.greeting


def open_call(caller_id: str = '') -> FlowCall:
    return FLOW.open_call()
