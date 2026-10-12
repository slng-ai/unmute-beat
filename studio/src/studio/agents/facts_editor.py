"""The Facts editor: an agent's facts read for the page, and one change to one fact proved on a copy before it is saved.

The facts live in Python (the state model `state:` names), so a change is made to the module's text
(`studio.agents.state_source`) and, when it changes which steps fill a fact in, to `agent.yaml` line by line
(`studio.agents.manifest_text`). Nothing is written until all of this holds, on a copy of the agent's folder:

1. the new module reads as Python, and differs from the old one in that fact and in imports only
   (`SourceProof`);
2. the copy loads and compiles in a process of its own (`studio.agents.check`): `FlowSpec.problems()` is
   empty, so every step, operation and prompt still names a real fact;
3. the state's JSON schema after is the schema before with exactly the intended change (`SchemaProof`):
   every other fact, `$defs` and the model's own keys the same, and the fact itself the schema Pydantic
   writes for what it should now be, built here from the edit, never from the edited code
   (`ExpectedProperty`). For a fact whose kind the studio cannot write from scratch, only its description
   may change, and the proof is that nothing else in its schema did;
4. a change to the steps that fill it in leaves exactly those steps filling it in, as the engine reads them.

A rename is refused: past runs saved under the old name could no longer go back, and code that reads a fact
by name (a choice function, an operation's alias) cannot all be found and proved. Adding the new fact,
moving its uses and removing the old one is the way, each step proved.

A save writes the changed files and keeps a version of the config, which includes the state module
(`studio.agents.configs`). Runners import code once, so the API restarts the studio's own runners after a
save (`studio.api.app.Studio.renew_runners`); runs already going keep the facts they started with.
"""

import datetime as dt
import difflib
import shutil
import tempfile
import typing
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import ConfigDict, Field, create_model

import core
from studio.agents.catalog import AgentCheckProcess, CheckResult
from studio.agents.configs import AgentConfig
from studio.agents.manifest_text import EditRefusedError, ManifestText
from studio.agents.models import Fact, FactChecked, FactEdit, FactSaved, FactSpec, FactsView, Problem
from studio.agents.state_source import NewFact, SourceProof, SourceRefusedError, StateSource

RENAME = (
    'renaming is not done here: runs saved under the old name could no longer go back, and code that reads a '
    'fact by name cannot all be found. Add the new fact, move its uses, then remove this one'
)


class ExpectedProperty:
    """The JSON schema Pydantic writes for a fact the studio can describe whole, built from that description alone."""

    TYPES: typing.ClassVar[dict[str, Any]] = {
        'text': str,
        'whole_number': int,
        'number': float,
        'yes_no': bool,
        'date': dt.date,
        'time': dt.time,
        'not_past': Annotated[dt.date, core.NotPast],
        'phone': core.Phone,
        'quoted': core.Quoted,
    }

    def __init__(self, name: str, spec: FactSpec) -> None:
        self.name = name
        self.spec = spec

    def kind(self) -> Any:
        """The Python kind the fact holds, `None` aside."""
        if self.spec.kind == 'choice':
            return Literal[tuple(self.spec.choices)]  # ty: ignore[invalid-type-form]
        if self.spec.kind not in self.TYPES:
            raise SourceRefusedError(f'the studio cannot describe a {self.spec.kind} fact whole')
        return self.TYPES[self.spec.kind]

    def schema(self) -> dict[str, Any]:
        """The fact's property in its model's JSON schema."""
        kind = self.kind()
        annotation = kind | None if self.spec.may_be_empty else kind
        default = None if self.spec.may_be_empty else self.spec.start
        info = Field(default, description=self.spec.description or None, **self.spec.limits)  # ty: ignore[no-matching-overload]
        fields: dict[str, Any] = {self.name: (annotation, info)}
        model = create_model('Expected', __config__=ConfigDict(extra='forbid'), **fields)
        return model.model_json_schema()['properties'][self.name]


class SchemaProof:
    """The state's JSON schema before and after one fact edit: the same, but for exactly that change."""

    def __init__(
        self, before: dict[str, Any], after: dict[str, Any], edit: FactEdit, spec: FactSpec | None
    ) -> None:
        self.before = before
        self.after = after
        self.edit = edit
        self.spec = spec

    def problems(self) -> list[str]:
        """What differs from the intended change, in words; empty when it is exactly that."""
        name, action = self.edit.name, self.edit.action
        old, new = self.before.get('properties', {}), self.after.get('properties', {})
        found = []
        outer = [
            {k: v for k, v in s.items() if k not in ('properties', '$defs')}
            for s in (self.before, self.after)
        ]
        if outer[0] != outer[1]:
            found.append("the state's own schema changed (its required facts, title or rules)")
        others = [[(k, v) for k, v in props.items() if k != name] for props in (old, new)]
        if others[0] != others[1]:
            found.append(f'a fact other than {name} changed in the schema')
        defs = [s.get('$defs', {}) for s in (self.before, self.after)]
        if action == 'remove':
            if any(defs[0].get(key) != value for key, value in defs[1].items()):
                found.append('a kind defined for the facts changed')
            if name in new:
                found.append(f'{name} is still in the schema')
            return found
        if defs[0] != defs[1]:
            found.append('a kind defined for the facts changed')
        if name not in new:
            return [*found, f'{name} is not in the schema after the edit']
        if action == 'add' and list(new)[-1] != name:
            found.append(f'{name} was not added as the last fact')
        expected = self.expected(old.get(name))
        if new[name] != expected:
            found.append(f'{name} is not what was asked for: expected {expected}, found {new[name]}')
        return found

    def expected(self, was: dict[str, Any] | None) -> dict[str, Any]:
        """The fact's property as the edit should leave it."""
        edit = self.edit
        if self.spec is not None:
            return ExpectedProperty(edit.name, self.spec).schema()
        expected = dict(was or {})
        if edit.description is not None:
            expected.pop('description', None)
            if edit.description:
                expected['description'] = edit.description
        return expected


class FillSteps:
    """`agent.yaml` with a fact filled in by exactly some steps: each one's `fields`, and its facts so it sees it.

    Taking a step away takes the fact out of its `fields` and out of its own `context.facts` too: the inverse
    of adding it. A step that only sees it (the flow's list, or a list that names it without filling it in)
    is left as it is, and that use is listed on the fact.
    """

    def __init__(self, text: str, name: str) -> None:
        self.text = text
        self.name = name
        self.data = yaml.safe_load(text) or {}

    def conversation_steps(self) -> dict[str, dict[str, Any]]:
        """Every conversation step, by id, as written."""
        steps = self.data.get('steps') or {}
        return {
            key: value or {}
            for key, value in steps.items()
            if isinstance(value or {}, dict) and (value or {}).get('kind', 'conversation') == 'conversation'
        }

    def flow_facts(self) -> Any:
        """The facts every step sees unless it says otherwise: the flow's `context.facts`, `none` by default."""
        context = self.data.get('context')
        return context.get('facts', 'none') if isinstance(context, dict) else 'none'

    def with_steps(self, wanted: list[str]) -> str:
        """The text with exactly these steps filling the fact in; a step that fills it in also sees it."""
        steps = self.conversation_steps()
        unknown = [step for step in wanted if step not in steps]
        if unknown:
            raise EditRefusedError(f'{", ".join(unknown)}: no conversation step of that name')
        text = self.text
        for step, written in steps.items():
            fields = list(written.get('fields') or [])
            if (step in wanted) == (self.name in fields):
                continue
            context = written.get('context')
            if step not in wanted:
                kept = [field for field in fields if field != self.name]
                changes: dict[str, Any] = {'fields': kept or None}
                if isinstance(context, dict) and isinstance(context.get('facts'), list):
                    changes['context'] = context | {
                        'facts': [f for f in context['facts'] if f != self.name] or 'none'
                    }
                text = ManifestText(text).with_keys('steps', step, changes, plain=True)
                continue
            changes = {'fields': [*fields, self.name]}
            mapping = (
                dict(context)
                if isinstance(context, dict)
                else {'history': 'none', 'facts': 'none'}
                if context == 'none'
                else {}
            )
            seen = mapping.get('facts', self.flow_facts())
            if seen != 'all' and self.name not in (seen if isinstance(seen, list) else []):
                changes['context'] = mapping | {
                    'facts': [*(seen if isinstance(seen, list) else []), self.name]
                }
            text = ManifestText(text).with_keys('steps', step, changes, plain=True)
        return text


class FactsEditor:
    """One agent's facts: read for the page, one edit proved on a copy, and a proved edit saved."""

    def __init__(self, manifest: Path, agent: str, root: Path) -> None:
        self.manifest = manifest
        self.agent = agent
        self.root = root
        self.config = AgentConfig(manifest)

    def found(self) -> CheckResult:
        """The check on the files on disk now (kept until the folder changes)."""
        return AgentCheckProcess(self.manifest).result()

    def read(self) -> FactsView:
        """The facts as the page opens them, or what stops the agent loading."""
        found = self.found()
        version = self.config.version()
        if found.problems or not found.facts:
            problems = found.problems or ['the agent keeps no facts the studio can read']
            return FactsView(
                agent=self.agent, version=version, problems=[Problem(where='agent', what=p) for p in problems]
            )
        facts = found.facts
        return FactsView(
            agent=self.agent,
            version=version,
            file=facts['file'],
            class_name=facts['class_name'],
            facts=[Fact.model_validate(fact) for fact in facts['facts']],
            rules=facts['rules'],
            steps=facts['steps'],
        )

    def state_path(self) -> str:
        """The state module's path inside the agent's folder, as the config's files name it."""
        state = self.config.state_file()
        if state is None:
            raise SourceRefusedError(
                "the agent's state module is not a .py file inside its folder: edit it in code"
            )
        return str(state.relative_to(self.manifest.parent.resolve()))

    def apply(self, current: dict[str, str], edit: FactEdit, before: dict[str, Any]) -> dict[str, str]:
        """The files after this edit, by path; refused with words when the studio cannot make it."""
        if edit.action == 'rename':
            raise SourceRefusedError(RENAME)
        path = self.state_path()
        class_name = before['class_name']
        text = current[path]
        fact = next((Fact.model_validate(f) for f in before['facts'] if f['name'] == edit.name), None)
        if edit.action == 'add':
            if edit.kind is None:
                raise SourceRefusedError('a new fact needs a kind')
            text = StateSource(text, class_name).with_fact(
                edit.name, NewFact(edit.kind, edit.choices or ()), edit.description or ''
            )
        elif fact is None:
            raise SourceRefusedError(f'{edit.name} is not one of the facts')
        elif edit.action == 'remove':
            if not fact.editable.get('remove'):
                raise SourceRefusedError(f'{edit.name} cannot be removed: {UseWords.listed(fact)}')
            text = StateSource(text, class_name).without(edit.name)
        else:
            text = self._changed(text, class_name, fact, edit)
        edited = current | {path: text}
        if edit.fill_steps is not None:
            name = self.manifest.name
            edited[name] = FillSteps(current[name], edit.name).with_steps(edit.fill_steps)
        return edited

    @staticmethod
    def _changed(text: str, class_name: str, fact: Fact, edit: FactEdit) -> str:
        """The module with each part of a change made, one after another; each part this fact cannot take refused."""
        why = fact.why_not
        parts = (
            ('description', edit.description is not None),
            ('choices', edit.choices is not None),
            ('limits', edit.limits is not None),
            ('empty', edit.may_be_empty is not None),
        )
        for part, asked in parts:
            if asked and not fact.editable.get(part):
                raise SourceRefusedError(f'{edit.name}: {why.get(part, "this cannot change here")}')
        if edit.description is not None:
            text = StateSource(text, class_name).with_description(edit.name, edit.description)
        if edit.choices is not None:
            text = StateSource(text, class_name).with_choices(edit.name, edit.choices)
        if edit.limits is not None:
            text = StateSource(text, class_name).with_limits(
                edit.name, edit.limits.model_dump(exclude_unset=True)
            )
        if edit.may_be_empty is not None:
            text = StateSource(text, class_name).with_empty(edit.name, edit.may_be_empty, edit.start)
        return text

    @staticmethod
    def spec_after(fact: Fact | None, edit: FactEdit) -> FactSpec | None:
        """What the fact should be after the edit, when the studio can describe it whole; None when only its description changes."""
        if edit.action == 'add' and edit.kind is not None:
            return FactSpec(kind=edit.kind, choices=edit.choices or [], description=edit.description or '')
        changes_kind = edit.choices is not None or edit.limits is not None or edit.may_be_empty is not None
        if fact is None or fact.spec is None or not changes_kind:
            return None
        spec = fact.spec.model_copy(deep=True)
        if edit.description is not None:
            spec.description = edit.description
        if edit.choices is not None:
            spec.choices = list(edit.choices)
        if edit.limits is not None:
            limits = spec.limits | edit.limits.model_dump(exclude_unset=True)
            spec.limits = {key: value for key, value in limits.items() if value is not None}
        if edit.may_be_empty is not None:
            spec.may_be_empty = edit.may_be_empty
            spec.start = None if edit.may_be_empty else edit.start
        return spec

    def check(self, edit: FactEdit) -> FactChecked:
        """The edit made on a copy of the agent's folder and proved. Nothing on disk changes."""
        current = self.config.files()
        found = self.found()
        if found.problems or not found.facts:
            return FactChecked(
                ok=False, problems=[Problem(where='agent', what='the agent does not load now: fix it first')]
            )
        before = found.facts
        try:
            edited = self.apply(current, edit, before)
        except (SourceRefusedError, EditRefusedError) as error:
            return FactChecked(ok=False, problems=[Problem(where=edit.name, what=str(error))])
        problems: list[Problem] = []
        if edit.base and edit.base != AgentConfig.version_of(current):
            problems.append(
                Problem(
                    where='files', what='the files changed on disk since this page opened them: reload first'
                )
            )
        path, proved = self.state_path(), []
        try:
            differences = SourceProof(current[path], edited[path], before['class_name']).differences(
                edit.name
            )
        except SourceRefusedError as error:
            differences = [str(error)]
        problems += [Problem(where=path, what=what) for what in differences]
        if not differences:
            proved.append(
                f'{path} still reads as Python, and nothing in it changed but {edit.name} and its imports'
            )
        after = self._checked_copy(edited)
        problems += [Problem(where='agent', what=what) for what in after.problems]
        if not after.problems and after.facts:
            proved.append('the agent loads with it: every step, tool and prompt still names a real fact')
            fact = next((Fact.model_validate(f) for f in before['facts'] if f['name'] == edit.name), None)
            try:
                schema = SchemaProof(
                    before['schema'], after.facts['schema'], edit, self.spec_after(fact, edit)
                ).problems()
            except SourceRefusedError as error:
                schema = [str(error)]
            problems += [Problem(where='facts', what=what) for what in schema]
            if not schema:
                proved.append(f'the facts are the same as before, but for exactly this change to {edit.name}')
            if edit.fill_steps is not None:
                filled = sorted(
                    use['step']
                    for f in after.facts['facts']
                    if f['name'] == edit.name
                    for use in f['uses']
                    if use['kind'] == 'fills'
                )
                if filled != sorted(edit.fill_steps):
                    problems.append(
                        Problem(
                            where='agent.yaml',
                            what=f'the steps that fill it in are {filled}, not {sorted(edit.fill_steps)}',
                        )
                    )
                else:
                    proved.append('exactly the steps asked for fill it in: ' + (', '.join(filled) or 'none'))
        changed = sorted(p for p, t in edited.items() if current.get(p) != t)
        return FactChecked(
            ok=not any(problem.level == 'error' for problem in problems),
            version=AgentConfig.version_of(edited),
            problems=problems,
            proved=proved,
            diff=''.join(
                ''.join(
                    difflib.unified_diff(
                        current.get(p, '').splitlines(True), edited[p].splitlines(True), p, p, n=1
                    )
                )
                for p in changed
            ),
            changed=changed,
        )

    def _checked_copy(self, edited: dict[str, str]) -> CheckResult:
        """The check on a copy of the agent's folder with these files written."""
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / self.manifest.parent.name
            shutil.copytree(self.manifest.parent, copy, ignore=shutil.ignore_patterns('__pycache__'))
            for path, text in edited.items():
                (copy / path).write_text(text)
            return AgentCheckProcess(copy / self.manifest.name, keep=False).result()

    def save(self, edit: FactEdit) -> FactSaved:
        """Prove, then write the files the edit changes. Nothing is written when any proof fails."""
        checked = self.check(edit)
        if not checked.ok:
            return FactSaved(**checked.model_dump())
        current = self.config.files()
        edited = self.apply(current, edit, self.found().facts)
        folder = self.manifest.parent
        for path in checked.changed:
            target = folder / path
            staged = target.with_name(f'.{target.name}.saving')
            staged.write_text(edited[path])
            staged.replace(target)
        row = self.config.row(self.agent, self.root, parent=AgentConfig.version_of(current))
        return FactSaved(**checked.model_dump(), saved=True, config=row)


class UseWords:
    """A fact's uses in a few words, for a refusal: "filled in by booking, input to book_appointment"."""

    WORDS: typing.ClassVar[dict[str, str]] = {
        'fills': 'filled in by',
        'sees': 'listed in the facts of',
        'asks': 'asked for by',
        'needed': 'needed before',
        'identity': 'who the caller is',
        'offered': 'offered in',
        'rests_on': 'a choice rests on it in',
        'read_back': 'read back in',
        'carried_out': 'carried out in',
        'input': 'input to',
        'set_by': 'set by',
        'prompt': 'named in',
        'code': 'named in code in',
    }

    @classmethod
    def listed(cls, fact: Fact) -> str:
        """Every use but being seen, grouped by kind."""
        grouped: dict[str, list[str]] = {}
        for use in fact.uses:
            if use.kind == 'sees' and use.detail != 'listed':
                continue
            where = use.step or use.operation or (f'{use.file}:{use.line}' if use.file else '')
            grouped.setdefault(cls.WORDS.get(use.kind, use.kind), [])
            if where and where not in grouped[cls.WORDS.get(use.kind, use.kind)]:
                grouped[cls.WORDS.get(use.kind, use.kind)].append(where)
        return '; '.join(f'{words} {", ".join(places)}'.strip() for words, places in grouped.items())
