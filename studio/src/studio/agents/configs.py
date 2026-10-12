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

from studio.runs.store import ConfigRow


class AgentConfig:
    """One agent's config files on disk: read, versioned, and kept as a row the store holds."""

    TEXT = frozenset({'.yaml', '.yml', '.md', '.json', '.txt', '.hbs'})

    def __init__(self, manifest: Path) -> None:
        self.manifest = manifest

    def files(self) -> dict[str, str]:
        """The config's files, by path relative to the manifest's folder: the manifest first, then the rest in order."""
        folder = self.manifest.parent
        others = {
            str(path.relative_to(folder)): path.read_text()
            for path in sorted(folder.rglob('*'))
            if path.is_file()
            and path.suffix in self.TEXT
            and '__pycache__' not in path.parts
            and path != self.manifest
        }
        return {self.manifest.name: self.manifest.read_text()} | others

    @staticmethod
    def version_of(files: dict[str, str]) -> str:
        """The version these files make: the same files, the same version."""
        digest = hashlib.sha256()
        for path in sorted(files):
            digest.update(path.encode() + b'\0' + files[path].encode() + b'\0')
        return digest.hexdigest()[:12]

    def version(self) -> str:
        """The version of the files on disk now."""
        return self.version_of(self.files())

    def row(self, agent: str, root: Path, parent: str | None = None) -> ConfigRow:
        """The config on disk now, as a row the store keeps."""
        files = self.files()
        shown = self.manifest.relative_to(root) if self.manifest.is_relative_to(root) else self.manifest
        return ConfigRow(
            agent=agent,
            version=self.version_of(files),
            path=str(shown),
            manifest=files[self.manifest.name],
            files={path: text for path, text in files.items() if path != self.manifest.name},
            parent=parent,
        )
