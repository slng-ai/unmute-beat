"""The config editor: an agent's manifest and prompts as the editor shows them, checked before anything is saved.

What can be changed, and how each change reaches the files:

    a prompt              its file, rewritten whole
    a closing line        the operation's `say:` (said after it runs, filled from its receipt; or lines by
                          result, each case's `when` naming receipt values or `refused`) and `recap:`
                          (what a goodbye says of it later). A long-form operation's keys are set in
                          place; a class operation becomes `{class: file.py:Class, say: ...}`, so the
                          line is config and the class's code is never touched (`core.flowspec`)
    an action step        `then`, `on_error`, `on_unknown`, `returns`
    the whole manifest    replaced as typed, on the Full config tab

Edits to `agent.yaml` are made line by line and proved (`studio.agents.manifest_text`). Before a save,
the edited folder is checked in a process of its own, on a copy (`studio.agents.check`): the loader's
problems, then each closing line against its receipt (`studio.agents.closing_review`). A save writes
only what changed, and the version it makes names the version it was edited from.
"""

import shutil
import tempfile
from pathlib import Path
from typing import Any, Literal

import yaml

import core
from core.flowspec import ActionSpec, ConversationSpec
from studio.agents.catalog import AgentCheckProcess, CheckResult
from studio.agents.closing_review import ClosingReview
from studio.agents.configs import AgentConfig
from studio.agents.manifest_text import EditRefusedError, ManifestText
from studio.agents.models import Checked, ClosingEdit, Config, Edit, Operation, Problem, Saved, Step


class ConfigEditor:
    """One agent's config: read for the editor, an edit checked on a copy, and a checked edit saved."""

    def __init__(self, manifest: Path, agent: str, root: Path) -> None:
        self.manifest = manifest
        self.agent = agent
        self.root = root
        self.config = AgentConfig(manifest)

    def read(self) -> Config:
        """The config as the editor opens it, with what the loader and the closing-line review find."""
        files = self.config.files()
        found = AgentCheckProcess(self.manifest).result()
        operations = self.operations(self.manifest, found)
        shown = (
            self.manifest.relative_to(self.root) if self.manifest.is_relative_to(self.root) else self.manifest
        )
        return Config(
            agent=self.agent,
            path=str(shown),
            version=AgentConfig.version_of(files),
            manifest=files[self.manifest.name],
            files={path: text for path, text in files.items() if path != self.manifest.name},
            steps=self.steps(),
            operations=operations,
            problems=self._loader_problems(found) + self._closing_problems(operations),
        )

    def check(self, edit: Edit) -> Checked:
        """The edit applied to a copy of the agent's folder and checked, as a save would. Nothing on disk changes."""
        current = self.config.files()
        try:
            edited = self.apply(current, edit)
        except EditRefusedError as error:
            return Checked(ok=False, version='', problems=[Problem(where='edit', what=str(error))])
        stale = edit.base and edit.base != AgentConfig.version_of(current)
        problems = (
            [
                Problem(
                    where='files',
                    what='the files changed on disk since this editor opened them: reload before saving',
                )
            ]
            if stale
            else []
        )
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / self.manifest.parent.name
            shutil.copytree(self.manifest.parent, copy, ignore=shutil.ignore_patterns('__pycache__'))
            for path, text in edited.items():
                (copy / path).parent.mkdir(parents=True, exist_ok=True)
                (copy / path).write_text(text)
            found = AgentCheckProcess(copy / self.manifest.name, keep=False).result()
            operations = self.operations(copy / self.manifest.name, found)
        problems += self._loader_problems(found) + self._closing_problems(operations)
        return Checked(
            ok=not any(problem.level == 'error' for problem in problems),
            version=AgentConfig.version_of(edited),
            problems=problems,
            operations=operations,
            changed=sorted(path for path, text in edited.items() if current.get(path) != text),
        )

    def save(self, edit: Edit) -> Saved:
        """Check, then write the files the edit changes. Nothing is written when any check fails."""
        checked = self.check(edit)
        if not checked.ok:
            return Saved(**checked.model_dump())
        current = self.config.files()
        edited = self.apply(current, edit)
        folder = self.manifest.parent.resolve()
        for path in checked.changed:
            target = self.manifest.parent / path
            if target.resolve().is_relative_to(folder):
                target.parent.mkdir(parents=True, exist_ok=True)
                staged = target.with_name(f'.{target.name}.saving')
                staged.write_text(edited[path])
                staged.replace(target)
        row = self.config.row(self.agent, self.root, parent=AgentConfig.version_of(current))
        return Saved(**checked.model_dump(), saved=True, config=row)

    def apply(self, current: dict[str, str], edit: Edit) -> dict[str, str]:
        """The files after this edit, by path; a path outside the folder, or an edit that cannot be proved, is refused."""
        name = self.manifest.name
        outside = [
            path
            for path in edit.files
            if path == name or Path(path).is_absolute() or '..' in Path(path).parts
        ]
        if outside:
            raise EditRefusedError(
                f"{outside[0]}: only a file inside the agent's folder, other than the manifest, can be edited here"
            )
        edited = current | edit.files
        if edit.manifest is not None:
            return edited | {name: edit.manifest}
        text = current[name]
        for operation, closing in edit.closing.items():
            text = self.with_closing(text, operation, closing)
        for step, routes in edit.actions.items():
            text = ManifestText(text).with_keys('steps', step, routes.model_dump(exclude_none=True))
        return edited | {name: text}

    @staticmethod
    def with_closing(text: str, operation: str, closing: ClosingEdit) -> str:
        """The manifest with this operation's closing lines set; '' removes one. A one-line class gets its override form."""
        changes = {
            key: [case.model_dump(exclude_none=True) for case in value] if isinstance(value, list) else value
            for key in ('say', 'recap')
            if (value := getattr(closing, key)) is not None
        }
        return ManifestText(text).with_keys(
            'operations', operation, {key: value or None for key, value in changes.items()}, one_line='class'
        )

    @staticmethod
    def operations(manifest: Path, found: CheckResult) -> list[Operation]:
        """Each operation the check reported, with where its closing line lives."""
        try:
            declared = (yaml.safe_load(manifest.read_text()) or {}).get('operations') or {}
        except yaml.YAMLError:
            declared = {}
        return [
            Operation(**reported, form=ConfigEditor._form(declared.get(reported['name'])))
            for reported in found.operations
        ]

    @staticmethod
    def _form(declared: Any) -> Literal['class', 'function']:
        """`function` for a long-form operation, `class` for a class, overridden or not."""
        return 'function' if isinstance(declared, dict) and 'function' in declared else 'class'

    def steps(self) -> list[Step]:
        """Every step as the editor shows it; none when the manifest does not load."""
        try:
            spec = core.load(self.manifest)
        except Exception:
            return []
        return [self._step(step) for step in spec.steps]

    @staticmethod
    def _step(step: Any) -> Step:
        """One step: a conversation's prompt and operations, an action's operation and routes, or an end."""
        if isinstance(step, ConversationSpec):
            return Step(
                id=step.id,
                kind='conversation',
                prompt=step.prompt,
                agent=step.agent,
                fields=list(step.fields),
                operations=list(step.operations),
            )
        if isinstance(step, ActionSpec):
            return Step(
                id=step.id,
                kind='action',
                operation=step.uses,
                then=step.then or '',
                on_error=step.on_error or '',
                on_unknown=step.on_unknown or '',
                returns=bool(step.returns),
            )
        return Step(id=step.id, kind='end')

    @staticmethod
    def _loader_problems(found: CheckResult) -> list[Problem]:
        """The loader's problems, as the editor lists them."""
        return [Problem(where='manifest', what=problem) for problem in found.problems]

    @staticmethod
    def _closing_problems(operations: list[Operation]) -> list[Problem]:
        """Every operation's closing lines, reviewed against its receipt."""
        return [problem for operation in operations for problem in ClosingReview(operation).problems()]
