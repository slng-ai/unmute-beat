"""The salon as a Pydantic AI agency in the LLM node of a Pipecat pipeline.

Pipecat owns the transport, VAD, turn taking, STT and TTS; the salon's agent nodes own the conversation,
compiled from `salon/agent.yaml` (`salon.app`). The pipeline is core's (`core.launch.run_call`), the
same for every use case; what is the salon's own is in `salon/entry.py`.

Load .env before importing the agents: they construct clients at import time, and those clients must
see the configured credentials. The `.env` at the repository root is found by walking up from here.

    uv run bot.py

Then open http://localhost:7860 and talk.

`FLOW`: The compiled flow every call of this process shares (`salon.app.FLOW`): `server.py` reads it
here.
"""

# ruff: noqa: E402
from dotenv import load_dotenv

load_dotenv(override=True)

from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.transports.base_transport import BaseTransport

from core.launch import run_call, serve, transport_params
from salon.entry import DIARY, ENTRY

FLOW = ENTRY.flow()


async def run_bot(
    transport: BaseTransport, runner_args: RunnerArguments, *, from_number: str | None = None, **options
) -> None:
    """One call, until the line drops (`core.launch.run_call`).

    `server.py` passes `warm=False`, its own `flow`, the caller's number from the carrier's webhook
    (`from_number`) and `settle`; a test passes its own `speech`.
    """
    await run_call(ENTRY, transport, runner_args, caller_id=from_number, **options)


__all__ = ['DIARY', 'ENTRY', 'FLOW', 'bot', 'run_bot', 'transport_params']


async def bot(runner_args: RunnerArguments) -> None:
    """Pipecat's runner entry point: the transport it was started with, then one call."""
    transport = await create_transport(runner_args, transport_params)  # ty: ignore[invalid-argument-type]
    await run_bot(transport, runner_args)


if __name__ == '__main__':
    serve()
