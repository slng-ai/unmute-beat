"""The desk fixture's state: two fields, both optional, so a call starts empty."""

from pydantic import BaseModel, ConfigDict


class DeskState(BaseModel):
    model_config = ConfigDict(extra='forbid')

    caller: str | None = None
    topic: str | None = None
