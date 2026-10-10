"""`python -m studio.check path/to/agent.yaml`: every reason the flow cannot run, as a JSON list.

`FlowSpec.problems()` imports the application's state and operation modules to check them, so it
runs here, in a process of its own: an application's imports never land in the engine, and one
that hangs or crashes takes nothing else down. The manifest's package folder's parent goes on
the path first, as each use case's own folder is its import root (`use-cases/salon` holds `salon`).
"""

import json
import sys
from pathlib import Path

import core


def problems(manifest: Path) -> list[str]:
    sys.path.insert(0, str(manifest.resolve().parent.parent))
    return core.load(manifest).problems()


if __name__ == '__main__':
    print(json.dumps(problems(Path(sys.argv[1]))))
