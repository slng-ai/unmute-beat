"""A Pydantic AI agency as the LLM node of a Pipecat pipeline.

Pipecat owns the transport, VAD, turn taking, STT and TTS. The agents own the
conversation: prompts, tools, typed state and which step is running. This file
only wires the pipeline; the seam between the two is one frame processor,
`PydanticAgentProcessor`.

Load .env before importing the services and agents: they construct clients
at import time, and those clients must see the configured credentials.

    uv run --env-file .env python bot.py

Then open http://localhost:7860 and talk.
"""

from __future__ import annotations

# ruff: noqa: E402
import os

from dotenv import load_dotenv

load_dotenv(override=True)

from loguru import logger
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMFullResponseStartFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.tts_service import TextAggregationMode
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner
from pipecat_slng import SlngSTTService, SlngTTSService

from core.graph import model_clients
from core.processor import PydanticAgentProcessor
from salon.agents import AGENTS, GREETING
from salon.diary import Diary
from salon.models.state import open_call
from utils.observability import setup

SLNG_HOST = os.getenv('SLNG_BASE_URL', 'eu-north.api.slng.ai')
DIARY = Diary()
"""One salon per process: a booking made on one call is there when the next one rings."""

transport_params = {
    'webrtc': lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    'daily': lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
}


def speech_services() -> tuple[SlngSTTService, SlngTTSService]:
    """STT and TTS over SLNG, one key for both.

    TTS aggregates by TOKEN rather than Pipecat's default SENTENCE, which holds
    every chunk until a sentence boundary before synthesising any of it: two to
    three hundred milliseconds by its own docstring, paid on the first word of
    every turn. The model streams tokens and SLNG accepts them, so there is
    nothing to buffer for.
    """
    key = os.environ['SLNG_API_KEY']
    stt = SlngSTTService(
        api_key=key,
        base_url=SLNG_HOST,
        model=os.getenv('SLNG_STT_MODEL', 'soniox/speech-ai:rt-v5'),
        language=Language('en'),
    )
    tts = SlngTTSService(
        api_key=key,
        base_url=SLNG_HOST,
        model=os.getenv('SLNG_TTS_MODEL', 'cartesia/sonic:3.5'),
        voice=os.getenv('SLNG_TTS_VOICE', '62ae83ad-4f6a-430b-af41-a9bede9286ca'),
        language=Language('en'),
        warm_standby_enabled=True,
        text_aggregation_mode=TextAggregationMode.TOKEN,
    )
    return stt, tts


def aggregators(context: LLMContext) -> LLMContextAggregatorPair:
    """The pair that decides when the caller has finished and records what the bot said.

    The smart-turn analyser is binary: it says COMPLETE and the agent starts at
    once, or INCOMPLETE and the caller waits the whole fallback out. Pipecat's
    default fallback is three seconds, which is the entire price of the model
    being unsure. Turns judged cleanly on the live call of 2026-09-20 came back
    in 1.2 to 1.9 seconds; the two it did not cost 5.6 and 4.5. A second and a
    half is already a long pause in a booking, and `SMART_TURN_STOP_SECS` is a
    dial to measure rather than a setting to get right once.
    """
    stop_secs = float(os.getenv('SMART_TURN_STOP_SECS', '1.5'))
    return LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=UserTurnStrategies(
                stop=[
                    TurnAnalyzerUserTurnStopStrategy(
                        turn_analyzer=LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=stop_secs))
                    )
                ]
            ),
        ),
    )


def pipeline_worker(pipeline: Pipeline, session_id: str) -> PipelineWorker:
    """The worker, with tracing on.

    `enable_tracing` is what makes Pipecat open the conversation and turn spans
    the agents' own spans hang from; without it the only spans in Langfuse are
    Pydantic AI's, one loose trace per agent run. The session and the trace name
    are `setup()`'s to place, because Pipecat would put them on the root span
    alone and Langfuse v4 queries observations.
    """
    return PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        conversation_id=session_id or None,
        enable_tracing=True,
        enable_turn_tracking=True,
    )


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    """One call: build the pipeline around the agency and run it until the line drops.

    Pipecat's `LLMContext` is kept even though no model reads it: the
    aggregators use it to work out the caller's turn and, on a barge-in, what
    the bot managed to say. `note_spoken` is its only consumer. The caller ID
    is the carrier's on a phone route and a guess until the caller agrees.
    """
    session_id = runner_args.session_id or ''
    setup(session=session_id, trace_name='salon call')
    stt, tts = speech_services()
    call = open_call(os.getenv('CALLER_ID', ''), diary=DIARY)
    agents = PydanticAgentProcessor(call, AGENTS)
    user_aggregator, assistant_aggregator = aggregators(LLMContext())
    pipeline = Pipeline(
        [transport.input(), stt, user_aggregator, agents, tts, transport.output(), assistant_aggregator]
    )
    worker = pipeline_worker(pipeline, session_id)
    playback_run: tuple[int, int] | None = None

    @assistant_aggregator.event_handler('on_before_process_frame')
    async def before_assistant_frame(aggregator, frame):
        """Remember which agency run a response frame belongs to, for the playback callback."""
        nonlocal playback_run
        if isinstance(frame, LLMFullResponseStartFrame):
            playback_run = frame.metadata.get('voice_run')

    @assistant_aggregator.event_handler('on_assistant_turn_started')
    async def on_assistant_turn_started(aggregator):
        """Bind the speech about to play to the run that produced it."""
        nonlocal playback_run
        agents.playback_started(aggregator._assistant_turn_start_timestamp, run=playback_run)
        playback_run = None

    @assistant_aggregator.event_handler('on_assistant_turn_stopped')
    async def on_assistant_turn_stopped(aggregator, message):
        """Cut the agents' record down to what the caller actually heard."""
        agents.note_spoken(message.content, message.interrupted, timestamp=message.timestamp)

    @worker.rtvi.event_handler('on_client_ready')
    async def on_client_ready(rtvi):
        await agents.greet(GREETING)

    @transport.event_handler('on_client_connected')
    async def on_client_connected(transport, client):
        logger.info('client connected')

    @transport.event_handler('on_client_disconnected')
    async def on_client_disconnected(transport, client):
        logger.info('client disconnected')
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    async with model_clients(AGENTS):
        await runner.run()


async def bot(runner_args: RunnerArguments) -> None:
    transport = await create_transport(runner_args, transport_params)  # ty: ignore[invalid-argument-type]
    await run_bot(transport, runner_args)


if __name__ == '__main__':
    from pipecat.runner.run import main

    main()
