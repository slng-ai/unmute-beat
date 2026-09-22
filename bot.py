"""slng-voice-harness -- a pydantic-ai agent as the LLM node of a Pipecat pipeline.

Pipecat owns the transport, VAD, turn taking, STT and TTS. The agents own the
conversation: prompts, tools, typed state and which step is running. The seam
between them is one processor, `voice/processor.py`.

    uv run --env-file .env python bot.py

Then open http://localhost:7860/client and talk.
"""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import asdict
from datetime import UTC, datetime

from dotenv import load_dotenv
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
from pydantic_ai.messages import ModelMessagesTypeAdapter

from utils.arm import settings
from utils.observability import setup
from voice.harness import model_clients
from voice.processor import PydanticAgentProcessor

load_dotenv(override=True)


def agency():
    """Which set of agents takes the call. `HARNESS=voice` for the small one."""
    if os.getenv('HARNESS', 'salon') == 'voice':
        from voice.agents import AGENTS, GREETING
        from voice.state import Call

        return Call(), AGENTS, GREETING

    from salon.agents import AGENTS, GREETING
    from salon.state import open_call

    # The carrier's caller ID on a phone route; a guess until the caller agrees.
    return open_call(os.getenv('CALLER_ID', '')), AGENTS, GREETING


SLNG_HOST = os.getenv('SLNG_BASE_URL', 'eu-north.api.slng.ai')

transport_params = {
    'webrtc': lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    'daily': lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
}


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    setup()  # Langfuse: the agents' spans, one per agent per turn

    from salon import diary

    if pinned := os.getenv('SALON_CLOCK'):
        # The salon's clock, for a call that has to be comparable with another
        # one. "Monday" and "tomorrow" resolve against it, so the same words
        # spoken on two different days book two different appointments and
        # neither run can state what the right answer was. Format:
        # SALON_CLOCK=2026-09-21T10:00, in the salon's own timezone.
        when = datetime.fromisoformat(pinned)
        diary.set_clock(when if when.tzinfo else when.replace(tzinfo=diary.TZ))
        logger.info('salon clock pinned to {}', diary.now().isoformat())

    # Emptying the salon is its own decision, named in its own variable.
    #
    # `fresh` is what a scored benchmark run wants: every call starts from the
    # same fixture, so two of them are comparable. `continue` is what a real
    # salon does and what a deliberate two-call scenario needs -- N1 books,
    # N3 rings back and the appointment is still there.
    #
    # It used to hang off `SALON_CLOCK`, which conflated two things: a pinned
    # clock makes two calls *comparable* and emptying the diary makes the
    # second one *destroy* the first. On 2026-09-21 a run without the reset
    # had the second call inherit the first's booking, and the fix wired the
    # reset to the clock -- which then meant a pinned call could delete an
    # in-flight call's records. Both modes are legitimate; neither is a side
    # effect of what time it is.
    fixture = os.getenv('SALON_FIXTURE', 'continue')
    if fixture not in ('fresh', 'continue'):
        raise SystemExit(f"SALON_FIXTURE is 'fresh' or 'continue', not {fixture!r}")
    if fixture == 'fresh':
        logger.info('starting this call from an empty salon')
        diary.reset()

    slng_key = os.environ['SLNG_API_KEY']
    stt = SlngSTTService(
        api_key=slng_key,
        base_url=SLNG_HOST,
        model='soniox/speech-ai:rt-v5',
        language=Language('en'),
    )

    tts = SlngTTSService(
        api_key=slng_key,
        base_url=SLNG_HOST,
        model=os.getenv('SLNG_TTS_MODEL', 'cartesia/sonic:3.5'),
        voice=os.getenv('SLNG_TTS_VOICE', '62ae83ad-4f6a-430b-af41-a9bede9286ca'),
        language=Language('en'),
        warm_standby_enabled=True,
        # Pipecat's default is SENTENCE, which holds every chunk until a
        # sentence boundary before synthesising any of it -- 200-300ms by its
        # own docstring, paid on the first word of every turn. The model streams
        # tokens and SLNG accepts them, so there is nothing to buffer for.
        text_aggregation_mode=TextAggregationMode.TOKEN,
    )

    call, table, greeting = agency()
    starting_settings = settings()
    agents = PydanticAgentProcessor(call, table)

    # Kept even though no model ever reads it: the aggregators use it to work
    # out the caller's turn and, on a barge-in, what the bot actually managed to
    # say. `note_spoken` below is the only consumer.
    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            # Pipecat's default stop strategy is this same analyser with
            # `stop_secs=3`. The analyser is binary: it says COMPLETE and the
            # agent starts at once, or it says INCOMPLETE and the caller waits
            # the whole fallback out. There is no middle, so that 3 is not a
            # tuning knob for "how long to wait", it is the entire price of the
            # model being unsure. Measured on the call of 2026-09-20: turns it
            # judged cleanly came back in 1.16-1.85s, and the two it did not
            # cost 5.57s and 4.53s.
            #
            # 1.5s of silence is already a long pause in a booking. Lower cuts
            # a trailing caller off sooner, so it is a dial to measure rather
            # than a setting to get right once -- `SMART_TURN_STOP_SECS`.
            user_turn_strategies=UserTurnStrategies(
                stop=[
                    TurnAnalyzerUserTurnStopStrategy(
                        turn_analyzer=LocalSmartTurnAnalyzerV3(
                            params=SmartTurnParams(stop_secs=float(os.getenv('SMART_TURN_STOP_SECS', '1.5')))
                        )
                    )
                ]
            ),
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            agents,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    # `enable_tracing` is what makes Pipecat open the conversation and turn spans
    # the agents' own spans hang from -- without it the only spans in Langfuse are
    # pydantic-ai's, one loose trace per agent run.
    session_id = runner_args.session_id or ''
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        conversation_id=runner_args.session_id,
        enable_tracing=True,
        enable_turn_tracking=True,
        additional_span_attributes={
            # Both are read by Langfuse off the root span: one groups calls into
            # a session, the other names the trace something better than
            # "conversation".
            'session.id': session_id,
            'langfuse.trace.name': f'{os.getenv("HARNESS", "salon")} call',
        },
    )

    playback_run = None

    @assistant_aggregator.event_handler('on_before_process_frame')
    async def before_assistant_frame(aggregator, frame):
        nonlocal playback_run
        if isinstance(frame, LLMFullResponseStartFrame):
            playback_run = frame.metadata.get('voice_run')

    @assistant_aggregator.event_handler('on_assistant_turn_started')
    async def on_assistant_turn_started(aggregator):
        nonlocal playback_run
        agents.playback_started(aggregator._assistant_turn_start_timestamp, run=playback_run)
        playback_run = None

    @assistant_aggregator.event_handler('on_assistant_turn_stopped')
    async def on_assistant_turn_stopped(aggregator, message):
        # Cut the agents' history down to what the caller heard.
        agents.note_spoken(message.content, message.interrupted, timestamp=message.timestamp)

    @worker.rtvi.event_handler('on_client_ready')
    async def on_client_ready(rtvi):
        await agents.greet(greeting)

    @transport.event_handler('on_client_connected')
    async def on_client_connected(transport, client):
        logger.info('client connected')

    @transport.event_handler('on_client_disconnected')
    async def on_client_disconnected(transport, client):
        logger.info('client disconnected')
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    # When this call began, as distinct from when the process did and from
    # when the record was written. Three moments the record used to carry one
    # number for.
    started_at = datetime.now(UTC).isoformat()
    try:
        async with model_clients(table):
            await runner.run()
    finally:
        _record(call, session_id, started_at, starting_settings)


def _record(call, session_id: str, started_at: str = '', starting_settings: dict | None = None) -> None:
    """Write down what this call was and what the salon ended up holding.

    The diary is in memory and dies with the process, so a live call
    otherwise leaves a trace full of timings and nothing that can answer
    whether it booked the right thing. A benchmark that can only score what
    it can see scores the wrong thing quickly.

    Written beside the scripted runs in `bench/raw/`, in the same shape, so
    `bench.turns --session-id` covers the timing half and this covers the
    correctness half.
    """
    try:
        from utils.arm import outcome

        record = {
            'arm': os.getenv('BENCH_LABEL', 'live'),
            'session': session_id,
            'settings': starting_settings or settings(),
            'outcome': outcome(getattr(call.state, 'customer_phone', '')),
            # JSON mode, so a day is the ISO string the record is read back as
            # rather than a `date` nothing outside Python can load.
            'state': call.state.model_dump(mode='json'),
            # What the caller and the agency actually said, kept apart from
            # the model and tool protocol: a trace full of timings cannot be
            # asked whether the right thing was booked, and neither can a
            # message history that does not hold the fixed lines.
            'transcript': call.transcript(),
            'speech': [asdict(segment) for segment in call.all_spoken],
            'protocol': ModelMessagesTypeAdapter.dump_python(call._protocol, mode='json'),
            'turns': call.turn,
            'call_started_at': started_at,
            'ended_at': datetime.now(UTC).isoformat(),
            'ended': call.ended or 'unknown',
        }
        path = pathlib.Path('bench/raw') / f'live-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=1) + '\n')
        logger.info('call recorded -> {}', path)
    except Exception:
        # A call that ended is not a call that failed. Never let the
        # bookkeeping be the thing that raises on the way out.
        logger.exception('could not record the call')


async def bot(runner_args: RunnerArguments) -> None:
    # ty: Pipecat's own parameter type is looser than the table it is given.
    transport = await create_transport(runner_args, transport_params)  # ty: ignore[invalid-argument-type]
    await run_bot(transport, runner_args)


if __name__ == '__main__':
    from pipecat.runner.run import main

    main()
