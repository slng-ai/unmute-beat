"""The library's help line: one conversation step, no interpreter, no business actions.

Each answer finishes the turn and the step stays where it is for the next
question. Nothing is written anywhere, so nothing needs a readback or a
consent check; a business action added later would bring its own.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_ai.models import infer_model

from core.flowspec import Registry, load_file
from core.steps import ConversationStep, FlowCall, StepFlow

NAME = 'library help'
MODEL = infer_model('google-cloud:gemini-3.5-flash-lite')
KNOWLEDGE = (Path(__file__).parent / 'knowledge.md').read_text()


def build(model=MODEL) -> StepFlow:
    """The flow, for a model. Tests build their own with a scripted one."""
    return StepFlow(
        [
            ConversationStep(
                id='answer',
                instructions=(
                    'You answer questions about Harbour Library on the phone, in one or two short sentences, '
                    'only from the knowledge below. If it does not say, say you do not know and suggest the front desk.'
                ),
                knowledge=KNOWLEDGE,
            )
        ],
        model=model,
    )


REGISTRY = Registry(knowledge={'library': KNOWLEDGE})
SPEC = load_file(Path(__file__).parent / 'flow.yaml')


def from_file(model=MODEL) -> StepFlow:
    """The same flow, compiled from `flow.yaml`."""
    return SPEC.compile(REGISTRY, model=model)


FLOW = build()
AGENTS = FLOW.nodes
GREETING = 'Hello, Harbour Library. What can I help you with?'


def open_call(caller_id: str = '') -> FlowCall:
    return FLOW.open_call()
