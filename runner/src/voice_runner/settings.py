"""A runner's settings, what the studio asks of it, and where the use cases keep their business data."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from core.snapshot import TurnSnapshot


@dataclass(frozen=True)
class Settings:
    """One runner process: where it listens and how the browser and the studio reach it.

    `public_url` defaults to `http://{host}:{port}`. `studio` is the studio's link
    (`ws://127.0.0.1:7800/api/runners/link`); '' runs with no studio. `warm` sends one warm-up request
    per model client at start; `drain_seconds` bounds how long a stop waits for calls to end.
    `livekit_url` is the LiveKit server a voice call on LiveKit joins (`LIVEKIT_URL`; its key and
    secret are read from the environment when a token is made); '' takes no LiveKit call.
    """

    port: int = 7810
    host: str = '127.0.0.1'
    public_url: str = ''
    studio: str = ''
    max_calls: int = 4
    warm: bool = True
    drain_seconds: float = 300.0
    runner_id: str = field(default_factory=lambda: f'runner-{os.getpid()}')
    ice_servers: tuple[dict[str, Any], ...] = ()
    livekit_url: str = ''

    @property
    def url(self) -> str:
        """How the browser and the studio reach this process."""
        return self.public_url or f'http://{self.host}:{self.port}'


class CallRequest(BaseModel):
    """What the studio asks for: an agent, a channel, and where to start from.

    `run` is the studio's run id (the runner makes one when it is not given) and `config` the config
    version the studio names the run by. `facts` is the state to carry on with, edited on the go-back
    page, over the snapshot's own. `simulated` delivery lets estimated playback count as heard on a voice
    call (a developer's choice). `framework` says which one a voice call runs on: Pipecat's SmallWebRTC,
    or a LiveKit room the browser and this runner both join (`Runner.livekit`).
    """

    agent: str
    channel: Literal['voice', 'text'] = 'text'
    run: str = ''
    config: str = ''
    caller_id: str = ''
    resume: TurnSnapshot | None = None
    facts: dict[str, Any] | None = None
    delivery: Literal['marks', 'simulated'] = 'marks'
    framework: Literal['pipecat', 'livekit'] = 'pipecat'


class TurnRequest(BaseModel):
    """A text call's next caller turn."""

    text: str


class DataFolder:
    """Where the use cases keep their business data when the environment does not say: one folder, shared by runners."""

    def __init__(self, root: Path) -> None:
        self.folder = root / 'data' / 'studio'

    def use(self) -> None:
        """Point the use cases' data paths at it, unless the environment already does."""
        self.folder.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault('DIARY_PATH', str(self.folder / 'salon-diary.sqlite3'))
        os.environ.setdefault('LEARNING_DB_PATH', str(self.folder / 'faro.sqlite3'))
