"""What a use case tells a launcher about itself: its flow, how to open a call, what to say first.

Every launcher (a use case's `bot.py`, a runner, a test) runs the same call
(`core.launch.run_call` for voice, `core.text_turn` for text) and needs the same five or six facts
about the application. Each use case says them once, in its package's `entry.py`:

    ENTRY = core.Entry(
        id=SPEC.id,                                   # the agent's id: on every span
        name='salon call',                            # what its traces are listed by
        flow=lambda: app.FLOW,                        # compiled on first use
        open=lambda flow, opening: app.open_call(flow, opening.caller_id, diary=DIARY, key=opening.key),
        greeting=lambda flow, call: GREETING,
    )

Imports no Pipecat: a text launcher reads it too.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from core.flowspec import FlowSpec
    from core.state import Question
    from core.steps import FlowCall, StepFlow


@dataclass(frozen=True)
class Opening:
    """What a launcher knows about a call as it opens it.

    Attributes:
        caller_id: The number the carrier (or the tester) gives: core fills a `core.Phone` identity
            with it.
        key: The call's own key: its session, a carrier's CallSid, or a run id.
        settle: Which earlier calls are over (by key), for an application that settles what they
            still owe.
    """

    caller_id: str = ''
    key: str = ''
    settle: Callable[[str], bool] | None = None


@dataclass(frozen=True)
class Entry:
    """One use case, as a launcher needs it. Nothing here runs at import: `flow` compiles when first asked.

    Attributes:
        id: The agent's id, the manifest's own `id`: stamped on every span (`slng.agent.id`).
        name: What its calls' traces are listed by: `salon call`.
        greeting: The opening line for this call, said by code: a string, or a question its first
            reply answers.
        close: Run once the line has dropped and the last turn finished: Faro closes its session
            here.
        started: Run in a thread of its own as each call opens: Nosh wakes its place tools here.
        tts_language: Each speech service's language, unless `SLNG_STT_LANGUAGE` /
            `SLNG_TTS_LANGUAGE` say otherwise.
        turn_taking: `tuned`: the salon's measured end-of-turn settings. `pipecat`: Pipecat's own
            defaults, unchanged (Faro).
        reminders: Whether a silent caller is asked if they are still there (`core.turn.remind`).
        manifest: Its `agent.yaml`: a long-lived launcher compiles it again when the config changes
            (`recompile`).
        compile: How its manifest is compiled, when not `spec.compile()` as it stands.
    """

    id: str
    name: str
    flow: Callable[[], StepFlow]
    open: Callable[[StepFlow, Opening], FlowCall[Any]]
    greeting: Callable[[StepFlow, FlowCall[Any]], str | Question]
    close: Callable[[FlowCall[Any]], Awaitable[object]] | None = None
    started: Callable[[], None] | None = None
    stt_language: str = 'en'
    tts_language: str = 'en'
    turn_taking: Literal['tuned', 'pipecat'] = 'tuned'
    reminders: bool = True
    manifest: Path | None = None
    compile: Callable[[FlowSpec], StepFlow] | None = None

    def recompile(self) -> StepFlow:
        """The flow compiled again from the manifest as it is on disk now: its prompts and YAML, not its code.

        Raises `ValueError` for an entry that names no manifest.
        """
        from core.flowspec import load_file

        if self.manifest is None:
            raise ValueError(f'{self.id} names no manifest to compile again')
        spec = load_file(self.manifest)
        return self.compile(spec) if self.compile is not None else spec.compile()
