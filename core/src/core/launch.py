"""One voice call of any use case: the Pipecat pipeline around a flow, built the same way for every launcher.

Every use case's `bot.py` used to repeat this body and differ in five places: the greeting, the
services, a hook when a call opens and one when it ends, the speech languages. Those five are now
the use case's `entry.py` (`core.Entry`); this module is the rest, and `run_call` is the one function
a `bot.py`, the salon's bounded `server.py` and the studio's runner all call.

    transport.input() -> STT -> user aggregator -> PydanticAgentProcessor -> TTS -> transport.output()

Pipecat owns the transport, VAD, turn taking, STT and TTS; the flow owns the conversation, through
one frame processor (`core.processor.PydanticAgentProcessor`). What each setting is, and why, is on
its function: the speech services, turn taking, the worker, playback, the call ceiling. Settings come
from the environment the launcher loaded (each `bot.py` reads `.env` first), read when a call starts.

`transport_params` gives each transport's audio settings; `twilio` is the route Coval's simulated
calls take (`concierge-tools/coval_sim`), and Pipecat's runner adds its serializer.

The second module of core that imports Pipecat, with `core.processor` (the `voice` extra).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Any, Literal

try:
    from loguru import logger
    from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    from pipecat.frames.frames import LLMFullResponseStartFrame
    from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair,
        LLMUserAggregatorParams,
    )
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.runner.types import RunnerArguments, WebSocketRunnerArguments
    from pipecat.services.tts_service import TextAggregationMode
    from pipecat.transcriptions.language import Language
    from pipecat.transports.base_transport import BaseTransport, TransportParams
    from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
    from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
    from pipecat.turns.user_turn_strategies import UserTurnStrategies
    from pipecat.workers.runner import WorkerRunner
    from pipecat_slng import SlngSTTService, SlngTTSService
except ImportError as missing:
    raise ImportError(
        f'core.launch needs the voice extra: install slng-voice-core[voice] ({missing})'
    ) from missing

from core.entry import Entry, Opening
from core.events import CallListener
from core.graph import model_clients
from core.processor import Ceiling, PydanticAgentProcessor, playout, record_ending, reports_playback
from core.snapshot import TurnSnapshot
from core.steps import StepFlow


class CallTiming:
    """The two waits a call is built with.

    `FINISH_SECONDS` is how long a turn already running may go on after the caller hangs up: a reading
    and a write fit in it. ponytail: a fixed bound; take it from the turn budget's own deadline if a
    slower model or tool ever needs longer.

    `IDLE_SECONDS` is the silence after the bot's last words before the caller is asked if they are
    still there (`core.turn.remind`). At 8 s it talked over Coval's simulated callers, who often answer
    9 to 13 s after a read-back: the reminder said the read-back again, their yes cut into that copy,
    so it was asked again, up to five times (gate 1, BWqBhyLj). The calls that really stalled were
    silent for 70 to 150 s.
    """

    FINISH_SECONDS = 8.0
    IDLE_SECONDS = 20.0


def max_call_seconds() -> float:
    """`MAX_CALL_SECONDS`: end a call this long after the caller connects. 0, the default, never ends one.

    Coval has no per-call duration limit (its API has none, and its polling
    timeout is not one), so a simulated call is capped here, on the transport
    that places the audio. `coval_sim` sets it for every simulated call.

    ponytail: the call is torn down the way a hang-up is, turn finished first,
    so it may run up to `CallTiming.FINISH_SECONDS` longer.
    """
    return float(os.getenv('MAX_CALL_SECONDS') or 0)


def vad_stop_seconds() -> float:
    """`VAD_STOP_SECS`: silence the voice detector waits before smart-turn judges whether the caller is done. 0.3 s by default.

    Session 4 measured both sides of it: at 0.2 s, 30% of model requests were
    cancelled because the caller went on after a pause, and 0.58 runs per caller
    turn answered a piece of a turn already under way; at 0.6 s (8 calls) that
    fell to 13% and 0.29, but the median turn waited 0.55 s longer. 0.3 s is the
    chosen middle, not yet measured on its own.
    """
    return float(os.getenv('VAD_STOP_SECS') or 0.3)


def twilio_marks() -> str:
    """`TWILIO_MARKS`: `absent` for a far end that speaks Twilio Media Streams but returns no marks (Coval's simulator).

    Then a read-back is settled from the audio actually sent, at real time, and
    recorded as `local` rather than a mark (`core.processor.TwilioMarks`). A real
    Twilio call returns marks: leave it `echoed`. `coval_sim` sets `absent`.
    """
    return os.getenv('TWILIO_MARKS', 'echoed')


def delivery() -> str:
    """`DELIVERY`: `marks` (the default), a read-back is heard only on the transport's own playback report for it.

    On a phone line that is its returned Twilio mark. In the browser it is the
    page's playout report (`core.processor.WebRTCPlayout`): core's page at
    /voice/ (Pipecat's own web client with core's playback reports,
    `core.processor.serve_client`); the runner's prebuilt client at /client
    reports no playback, and a call from it is refused before the greeting
    (`reports_playback`). `simulated` lets estimated playback count as heard
    instead (`core.processor.playout(simulate=True)`): only for Coval's
    simulated carrier, which returns no marks (`coval_sim` sets it with
    `TWILIO_MARKS=absent`), or a developer who asks for it.
    """
    return os.getenv('DELIVERY', 'marks')


transport_params = {
    'webrtc': lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    'daily': lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    'twilio': lambda: FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True),
}


def speech_services(entry: Entry) -> tuple[SlngSTTService, SlngTTSService]:
    """STT and TTS over SLNG, one key for both, in the entry's languages unless the environment names others.

    TTS aggregates by TOKEN rather than Pipecat's default SENTENCE, which holds
    every chunk until a sentence boundary before synthesising any of it: two to
    three hundred milliseconds by its own docstring, paid on the first word of
    every turn. The model streams tokens and SLNG accepts them, so there is
    nothing to buffer for. Fish Audio is served from eu-west, in Germany; STT
    stays on `SLNG_BASE_URL`.
    """
    key = os.environ['SLNG_API_KEY']
    stt = SlngSTTService(
        api_key=key,
        base_url=os.getenv('SLNG_BASE_URL', 'eu-north.api.slng.ai'),
        model=os.getenv('SLNG_STT_MODEL', 'soniox/speech-ai:rt-v5'),
        language=Language(os.getenv('SLNG_STT_LANGUAGE') or entry.stt_language),
    )
    tts = SlngTTSService(
        api_key=key,
        base_url=os.getenv('SLNG_TTS_BASE_URL', 'eu-west.api.slng.ai'),
        model=os.getenv('SLNG_TTS_MODEL', 'slng/fish/tts:s2.1-pro'),
        voice=os.getenv('SLNG_TTS_VOICE', '1258154bb41540c7b9dc0d10cd6704bf'),
        language=Language(os.getenv('SLNG_TTS_LANGUAGE') or entry.tts_language),
        warm_standby_enabled=True,
        text_aggregation_mode=TextAggregationMode.TOKEN,
    )
    return stt, tts


def aggregators(
    context: LLMContext, turn_taking: Literal['tuned', 'pipecat'] = 'tuned'
) -> LLMContextAggregatorPair:
    """The pair that decides when the caller has finished and records what the bot said.

    `tuned`: Pipecat's own turn taking, set exactly as the baseline sets it (the
    unmute graph build of the salon, on Pipecat Flows): Silero VAD with a short
    silence window (`vad_stop_seconds`), and the smart-turn v3 stop strategy with
    a 1.6 s ceiling. The same settings on both sides, so a comparison measures
    the builds and not their end-of-turn timing. The ceiling is how long a pause
    may last once smart-turn has judged the caller not finished. Left at its 3 s
    default, it was the whole tail: in Coval run 4bLgLR4t, 29 turns ended on it,
    each one about 3 s before the harness even started.

    `pipecat`: Pipecat's recommended turn taking, unchanged: Silero VAD and its
    default strategies (Faro's choice). Either way, when the caller goes on after
    a turn ended, the harness joins the pieces (`CallContext.continues_turn`).
    """
    if turn_taking == 'pipecat':
        return LLMContextAggregatorPair(
            context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer())
        )
    return LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_idle_timeout=CallTiming.IDLE_SECONDS,
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=vad_stop_seconds())),
            user_turn_strategies=UserTurnStrategies(
                stop=[
                    TurnAnalyzerUserTurnStopStrategy(
                        turn_analyzer=LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=1.6))
                    )
                ]
            ),
        ),
    )


def pipeline_worker(pipeline: Pipeline, session_id: str) -> PipelineWorker:
    """The worker, with tracing on.

    `enable_tracing` is what makes Pipecat open the conversation and turn spans
    the agents' own spans hang from; without it the only spans are Pydantic AI's,
    one loose trace per agent run. The session, trace name, agent and run are
    `core.observability.setup()`'s to place, on every span.
    """
    return PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        conversation_id=session_id or None,
        enable_tracing=True,
        enable_turn_tracking=True,
        observers=[latency_log()],
    )


def latency_log() -> UserBotLatencyObserver:
    """Pipecat's own breakdown of each wait, from the caller's stop to the bot's first audio, in the bot log.

    Endpointing, transcription, the agent step (Pipecat calls it LLM
    inference) and speech synthesis, each as Pipecat measured it on its own
    clock. Session 4e reads the upstream parts from here and core's own work
    from the spans, and never subtracts one from the other.
    """
    observer = UserBotLatencyObserver()

    @observer.event_handler('on_latency_breakdown')
    async def breakdown(_observer, breakdown):
        """Log each turn's latency breakdown, line by line."""
        for line in breakdown.turn_contribution_lines():
            logger.info('latency breakdown: {}', line)

    return observer


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


def _outcome(call: Any) -> Literal['goodbye', 'hangup', 'ceiling', 'failed', 'stopped']:
    """How a call ended, from what it recorded: the ceiling first, then a goodbye, a failure, else a hang-up."""
    kinds = {ending.get('kind') for ending in call.endings}
    if 'ceiling' in kinds:
        return 'ceiling'
    if call.ended == 'caller' or call.departed:
        return 'goodbye'
    if call.ended == 'error':
        return 'failed'
    return 'hangup'


async def run_call(
    entry: Entry,
    transport: BaseTransport,
    runner_args: RunnerArguments,
    *,
    flow: StepFlow | None = None,
    speech: Callable[[], tuple[FrameProcessor, FrameProcessor]] | None = None,
    warm: bool = True,
    trace: bool = True,
    caller_id: str | None = None,
    settle: Callable[[str], bool] | None = None,
    listener: CallListener | None = None,
    run: str = '',
    config: str = '',
    resume: TurnSnapshot | None = None,
    facts: dict[str, Any] | None = None,
    simulate: bool | None = None,
    opened: Callable[[Any], None] | None = None,
) -> None:
    """One call: build the pipeline around the entry's flow and run it until the line drops.

    A `bot.py` runs it once per call and warms the model clients for that call.
    A runner that owns them for a process's life (the salon's `server.py`, the
    studio's runner) passes `warm=False` and its own `flow`; a test passes
    `speech` (offline doubles) and `trace=False`. `caller_id` is the carrier's
    number on a phone route, else `CALLER_ID`, a guess until the caller agrees.

    `listener` gets the call's events (`core.events`) under `run`, from the run's
    start (`config` is the config version the studio names) to its end. `resume`
    carries on from a turn of an earlier call (`core.snapshot`), with `facts` in
    place of its own when a tester edited them: the call then opens with no
    greeting, since the conversation is already under way, and the caller speaks
    first. `opened` is told the call as soon as it exists (a runner keeps it to
    hang up). `simulate` overrides `DELIVERY` for this call.

    Pipecat's `LLMContext` is kept even though no model reads it: the
    aggregators use it to work out the caller's turn and, on a barge-in, what
    the bot managed to say. `note_spoken` is its only consumer.

    On a phone route the session is the carrier's call id (Twilio's
    `callSid`), so a call and its trace share one key: Coval records the same
    id as the simulation's `call_sid`, and one Langfuse session is one call.
    """
    from core.observability import setup

    call_data = getattr(runner_args, 'call_data', None)
    session_id = (call_data.call_id if call_data else '') or runner_args.session_id or ''
    flow = flow or entry.flow()
    if trace:
        setup(session=session_id, trace_name=entry.name, agent=entry.id, run=run)
    stt, tts = (speech or (lambda: speech_services(entry)))()
    caller = os.getenv('CALLER_ID', '') if caller_id is None else caller_id
    call = entry.open(flow, Opening(caller_id=caller, key=run or session_id, settle=settle))
    events = call.listen(listener, run=run) if listener is not None else None
    if resume is not None:
        flow.resume(call, resume, facts)
    if events is not None:
        events.started(
            agent=entry.id,
            channel='twilio' if isinstance(runner_args, WebSocketRunnerArguments) else 'voice',
            entry=call.state.agent,
            state=call.state.facts.model_dump(mode='json'),
            config=config,
            forked_from=resume.run_id if resume is not None else None,
            forked_turn=resume.turn if resume is not None else None,
        )
    if opened is not None:
        opened(call)
    if entry.started is not None:
        threading.Thread(target=entry.started, daemon=True).start()
    context, node = LLMContext(), PydanticAgentProcessor(call, flow.nodes)
    user_aggregator, assistant_aggregator = aggregators(context, entry.turn_taking)

    if entry.reminders:

        @user_aggregator.event_handler('on_user_turn_idle')
        async def on_user_turn_idle(aggregator):
            """Ask a silent caller whether they are still there."""
            await node.remind()

    simulated = delivery() == 'simulated' if simulate is None else simulate
    wiring = playout(transport, call, echoes_marks=twilio_marks() != 'absent', simulate=simulated)
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            node,
            tts,
            transport.output(),
            *wiring,
            assistant_aggregator,
        ]
    )
    worker = pipeline_worker(pipeline, session_id)
    playback(node, assistant_aggregator)

    async def greet() -> None:
        """The opening line, or, on a line that can never confirm anything, the refusal and the end of the call.

        The harness records the line as said, Pipecat adds it to the context.
        A line with no playback evidence (a browser client that reports none,
        a Twilio far end that returns no marks, outside simulation) could
        never confirm a read-back: saying so first means nobody gives details
        that could never be confirmed. A resumed call says nothing: the
        conversation is already under way.
        """
        if flow.confirms and not await reports_playback(wiring, call):
            logger.error(
                f"this {call.transport} line reports no playback: open core's page at /voice/ in a browser"
            )
            call.note_ending('unconfirmable', 'the line reports no playback', observed=True)
            await node.greet(call.locale.unconfirmable)
            await node.hang_up()
            return
        if resume is None:
            await node.greet(entry.greeting(flow, call))

    @worker.rtvi.event_handler('on_client_ready')
    async def on_client_ready(rtvi):
        """A browser client is ready: it said, in its `client-ready`, whether it reports playback."""
        await greet()

    ceiling_seconds = max_call_seconds()

    async def hang_up() -> None:
        """The call ceiling (`MAX_CALL_SECONDS`) was reached: hang up as the caller would."""
        logger.warning(f'call ceiling of {ceiling_seconds:.0f}s reached: hanging up')
        call.note_ending('ceiling', f'{ceiling_seconds:.0f}s call ceiling', observed=False)
        await node.finish_turn(CallTiming.FINISH_SECONDS)
        await worker.cancel()

    ceiling = Ceiling(ceiling_seconds, hang_up)

    @transport.event_handler('on_client_connected')
    async def on_client_connected(transport, client):
        """A phone websocket has no RTVI client to say it is ready, so the greeting goes out here."""
        logger.info('client connected')
        ceiling.start()
        if isinstance(runner_args, WebSocketRunnerArguments):
            await greet()

    @transport.event_handler('on_client_disconnected')
    async def on_client_disconnected(transport, client):
        """Let a turn already running finish its write, for a few seconds, before the line is torn down."""
        logger.info('client disconnected')
        call.note_ending('media_disconnect', 'the media websocket closed', observed=True)
        await ceiling.stop()
        await node.finish_turn(CallTiming.FINISH_SECONDS)
        if entry.close is not None:
            await entry.close(call)
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    try:
        async with model_clients(flow.nodes, warm=warm):
            await runner.run()
    finally:
        await ceiling.stop()
        call.note_ending('pipeline_end', 'the pipeline stopped', observed=True)
        record_ending(call)
        if events is not None:
            call.turn_snapshot()
            events.ended(call.turn, _outcome(call))
            await events.close()


def serve() -> None:
    """`uv run bot.py`: Pipecat's development runner, with core's reporting page at `/voice/` (and `/`).

    Run from a `bot.py` whose module defines `bot(runner_args)`: the runner finds it there.
    """
    from pipecat.runner.run import app, main

    from core.processor import serve_client

    serve_client(app)
    main()
