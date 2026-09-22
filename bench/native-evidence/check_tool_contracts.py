"""Capture current POC request tools and native declarations without model requests.

Run from the repository root with the POC interpreter. The existing native
configuration probe supplies dummy credentials and disables client construction.
This inventory can disprove tool parity; it does not prove handler equivalence.
"""

import ast
import asyncio
import hashlib
import json
import runpy
import sys
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


async def capture() -> dict:
    """Observe prepared POC schemas, then inspect the compiled native declarations."""
    native_config = runpy.run_path(str(Path(__file__).with_name('check_config.py')))
    bot, build = native_config['bot'], native_config['BUILD']

    import pydantic_ai.models
    from pipecat.adapters.schemas.tools_schema import ToolsSchema
    from pydantic_ai.models.function import FunctionModel

    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    from salon.agents import AGENTS
    from salon.state import Asked, SalonCall, SalonState

    class CapturedError(Exception):
        """Stop before executing any tool or service."""

    poc: dict[str, dict] = {}
    for name, verified, approved in (
        ('concierge', False, False),
        ('concierge', True, False),
        ('verify', False, False),
        ('verify', True, False),
        ('booking', True, False),
        ('complaints', True, False),
        ('complaints', True, True),
    ):
        call = SalonCall(
            state=SalonState(
                agent=name,
                customer_phone='+34600111222',
                customer_verified='existing' if verified else '',
                verified_phone='+34600111222' if verified else '',
            )
        )
        if approved:
            call.ask(
                Asked(
                    about='complaint',
                    summary='My haircut was uneven.',
                    requested_resolution='I want a refund.',
                    owner=call.state.customer_phone,
                )
            )
        call.turn_started('Yes, that is right.')
        assert call.complaint_approved == approved
        key = f'{name}:verified={verified}:complaint_approved={approved}'

        def observe(messages, info, key=key):
            poc[key] = {
                'allows_text': info.allow_text_output,
                'tools': [
                    {
                        'name': tool.name,
                        'description': tool.description,
                        'parameters': tool.parameters_json_schema,
                        'kind': kind,
                    }
                    for kind, definitions in (
                        ('function', info.function_tools),
                        ('output', info.output_tools),
                    )
                    for tool in definitions
                ],
            }
            raise CapturedError

        node = AGENTS[name]
        with node.agent.override(model=FunctionModel(function=observe)), suppress(CapturedError):
            await node.agent.run('Inspect tools only.', deps=call, output_type=node.outputs(call.state))
        assert key in poc

    native: dict[str, list[dict]] = {}
    for cls in (bot.ConciergeAgent, bot.ComplaintSpecialistAgent):
        worker = cls.__new__(cls)
        native[cls.__name__] = [
            tool.to_default_dict() for tool in ToolsSchema(standard_tools=worker.build_tools()).standard_tools
        ]

    tree = ast.parse((build / 'bot.py').read_text())
    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        for method in (node for node in cls.body if isinstance(node, ast.AsyncFunctionDef)):
            definitions = []
            for node in ast.walk(method):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                if node.func.id != 'FlowsFunctionSchema':
                    continue
                fields = {}
                for keyword in node.keywords:
                    if keyword.arg in ('name', 'description', 'properties', 'required'):
                        expression = ast.Expression(body=keyword.value)
                        fields[keyword.arg] = eval(
                            compile(expression, str(build / 'bot.py'), 'eval'), vars(bot)
                        )
                definitions.append(
                    {
                        'name': fields['name'],
                        'description': fields['description'],
                        'parameters': {
                            'type': 'object',
                            'properties': fields['properties'],
                            'required': fields['required'],
                        },
                    }
                )
            if definitions:
                native[f'{cls.name}.{method.name}'] = definitions

    names_poc = {t['name'] for row in poc.values() for t in row['tools']}
    names_native = {t['name'] for tools in native.values() for t in tools}
    sources = [
        ROOT / 'salon/agents.py',
        ROOT / 'salon/state.py',
        ROOT / 'bench/native_tools.py',
        build / 'bot.py',
    ]
    return {
        'scope': 'POC prepared first requests in seven states; native direct schemas and declared flow schemas. No tool handlers, model requests or audio executed.',
        'hashes': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        'only_poc': sorted(names_poc - names_native),
        'only_native': sorted(names_native - names_poc),
        'shared_names': sorted(names_poc & names_native),
        'poc': poc,
        'native': native,
    }


if __name__ == '__main__':
    result = asyncio.run(capture())
    destination = Path(__file__).with_name('tool-contracts.json')
    destination.write_text(json.dumps(result, indent=2) + '\n')
    for key in ('only_poc', 'only_native', 'shared_names'):
        print(key, result[key])
    print('Inventory:', destination)
