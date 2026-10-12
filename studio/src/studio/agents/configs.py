"""An agent's config as files, its version, and every version a run ran on.

An agent's config is its `agent.yaml` and the text files beside it in its package folder (the
prompts, a locale file): everything the manifest can name that is not code. Its version is the
first 12 hex of a sha256 over those files, by path, so the same files always give the same version
and any edit gives a new one. Every run names the version it ran on (`run.started`'s `config`),
and the store keeps each version's files (`agent_configs`), so a run can always be read against
the config it actually had.

The state module (the file `state:` names) is part of a version too: its fields are the agent's facts,
which the Facts tab edits, so a run can always be read against the facts it had. Other code (`*.py`) is
not: an operation's behaviour changed in Python is the application's release, named by `app_version`.

A runner imports an agent's code once and never again, so the studio also needs to know whether a runner
carries the code on disk now: `AgentConfig.code()` is a digest of every module in the package, the same one
a runner reports for each agent when it links (`voice_runner.runner.CodeFiles`).
"""

import hashlib
from pathlib import Path

import yaml

from studio.runs.store import ConfigRow


class AgentConfig:
    """One agent's config files on disk: read, versioned, and kept as a row the store holds."""

    TEXT = frozenset({'.yaml', '.yml', '.md', '.json', '.txt', '.hbs'})

    def __init__(self, manifest: Path) -> None:
        self.manifest = manifest

    def files(self) -> dict[str, str]:
        """The config's files, by path relative to the manifest's folder: the manifest first, the text files, the state module."""
        folder = self.manifest.parent
        others = {
            str(path.relative_to(folder)): path.read_text()
            for path in sorted(folder.rglob('*'))
            if path.is_file()
            and path.suffix in self.TEXT
            and '__pycache__' not in path.parts
            and path != self.manifest
        }
        state = self.state_file()
        code = {str(state.relative_to(folder.resolve())): state.read_text()} if state is not None else {}
        return {self.manifest.name: self.manifest.read_text()} | others | code

    def state_file(self) -> Path | None:
        """The module that defines the state (`state: state.py:SalonState`), when it is inside the agent's folder."""
        try:
            data = yaml.safe_load(self.manifest.read_text()) or {}
        except yaml.YAMLError, OSError:
            return None
        named = data.get('state') if isinstance(data, dict) else None
        if not isinstance(named, str) or ':' not in named:
            return None
        path = (self.manifest.parent / named.partition(':')[0]).resolve()
        folder = self.manifest.parent.resolve()
        return path if path.is_file() and path.is_relative_to(folder) and path.suffix == '.py' else None

    def code(self) -> str:
        """A digest of every module in the agent's package, as a runner that imported it reports it."""
        folder = self.manifest.parent
        digest = hashlib.sha256()
        for path in sorted(folder.rglob('*.py')):
            if '__pycache__' not in path.parts:
                digest.update(str(path.relative_to(folder)).encode() + b'\0' + path.read_bytes() + b'\0')
        return digest.hexdigest()[:12]

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
