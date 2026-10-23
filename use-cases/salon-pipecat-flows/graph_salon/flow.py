"""`flow.yaml` loaded, joined to its handlers, and checked, once, at import.

Three checks, so a broken flow fails when the salon starts and never mid-call:

1. `FlowConfig.from_file` (Pydantic): every node's shape, and every branch
   case names a node.
2. `Flow(config, handlers=graph_salon.handlers)`: every tool the config names
   exists and is a valid direct function.
3. Here: the config's nodes are exactly `NodeName`, and every tool has an
   argument model.

Then `typed` gives each tool its Pydantic schema.
"""

import json
from pathlib import Path
from typing import Any, get_args

from pipecat.flows import Flow, FlowConfig, FlowsFunctionSchema

from graph_salon import handlers
from graph_salon.call import SalonCall
from graph_salon.models import NodeName
from graph_salon.speech import GREETING

HERE = Path(__file__).parents[1]
"""The folder: `flow.yaml` and `prompts/` sit beside the package, as in Pipecat's own YAML examples."""
SALON_PROMPTS = HERE / 'salon' / 'prompts'
"""The salon's own voice rules and documents: copies of the salon's, as salon-prompt keeps them."""


def _read(path: Path) -> str:
    return path.read_text().strip()


def role_message() -> str:
    """Who Robin is and what the salon knows: `{{ role }}`, the system instruction, which holds across nodes.

    The salon's own voice rules, up to their "Actions" section, which speaks
    of core's runtime; this build's own replaces it. Then the salon's
    documents and complaint answers, as core and the baseline send them.
    """
    guidance = json.loads(_read(SALON_PROMPTS / 'knowledge' / 'complaint-guidance.json'))
    return '\n\n'.join(
        [
            _read(SALON_PROMPTS / '_voice.md').split('## Actions')[0].strip(),
            _read(HERE / 'prompts' / 'actions.md'),
            '## Salon information',
            _read(SALON_PROMPTS / 'knowledge' / 'services.md'),
            _read(SALON_PROMPTS / 'knowledge' / 'refunds.md'),
            '## Complaint guidance',
            '\n'.join(f'- {topic}: {line}' for topic, line in guidance.items()),
        ]
    )


def typed(flow: Flow) -> Flow:
    """Give every tool the JSON schema of its Pydantic argument model, in place.

    Pipecat builds a direct function's schema from its type hints and sends
    a `Literal` as a bare description, with no type and no enum. The tools'
    arguments are Pydantic models, so their schemas are used instead: the
    model is sent the same enums and constraints its arguments are then
    validated against.

    ponytail: edits Pipecat's `FlowsFunctionSchema` after `Flow` built it.
    Drop it when Pipecat maps `Literal` to `enum` (the TODO in
    `pipecat/adapters/schemas/direct_function.py`).
    """
    for name in flow.config.nodes:
        for function in flow.node(name).get('functions', []):
            if not isinstance(function, FlowsFunctionSchema):
                raise TypeError(f'{name}: Flow built a tool that is not a FlowsFunctionSchema: {function!r}')
            schema = handlers.ARGS[function.name].model_json_schema()
            function.properties = {
                field: {key: value for key, value in spec.items() if key != 'title'}
                for field, spec in schema.get('properties', {}).items()
            }
            function.required = schema.get('required', [])
    return flow


def load() -> Flow:
    """The salon's flow, checked. Raises on any mismatch between the config, the node names and the handlers."""
    config = FlowConfig.from_file(HERE / 'flow.yaml')
    names = set(get_args(NodeName))
    if set(config.nodes) != names:
        raise RuntimeError(
            f'flow.yaml nodes and NodeName disagree: only in flow.yaml {set(config.nodes) - names}, '
            f'only in NodeName {names - set(config.nodes)}'
        )
    tools = {f.name for node in config.nodes.values() for f in node.functions}
    if missing := tools - set(handlers.ARGS):
        raise RuntimeError(f'flow.yaml names tools with no argument model: {missing}')
    return typed(Flow(config, handlers=handlers))


FLOW = load()
ROLE = role_message()
DIARY_RULES = _read(SALON_PROMPTS / '_diary.md')


def seeded(call: SalonCall) -> dict[str, Any]:
    """`flow_manager.state` before the first node: the call, and every key a placeholder in `flow.yaml` reads."""
    return {
        'call': call,
        'role': ROLE,
        'diary_rules': DIARY_RULES,
        'say': GREETING,
        'question': '',
        'facts': call.facts(),
    }
