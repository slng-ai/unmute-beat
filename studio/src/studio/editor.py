"""The config editor: an agent's manifest and prompts as the editor shows them, checked before anything is saved.

What can be changed, and how each change reaches the files:

    a prompt              its file, rewritten whole
    a closing line        the operation's `say:` (said after it works, filled from its receipt) and
                          `recap:` (what a goodbye says of it later). A long-form operation's keys are
                          set in place; a class operation becomes `{class: file.py:Class, say: ...}`, so
                          the line is config and the class's code is never touched (`core.flowspec`)
    an action step        `then`, `on_error`, `on_unknown`, `returns`
    the whole manifest    replaced as typed, on the Full config tab

Edits to `agent.yaml` are made line by line, so its comments and layout stay as they were, and each is
proved: the edited text is parsed again and must equal the original with exactly that change, or the
edit is refused and the Full config tab is the way to make it.

Before a save, the edited folder is checked in a process of its own (`studio.check`, on a copy): the
loader's problems, then each closing line against its operation's receipt. Core does not check a
closing line's placeholders when it loads a manifest, so the studio does: a field the receipt does not
have would be spoken as a gap, and an unclosed brace would fail the line when it is said. A recap on an
operation that changes nothing is the loader's own refusal. A save writes only what changed, and the
version it makes names the version it was edited from.
"""

import copy
import json
import re
import shutil
import string
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel

import core
from core.flowspec import ActionSpec, ConversationSpec
from studio import configs
from studio.store import ConfigRow


class Operation(BaseModel):
    name: str
    say: str = ''
    recap: str = ''
    changes: bool = True
    approval: str = 'readback'
    fields: list[str] = []
    """Every field its receipts can have: what `{field}` in a closing line may name."""
    form: Literal['class', 'function'] = 'class'
    """Where its closing line lives: the manifest (`function`, or a class's override) or its class's code."""
    route: str = ''


class Step(BaseModel):
    id: str
    kind: Literal['conversation', 'action', 'end']
    prompt: str = ''
    agent: str = ''
    fields: list[str] = []
    operations: list[str] = []
    operation: str = ''
    """An action step's one operation: it has no prompt and no model."""
    then: str = ''
    on_error: str = ''
    on_unknown: str = ''
    returns: bool = False


class Problem(BaseModel):
    where: str
    what: str
    level: Literal['error', 'warning'] = 'error'


class Config(BaseModel):
    agent: str
    path: str
    version: str
    manifest: str
    files: dict[str, str]
    steps: list[Step]
    operations: list[Operation]
    problems: list[Problem]


class Closing(BaseModel):
    say: str | None = None
    recap: str | None = None


class ActionRoutes(BaseModel):
    then: str | None = None
    on_error: str | None = None
    on_unknown: str | None = None
    returns: bool | None = None


class Edit(BaseModel):
    """What the editor sends: only what changed, against the version it was opened at."""

    base: str = ''
    manifest: str | None = None
    """The whole manifest, from the Full config tab: replaces the file, and any closing or action edit beside it."""
    files: dict[str, str] = {}
    """Prompt files by path relative to the manifest's folder."""
    closing: dict[str, Closing] = {}
    actions: dict[str, ActionRoutes] = {}


class Checked(BaseModel):
    ok: bool
    version: str
    """The version these files would be."""
    problems: list[Problem]
    operations: list[Operation] = []
    changed: list[str] = []
    """The files the edit changes, by path relative to the manifest's folder."""


class Saved(Checked):
    saved: bool = False
    config: ConfigRow | None = None


class EditRefusedError(ValueError):
    """An edit the line editor could not make and prove; the Full config tab can."""


def read(manifest: Path, agent: str, root: Path) -> Config:
    files = configs.files_of(manifest)
    found = _checked(manifest)
    problems = [Problem(where='manifest', what=p) for p in found.get('problems', [])]
    return Config(
        agent=agent,
        path=str(manifest.relative_to(root)) if manifest.is_relative_to(root) else str(manifest),
        version=configs.version(files),
        manifest=files[manifest.name],
        files={k: v for k, v in files.items() if k != manifest.name},
        steps=_steps(manifest),
        operations=_operations(manifest, found),
        problems=problems + _closing_problems(_operations(manifest, found)),
    )


def check(manifest: Path, edit: Edit) -> Checked:
    """Apply the edit to a copy of the agent's folder and check the copy, as a save would. Nothing on disk changes."""
    current = configs.files_of(manifest)
    try:
        edited = _apply(manifest.name, current, edit)
    except EditRefusedError as error:
        return Checked(ok=False, version='', problems=[Problem(where='edit', what=str(error))])
    problems: list[Problem] = []
    if edit.base and edit.base != configs.version(current):
        problems.append(
            Problem(
                where='files',
                what='the files changed on disk since this editor opened them: reload before saving',
            )
        )
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / manifest.parent.name
        shutil.copytree(manifest.parent, copy, ignore=shutil.ignore_patterns('__pycache__'))
        for path, text in edited.items():
            (copy / path).parent.mkdir(parents=True, exist_ok=True)
            (copy / path).write_text(text)
        found = _checked(copy / manifest.name)
        problems += [Problem(where='manifest', what=p) for p in found.get('problems', [])]
        operations = _operations(copy / manifest.name, found)
    problems += _closing_problems(operations)
    changed = sorted(path for path, text in edited.items() if current.get(path) != text)
    return Checked(
        ok=not any(p.level == 'error' for p in problems),
        version=configs.version(edited),
        problems=problems,
        operations=operations,
        changed=changed,
    )


def save(manifest: Path, edit: Edit, agent: str, root: Path) -> Saved:
    """Check, then write the files the edit changes. Nothing is written when any check fails."""
    checked = check(manifest, edit)
    if not checked.ok:
        return Saved(**checked.model_dump())
    parent = configs.version(configs.files_of(manifest))
    edited = _apply(manifest.name, configs.files_of(manifest), edit)
    for path in checked.changed:
        target = manifest.parent / path
        if not target.resolve().is_relative_to(manifest.parent.resolve()):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f'.{target.name}.saving')
        staged.write_text(edited[path])
        staged.replace(target)
    config = configs.current(agent, manifest, root, parent=parent)
    return Saved(**checked.model_dump(), saved=True, config=config)


def placeholders(line: str) -> list[str]:
    """The receipt fields a closing line names: `{spoken}` and `{customer.name}` name `spoken` and `customer`.

    Raises `ValueError` for a line `str.format_map` cannot read (an unclosed brace).
    """
    names = []
    for _, field, _, _ in string.Formatter().parse(line):
        if field:
            base = re.split(r'[.\[]', field, maxsplit=1)[0]
            if base and base not in names:
                names.append(base)
    return names


def _closing_problems(operations: list[Operation]) -> list[Problem]:
    problems = []
    for op in operations:
        for key in ('say', 'recap'):
            line = getattr(op, key)
            if not line:
                continue
            where = f'operation {op.name} · {key}'
            try:
                named = placeholders(line)
            except ValueError as error:
                problems.append(Problem(where=where, what=f'{error}: the line could not be said'))
                continue
            if missing := [name for name in named if op.fields and name not in op.fields]:
                problems.append(
                    Problem(
                        where=where,
                        what=f'names {", ".join("{" + m + "}" for m in missing)}, which its receipt does not have '
                        f'({", ".join(op.fields)}): it would be said as a gap',
                    )
                )
    return problems


def _checked(manifest: Path) -> dict[str, Any]:
    try:
        done = subprocess.run(
            [sys.executable, '-m', 'studio.check', str(manifest)],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {'problems': ['the check did not finish in 90 s: an import in the application hangs']}
    if done.returncode != 0:
        last = done.stderr.strip().splitlines()[-1:] or [f'exit {done.returncode}']
        return {'problems': [f'the check crashed: {last[0]}']}
    return json.loads(done.stdout)


def _manifest_operations(manifest: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(manifest.read_text()) or {}
    except yaml.YAMLError:
        return {}
    return data.get('operations') or {}


def _operations(manifest: Path, found: dict[str, Any]) -> list[Operation]:
    declared = _manifest_operations(manifest)
    return [
        Operation(
            **op,
            form='function'
            if isinstance(declared.get(op['name']), dict) and 'function' in declared[op['name']]
            else 'class',
        )
        for op in found.get('operations', [])
    ]


def _steps(manifest: Path) -> list[Step]:
    try:
        spec = core.load(manifest)
    except Exception:
        return []
    steps = []
    for step in spec.steps:
        if isinstance(step, ConversationSpec):
            steps.append(
                Step(
                    id=step.id,
                    kind='conversation',
                    prompt=step.prompt,
                    agent=step.agent,
                    fields=list(step.fields),
                    operations=list(step.operations),
                )
            )
        elif isinstance(step, ActionSpec):
            steps.append(
                Step(
                    id=step.id,
                    kind='action',
                    operation=step.uses,
                    then=step.then or '',
                    on_error=step.on_error or '',
                    on_unknown=step.on_unknown or '',
                    returns=bool(step.returns),
                )
            )
        else:
            steps.append(Step(id=step.id, kind='end'))
    return steps


def _apply(name: str, current: dict[str, str], edit: Edit) -> dict[str, str]:
    """The files after this edit, by path; a path outside the folder, or an edit that cannot be proved, is refused."""
    edited = dict(current)
    for path, text in edit.files.items():
        if path == name or Path(path).is_absolute() or '..' in Path(path).parts:
            raise EditRefusedError(
                f"{path}: only a file inside the agent's folder, other than the manifest, can be edited here"
            )
        edited[path] = text
    if edit.manifest is not None:
        edited[name] = edit.manifest
        return edited
    text = current[name]
    for op, closing in edit.closing.items():
        text = set_closing(text, op, closing)
    for step, routes in edit.actions.items():
        text = set_keys(text, 'steps', step, routes.model_dump(exclude_none=True))
    edited[name] = text
    return edited


def set_closing(text: str, op: str, closing: Closing) -> str:
    """The manifest with this operation's closing lines set; '' removes one. A one-line class gets its override form."""
    changes = closing.model_dump(exclude_none=True)
    return set_keys(text, 'operations', op, {k: (v or None) for k, v in changes.items()}, one_line='class')


def set_keys(text: str, section: str, name: str, changes: dict[str, Any], one_line: str = '') -> str:
    """Set (or, with None, remove) keys of one entry under a top-level section, line by line, and prove it.

    The entry may be written three ways: a one-line value (`name: file.py:Class`, which becomes
    `{<one_line>: file.py:Class, ...}`), a flow mapping on one line, or a block mapping under it.
    """
    if not changes:
        return text
    original = yaml.safe_load(text) or {}
    entries = original.get(section) or {}
    if name not in entries:
        raise EditRefusedError(f'{section}.{name} is not in the manifest')
    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if re.match(rf'^{re.escape(section)}:\s*(#.*)?$', line)), None
    )
    if start is None:
        raise EditRefusedError(f'{section}: is not a block this editor can find; use Full config')
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].strip() and not lines[i].startswith((' ', '#'))),
        len(lines),
    )
    at = next(
        (i for i in range(start + 1, end) if re.match(rf'^(\s+){re.escape(name)}:(\s|$)', lines[i])),
        None,
    )
    if at is None:
        raise EditRefusedError(f'{section}.{name}: its line could not be found; use Full config')
    indent = _indent(lines[at])
    value = lines[at].split(':', 1)[1].strip()
    current = entries[name]
    if isinstance(current, str):
        current = {one_line: current} if one_line else None
        if current is None:
            raise EditRefusedError(f'{section}.{name} is a plain value; use Full config')
    if not isinstance(current, dict):
        raise EditRefusedError(f'{section}.{name} is not a mapping; use Full config')
    wanted = {k: v for k, v in ({**current, **changes}).items() if v is not None}
    if value and not value.startswith('#'):
        if value.startswith('{') and not value.rstrip().endswith('}') and '#' not in value:
            raise EditRefusedError(f'{section}.{name} spans several lines; use Full config')
        if one_line and set(wanted) == {one_line}:
            new = f'{indent}{name}: {wanted[one_line]}\n'
        else:
            new = f'{indent}{name}: {{{", ".join(f"{k}: {_scalar(v)}" for k, v in wanted.items())}}}\n'
        lines[at] = new
    else:
        child = next(
            (
                _indent(lines[i])
                for i in range(at + 1, end)
                if lines[i].strip() and not lines[i].lstrip().startswith('#')
            ),
            indent + '  ',
        )
        block_end = next(
            (
                i
                for i in range(at + 1, end)
                if lines[i].strip() and len(lines[i]) - len(lines[i].lstrip()) <= len(indent)
            ),
            end,
        )
        for key, v in changes.items():
            found = next(
                (
                    i
                    for i in range(at + 1, block_end)
                    if re.match(rf'^{child}{re.escape(key)}:(\s|$)', lines[i])
                ),
                None,
            )
            if v is None:
                if found is not None:
                    del lines[found]
                    block_end -= 1
            elif found is not None:
                lines[found] = f'{child}{key}: {_scalar(v)}\n'
            else:
                last = max((i for i in range(at + 1, block_end) if lines[i].strip()), default=at)
                lines.insert(last + 1, f'{child}{key}: {_scalar(v)}\n')
                block_end += 1
    result = ''.join(lines)
    expected = copy.deepcopy(original)
    expected[section][name] = wanted if not (one_line and set(wanted) == {one_line}) else wanted[one_line]
    if (yaml.safe_load(result) or {}) != expected:
        raise EditRefusedError(
            f'{section}.{name}: the edit could not be made without changing something else; use Full config'
        )
    return result


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _scalar(value: Any) -> str:
    """A YAML scalar that reads back as exactly this value: booleans bare, every string double-quoted."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)
