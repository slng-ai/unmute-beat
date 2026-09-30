"""Compile an application's manifest offline and print each node's legal moves.

    uv run --directory use-cases/<name> python ../../.claude/skills/voice-use-case/scripts/check_flow.py <pkg>/agent.yaml

No model key and no provider request: every agent gets a scripted model that is never called. A wiring
mistake (a missing file, field, operation, exit or fact) fails here with core's own message, naming the step.
Exit 0 when the manifest compiles, 1 when it does not.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault('GOOGLE_API_KEY', 'offline-check-placeholder')
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')


def older_forms(spec) -> list[str]:
    """What in a loaded manifest core accepts but this skill does not write: an operation declared with keys.

    An older version or a `speech:` key never gets this far: core refuses it at load.
    """
    return [
        f'{name}: declared with function: keys: write it as one line, operations.py:Class'
        for name, operation in spec.operations.items()
        if not isinstance(operation, str)
    ]


def main(manifest: Path) -> int:
    """Put the folder that holds the application package on the path, so `from <pkg>.state import ...` resolves."""
    sys.path.insert(0, str(manifest.resolve().parent.parent))
    import pydantic_ai.models
    from pydantic_ai.models.function import FunctionModel

    import core
    from core.flow import Flow

    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    try:
        spec = core.load(manifest)
        flow = spec.compile(model=FunctionModel(lambda messages, info: None))
    except Exception as error:
        print(f'does not compile: {type(error).__name__}: {error}')
        return 1
    broken = older_forms(spec)
    if broken:
        print('compiles, but breaks the version 2 contract:')
        print('\n'.join(f'  {problem}' for problem in broken))
        return 1
    print(f'{spec.id}: compiles, entry {spec.entry or spec.steps[0].id!r}')
    for step, allowed in sorted(Flow.of(flow.nodes).moves.items()):
        print(f'  {step:14} -> {", ".join(sorted(allowed)) or "(stays)"}')
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    raise SystemExit(main(Path(sys.argv[1])))
