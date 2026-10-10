"""Wasla as a Pydantic AI agency in the LLM node of a Pipecat pipeline.

Pipecat owns the transport, VAD, turn taking, STT and TTS; Wasla's agent nodes own the conversation,
compiled from `wasla/agent.yaml` (`wasla.app`). The pipeline is core's (`core.launch.run_call`), the
same for every use case; what is Wasla's own is in `wasla/entry.py`.

Each call is about one account of the fake ledger, `ACCOUNT_ID` (default `WA-1001`, Layla Haddad):
Wasla rings the holder, so the bot speaks first.

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
from wasla.entry import ENTRY


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments, **options) -> None:
    """One call, until the line drops (`core.launch.run_call`); a test passes its own `flow` and `speech`."""
    await run_call(ENTRY, transport, runner_args, **options)


async def bot(runner_args: RunnerArguments) -> None:
    """Pipecat's runner entry point: the transport it was started with, then one call."""
    transport = await create_transport(runner_args, transport_params)  # ty: ignore[invalid-argument-type]
    await run_bot(transport, runner_args)


if __name__ == '__main__':
    serve()
