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
