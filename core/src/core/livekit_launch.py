"""One voice call of any use case on LiveKit Agents: the session around a flow, built the same way for every launcher.

The LiveKit twin of `core.launch.run_call`. A use case's `entry.py` (`core.Entry`) says the same five
or six things for both, and its `bot.py` runs either from one command line (`core.voice.serve`):

    uv run bot.py livekit dev        # a worker for LiveKit Cloud or a local server, one call per room
    uv run bot.py livekit console    # one call in the terminal, on this machine's microphone and speaker

    room audio -> Silero VAD + SLNG STT -> turn detector -> FlowAgent (core.livekit) -> SLNG TTS -> room

LiveKit owns the room, VAD, end of turn, interruptions, STT and TTS; the flow owns the conversation,
through one agent (`core.livekit.FlowAgent`). The settings are the Pipecat launcher's, read from the
same environment: the speech services and their languages, the end-of-turn waits (`turn_handling`),
the call ceiling (`MAX_CALL_SECONDS`) and the delivery mode (`DELIVERY`). LiveKit reports its own
playout clock, never the far end's, so a flow that confirms writes refuses a LiveKit call before its
greeting unless the launcher opened it for simulated playout (`DELIVERY=simulated`), as Coval's
mark-less Twilio route does (`core.livekit.reports_playback`).

`LIVEKIT_URL`, `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` say which server; `LIVEKIT_AGENT_NAME`, when
set, makes the worker take only rooms dispatched to that name; `LIVEKIT_IDLE_PROCESSES` sets how many
call processes a worker keeps ready (LiveKit's default: none in `dev`, one per CPU in `start`), and
`LIVEKIT_HTTP_PORT` the port of its health check (LiveKit's default in `start`: 8081, so a second
worker on one machine needs another, or `0` for any free one).

Imports LiveKit (the `livekit` extra), with `core.livekit`.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib
import os
import sys
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any

try:
    import aiohttp
    from livekit import api, rtc
    from livekit.agents import (
        NOT_GIVEN,
        AgentServer,
        AgentSession,
        JobContext,
        JobProcess,
        TurnHandlingOptions,
        cli,
        get_job_context,
    )
    from livekit.agents.voice.room_io import RoomOptions
    from livekit.plugins import silero, slng
except ImportError as missing:
    raise ImportError(
        f'core.livekit_launch needs the livekit extra: install slng-voice-core[livekit] ({missing})'
    ) from missing

from loguru import logger

from core.entry import Entry, Opening
from core.events import CallListener
from core.graph import model_clients
from core.livekit import FlowAgent, playout, reports_playback
from core.snapshot import TurnSnapshot
from core.steps import StepFlow
from core.voice import (
    CallTiming,
    Ceiling,
    delivery,
    max_call_seconds,
    outcome,
    record_ending,
    vad_stop_seconds,
)

TRANSCRIPTION_TIMEOUT = 2.0
"""Seconds after speech with no words before LiveKit says so (`user_transcription_timeout`): `FlowAgent` drops its onset."""

READY_SECONDS = 60.0
"""How long a job process may take to get ready (`prewarm`): it imports the use case and compiles its flow."""

SMART_TURN_CEILING = 1.6
"""The longest wait after the caller stops before the turn ends: the Pipecat launcher's smart-turn `stop_secs`."""


def speech_services(entry: Entry, *, http_session: Any = None) -> tuple[slng.STT, slng.TTS]:
    """STT and TTS over SLNG, LiveKit's own plugin, with the Pipecat launcher's models, voice and regions.

    The same environment names (`SLNG_STT_MODEL`, `SLNG_TTS_MODEL`, `SLNG_TTS_VOICE`, the base URLs and
    languages), so one profile runs both frameworks on the same speech. `http_session` is for a caller
    outside a LiveKit job (a live check), which has no job's HTTP session to lend the plugins.
    """
    key = os.environ['SLNG_API_KEY']
    shared = {'http_session': http_session} if http_session is not None else {}
    stt = slng.STT(
        api_key=key,
        slng_base_url=os.getenv('SLNG_BASE_URL', 'eu-north.api.slng.ai'),
        model=os.getenv('SLNG_STT_MODEL', 'soniox/speech-ai:rt-v5'),
        language=os.getenv('SLNG_STT_LANGUAGE') or entry.stt_language,
        **shared,
    )
    tts = slng.TTS(
        api_key=key,
        slng_base_url=os.getenv('SLNG_TTS_BASE_URL', 'eu-west.api.slng.ai'),
        model=os.getenv('SLNG_TTS_MODEL', 'slng/fish/tts:s2.1-pro'),
        voice=os.getenv('SLNG_TTS_VOICE', '1258154bb41540c7b9dc0d10cd6704bf'),
        language=os.getenv('SLNG_TTS_LANGUAGE') or entry.tts_language,
        warm_standby_enabled=True,
        **shared,
    )
    return stt, tts


def turn_handling(entry: Entry) -> TurnHandlingOptions:
    """When the caller has finished, and what speech over the agent does.

    `tuned`: the Pipecat launcher's measured waits, in LiveKit's terms: a turn may end
    `VAD_STOP_SECS` (0.3 s) after the caller stops, and waits at most `SMART_TURN_CEILING` (1.6 s)
    while the turn detector judges them not finished. `pipecat` (Faro's choice): LiveKit's own
    defaults. Either way interruptions are on (LiveKit picks its adaptive detector where it can, which
    lets a backchannel through) and preemptive generation is off: a turn's writes run only for words
    LiveKit committed (`FlowAgent` refuses it too).

    `LIVEKIT_TURN_DETECTION` picks the end-of-turn signal: unset, LiveKit's own turn detector as it
    provisions it; `vad` or `stt` for those alone.
    """
    options: TurnHandlingOptions = {
        'interruption': {'enabled': True},
        'preemptive_generation': {'enabled': False},
    }
    if entry.turn_taking == 'tuned':
        options['endpointing'] = {'min_delay': vad_stop_seconds(), 'max_delay': SMART_TURN_CEILING}
    match os.getenv('LIVEKIT_TURN_DETECTION', ''):
        case '':
            pass
        case 'vad':
            options['turn_detection'] = 'vad'
        case 'stt':
            options['turn_detection'] = 'stt'
        case other:
            raise ValueError(f'LIVEKIT_TURN_DETECTION={other!r}: leave it unset, or name vad or stt')
    return options


def session_for(entry: Entry, *, stt: Any, tts: Any, vad: Any) -> AgentSession:
    """The session one call runs in: these speech services, the entry's turn taking, no away timer.

    Silence is `FlowAgent`'s to notice (`core.turn.remind`), so LiveKit's own away timer stays off. An
    STT that can be told when the caller stops (SLNG's) is attached, so it finalises at once. LiveKit
    says when speech gave no words (`TRANSCRIPTION_TIMEOUT`), so a cough is never the start of a reply.
    """
    session = AgentSession(
        stt=stt,
        tts=tts,
        vad=vad,
        turn_handling=turn_handling(entry),
        user_away_timeout=None,
        transcription_timeout=TRANSCRIPTION_TIMEOUT,
    )
    if callable(attach := getattr(stt, 'attach_to_session', None)):
        attach(session)
    return session


def prewarm(proc: JobProcess, where: str = '') -> None:
    """Get a job process ready before LiveKit gives it a call: `AgentServer.setup_fnc`.

    LiveKit runs each call in a process of its own and calls this as the process starts, before any
    call is assigned to it (a production worker keeps idle processes ready). Silero is loaded, and
    the entry `where` names (`locate`) is imported with its flow compiled: otherwise the call's process
    would read `agent.yaml`, its prompts and build every step's agent when the call arrives, before
    the greeting. The model clients' connections stay per call (`model_clients`), opened while the
    greeting plays: a client is bound to the event loop of the call that uses it.
    """
    proc.userdata['vad'] = silero.VAD.load(min_silence_duration=vad_stop_seconds())
    if where:
        _entry(where).flow()


def _entry(where: str) -> Entry:
    """The entry `where` names (`module:name`), imported if it is not yet."""
    module, _, name = where.partition(':')
    return getattr(importlib.import_module(module), name)


def caller_number(participant: rtc.RemoteParticipant) -> str:
    """The number a SIP caller rang from, '' for a browser or any other participant."""
    if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
        return participant.attributes.get('sip.phoneNumber', '')
    return ''


async def run_call(
    entry: Entry,
    ctx: JobContext,
    *,
    flow: StepFlow | None = None,
    speech: Callable[[], tuple[Any, Any]] | None = None,
    vad: Any = None,
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
    """One call in the job's room: connect, wait for the caller, and run the call there (`run_room`).

    The same arguments as `core.launch.run_call`, where they mean the same thing; `speech` and `vad` are a
    test's or a launcher's own services. In `console` mode there is no room participant: the terminal is
    the caller. The room is deleted once the call is over.
    """
    await ctx.connect()
    participant = None if ctx.is_fake_job() else await ctx.wait_for_participant()
    await run_room(
        entry,
        ctx.room,
        participant,
        flow=flow,
        speech=speech,
        vad=vad or ctx.proc.userdata.get('vad'),
        warm=warm,
        trace=trace,
        caller_id=caller_id,
        settle=settle,
        listener=listener,
        run=run,
        config=config,
        resume=resume,
        facts=facts,
        simulate=simulate,
        opened=opened,
        delete_room=True,
    )


async def run_room(
    entry: Entry,
    room: rtc.Room,
    participant: rtc.RemoteParticipant | None,
    *,
    flow: StepFlow | None = None,
    speech: Callable[[], tuple[Any, Any]] | None = None,
    vad: Any = None,
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
    delete_room: bool = False,
) -> None:
    """One call in a connected room with its caller in it: build the session around the entry's flow and run it.

    A LiveKit job's call (`run_call`), or one a process of its own placed in a room it joined itself (the
    studio's runner): no job is needed. The room is the call's key, as a Twilio `callSid` is on Pipecat:
    one Langfuse session per room. `caller_id` is the SIP caller's number when there is one, else
    `CALLER_ID`, a guess until the caller agrees. Outside a job, the speech services get an HTTP session
    of the call's own.

    Returns when the call is over: the caller left, the agent said goodbye, or the ceiling was reached.
    A turn already running when the room closes may finish its write (`CallTiming.FINISH_SECONDS`).

    The caller leaving the room, or the room going, closes the session whatever LiveKit's own
    disconnect reasons would do (`close_on_disconnect` only covers some), and is recorded as the media
    ending. Everything after the call is opened is inside the `finally`: a setup that fails (a missing
    key, a model warm-up) still records how the call ended, and a failing `entry.close` stops nothing.
    """
    from core.observability import setup

    session_id = room.name
    flow = flow or entry.flow()
    if trace:
        setup(session=session_id, trace_name=entry.name, agent=entry.id, run=run)
    number = caller_number(participant) if participant is not None else ''
    caller = caller_id if caller_id is not None else number or os.getenv('CALLER_ID', '')
    call = entry.open(flow, Opening(caller_id=caller, key=run or session_id, settle=settle))
    events = call.listen(listener, run=run) if listener is not None else None
    if resume is not None:
        flow.resume(call, resume, facts)
    if events is not None:
        events.started(
            agent=entry.id,
            channel='voice',
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

    closed = asyncio.Event()
    left: list[str] = []
    session: AgentSession | None = None
    agent: FlowAgent | None = None
    ceiling_seconds = max_call_seconds()

    def leave(why: str) -> None:
        """The caller is gone (they left the room, or the room went): close the session if it is still open."""
        left.append(why)
        if session is not None and not closed.is_set():
            session.shutdown(drain=False)
        elif session is None:
            closed.set()

    async def reached() -> None:
        """The call ceiling (`MAX_CALL_SECONDS`) was reached: hang up as the caller would."""
        logger.warning(f'call ceiling of {ceiling_seconds:.0f}s reached: hanging up')
        call.note_ending('ceiling', f'{ceiling_seconds:.0f}s call ceiling', observed=False)
        if agent is not None:
            await agent.finish_turn(CallTiming.FINISH_SECONDS)
        if session is not None:
            session.shutdown(drain=False)

    ceiling = Ceiling(ceiling_seconds, reached)
    if participant is not None:

        @room.on('participant_disconnected')
        def on_left(gone: rtc.RemoteParticipant) -> None:
            if gone.identity == participant.identity:
                leave('the caller left the room')

    @room.on('disconnected')
    def on_room_gone(*_: Any) -> None:
        leave('the room disconnected')

    async with contextlib.AsyncExitStack() as stack:
        try:
            if speech is None and get_job_context(required=False) is None:
                own = await stack.enter_async_context(aiohttp.ClientSession())
                stt, tts = speech_services(entry, http_session=own)
            else:
                stt, tts = (speech or (lambda: speech_services(entry)))()
            vad = vad or silero.VAD.load(min_silence_duration=vad_stop_seconds())
            session = session_for(entry, stt=stt, tts=tts, vad=vad)
            session.on('close', lambda _event: closed.set())
            agent = FlowAgent(
                call, flow.nodes, idle_seconds=CallTiming.IDLE_SECONDS, reminders=entry.reminders
            )
            playout(call, simulate=delivery() == 'simulated' if simulate is None else simulate)
            await stack.enter_async_context(model_clients(flow.nodes, warm=warm))
            agent.trace.start()
            stack.enter_context(agent.trace.current())
            await session.start(
                agent,
                room=room,
                room_options=RoomOptions(
                    participant_identity=participant.identity if participant is not None else NOT_GIVEN,
                    close_on_disconnect=True,
                    delete_room_on_close=delete_room,
                ),
            )
            agent.attach(session)
            ceiling.start()
            if flow.confirms and not reports_playback(call):
                logger.error(
                    'this LiveKit call reports no playback the caller heard: set DELIVERY=simulated to let'
                    " LiveKit's own playout count"
                )
                call.note_ending('unconfirmable', 'the line reports no playback', observed=True)
                await agent.greet(call.locale.unconfirmable)
                await agent.hang_up()
            elif resume is None:
                await agent.greet(entry.greeting(flow, call))
            if not left:
                await closed.wait()
        finally:
            await ceiling.stop()
            if left:
                call.note_ending('media_disconnect', left[0], observed=True)
            if agent is not None:
                await agent.finish_turn(CallTiming.FINISH_SECONDS)
            if entry.close is not None:
                try:
                    await entry.close(call)
                except Exception:
                    logger.exception(f'{entry.id}: closing the call failed')
            if agent is not None:
                await agent.close()
            if session is not None and not closed.is_set():
                with contextlib.suppress(Exception):
                    await session.aclose()
            call.note_ending('pipeline_end', 'the session closed', observed=True)
            record_ending(call)
            if events is not None:
                call.turn_snapshot()
                events.ended(call.turn, outcome(call))
                await events.close()


def room_token(room: str, identity: str, *, agent: bool = False, seconds: int = 3600) -> str:
    """A token to join `room` as `identity`, from `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET`.

    A caller's token (a browser the studio placed in a room) may publish and subscribe; an agent's
    (`agent=True`, the process that runs the call in that room) is also marked as an agent, so the room
    shows it as one. Raises `KeyError` naming the variable that is missing.
    """
    grants = api.VideoGrants(
        room=room, room_join=True, can_publish=True, can_subscribe=True, can_publish_data=True, agent=agent
    )
    token = (
        api.AccessToken(os.environ['LIVEKIT_API_KEY'], os.environ['LIVEKIT_API_SECRET'])
        .with_identity(identity)
        .with_name(identity)
        .with_grants(grants)
        .with_ttl(timedelta(seconds=seconds))
    )
    if agent:
        token = token.with_kind('agent')
    return token.to_jwt()


def locate(entry: Entry) -> str:
    """Where `entry` can be imported from, as `module:name`: how a LiveKit job process finds it again.

    LiveKit runs each job in a process of its own, started from a pickled reference to the entrypoint,
    so the entry itself is named by the module that holds it (a use case's `entry.py`), never passed.
    """
    for name, module in list(sys.modules.items()):
        if name in ('__main__', '__mp_main__') or module is None:
            continue
        for attribute, value in list(vars(module).items()):
            if value is entry:
                return f'{name}:{attribute}'
    raise LookupError(f'{entry.id}: no imported module holds this entry; import it from its entry.py')


async def entrypoint(where: str, ctx: JobContext) -> None:
    """A job's entrypoint: the entry `where` names (`locate`), run for this job's room."""
    await run_call(_entry(where), ctx)


def serve(entry: Entry) -> None:
    """`uv run bot.py livekit <dev|start|console|connect ...>`: this entry as a LiveKit agent server."""
    where = locate(entry)
    idle, port = os.getenv('LIVEKIT_IDLE_PROCESSES'), os.getenv('LIVEKIT_HTTP_PORT')
    server = AgentServer(
        initialize_process_timeout=READY_SECONDS,
        **({'num_idle_processes': int(idle)} if idle else {}),
        **({'port': int(port)} if port else {}),
    )
    server.setup_fnc = functools.partial(prewarm, where=where)
    server.rtc_session(functools.partial(entrypoint, where), agent_name=os.getenv('LIVEKIT_AGENT_NAME', ''))
    cli.run_app(server)
