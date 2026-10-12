"""What the editor shows and takes: an agent's config, an edit to it, and what checking or saving it found."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from studio.runs.store import ConfigRow


class Case(BaseModel):
    """One closing line and the result it is for, as `core.closing.Case` reads it."""

    line: str
    when: dict[str, Any] | Literal['refused'] | None = None


type Words = str | list[Case]


class Operation(BaseModel):
    """One operation as the editor shows it.

    `say` and `recap` are one line or lines by result. `fields` are the paths a success has (`customer.name`
    inside a nested model) and `refused_fields` those a refusal has; `values` and `refused_values` give each
    closed field's values; `receipt_schema` (`schema` on the wire) is the receipt type's JSON schema, as the state's is given. `form` says
    where the closing line lives: the manifest (`function`, or a class's override) or its class's code.
    """

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)
    name: str
    say: Words = ''
    recap: Words = ''
    changes: bool = True
    approval: str = 'readback'
    fields: list[str] = []
    refused_fields: list[str] = []
    values: dict[str, list[Any]] = {}
    refused_values: dict[str, list[Any]] = {}
    receipt_schema: dict[str, Any] = Field(default={}, alias='schema')
    form: Literal['class', 'function'] = 'class'
    route: str = ''


class Step(BaseModel):
    """One step as the editor shows it: an action step's one `operation`, its routes, and no prompt."""

    id: str
    kind: Literal['conversation', 'action', 'end']
    prompt: str = ''
    agent: str = ''
    fields: list[str] = []
    operations: list[str] = []
    operation: str = ''
    then: str = ''
    on_error: str = ''
    on_unknown: str = ''
    returns: bool = False


class Problem(BaseModel):
    """Something a check found, where, and whether it stops a save."""

    where: str
    what: str
    level: Literal['error', 'warning'] = 'error'


class Config(BaseModel):
    """An agent's config as the editor opens it: its files, steps, operations and what is wrong with them."""

    agent: str
    path: str
    version: str
    manifest: str
    files: dict[str, str]
    steps: list[Step]
    operations: list[Operation]
    problems: list[Problem]


class ClosingEdit(BaseModel):
    """New closing lines for one operation; None keeps a line, '' removes it."""

    say: Words | None = None
    recap: Words | None = None


class ActionRoutes(BaseModel):
    """New routes for one action step; None keeps a route."""

    then: str | None = None
    on_error: str | None = None
    on_unknown: str | None = None
    returns: bool | None = None


class Edit(BaseModel):
    """What the editor sends: only what changed, against the version it was opened at.

    `manifest` (the Full config tab) replaces the file, and any closing or action edit beside it;
    `files` are prompt files by path relative to the manifest's folder.
    """

    base: str = ''
    manifest: str | None = None
    files: dict[str, str] = {}
    closing: dict[str, ClosingEdit] = {}
    actions: dict[str, ActionRoutes] = {}


class Checked(BaseModel):
    """What checking an edit found: whether it may be saved, the version it would make, and the files it changes."""

    ok: bool
    version: str
    problems: list[Problem]
    operations: list[Operation] = []
    changed: list[str] = []


class Saved(Checked):
    """A save: what the check found, and the config row it wrote when it did."""

    saved: bool = False
    config: ConfigRow | None = None


class FactKind(BaseModel):
    """A fact's kind in words (`studio.agents.facts.Kind`): its code, its choices, and what is inside a group or a list."""

    code: str
    words: str
    article: str = ''
    choices: list[Any] = []
    nullable: bool = False
    members: list[FactKind] = []
    fields: list[NestedFact] = []
    item: FactKind | None = None
    defined_in: str = ''
    model: str = ''
    description: str = ''


class NestedFact(BaseModel):
    """A field inside a group of details: shown, edited only in code."""

    name: str
    label: str
    description: str = ''
    kind: FactKind


class FactRule(BaseModel):
    """A validator the application wrote, read-only: where it is, what its errors say, and what the caller hears."""

    name: str
    where: str = ''
    says: list[str] = []
    types: list[str] = []
    heard: list[str] = []


class FactExcerpt(BaseModel):
    """A few lines of a file around the one that matters (`studio.agents.facts.Excerpt`): the first line's number, and the text."""

    first: int
    lines: list[str]


class FactUse(BaseModel):
    """One place a fact is used (`studio.agents.facts.Usage`): what kind of use, and the step, operation, file it is in."""

    kind: str
    step: str = ''
    operation: str = ''
    function: str = ''
    choice: str = ''
    argument: str = ''
    path: str = ''
    detail: str = ''
    file: str = ''
    line: int = 0
    excerpt: FactExcerpt | None = None


class FactSpec(BaseModel):
    """A fact as the studio could write it from scratch: what a proof rebuilds its expected schema from."""

    kind: str
    choices: list[Any] = []
    may_be_empty: bool = True
    start: Any = None
    description: str = ''
    limits: dict[str, int | float] = {}


class Fact(BaseModel):
    """One fact as the Facts page shows it: what it is, who fills it in, its rules, its uses, and what can change here.

    `editable` says, for `description`, `choices`, `limits`, `empty` and `remove`, whether the studio can make
    and prove that change; `why_not` says why not where it cannot. `latest` is its value in the agent's
    latest run, `latest_run` that run.
    """

    name: str
    label: str
    group: Literal['caller', 'tools', 'open']
    description: str = ''
    note: str = ''
    line: int = 0
    excerpt: FactExcerpt | None = None
    kind: FactKind
    starts_empty: bool = True
    start: Any = None
    limits: dict[str, Any] = {}
    rules: list[FactRule] = []
    uses: list[FactUse] = []
    editable: dict[str, bool] = {}
    why_not: dict[str, str] = {}
    spec: FactSpec | None = None
    latest: Any = None


class FactsView(BaseModel):
    """An agent's facts as the page opens them.

    `file` is the state module, relative to the use case's folder, and `class_name` its class. `rules` are
    the state's own whole-state validators. `steps` are the conversation steps that could fill a fact in.
    `latest_run` is the run the latest values come from; `runners` says how the runners stand after a save.
    """

    agent: str
    version: str
    file: str = ''
    class_name: str = ''
    facts: list[Fact] = []
    rules: list[FactRule] = []
    steps: list[str] = []
    problems: list[Problem] = []
    latest_run: str = ''
    runners: str = ''


class FactLimits(BaseModel):
    """Limits to set on a fact; None removes one. Text takes `min_length`/`max_length`, a number `ge`/`le`."""

    min_length: int | None = None
    max_length: int | None = None
    ge: int | float | None = None
    le: int | float | None = None


type NewKind = Literal[
    'text', 'whole_number', 'number', 'yes_no', 'date', 'time', 'not_past', 'phone', 'quoted', 'choice'
]


class FactEdit(BaseModel):
    """One change to one fact, against the version the page opened.

    `add` takes `kind`, `description`, `choices` for one of a list, and `fill_steps`. `change` takes any of
    `description`, `choices`, `limits` (only the keys given), `may_be_empty` with `start`, and `fill_steps`
    (the steps that fill it in, all of them). `remove` takes nothing else. `rename` is always refused.
    """

    base: str = ''
    action: Literal['add', 'change', 'remove', 'rename']
    name: str
    new_name: str = ''
    kind: NewKind | None = None
    description: str | None = None
    choices: list[str] | None = None
    limits: FactLimits | None = None
    may_be_empty: bool | None = None
    start: Any = None
    fill_steps: list[str] | None = None


class FactChecked(BaseModel):
    """What proving a fact edit found: whether it may be saved, what was proved, the version, and the change itself."""

    ok: bool
    version: str = ''
    problems: list[Problem] = []
    proved: list[str] = []
    diff: str = ''
    changed: list[str] = []


class FactSaved(FactChecked):
    """A fact save: what the proof found, the config row it wrote, and how the runners take it up."""

    saved: bool = False
    config: ConfigRow | None = None
    runners: str = ''
