"""An agent's config as files, its version, and every version a run ran on.

An agent's config is its `agent.yaml` and the text files beside it in its package folder (the
prompts, a locale file): everything the manifest can name that is not code. Its version is the
first 12 hex of a sha256 over those files, by path, so the same files always give the same version
and any edit gives a new one. Every run names the version it ran on (`run.started`'s `config`),
and the store keeps each version's files (`agent_configs`), so a run can always be read against
the config it actually had.

ponytail: code (`*.py`) is not part of a version: an operation's behaviour changed in Python is the
application's release, named by `app_version`. Hash the package's code too if runs ever need to
tell two builds of the same manifest apart.
"""

import hashlib
from pathlib import Path

from studio.store import ConfigRow

TEXT = {'.yaml', '.yml', '.md', '.json', '.txt', '.hbs'}
"""The file kinds a manifest can name besides itself: prompts, includes, locales."""


def files_of(manifest: Path) -> dict[str, str]:
    """The config's files, by path relative to the manifest's folder: the manifest first, then the rest in order."""
    folder = manifest.parent
    found = {manifest.name: manifest.read_text()}
    for path in sorted(folder.rglob('*')):
        if path.is_file() and path.suffix in TEXT and '__pycache__' not in path.parts and path != manifest:
            found[str(path.relative_to(folder))] = path.read_text()
    return found


def version(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode() + b'\0' + files[path].encode() + b'\0')
    return digest.hexdigest()[:12]


def current(agent: str, manifest: Path, root: Path, parent: str | None = None) -> ConfigRow:
    """The config on disk now, as a row the store keeps."""
    files = files_of(manifest)
    shown = str(manifest.relative_to(root)) if manifest.is_relative_to(root) else str(manifest)
    return ConfigRow(
        agent=agent,
        version=version(files),
        path=shown,
        manifest=files[manifest.name],
        files={k: v for k, v in files.items() if k != manifest.name},
        parent=parent,
    )
