"""The timings the Metrics page shows, and what counts as slow."""

from typing import Literal

from pydantic import BaseModel

type Kind = Literal['stt', 'llm', 'tts']
type Level = Literal['ok', 'slow', 'bad']


class Limit(BaseModel):
    """Above `slow` a number is shown as slow; above `bad` it is over its limit, and the turn or request is flagged."""

    slow: float
    bad: float

    def level(self, value: float) -> Level:
        """How a number stands against this limit."""
        return 'bad' if value > self.bad else 'slow' if value > self.slow else 'ok'

    def crossed(self, level: Level) -> float:
        """The limit a number at this level went over."""
        return self.bad if level == 'bad' else self.slow


class Limits(BaseModel):
    """What counts as slow, in milliseconds, except requests per turn, which is a count.

    `llm_request` judges an LLM request with no first-token time on its whole duration.
    """

    voice_to_voice: Limit = Limit(slow=1000, bad=1500)
    stt_first_byte: Limit = Limit(slow=250, bad=400)
    llm_first_token: Limit = Limit(slow=600, bad=700)
    llm_request: Limit = Limit(slow=1000, bad=1500)
    tts_first_byte: Limit = Limit(slow=300, bad=400)
    requests_per_turn: Limit = Limit(slow=1, bad=2)

    def for_request(self, kind: Kind, streamed: bool) -> Limit:
        """The limit a request is judged by: its first byte, or for an LLM that did not stream, its duration."""
        if kind == 'stt':
            return self.stt_first_byte
        if kind == 'tts':
            return self.tts_first_byte
        return self.llm_first_token if streamed else self.llm_request


class Flag(BaseModel):
    """Something over a limit, in words (`LLM first token 912 ms`), with its value and the limit it crossed."""

    level: Literal['slow', 'bad']
    what: str
    value: float
    limit: float
    span: str = ''


class Request(BaseModel):
    """One STT, LLM or TTS request.

    `first_byte_ms` is the time to first byte (STT, TTS) or first token (LLM), where the span says; `at`
    is when it started, in microseconds since the epoch (Jaeger's own unit).
    """

    kind: Kind
    model: str
    provider: str
    duration_ms: float
    first_byte_ms: float | None
    error: bool
    span: str
    level: Level
    at: int = 0
    trace: str = ''


class TurnTiming(BaseModel):
    """One caller turn: its voice-to-voice time, its requests, and what in it is slow."""

    trace: str
    run: str = ''
    agent: str = ''
    turn: int
    voice_to_voice_ms: float | None
    interrupted: bool
    requests: list[Request]
    flags: list[Flag]
    level: Level


class ModelStats(BaseModel):
    """One model's requests, keyed by kind, model and provider: a model swapped between runs is two rows.

    `runs` counts the traces it served in; `first_byte_p90_ms` is None when no request of it says;
    `over_limit` is the share of its requests over the limit, 0 to 1; `first_seen` and `last_seen` are
    ISO times of its first and last request in the period.
    """

    kind: Kind
    model: str
    provider: str
    requests: int
    runs: int
    first_byte_p90_ms: float | None
    total_p90_ms: float
    errors: int
    over_limit: float
    first_seen: str
    last_seen: str


class Summary(BaseModel):
    """The tiles: p90s over every turn, the share of turns over a limit, errors, and the kinds served by several models."""

    turns: int
    runs: int
    voice_to_voice_p90_ms: float | None
    llm_first_token_p90_ms: float | None
    tts_first_audio_p90_ms: float | None
    stt_first_byte_p90_ms: float | None
    over_limit: float
    errors: int
    mixed: list[Kind] = []


class Run(BaseModel):
    """One run, as Jaeger holds it: `run` is the studio's run id and `agent` its agent, '' when its launcher named none."""

    trace: str
    run: str
    agent: str
    started: str
    turns: int


class Metrics(BaseModel):
    """The overview, for every agent or the one named in `agent`, or one run's view, which names its agent in `runs`."""

    source: str
    scope: Literal['overview', 'run']
    agent: str = ''
    limits: Limits
    summary: Summary
    runs: list[Run]
    turns: list[TurnTiming]
    models: list[ModelStats]
