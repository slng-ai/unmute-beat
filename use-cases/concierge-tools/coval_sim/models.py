"""What Coval returns, validated, and the rule that decides whether a run passed.

Only the fields this tool reads are declared. Pydantic ignores the rest, so a
field Coval adds later changes nothing here.
"""

import re
from collections.abc import Collection
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

AGENT_NAME = re.compile(r'^voice-harness-(?P<agency>[a-z][a-z-]*)-pipecat$')
"""A Coval agent this tool drives is named after the flow it calls: voice-harness-salon-pipecat.
The name is the whole link between the repository and Coval, so nothing here holds a Coval ID.
The agency names the folder under `use-cases/` whose `bot.py` answers (`FOLDERS`)."""
FOLDERS = {'salon': 'salon-graph'}
"""An agency whose folder has another name. `salon` is the Coval agent that existed before the
split, and it has always called the graph build. Any other agency is its own folder's name."""
FINISHED = {'COMPLETED', 'FAILED', 'CANCELLED', 'ERROR'}
FALLBACKS = {
    'STUMBLE (a turn failed)': 'problem there',
    'EXHAUSTED (a turn ran out of budget)': 'round in circles',
    'UNBACKED (claimed a change nothing made)': 'actually made that change',
    'WITHHELD (held back a claim)': 'nothing has been changed',
}
"""The fixed recovery lines of `core/turn.py`, by a fragment Coval's transcription keeps."""


class Agent(BaseModel):
    """A Coval agent: the connection Coval uses to reach the bot."""

    id: str
    display_name: str
    model_type: str = ''
    test_set_ids: list[str] = []
    metric_ids: list[str] = []
    metadata: dict[str, Any] = {}

    @property
    def agency(self) -> str | None:
        """The flow this agent calls, or None if it is not ours."""
        match = AGENT_NAME.match(self.display_name)
        return match['agency'] if match else None

    @staticmethod
    def name_for(agency: str) -> str:
        """The Coval agent name for a flow."""
        return f'voice-harness-{agency}-pipecat'


class Persona(BaseModel):
    """The simulated caller."""

    id: str
    name: str


class Message(BaseModel):
    """One line of a simulated call's transcript."""

    role: str
    content: str | None = None


class SpeechProfile(BaseModel):
    """The speech settings a bot runs on: one env file, every name required, nothing else.

    Session 4's first load runs started without the session's profile and
    ran on the bot's own defaults (a different speech-to-text and voice), so
    their latency could not be compared with the calls before. A job now
    names its profile, and a file missing a name or carrying a stray one is
    refused before any bot starts.
    """

    model_config = ConfigDict(extra='forbid')

    SLNG_BASE_URL: str = Field(min_length=1)
    SLNG_STT_MODEL: str = Field(min_length=1)
    SLNG_STT_LANGUAGE: str = Field(min_length=1)
    SLNG_TTS_MODEL: str = Field(min_length=1)
    SLNG_TTS_VOICE: str = Field(min_length=1)
    SLNG_TTS_LANGUAGE: str = Field(min_length=1)

    def describe(self) -> str:
        return f'stt {self.SLNG_STT_MODEL}, tts {self.SLNG_TTS_MODEL}, host {self.SLNG_BASE_URL}'


class Simulation(BaseModel):
    """One simulated call."""

    simulation_id: str
    status: str
    error_message: str | None = None
    transcript: list[Message] = []
    call_sid: str | None = None
    """The Twilio call ID, which is the call's Langfuse session: how `load_report.py` finds its traces."""

    @property
    def agent_spoke(self) -> bool:
        """Whether the agent said anything at all during the call."""
        return any(m.role not in ('user', 'system', 'tool') and m.content for m in self.transcript)

    @property
    def fallbacks(self) -> list[str]:
        """The harness's own recovery lines the agent said: each one is a turn that went wrong.

        Matched on a fragment of each fixed line in `core/turn.py` (`STUMBLE`,
        `EXHAUSTED`, `UNBACKED`, `WITHHELD`), because Coval's transcript is its
        own transcription of the audio and rarely matches the text word for word.
        """
        spoken = ' '.join(
            m.content or '' for m in self.transcript if m.role not in ('user', 'system', 'tool')
        )
        return [line for line, fragment in FALLBACKS.items() if fragment in spoken.lower()]


class MetricValue(BaseModel):
    """One metric's answer for one call."""

    simulation_output_id: str
    value: Any = None


class MetricResult(BaseModel):
    """One metric's answers across the run."""

    metric_name: str
    values: list[MetricValue] = []


class RunResults(BaseModel):
    """The metric answers of a finished run, keyed by metric ID."""

    metrics: dict[str, MetricResult] = {}


class Run(BaseModel):
    """A Coval run: every call placed for one agent and one test set."""

    run_id: str
    status: str
    error_status: str | None = None
    results: RunResults | None = None

    @property
    def finished(self) -> bool:
        """Whether Coval has stopped working on this run."""
        return self.status in FINISHED

    def problems(self, simulations: list[Simulation], composite: Collection[str] = ()) -> list[str]:
        """Every reason this run fails the check. Empty means it passed.

        The run's own status is not enough: a run whose every call failed can
        still end COMPLETED. A metric is not enough either: a criterion like "did
        not ask twice" passes on a call where the agent said nothing. So each
        call has to have finished, and the agent has to have spoken in it.

        A yes/no metric fails the check when it answers NO. Write each one so
        that YES means the agent did the right thing. A composite metric scores
        the fraction of a test case's expected behaviors met, so it fails below 1.
        Other numbers, such as a turn count, are not judged.

        Args:
            simulations: The run's calls, each with its transcript.
            composite: The IDs of the run's composite metrics.

        Returns:
            One line per problem, in the order they were found.
        """
        problems = []
        if self.status != 'COMPLETED':
            problems.append(f'run ended {self.status}')
        if self.error_status not in (None, '', 'SUCCESS'):
            problems.append(f'run error: {self.error_status}')
        if not simulations:
            problems.append('the run has no simulated calls')
        for sim in simulations:
            if sim.status != 'COMPLETED':
                problems.append(
                    f'call {sim.simulation_id} {sim.status}: {sim.error_message or "no error message"}'
                )
            elif not sim.agent_spoke:
                problems.append(f'call {sim.simulation_id}: the agent never spoke')
            problems += [
                f'call {sim.simulation_id}: the harness said its {line} line' for line in sim.fallbacks
            ]
        metrics = self.results.metrics.items() if self.results else ()
        for metric_id, metric in metrics:
            for answer in metric.values:
                call = f'call {answer.simulation_output_id}: {metric.metric_name}'
                if str(answer.value).upper() == 'NO':
                    problems.append(f'{call} answered NO')
                elif metric_id in composite and isinstance(answer.value, int | float) and answer.value < 1:
                    problems.append(f'{call} scored {answer.value:.2f}')
        return problems
