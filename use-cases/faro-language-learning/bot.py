"""Faro, the language-learning companion, as a Pydantic AI agency in the LLM node of a Pipecat pipeline.

Pipecat owns the transport, VAD, turn taking, STT and TTS; the agents own the conversation: prompts,
tools, typed state and which step is running. The pipeline is core's (`core.launch.run_call`), the same
for every use case; what is Faro's own is in `faro/entry.py`.

Faro reads the caller ID back in its greeting, closes the learner's session when the line drops, and
keeps its own speech languages (`SLNG_STT_LANGUAGE`, `SLNG_TTS_LANGUAGE` override them).

Load .env before importing the agents: they construct clients at import time, and those clients must
see the configured credentials. The `.env` at the repository root is found by walking up from here.

    uv run bot.py

Then open http://localhost:7860 and talk.
"""

# ruff: noqa: E402
from dotenv import load_dotenv

load_dotenv(override=True)

from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.transports.base_transport import BaseTransport

from core.launch import run_call, serve, transport_params
from faro.entry import ENTRY


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments, **options) -> None:
    """One call, until the line drops (`core.launch.run_call`); a test passes its own `flow` and `speech`."""
    await run_call(ENTRY, transport, runner_args, **options)


async def bot(runner_args: RunnerArguments) -> None:
    transport = await create_transport(runner_args, transport_params)  # ty: ignore[invalid-argument-type]
    await run_bot(transport, runner_args)


if __name__ == '__main__':
    serve()
