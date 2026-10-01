"""The salon on declarative Pipecat flows, as its own voice bot.

The same pipeline as the salon's `bot.py` (SLNG STT and TTS, Silero VAD
with smart turn, the same transports and tracing), with Pipecat's LLM
service and `FlowManager` where the harness stands:

    transport.input() -> STT -> user aggregator -> Gemini -> ClaimFilter -> EmptyReplyGuard -> TTS -> transport.output()

Load .env before importing the services: they construct clients at import
time. The `.env` at the repository root is found by walking up from this folder.

    uv run bot.py

Then open http://localhost:7860 and talk. `--port` picks another port;
`-t twilio -x <host>` answers phone calls, as the salon's `bot.py` does.
"""

# ruff: noqa: E402
import os

from dotenv import load_dotenv

load_dotenv(override=True)

from google import genai
from google.genai.types import HttpOptions
from loguru import logger
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments, WebSocketRunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.tts_service import TextAggregationMode
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner
from pipecat_slng import SlngSTTService, SlngTTSService

from graph_salon import GraphSalon
from salon.diary import Diary
from utils.observability import setup

MODEL = 'gemini-3.1-flash-lite'
"""Not the harness's 3.5 flash lite: Pipecat's Gemini service has problems with it."""
SLNG_HOST = os.getenv('SLNG_BASE_URL', 'eu-north.api.slng.ai')
TTS_HOST = os.getenv('SLNG_TTS_BASE_URL', 'eu-west.api.slng.ai')
"""Fish Audio is served from eu-west, in Germany. STT stays on `SLNG_HOST`."""
TRACE_NAME = 'salon call'
"""The harness's trace name, so the ledger (`concierge-tools/ledger.py`) and Langfuse filters find these calls the same way."""
TURN_STOP_SECS = 1.5
"""How long a silence ends a turn the smart-turn model judged unfinished. Pipecat's default is 3.

In Langfuse traces 1e5494cc and ad8db194, a hesitant sentence ("I. Said
haircut.") waited the full 3 s before the model ran. Lower is quicker, but a
caller who pauses longer mid-sentence is cut off, and this build does not
join the two pieces into one turn the way the harness does.

ponytail: one fixed value; raise it if calls show callers cut off mid-sentence.
"""

transport_params = {
    'webrtc': lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    'daily': lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    'twilio': lambda: FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True),
}


class VertexExpress(GoogleLLMService):
    """Pipecat's Gemini service on the Vertex AI Express-Mode key the harness uses.

    ponytail: Pipecat builds a Gemini API client from the key; this key is
    Vertex's, so the client is built for Vertex instead. Nothing else changes.
    """

    def create_client(self):
        self._client = genai.Client(vertexai=True, api_key=self._api_key, http_options=self._http_options)


def gemini() -> VertexExpress:
    """The flow's model, thinking pinned to minimal. No system instruction: the first node sets it."""
    return VertexExpress(
        api_key=os.environ['GOOGLE_API_KEY'],
        settings=GoogleLLMService.Settings(
            model=MODEL, thinking=GoogleLLMService.ThinkingConfig(thinking_level='minimal')
        ),
        http_options=HttpOptions(headers={'X-Vertex-AI-LLM-Shared-Request-Type': 'priority'}),
    )


def speech_services() -> tuple[SlngSTTService, SlngTTSService]:
    """STT and TTS over SLNG, as the salon's `bot.py` sets them up: TTS by token, not by sentence."""
    key = os.environ['SLNG_API_KEY']
    stt = SlngSTTService(
        api_key=key,
        base_url=SLNG_HOST,
        model=os.getenv('SLNG_STT_MODEL', 'soniox/speech-ai:rt-v5'),
        language=Language('en'),
    )
    tts = SlngTTSService(
        api_key=key,
        base_url=TTS_HOST,
        model=os.getenv('SLNG_TTS_MODEL', 'slng/fish/tts:s2.1-pro'),
        voice=os.getenv('SLNG_TTS_VOICE', '1258154bb41540c7b9dc0d10cd6704bf'),
        language=Language('en'),
        warm_standby_enabled=True,
        text_aggregation_mode=TextAggregationMode.TOKEN,
    )
    return stt, tts


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    """One call: a fresh diary, the flow, and the pipeline around it, run until the line drops.

    On a phone route the session is the carrier's call id, so a call and its
    trace share one key, as in the salon's `bot.py`.
    """
    call_data = getattr(runner_args, 'call_data', None)
    session_id = (call_data.call_id if call_data else '') or runner_args.session_id or ''
    setup(session=session_id, trace_name=TRACE_NAME)
    stt, tts = speech_services()
    salon = GraphSalon(Diary(), session_id or 'call', gemini(), caller=os.getenv('CALLER_ID', ''))
    turns = UserTurnStrategies(
        stop=[
            TurnAnalyzerUserTurnStopStrategy(
                turn_analyzer=LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=TURN_STOP_SECS))
            )
        ]
    )
    pair = LLMContextAggregatorPair(
        salon.context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer(), user_turn_strategies=turns),
    )
    user_aggregator, assistant_aggregator = pair
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            salon.llm,
            *salon.processors,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        conversation_id=session_id or None,
        enable_tracing=True,
        enable_turn_tracking=True,
    )
    salon.attach(worker, pair)

    @worker.rtvi.event_handler('on_client_ready')
    async def on_client_ready(rtvi):
        await salon.start()

    @transport.event_handler('on_client_connected')
    async def on_client_connected(transport, client):
        """A phone websocket has no RTVI client to say it is ready, so the flow starts here."""
        logger.info('client connected')
        if isinstance(runner_args, WebSocketRunnerArguments):
            await salon.start()

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
