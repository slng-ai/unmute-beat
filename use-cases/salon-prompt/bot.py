"""The salon as one prompt on Pipecat's own Gemini service: the baseline salon-graph is measured against.

The same pipeline as salon-graph's `bot.py`: STT, VAD, smart-turn, TTS and
the transport. What stands between the aggregators and TTS is Pipecat's
`GoogleLLMService` with one system prompt and the diary's tools
(`salon/baseline.py`), not the harness.

Load .env before importing the services: they construct clients at import
time, and those clients must see the configured credentials. The `.env` at
the repository root is found by walking up from this folder.

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
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
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

from salon import baseline
from salon.diary import Diary
from utils.observability import setup

NAME = 'salon call'
"""The same trace name as salon-graph's, so the ledger reads both builds' calls alike."""
SLNG_HOST = os.getenv('SLNG_BASE_URL', 'eu-north.api.slng.ai')
DIARY = Diary()
"""One salon per process: a booking made on one call is there when the next one rings."""
DIARY_PER_CALL = bool(os.getenv('DIARY_PER_CALL'))
"""Give every call a fresh diary instead. Coval's simulated calls run side by side with one
phone number, so a shared diary lets one test case's booking change the next case's path."""
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
    """STT and TTS over SLNG, one key for both, set up exactly as salon-graph sets them up.

    TTS aggregates by TOKEN rather than Pipecat's default SENTENCE, which holds
    every chunk until a sentence boundary before synthesising any of it.
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


def pipeline_worker(pipeline: Pipeline, session_id: str) -> PipelineWorker:
    """The worker, with tracing on, so the baseline's calls land in Langfuse like the graph's."""
    return PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        conversation_id=session_id or None,
        enable_tracing=True,
        enable_turn_tracking=True,
    )


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    """One call: the model's own context holds its tools, and Pipecat's aggregators keep its history.

    On a phone route the session is the carrier's call id (Twilio's
    `callSid`), so a call and its trace share one key.
    """
    call_data = getattr(runner_args, 'call_data', None)
    session_id = (call_data.call_id if call_data else '') or runner_args.session_id or ''
    setup(session=session_id, trace_name=NAME)
    stt, tts = speech_services()
    context, llm = baseline.node(Diary() if DIARY_PER_CALL else DIARY, session_id or 'call')
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer())
    )
    pipeline = Pipeline(
        [transport.input(), stt, user_aggregator, llm, tts, transport.output(), assistant_aggregator]
    )
    worker = pipeline_worker(pipeline, session_id)

    async def greet() -> None:
        """The fixed opening line, spoken as is; Pipecat adds it to the context."""
        await worker.queue_frames([TTSSpeakFrame(baseline.GREETING)])

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
        logger.info('client disconnected')
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments) -> None:
    transport = await create_transport(runner_args, transport_params)  # ty: ignore[invalid-argument-type]
    await run_bot(transport, runner_args)


if __name__ == '__main__':
    from pipecat.runner.run import main

    main()
