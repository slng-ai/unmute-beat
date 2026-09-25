"""The salon as a Pydantic AI agency in the LLM node of a Pipecat pipeline.

Pipecat owns the transport, VAD, turn taking, STT and TTS. The agents own the
conversation: prompts, tools, typed state and which step is running. This file
only wires the pipeline; the seam between the two is one frame processor,
`PydanticAgentProcessor`.

Load .env before importing the services and agents: they construct clients
at import time, and those clients must see the configured credentials. The
`.env` at the repository root is found by walking up from this folder.

    uv run bot.py

Then open http://localhost:7860 and talk.
"""

from __future__ import annotations

# ruff: noqa: E402
import os

from dotenv import load_dotenv

load_dotenv(override=True)

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMFullResponseStartFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments, WebSocketRunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.tts_service import TextAggregationMode
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.workers.runner import WorkerRunner
from pipecat_slng import SlngSTTService, SlngTTSService

from core.graph import model_clients
from core.observability import setup
from core.processor import PydanticAgentProcessor
from salon.agents import AGENTS, GREETING
from salon.diary import Diary
from salon.models.state import open_call

NAME = 'salon call'
SLNG_HOST = os.getenv('SLNG_BASE_URL', 'eu-north.api.slng.ai')
DIARY = Diary()
"""One salon per process: a booking made on one call is there when the next one rings."""
DIARY_PER_CALL = bool(os.getenv('DIARY_PER_CALL'))
"""Give every call a fresh diary instead. Coval's simulated calls run side by side with one
phone number, so a shared diary lets one test case's booking change the next case's path."""
FINISH_SECONDS = 8.0
"""How long a turn already running may go on after the caller hangs up: a reading and a write fit in it.

ponytail: a fixed bound. Take it from the turn budget's own deadline if a
slower model or tool ever needs longer.
"""
STT_LANGUAGE = os.getenv('SLNG_STT_LANGUAGE', 'en')
TTS_LANGUAGE = os.getenv('SLNG_TTS_LANGUAGE', 'en')
"""One language per call for each speech service."""

transport_params = {
    'webrtc': lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    'daily': lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    'twilio': lambda: FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True),
}
"""`twilio` is the route Coval's simulated calls take (`concierge-tools/coval_sim`); the runner adds the serializer."""


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
        language=Language(STT_LANGUAGE),
    )
    tts = SlngTTSService(
        api_key=key,
        base_url=SLNG_HOST,
        model=os.getenv('SLNG_TTS_MODEL', 'cartesia/sonic:3.5'),
        voice=os.getenv('SLNG_TTS_VOICE', '62ae83ad-4f6a-430b-af41-a9bede9286ca'),
        language=Language(TTS_LANGUAGE),
        warm_standby_enabled=True,
        text_aggregation_mode=TextAggregationMode.TOKEN,
    )
    return stt, tts


def aggregators(context: LLMContext) -> LLMContextAggregatorPair:
    """The pair that decides when the caller has finished and records what the bot said.

    Pipecat's recommended turn taking, unchanged: Silero VAD, and its default
    strategies, which end a turn with the smart-turn v3 model on its default
    parameters. When the caller goes on after a turn ended, the harness joins
    the pieces (`CallContext.continues_turn`); how the turn ends is Pipecat's.

    ponytail: the defaults are what Pipecat measured. Tune end-of-turn timing
    later, in the stop strategy as Pipecat recommends, from traces.
    """
    return LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer())
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


def playback(agents: PydanticAgentProcessor, assistant_aggregator) -> None:
    """Tell the harness what the caller actually heard: which run each response came from, and where it was cut."""
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


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    """One call: build the pipeline around the agency and run it until the line drops.

    Pipecat's `LLMContext` is kept even though no model reads it: the
    aggregators use it to work out the caller's turn and, on a barge-in, what
    the bot managed to say. `note_spoken` is its only consumer. The caller ID
    is the carrier's on a phone route and a guess until the caller agrees.

    On a phone route the session is the carrier's call id (Twilio's
    `callSid`), so a call and its trace share one key: Coval records the same
    id as the simulation's `call_sid`, and one Langfuse session is one call.
    """
    call_data = getattr(runner_args, 'call_data', None)
    session_id = (call_data.call_id if call_data else '') or runner_args.session_id or ''
    setup(session=session_id, trace_name=NAME)
    stt, tts = speech_services()
    call = open_call(os.getenv('CALLER_ID', ''), diary=Diary() if DIARY_PER_CALL else DIARY)
    context, node = LLMContext(), PydanticAgentProcessor(call, AGENTS)
    user_aggregator, assistant_aggregator = aggregators(context)
    pipeline = Pipeline(
        [transport.input(), stt, user_aggregator, node, tts, transport.output(), assistant_aggregator]
    )
    worker = pipeline_worker(pipeline, session_id)
    playback(node, assistant_aggregator)

    async def greet() -> None:
        """The fixed opening line: the harness records it as said, Pipecat adds it to the context."""
        await node.greet(GREETING)

    @worker.rtvi.event_handler('on_client_ready')
    async def on_client_ready(rtvi):
        await greet()

    @transport.event_handler('on_client_connected')
    async def on_client_connected(transport, client):
        """A phone websocket has no RTVI client to say it is ready, so the greeting goes out here."""
        logger.info('client connected')
        if isinstance(runner_args, WebSocketRunnerArguments):
            await greet()

    @transport.event_handler('on_client_disconnected')
    async def on_client_disconnected(transport, client):
        """Let a turn already running finish its write, for a few seconds, before the line is torn down."""
        logger.info('client disconnected')
        await node.finish_turn(FINISH_SECONDS)
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    async with model_clients(AGENTS, warm=True):
        await runner.run()


async def bot(runner_args: RunnerArguments) -> None:
    transport = await create_transport(runner_args, transport_params)  # ty: ignore[invalid-argument-type]
    await run_bot(transport, runner_args)


if __name__ == '__main__':
    from pipecat.runner.run import main

    main()
